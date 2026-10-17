"""Safely correct manually approved historic 49% SKIPs using current deterministic rules.

Read-only by default. Only complete, fresh cached scores whose EVERY saved
hard gap is now unsupported may be proposed. AI APIs are never called.
A fingerprint allowlist, DuckDB backup and transactional compare-and-update
are mandatory for any write. Stop Streamlit before applying.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from src.ai.resume_matcher import MATCHER_PROMPT_VERSION
from ops.audit_49_scores import gap_evidence, local_day, read_scores
from ops.audit_cached_fit import decode_list, replay, update_one
from src.ai.resume_profiler import ResumeProfile
from src.database import DATABASE_PATH


def promotion_candidate(row: dict[str, Any]) -> tuple[dict[str, Any], Any] | None:
    """Only a corrected hard-cap rejection, NEVER free-standing score drift."""
    if not (
        row["overall_score"] == 49
        and row["recommendation"] == "skip"
        and row.get("prompt_version") == MATCHER_PROMPT_VERSION
        and row.get("description_complete") is True
        and row.get("description_is_current") is True
        and row.get("profile_json")
        and row.get("description")
    ):
        return None

    stored = decode_list(row.get("hard_requirements_missing"))
    if not stored:
        return None
    profile = ResumeProfile.model_validate_json(row["profile_json"])
    evidence = [
        gap_evidence(gap, str(row["description"]), profile) for gap in stored
    ]
    if any(not item["policy_status"].startswith("not_confirmed") for item in evidence):
        return None

    updated = replay(row)
    if not (
        updated.description_complete
        and updated.recommendation == "review"
        and updated.overall_score > row["overall_score"]
        and not updated.analysis.hard_requirements_missing
    ):
        return None
    signature = [
        row["resume_hash"], row["record_key"], row["scored_at"].isoformat(),
        row["overall_score"], row["recommendation"], row["prompt_version"],
        row["description"], row["profile_json"], sorted(stored),
        updated.overall_score, updated.recommendation,
        sorted(updated.analysis.hard_requirements_missing),
    ]
    digest = hashlib.sha256(
        json.dumps(signature, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "company": row["company_name"],
        "title": row["title"],
        "record_key": row["record_key"],
        "old_score": row["overall_score"],
        "old_recommendation": row["recommendation"],
        "new_score": updated.overall_score,
        "new_recommendation": updated.recommendation,
        "reason": "every saved hard gap is no longer confirmed by current JD policy",
        "previous_gaps": stored,
        "approval_fingerprint": digest,
    }, updated


def candidates_for_day(
    rows: list[dict[str, Any]], day: date, timezone_name: str,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any], Any]], list[str]]:
    candidates: list[tuple[dict[str, Any], dict[str, Any], Any]] = []
    failures: list[str] = []
    for row in rows:
        if not isinstance(row.get("scored_at"), datetime):
            continue
        if local_day(row["scored_at"], timezone_name) != day:
            continue
        if row.get("overall_score") != 49 or row.get("recommendation") != "skip":
            continue
        try:
            result = promotion_candidate(row)
            if result:
                summary, match = result
                candidates.append((summary, row, match))
        except Exception as error:
            failures.append(f"{row.get('record_key')}: {type(error).__name__}: {error}")
    return candidates, failures


def approvals(path: Path) -> set[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"version", "approved_fingerprints"}:
        raise ValueError("Approval must contain only version and approved_fingerprints")
    values = data["approved_fingerprints"]
    if data["version"] != 1 or not isinstance(values, list) or not values:
        raise ValueError("Approval version must be 1 with nonempty fingerprint list")
    if not all(
        isinstance(s, str) and len(s) == 64
        and all(c in "0123456789abcdef" for c in s)
        for s in values
    ):
        raise ValueError("Invalid fingerprint")
    if len(set(values)) != len(values):
        raise ValueError("Duplicate approval fingerprint")
    return set(values)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--timezone", default="America/Los_Angeles")
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--report", type=Path, help="Write preview without private JD/resume")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-file", type=Path)
    opts = parser.parse_args()
    try:
        ZoneInfo(opts.timezone)
    except (KeyError, ValueError) as error:
        parser.error(f"Invalid timezone: {error}")
    if opts.apply != (opts.approved_file is not None):
        parser.error("--apply and --approved-file must appear together")
    if opts.apply and opts.report:
        parser.error("Cannot generate the evidence report while applying")
    db = opts.database.expanduser().resolve()
    if not db.is_file():
        parser.error(f"Database does not exist: {db}")
    report_file = opts.report.expanduser().resolve() if opts.report else None
    if report_file and (report_file.exists() or report_file == db):
        parser.error("Refusing to overwrite an existing report or the database")

    with duckdb.connect(str(db), read_only=True) as connection:
        rows = read_scores(connection)
    candidates, failures = candidates_for_day(rows, opts.date, opts.timezone)
    print(f"Evaluated {len(rows)} saved scores; {len(candidates)} possible REVIEW corrections.")
    for summary, _, _ in candidates:
        print(
            f"  {summary['company']} | {summary['title']}: "
            f"{summary['old_score']}% {summary['old_recommendation']} -> "
            f"{summary['new_score']}% {summary['new_recommendation']}"
        )
    print(f"Replay errors: {len(failures)}")
    for error in failures[:4]:
        print(f"  {error}")
    if report_file:
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(json.dumps({
            "version": 1, "date": opts.date.isoformat(), "read_only": True,
            "note": "Replays are not automatic authorizations; approve only verified records.",
            "candidate_count": len(candidates),
            "candidates": [summary for summary, _, _ in candidates],
            "replay_errors": len(failures),
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Saved approval evidence: {report_file}")

    if not opts.apply:
        print("READ ONLY — zero stored scores changed.")
        return
    if failures:
        raise SystemExit("Refusing update: fix replay errors first.")
    try:
        allowed = approvals(opts.approved_file)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Invalid approval: {error}") from error
    present = {summary["approval_fingerprint"] for summary, _, _ in candidates}
    if allowed - present:
        raise SystemExit("Refusing update: one or more approvals are missing or stale")
    picked = [
        (summary, row, match) for summary, row, match in candidates
        if summary["approval_fingerprint"] in allowed
    ]
    if len(picked) != len(allowed):
        raise SystemExit("Refusing update: approval did not uniquely identify records")

    backups = db.parent / "backups"
    backups.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup = backups / f"{db.stem}-before-approved-49-review-{stamp}{db.suffix}"
    shutil.copy2(db, backup)
    if backup.stat().st_size <= 0:
        raise RuntimeError("Backup empty; refusing update")
    print(f"Database backup created: {backup}")

    with duckdb.connect(str(db)) as connection:
        connection.execute("BEGIN TRANSACTION")
        try:
            for _, row, match in picked:
                # Existing compare-and-update also guards score, recommendation,
                # prompt version, resume hash and record identity.
                update_one(connection, row, match)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    print(f"Committed {len(picked)} explicitly approved REVIEW corrections.")
    print("Refresh analytics for the UI: scripts/dbt_jobs.sh build")


if __name__ == "__main__":
    main()
