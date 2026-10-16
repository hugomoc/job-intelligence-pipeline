"""Guarded quarantine of confirmed expired LinkedIn search-page descriptions.

Dry-run by default. Applying requires a matching audit, explicit count
confirmation, a durable full DuckDB backup, and an all-or-nothing transaction.
Never calls Tavily, Serper, LinkedIn, or any external service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from src.audit_linkedin_expired_descriptions import (
    CLASS_CONFIRMED,
    classify_row,
    load_rows,
)
from src.database import DATABASE_PATH


AUDIT_TYPE = "stored_linkedin_description_redirects"
QUARANTINE_REASON = (
    "Quarantined confirmed contamination: expired LinkedIn posting "
    "redirected to unrelated job-search listings."
)
IDENTITY_REASON = (
    "Quarantined: generic LinkedIn search-results text is not the requested JD."
)


def load_confirmed_manifest(report_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    audit = json.loads(report_path.read_text())
    if audit.get("audit") != AUDIT_TYPE or audit.get("read_only") is not True:
        raise ValueError("Input is not the expected read-only LinkedIn audit report")
    findings = audit.get("findings")
    if not isinstance(findings, list):
        raise ValueError("Audit findings are missing")
    confirmed = [
        row for row in findings
        if row.get("classification") == CLASS_CONFIRMED
    ]
    if audit.get("counts", {}).get(CLASS_CONFIRMED) != len(confirmed):
        raise ValueError("Audit confirmed count does not match individual findings")
    keys = [row.get("record_key") for row in confirmed]
    if not all(isinstance(key, str) and key for key in keys):
        raise ValueError("Missing record key in confirmed audit findings")
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate confirmed record keys in audit manifest")
    return audit, confirmed


def row_signature(row: dict[str, Any]) -> str:
    """Hash both content and provenance for detecting intervening writes."""
    fields = {
        "record_key": row.get("record_key"),
        "source": row.get("source"),
        "title": row.get("title"),
        "company_name": row.get("company_name"),
        "apply_url": row.get("apply_url"),
        "description": row.get("description"),
        "final_url": row.get("final_url"),
        "attempt_status": row.get("attempt_status"),
        "official_url_status": row.get("official_url_status"),
        "verified_posting_key": row.get("verified_posting_key"),
        "description_source": row.get("description_source"),
        "identity_validation_reason": row.get("identity_validation_reason"),
    }
    data = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def matching_failures(stored: dict[str, Any] | None, manifest: dict[str, Any]) -> list[str]:
    if stored is None:
        return ["row missing or description already cleared"]
    actual = classify_row(stored)
    failures = []
    if actual["classification"] != CLASS_CONFIRMED:
        failures.append(f"no longer confirmed ({actual['classification']})")
    for key in (
        "record_key", "title", "company", "original_url", "final_url",
        "attempt_status", "official_status", "description_source",
        "description_words", "description_characters", "description_preview",
    ):
        if actual.get(key) != manifest.get(key):
            failures.append(f"audited field changed: {key}")
    if actual["evidence_markers"] != manifest.get("evidence_markers"):
        failures.append("search-results markers differ")
    if actual["verified_posting_key"] is not None or manifest.get("verified_posting_key") is not None:
        failures.append("verified posting key present; manual review required")
    if actual["official_status"] == "FOUND_VERIFIED":
        failures.append("official verification present; manual review required")
    if actual["attempt_status"] != "enriched":
        failures.append("attempt is not enriched")
    return failures


def make_plan(
    db_path: Path,
    report_path: Path,
    expected_count: int,
) -> tuple[dict[str, Any], dict[str, str]]:
    if expected_count < 1:
        raise ValueError("Expected count must be positive")
    audit, confirmed = load_confirmed_manifest(report_path)
    rows = {row["record_key"]: row for row in load_rows(db_path)}
    issues = []
    signatures = {}
    for finding in confirmed:
        key = finding["record_key"]
        row = rows.get(key)
        failures = matching_failures(row, finding)
        if failures:
            issues.append({"record_key": key, "problems": failures})
        elif row is not None:
            signatures[key] = row_signature(row)
    if len(confirmed) != expected_count:
        issues.append({
            "record_key": None,
            "problems": [
                f"Expected {expected_count} confirmed records but audit contains {len(confirmed)}"
            ],
        })
    plan = {
        "operation": "quarantine_confirmed_expired_linkedin_descriptions",
        "mode": "dry_run",
        "database": str(db_path),
        "audit_report": str(report_path),
        "audit_generated_at": audit.get("generated_at"),
        "expected_confirmed": expected_count,
        "confirmed_in_audit": len(confirmed),
        "checked_against_database": len(confirmed),
        "eligible_for_quarantine": len(signatures),
        "safety_failures": issues,
        "ready_to_apply": not issues and len(signatures) == expected_count,
        "record_keys": sorted(signatures),
        "other_audit_categories_untouched": {
            key: value for key, value in audit.get("counts", {}).items()
            if key != CLASS_CONFIRMED
        },
        "note": (
            "Review only. No database changes in dry-run. Applying clears only "
            "these confirmed descriptions, retains posting/attempt histories, "
            "and updates description_updated_at so historical scores are stale."
        ),
    }
    return plan, signatures


def file_digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def make_backup(db_path: Path, backup_dir: Path) -> tuple[Path, str]:
    # A standalone copy is not a safe snapshot while WAL has uncheckpointed
    # writes. Stop Streamlit and all workers, and let DuckDB close cleanly.
    wal = Path(str(db_path) + ".wal")
    if wal.exists():
        raise RuntimeError(
            f"Refusing backup while WAL exists: {wal}. "
            "Stop all DuckDB writers and ensure the database is checkpointed."
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"{db_path.name}.before-linkedin-quarantine.{stamp}.bak"
    if destination.resolve() == db_path.resolve():
        raise ValueError("Backup must be different from original")
    try:
        with db_path.open("rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
            target.flush()
            import os
            os.fsync(target.fileno())
        shutil.copystat(db_path, destination)
        original_sha = file_digest(db_path)
        if original_sha != file_digest(destination):
            raise RuntimeError("Backup hash mismatch, refusing cleanup")
        if wal.exists():
            raise RuntimeError("WAL appeared during backup; refusing cleanup")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination, original_sha


def recheck_in_transaction(
    connection: duckdb.DuckDBPyConnection,
    keys: list[str],
    signatures: dict[str, str],
    manifest_by_key: dict[str, dict[str, Any]],
) -> None:
    placeholders = ", ".join("?" for _ in keys)
    cursor = connection.execute(
        f"""
        SELECT r.record_key, r.source, r.title, r.company_name,
               r.apply_url, r.description, a.final_url,
               a.status AS attempt_status, a.identity_validation_reason,
               a.description_source, a.official_url_status, a.verified_posting_key
        FROM raw_jobs AS r
        JOIN job_enrichment_attempts AS a ON a.record_key = r.record_key
        WHERE r.record_key IN ({placeholders})
        """,
        keys,
    )
    fields = [col[0] for col in cursor.description]
    rows = {row[0]: dict(zip(fields, row)) for row in cursor.fetchall()}
    if set(rows) != set(keys):
        raise RuntimeError("Some audited records disappeared before cleanup")
    for key in keys:
        row = rows[key]
        if row_signature(row) != signatures[key] or matching_failures(row, manifest_by_key[key]):
            raise RuntimeError(f"Record changed since preflight: {key}")


def apply_quarantine(
    db_path: Path,
    audit_report: Path,
    expected_count: int,
    backup_dir: Path,
) -> dict[str, Any]:
    plan, signatures = make_plan(db_path, audit_report, expected_count)
    if not plan["ready_to_apply"]:
        raise ValueError("Safety checks failed; no records changed. Run dry-run and inspect its report.")

    # Never create the backup until all read-only eligibility checks pass.
    backup_path, backup_hash = make_backup(db_path, backup_dir)
    _, manifest = load_confirmed_manifest(audit_report)
    by_key = {item["record_key"]: item for item in manifest}
    keys = sorted(signatures)

    with duckdb.connect(str(db_path)) as connection:
        connection.execute("BEGIN TRANSACTION")
        try:
            recheck_in_transaction(connection, keys, signatures, by_key)
            raw_count = 0
            attempt_count = 0
            for key in keys:
                raw_count += len(connection.execute(
                    """
                    UPDATE raw_jobs SET
                        description = NULL,
                        description_updated_at = CURRENT_TIMESTAMP
                    WHERE record_key = ? AND description IS NOT NULL
                    RETURNING record_key
                    """, [key],
                ).fetchall())
                attempt_count += len(connection.execute(
                    """
                    UPDATE job_enrichment_attempts SET
                        status = 'resolution_rejected',
                        description_word_count = 0,
                        extraction_method = NULL,
                        description_hash = NULL,
                        description_source = NULL,
                        error_message = ?,
                        identity_validation_reason = ?,
                        identity_confidence = 0,
                        title_similarity = NULL,
                        company_similarity = NULL,
                        resolved_candidate_title = NULL,
                        resolved_candidate_company = NULL,
                        resolved_candidate_location = NULL,
                        reused_description = FALSE,
                        matched_prior_record_key = NULL,
                        matched_prior_source = NULL
                    WHERE record_key = ? AND status = 'enriched'
                      AND verified_posting_key IS NULL
                    RETURNING record_key
                    """, [QUARANTINE_REASON, IDENTITY_REASON, key],
                ).fetchall())
            if raw_count != expected_count or attempt_count != expected_count:
                raise RuntimeError(
                    f"Expected {expected_count} changes per table, "
                    f"got {raw_count} raw and {attempt_count} attempts"
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    return {
        **plan,
        "mode": "applied",
        "changed_raw_jobs": raw_count,
        "changed_enrichment_attempts": attempt_count,
        "backup_file": str(backup_path),
        "backup_sha256": backup_hash,
        "result": "all target descriptions cleared; historical scores made stale",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--audit-report", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument(
        "--plan-report", type=Path,
        default=DATABASE_PATH.parent / "linkedin-quarantine-plan.json",
    )
    parser.add_argument("--backup-dir", type=Path, default=DATABASE_PATH.parent / "quarantine")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm-count", type=int,
        help="For --apply, must explicitly match --expected-count",
    )
    args = parser.parse_args()
    if args.apply and args.confirm_count != args.expected_count:
        parser.error("--apply requires --confirm-count matching --expected-count")

    plan = (
        apply_quarantine(args.database, args.audit_report, args.expected_count, args.backup_dir)
        if args.apply
        else make_plan(args.database, args.audit_report, args.expected_count)[0]
    )
    args.plan_report.parent.mkdir(parents=True, exist_ok=True)
    args.plan_report.write_text(json.dumps(plan, indent=2, default=str) + "\n")
    print("Confirmed LinkedIn quarantine:", plan["mode"])
    print("Audited records:", plan["confirmed_in_audit"])
    print("Eligible records:", plan["eligible_for_quarantine"])
    print("Safety failures:", len(plan["safety_failures"]))
    if plan["mode"] == "applied":
        print("Descriptions cleared:", plan["changed_raw_jobs"])
        print("Attempt records updated:", plan["changed_enrichment_attempts"])
        print("Full backup:", plan["backup_file"])
    else:
        print("Ready to apply:", plan["ready_to_apply"])
    print("Plan report:", args.plan_report)
    print("Network requests: 0")
    if not plan["ready_to_apply"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
