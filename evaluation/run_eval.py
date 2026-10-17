"""Evaluate private labels against stored results without running the pipeline."""
from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from urllib.parse import urlsplit

from evaluation.make_label_sheet import LABEL_COLUMNS, REPO_ROOT, private_path, read_stored_rows
from evaluation.metrics import equivalent_urls, is_verified, render_report
from src.database import DATABASE_PATH


def read_labels(path: Path) -> list[dict]:
    source = private_path(path, input_file=True)
    with source.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not set(LABEL_COLUMNS) <= set(reader.fieldnames or []):
            raise ValueError("Label CSV is missing required columns")
        rows = list(reader)
    seen = set()
    for row in rows:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("Malformed CSV row")
        for key in row:
            row[key] = row[key].strip()
        key = row["record_key"]
        if not key or key in seen:
            raise ValueError("Label record keys must be nonempty and unique")
        seen.add(key)
        if row["resolution_label"] not in {"", "correct", "wrong_job", "unsure"}:
            raise ValueError("Invalid resolution label")
        if row["my_decision"] not in {"", "apply", "review", "skip"}:
            raise ValueError("Invalid human decision")
        if row["my_fit_rating"]:
            rating = float(row["my_fit_rating"])
            if not math.isfinite(rating) or not 0 <= rating <= 100:
                raise ValueError("Human fit rating must be finite and between 0 and 100")
            row["my_fit_rating"] = rating
        expected = row["expected_official_url"]
        if expected:
            parsed = urlsplit(expected)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Expected official URL must be an absolute HTTP(S) URL")
    return rows


def join_labels(labels: list[dict], stored: list[dict]) -> tuple[list[dict], list[dict]]:
    by_key = {row["record_key"]: row for row in stored}
    joined, details = [], []
    for label in labels:
        current = by_key.get(label["record_key"])
        if current is None:
            details.append({**label, "join_status": "unmatched_or_excluded"})
            continue
        # Label-sheet metadata is for the human only. Current stored fields win.
        row = {**current, **{field: label[field] for field in (
            "resolution_label", "expected_official_url", "my_fit_rating", "my_decision"
        )}}
        joined.append(row)
        details.append({**row, "join_status": "joined", "absolute_error": (
            abs(float(row["overall_score"]) - float(row["my_fit_rating"]))
            if row.get("overall_score") is not None and row["my_fit_rating"] != "" else ""
        ), "expected_url_match": (is_verified(row) and equivalent_urls(
            row.get("official_job_url", ""), row["expected_official_url"]
        )) if row["expected_official_url"] else ""})
    return joined, details


def run_evaluation(labels_path: Path, database: Path, out_dir: Path, *, force: bool = False,
                   detail_path: Path | None = None) -> tuple[int, int]:
    destination = private_path(detail_path if detail_path is not None else Path.home() / "job-eval/results_detail.csv",
                               overwrite=force)
    # Only aggregate output may be placed in the repository's docs directory.
    out_dir = out_dir.expanduser().resolve()
    if out_dir.is_relative_to(REPO_ROOT) and out_dir != REPO_ROOT / "docs":
        raise ValueError("Repository aggregate output must be in docs/")
    report_path = out_dir / "eval_results.md"
    if report_path.resolve().parent != out_dir:
        raise ValueError("Aggregate report symlink must not escape its output directory")
    reserved = {database.expanduser().resolve(), labels_path.expanduser().resolve()}
    if destination in reserved or report_path.resolve() in reserved or destination == report_path.resolve():
        raise ValueError("Outputs must not replace the database, labels, or each other")
    labels = read_labels(labels_path)
    joined, details = join_labels(labels, read_stored_rows(database))
    report = render_report(joined, label_count=len(labels), unmatched_count=len(labels) - len(joined))
    fields = sorted({field for row in details for field in row} or {"record_key", "join_status"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w" if force else "x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(details)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    return len(joined), len(labels) - len(joined)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, default=Path.home() / "job-eval/labels_done.csv")
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "docs")
    parser.add_argument("--force", action="store_true", help="Allow replacing the private detail CSV")
    args = parser.parse_args()
    joined, unmatched = run_evaluation(args.labels, args.database, args.out_dir, force=args.force)
    print(f"Evaluated {joined} joined label rows; {unmatched} unmatched/excluded. Aggregate report contains no job details.")


if __name__ == "__main__":
    main()
