"""Read-only loader for frozen October 20 v9 comparison cases.

Exact record keys prevent accidental changes in job identity/selection.
No imports from writable pipeline repositories or scorer modules.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from src.database import DATABASE_PATH

# Original audit sample identifiers, never matching by company/title alone.
FROZEN_RECORDS = {
    "aurora": "b464bdd574b8a4b6bdd2aa5315d7ba0675296248cc68aad20f265695bc03e241",
    "netomi": "14a7f88194acf3f0cde6710358cba5353f49fafae224b819bb46522a69d1e38d",
    "tesla": "c79deff7e14dac556698e9cfd87486c4b1af3a52fc4c6ebb46f5468ed50fad21",
    "centerwell": "aad655cfccd300ff5432eb89cde950fa15f7e82dac6766bb69a9e85c2f3fd8d2",
    "constructor": "708017d2074071c34699948b8d2ba1eacac6151e5bf99ecd16ce8289cac35adc",
}

SQL = """
SELECT a.assessment_id, a.record_key, a.resume_hash,
       a.payload_json, j.description, r.redacted_resume_text
FROM candidate_assessments AS a
JOIN candidate_active_assessments AS active
  ON active.assessment_id = a.assessment_id
JOIN raw_jobs AS j ON j.record_key = a.record_key
JOIN resume_profiles AS r ON r.resume_hash = a.resume_hash
WHERE a.record_key = ?
"""


def load_cases(names: list[str], database: Path = DATABASE_PATH) -> list[dict]:
    if not names or len(set(names)) != len(names):
        raise ValueError("Select one or more distinct cases")
    if any(name not in FROZEN_RECORDS for name in names):
        raise ValueError("Unknown frozen case name")
    path = database.expanduser().resolve(strict=True)
    results = []
    with duckdb.connect(str(path), read_only=True) as connection:
        for name in names:
            rows = connection.execute(SQL, [FROZEN_RECORDS[name]]).fetchall()
            if len(rows) != 1:
                raise ValueError(f"{name}: expected exactly one active assessment, found {len(rows)}")
            assessment_id, record_key, resume_hash, payload_json, jd, resume = rows[0]
            if not isinstance(jd, str) or not jd.strip():
                raise ValueError(f"{name}: full stored description missing")
            if not isinstance(resume, str) or not resume.strip():
                raise ValueError(f"{name}: scored resume text missing")
            payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
            score = payload.get("overall_score") if isinstance(payload, dict) else None
            results.append({
                "case_id": name,
                "jd": jd,
                "resume": resume,
                "original_score": score,
                "record_key": record_key,
                "resume_hash": resume_hash,
                "assessment_id": str(assessment_id),
            })
    return results
