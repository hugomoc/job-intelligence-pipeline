"""Deterministic offline tests of guarded LinkedIn description quarantine."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb

from src.audit_linkedin_expired_descriptions import audit, CLASS_CONFIRMED
from src.quarantine_confirmed_linkedin_descriptions import (
    apply_quarantine, load_confirmed_manifest, make_plan, make_backup,
)

LISTING = (
    "Get notified when a new job is posted. Set alert. Sign in to set job "
    "alerts for Senior Data Engineer roles. 11,000+ Senior Data Engineer "
    "Jobs in United States. Actively Hiring. Be an early applicant. "
) * 8
VALID = "Build and manage Python data pipelines, SQL analytics and business intelligence." * 12
EXPIRED = "https://www.linkedin.com/jobs/senior-data-engineer-jobs?trk=expired_jd_redirect"


def init_db(path: Path) -> None:
    with duckdb.connect(str(path)) as db:
        db.execute("""
        CREATE TABLE raw_jobs(
            record_key VARCHAR PRIMARY KEY, source VARCHAR,
            title VARCHAR, company_name VARCHAR, apply_url VARCHAR,
            description VARCHAR, description_updated_at TIMESTAMPTZ)
        """)
        db.execute("""
        CREATE TABLE job_enrichment_attempts(
            record_key VARCHAR PRIMARY KEY, final_url VARCHAR,
            status VARCHAR, identity_validation_reason VARCHAR,
            description_source VARCHAR, official_url_status VARCHAR,
            verified_posting_key VARCHAR, error_message VARCHAR,
            description_word_count INTEGER, extraction_method VARCHAR,
            description_hash VARCHAR, identity_confidence DOUBLE,
            title_similarity DOUBLE, company_similarity DOUBLE,
            resolved_candidate_title VARCHAR,
            resolved_candidate_company VARCHAR,
            resolved_candidate_location VARCHAR,
            reused_description BOOLEAN, matched_prior_record_key VARCHAR,
            matched_prior_source VARCHAR, attempt_count INTEGER)
        """)
        for key, desc, dest, final_status in [
            ("confirmed-1", LISTING, EXPIRED, "enriched"),
            ("confirmed-2", LISTING, EXPIRED, "enriched"),
            ("safe", VALID, "https://www.linkedin.com/jobs/view/123/", "enriched"),
        ]:
            db.execute(
                "INSERT INTO raw_jobs VALUES (?,?,?,?,?,?,NULL)",
                [key, "linkedin", "Senior Data Engineer", "Acme",
                 "https://www.linkedin.com/jobs/view/123/", desc],
            )
            db.execute(
                """INSERT INTO job_enrichment_attempts
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [key, dest, final_status, "identity validation not required for this source",
                 "search", "SEARCH_DEFERRED", None, "older result",
                 len(desc.split()), "html_selector", "stale-hash",
                 1.0, 1.0, 1.0, "Wrong candidate", "Wrong company",
                 "Remote", True, "some-old-record", "linkedin", 4],
            )


def prepare(temp: Path):
    database = temp / "fixture.duckdb"
    init_db(database)
    audit_path = temp / "audit.json"
    report = audit(database)
    audit_path.write_text(json.dumps(report))
    assert report["counts"][CLASS_CONFIRMED] == 2
    return database, audit_path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_dry_run_is_byte_identical_and_reports_two_targets() -> None:
    with TemporaryDirectory() as base:
        database, report = prepare(Path(base))
        before = digest(database)
        plan, hashes = make_plan(database, report, expected_count=2)
        assert plan["ready_to_apply"]
        assert len(plan["record_keys"]) == len(hashes) == 2
        assert plan["eligible_for_quarantine"] == 2
        assert digest(database) == before


