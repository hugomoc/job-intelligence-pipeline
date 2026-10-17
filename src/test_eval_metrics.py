"""Synthetic-only evaluation checks, runnable by pytest and the legacy runner."""
from __future__ import annotations

import csv
import hashlib
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

from evaluation import metrics
from evaluation.make_label_sheet import (
    LABEL_COLUMNS, REPO_ROOT, make_label_sheet, private_path, read_stored_rows, sample_rows,
)
from evaluation.run_eval import read_labels, run_evaluation
from scripts.build_ci_fixture_db import build_fixture


def prepared_fixture(directory):
    path = Path(directory) / "synthetic.db"
    build_fixture(path)
    # The original dbt fixture has no resolution outcomes. Add synthetic
    # metadata only to this disposable test database; do not change its builder.
    with duckdb.connect(str(path)) as connection:
        connection.execute("""
            UPDATE job_enrichment_attempts SET official_url_status =
                CASE WHEN record_key = 'ci-unscored' THEN 'OFFICIAL_NOT_FOUND'
                     WHEN record_key = 'ci-review' THEN 'AMBIGUOUS'
                     ELSE 'FOUND_VERIFIED' END,
                official_job_url = CASE WHEN record_key IN ('ci-unscored', 'ci-review') THEN NULL
                                       ELSE 'https://example.invalid/official/' || record_key END
        """)
    return path


