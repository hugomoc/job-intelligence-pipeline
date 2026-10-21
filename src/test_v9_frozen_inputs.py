"""Synthetic, offline tests of the frozen-assessment DuckDB loader."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

from evaluation.v9_frozen_inputs import FROZEN_RECORDS, load_cases


class FrozenInputTests(unittest.TestCase):
    def _database(self, directory):
        path = Path(directory) / "test.duckdb"
        with duckdb.connect(str(path)) as db:
            db.execute("CREATE TABLE candidate_assessments (assessment_id VARCHAR, record_key VARCHAR, resume_hash VARCHAR, payload_json VARCHAR)")
            db.execute("CREATE TABLE candidate_active_assessments (assessment_id VARCHAR)")
            db.execute("CREATE TABLE raw_jobs (record_key VARCHAR, description VARCHAR)")
            db.execute("CREATE TABLE resume_profiles (resume_hash VARCHAR, redacted_resume_text VARCHAR)")
            key = FROZEN_RECORDS["aurora"]
            db.execute("INSERT INTO candidate_assessments VALUES ('a-1', ?, 'r-1', ?)",
                       [key, json.dumps({"overall_score": 95})])
            db.execute("INSERT INTO candidate_active_assessments VALUES ('a-1')")
            db.execute("INSERT INTO raw_jobs VALUES (?, 'Mandatory experience with dbt and Looker')", [key])
            db.execute("INSERT INTO resume_profiles VALUES ('r-1', 'Production Looker and project dbt')")
        return path

    def test_exact_active_assessment_and_original_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._database(tmp)
            before = db_path.read_bytes()
            cases = load_cases(["aurora"], db_path)
            self.assertEqual(cases[0]["original_score"], 95)
            self.assertEqual(cases[0]["resume_hash"], "r-1")
            self.assertIn("project dbt", cases[0]["resume"])
            self.assertEqual(db_path.read_bytes(), before)

    def test_read_only_and_missing_identity_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._database(tmp)
            real_connect = duckdb.connect
            with patch("evaluation.v9_frozen_inputs.duckdb.connect", wraps=real_connect) as observed:
                load_cases(["aurora"], db_path)
                self.assertTrue(observed.call_args.kwargs["read_only"])
            with self.assertRaises(ValueError):
                load_cases(["netomi"], db_path)
            with self.assertRaises(ValueError):
                load_cases(["aurora", "aurora"], db_path)
            with self.assertRaises(ValueError):
                load_cases(["unknown"], db_path)

    def test_ambiguous_active_assessment_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._database(tmp)
            with duckdb.connect(str(db_path)) as db:
                db.execute("INSERT INTO candidate_active_assessments VALUES ('a-1')")
            with self.assertRaises(ValueError):
                load_cases(["aurora"], db_path)

    def test_missing_resume_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._database(tmp)
            with duckdb.connect(str(db_path)) as db:
                db.execute("UPDATE resume_profiles SET redacted_resume_text=''")
            with self.assertRaises(ValueError):
                load_cases(["aurora"], db_path)


if __name__ == "__main__":
    unittest.main()