def test_apply_creates_full_backup_and_changes_only_confirmed() -> None:
    with TemporaryDirectory() as base:
        directory = Path(base)
        database, report = prepare(directory)
        before = digest(database)
        result = apply_quarantine(database, report, 2, directory / "backups")
        assert result["mode"] == "applied"
        assert result["changed_raw_jobs"] == 2
        assert result["changed_enrichment_attempts"] == 2
        backup = Path(result["backup_file"])
        assert backup.is_file()
        assert result["backup_sha256"] == digest(backup) == before
        with duckdb.connect(str(database), read_only=True) as db:
            cleared = db.execute("""
                SELECT r.record_key, r.description, r.description_updated_at,
                    a.status, a.description_hash, a.description_source,
                    a.description_word_count, a.final_url,
                    a.official_url_status, a.verified_posting_key,
                    a.attempt_count
                FROM raw_jobs r
                JOIN job_enrichment_attempts a USING (record_key)
                WHERE r.record_key LIKE 'confirmed-%'
                ORDER BY r.record_key
            """).fetchall()
            assert len(cleared) == 2
            for row in cleared:
                assert row[1] is None
                assert row[2] is not None
                assert row[3] == "resolution_rejected"
                assert row[4] is None and row[5] is None and row[6] == 0
                assert row[7] == EXPIRED
                assert row[8] == "SEARCH_DEFERRED"
                assert row[9] is None
                assert row[10] == 4
            safe = db.execute("""
                SELECT r.description, a.status
                FROM raw_jobs r JOIN job_enrichment_attempts a USING (record_key)
                WHERE r.record_key = 'safe'
            """).fetchone()
            assert safe == (VALID, "enriched")


def test_manifest_drift_or_present_verified_key_aborts_before_backup() -> None:
    with TemporaryDirectory() as base:
        directory = Path(base)
        database, report = prepare(directory)
        with duckdb.connect(str(database)) as db:
            db.execute("""
                UPDATE raw_jobs SET description = 'Actually a different description'
                WHERE record_key = 'confirmed-1'
            """)
        before = digest(database)
        plan, _ = make_plan(database, report, 2)
        assert not plan["ready_to_apply"]
        assert plan["safety_failures"]
        try:
            apply_quarantine(database, report, 2, directory / "backups")
        except ValueError as error:
            assert "Safety checks failed" in str(error)
        else:
            raise AssertionError("Must refuse changed audited text")
        assert digest(database) == before
        assert not (directory / "backups").exists()

    with TemporaryDirectory() as base:
        directory = Path(base)
        database, report = prepare(directory)
        with duckdb.connect(str(database)) as db:
            db.execute("""
                UPDATE job_enrichment_attempts
                SET verified_posting_key = 'greenhouse:999'
                WHERE record_key = 'confirmed-1'
            """)
        plan, _ = make_plan(database, report, 2)
        assert not plan["ready_to_apply"]


def test_wrong_expected_count_or_duplicate_manifest_aborts() -> None:
    with TemporaryDirectory() as base:
        directory = Path(base)
        database, report = prepare(directory)
        plan, _ = make_plan(database, report, 3)
        assert not plan["ready_to_apply"]
        audit_data = json.loads(report.read_text())
        audit_data["findings"].append(dict(audit_data["findings"][0]))
        audit_data["counts"][CLASS_CONFIRMED] += 1
        report.write_text(json.dumps(audit_data))
        try:
            load_confirmed_manifest(report)
        except ValueError as error:
            assert "Duplicate" in str(error)
        else:
            raise AssertionError("Duplicated key in manifest must fail")


def test_existing_wal_prevents_unsafe_file_copy() -> None:
    with TemporaryDirectory() as base:
        directory = Path(base)
        database, report = prepare(directory)
        wal = Path(str(database) + ".wal")
        wal.write_bytes(b"writer-in-progress")
        try:
            make_backup(database, directory / "backups")
        except RuntimeError as error:
            assert "WAL exists" in str(error)
        else:
            raise AssertionError("Must refuse a potentially incomplete backup")
        wal.unlink()


def main():
    test_dry_run_is_byte_identical_and_reports_two_targets()
    test_apply_creates_full_backup_and_changes_only_confirmed()
    test_manifest_drift_or_present_verified_key_aborts_before_backup()
    test_wrong_expected_count_or_duplicate_manifest_aborts()
    test_existing_wal_prevents_unsafe_file_copy()
    print("Guarded LinkedIn quarantine tests passed.")


if __name__ == "__main__":
    main()