def label_rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_labels(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LABEL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


class EvaluationTests(unittest.TestCase):
    def test_false_accept_precision_and_unsure(self):
        rows = [{"official_url_status": "FOUND_VERIFIED", "official_job_url": "https://example.invalid/1",
                 "resolution_label": label} for label in ("correct", "correct", "wrong_job", "unsure", "")]
        result = metrics.resolution_metrics(rows)
        self.assertEqual(result["precision"]["n"], 3)
        self.assertAlmostEqual(result["precision"]["value"], 2 / 3)
        self.assertAlmostEqual(result["false_accept_rate"]["value"], 1 / 3)
        self.assertEqual(result["unsure_count"], 1)

    def test_recall_uses_posting_identity(self):
        expected = "https://boards.greenhouse.io/example/jobs/123?utm_source=email"
        rows = [dict(expected_official_url=expected, official_job_url=url, official_url_status=status)
                for url, status in (("https://job-boards.greenhouse.io/example/jobs/123", "FOUND_VERIFIED"),
                                    ("https://boards.greenhouse.io/example/jobs/124", "FOUND_VERIFIED"),
                                    (expected, "SEARCH_DEFERRED"))]
        result = metrics.resolution_metrics(rows)["recall"]
        self.assertEqual(result["n"], 3)
        self.assertAlmostEqual(result["value"], 1 / 3)
        self.assertTrue(metrics.equivalent_urls("https://example.invalid/job?jobid=1&utm_source=a",
                                                "https://EXAMPLE.invalid/job?jobid=1"))
        self.assertFalse(metrics.equivalent_urls("https://example.invalid/job?jobid=1",
                                                 "https://example.invalid/job?jobid=2"))

    def test_deferred_not_found_rate(self):
        result = metrics.resolution_metrics([{"official_url_status": s} for s in
            ("OFFICIAL_NOT_FOUND", "SEARCH_DEFERRED", "AMBIGUOUS", "FOUND_VERIFIED")])
        self.assertEqual(result["deferred_not_found_rate"]["value"], 0.5)
        self.assertEqual(result["deferred_not_found_rate"]["n"], 4)

    def test_spearman_reversed(self):
        self.assertEqual(metrics.spearman([1, 2, 3], [3, 2, 1]), -1)

    def test_spearman_average_rank_ties(self):
        self.assertAlmostEqual(metrics.spearman([1, 1, 2], [1, 2, 3]), math.sqrt(3) / 2)

    def test_spearman_undefined(self):
        self.assertIsNone(metrics.spearman([1, 1], [2, 3]))
        self.assertIsNone(metrics.spearman([], []))
        with self.assertRaises(ValueError):
            metrics.spearman([1], [])

    def test_mae_within_10_and_missing_pairs(self):
        rows = [{"overall_score": a, "my_fit_rating": b} for a, b in ((90, 100), (80, 75), (40, 70), (None, 10), (20, ""))]
        result = metrics.scoring_metrics(rows)
        self.assertEqual(result["mae"]["n"], 3)
        self.assertEqual(result["mae"]["value"], 15)
        self.assertAlmostEqual(result["within_10"]["value"], 2 / 3)

    def test_confusion_matrix_and_prompt_agreement(self):
        rows = [{"recommendation": a, "my_decision": b, "prompt_version": v} for a, b, v in (
            ("apply", "apply", "v1"), ("apply", "review", "v1"), ("skip", "skip", "v2"), ("review", "", "v2"))]
        result = metrics.scoring_metrics(rows)
        self.assertEqual(result["confusion_matrix"]["n"], 3)
        self.assertEqual(result["confusion_matrix"]["counts"]["apply"]["review"], 1)
        self.assertEqual(result["agreement_by_prompt_version"]["v1"]["value"], 0.5)
        self.assertEqual(result["agreement_by_prompt_version"]["v2"]["value"], 1)

    def test_wilson_interval(self):
        low, high = metrics.wilson_interval(5, 10)
        self.assertAlmostEqual(low, 0.2365930905)
        self.assertAlmostEqual(high, 0.7634069095)
        self.assertIsNone(metrics.wilson_interval(0, 0))
        with self.assertRaises(ValueError):
            metrics.wilson_interval(2, 1)

    def test_insufficient_data_reporting(self):
        row = dict(official_url_status="FOUND_VERIFIED", official_job_url="https://example.invalid/1",
                   resolution_label="correct", overall_score=90, my_fit_rating=90)
        self.assertEqual(metrics.resolution_metrics([row] * 9)["precision"]["status"], "insufficient data")
        self.assertEqual(metrics.resolution_metrics([row] * 10)["precision"]["status"], "ok")
        self.assertIn("insufficient data (n=9)", metrics.render_report([row] * 9))
        self.assertIn("insufficient data (n=0)", metrics.render_report([]))

    def test_report_does_not_echo_job_data(self):
        private = ("SECRET COMPANY", "SECRET JOB TITLE", "https://secret.invalid/job/1", "secret-record-42")
        row = dict(company=private[0], title=private[1], official_job_url=private[2], record_key=private[3],
                   official_url_status="FOUND_VERIFIED", resolution_label="correct", overall_score=90,
                   my_fit_rating=80, recommendation="apply", my_decision="review", prompt_version=private[0])
        report = metrics.render_report([row] * 10)
        for value in private:
            self.assertNotIn(value, report)
        self.assertIn("Wilson CI", report)
        self.assertIn("version-", report)

    def test_sheet_columns_empty_labels_and_quarantine_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            database = prepared_fixture(directory)
            with duckdb.connect(str(database)) as connection:
                connection.execute("UPDATE raw_jobs SET description=NULL WHERE record_key='ci-skip'")
                connection.execute("UPDATE job_enrichment_attempts SET error_message='Quarantined contamination' WHERE record_key='ci-incomplete'")
            output = Path(directory) / "labels.csv"
            self.assertEqual(make_label_sheet(database, output, 50), 4)
            rows = label_rows(output)
            self.assertEqual(set(rows[0]), set(LABEL_COLUMNS))
            self.assertFalse({"overall_score", "recommendation", "confidence"} & set(rows[0]))
            for row in rows:
                self.assertLessEqual(len(row["description_excerpt"]), 600)
                for field in ("resolution_label", "expected_official_url", "my_fit_rating", "my_decision"):
                    self.assertEqual(row[field], "")

    def test_repository_path_guard_before_database_access(self):
        with self.assertRaises(ValueError):
            make_label_sheet(Path("/nonexistent/no-read.db"), REPO_ROOT / "labels-secret.csv")
        with tempfile.TemporaryDirectory() as directory:
            symlink = Path(directory) / "repo-link"
            symlink.symlink_to(REPO_ROOT, target_is_directory=True)
            with self.assertRaises(ValueError):
                private_path(symlink / "labels.csv")
            with self.assertRaises(ValueError):
                read_labels(REPO_ROOT / "evaluation/labels.example.csv")

    def test_sheet_refuses_overwrite_before_database_access(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "labels.csv"
            output.write_text("preserve")
            with self.assertRaises(FileExistsError):
                make_label_sheet(Path("/nonexistent/no-read.db"), output)
            self.assertEqual(output.read_text(), "preserve")

    def test_unmodified_fixture_has_no_eligible_resolution_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bare.db"
            build_fixture(database)
            output = Path(directory) / "labels.csv"
            self.assertEqual(make_label_sheet(database, output), 0)
            self.assertEqual(label_rows(output), [])

    def test_read_only_connection_and_unchanged_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = prepared_fixture(directory)
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            connect = duckdb.connect
            with patch("evaluation.make_label_sheet.duckdb.connect", wraps=connect) as observed:
                read_stored_rows(database)
                self.assertTrue(observed.call_args.kwargs["read_only"])
            self.assertEqual(before, hashlib.sha256(database.read_bytes()).hexdigest())

    def test_stratified_mix_and_reproducibility(self):
        rows = [dict(record_key=str(i), source=("linkedin", "lensa", "direct")[i % 3],
                     official_job_url=f"https://example.invalid/job/{i}",
                     official_url_status="FOUND_VERIFIED" if i < 80 else "SEARCH_DEFERRED") for i in range(100)]
        sample = sample_rows(rows)
        self.assertEqual(sum(metrics.is_verified(row) for row in sample), 40)
        self.assertEqual(len(sample), 50)
        self.assertEqual({row["source"] for row in sample}, {"linkedin", "lensa", "direct"})
        self.assertEqual(sample, sample_rows(rows))
        with self.assertRaises(ValueError):
            sample_rows(rows, 0)

    def test_label_validation_duplicate_and_range(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labels.csv"
            row = {field: "" for field in LABEL_COLUMNS}
            row["record_key"] = "one"
            write_labels(path, [row, row])
            with self.assertRaises(ValueError):
                read_labels(path)
            for invalid in ("nan", "101", "-1"):
                row["my_fit_rating"] = invalid
                write_labels(path, [row])
                with self.assertRaises(ValueError):
                    read_labels(path)

    def test_run_eval_private_detail_force_and_aggregate_privacy(self):
        with tempfile.TemporaryDirectory() as directory:
            database = prepared_fixture(directory)
            labels = Path(directory) / "labels.csv"
            make_label_sheet(database, labels, 50)
            rows = label_rows(labels)
            for row in rows:
                row.update(resolution_label="unsure", my_fit_rating="77", my_decision="review")
            write_labels(labels, rows)
            detail = Path(directory) / "private/results.csv"
            out_dir = Path(directory) / "aggregate"
            self.assertEqual(run_evaluation(labels, database, out_dir, detail_path=detail), (6, 0))
            report = (out_dir / "eval_results.md").read_text()
            for row in rows:
                for field in ("company", "title", "record_key", "source_apply_url"):
                    self.assertNotIn(row[field], report)
            with self.assertRaises(FileExistsError):
                run_evaluation(labels, database, out_dir, detail_path=detail)
            self.assertEqual(run_evaluation(labels, database, out_dir, detail_path=detail, force=True), (6, 0))
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            for reserved in (database, labels):
                with self.assertRaises(ValueError):
                    run_evaluation(labels, database, out_dir, detail_path=reserved, force=True)
            self.assertEqual(before, hashlib.sha256(database.read_bytes()).hexdigest())
            report_database = Path(directory) / "db-report-collision/eval_results.md"
            report_database.parent.mkdir()
            report_database.write_bytes(database.read_bytes())
            with self.assertRaises(ValueError):
                run_evaluation(labels, report_database, report_database.parent, detail_path=detail, force=True)

    def test_latest_exact_score_and_unmatched_count(self):
        with tempfile.TemporaryDirectory() as directory:
            database = prepared_fixture(directory)
            with duckdb.connect(str(database)) as connection:
                connection.execute("""INSERT INTO resume_job_scores SELECT * REPLACE (
                    'new-profile' AS resume_hash, 99 AS overall_score,
                    TIMESTAMPTZ '2099-01-01 00:00:00+00' AS scored_at
                ) FROM resume_job_scores WHERE record_key='ci-review'""")
            rows = read_stored_rows(database)
            self.assertEqual(next(row for row in rows if row["record_key"] == "ci-review")["overall_score"], 99)
            self.assertIn("unmatched/excluded rows: 2", metrics.render_report(rows, label_count=8, unmatched_count=2))


if __name__ == "__main__":
    unittest.main()
