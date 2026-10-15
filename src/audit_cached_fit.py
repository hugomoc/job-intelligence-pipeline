"""Audit cached AI scores against current deterministic policy without AI calls.

Default is read-only. --apply backs up the entire local DuckDB database before
downgrading only confirmed NEW hard-requirement misses to SKIP. Replayed score
drift or REVIEW-only changes are never persisted automatically.
Stop Streamlit/other database users before --apply.
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

from src.ai.resume_matcher import (
    MATCHER_PROMPT_VERSION,
    JobMatchAnalysis,
    ResumeJobMatch,
    build_resume_job_match,
    get_description_information,
)
from src.ai.resume_profiler import ResumeProfile
from src.database import DATABASE_PATH


COMPONENTS = (
    "title_fit", "skills_fit", "experience_fit", "seniority_fit",
    "industry_fit", "location_fit",
)
JSON_FIELDS = (
    "matching_strengths", "hard_requirements_missing",
    "preferred_qualifications_missing", "risk_factors",
)
PRIORITY = {"skip": 0, "review": 1, "apply": 2}


def decode_list(value: str | None) -> list[str]:
    try:
        decoded = json.loads(value) if value else []
        return decoded if isinstance(decoded, list) else []
    except (ValueError, TypeError):
        return []


def replay(row: dict[str, Any]) -> ResumeJobMatch:
    profile = ResumeProfile.model_validate_json(row["profile_json"])
    analysis = JobMatchAnalysis(
        **{name: row[name] for name in COMPONENTS},
        confidence=row["confidence"],
        **{name: decode_list(row[name]) for name in JSON_FIELDS},
        summary=row["summary"] or "Cached assessment deterministic replay",
    )
    job = {
        "record_key": row["record_key"],
        "title": row["title"],
        "company_name": row["company_name"],
        "description": row["description"],
        "location": row["location"],
    }
    _, word_count, complete = get_description_information(job)
    return build_resume_job_match(
        resume_profile=profile,
        job=job,
        model_name=row["model_name"],
        analysis=analysis,
        description_word_count=word_count,
        description_complete=complete,
    )


def should_downgrade(row: dict[str, Any], match: ResumeJobMatch) -> bool:
    # Never promote/upgrade scores based only on a deterministic replay.
    if row["prompt_version"] != MATCHER_PROMPT_VERSION:
        return False
    if not row["description_complete"] or not match.description_complete:
        return False
    if not row["description_is_current"]:
        return False
    if match.overall_score > row["overall_score"]:
        return False
    if PRIORITY[match.recommendation] > PRIORITY[row["recommendation"]]:
        return False
    # Only newly confirmed deterministic HARD requirements are eligible for
    # automatic corrections. Risk-only REVIEW changes and numeric drift stay
    # read-only: replaying normalized AI scores cannot prove a new assessment.
    if row["recommendation"] == "skip" or match.recommendation != "skip":
        return False
    existing = {
        gap.strip().casefold()
        for gap in decode_list(row["hard_requirements_missing"])
    }
    confirmed_new = [
        gap for gap in match.analysis.hard_requirements_missing
        if gap.startswith("Major gap: ")
        and gap.strip().casefold() not in existing
    ]
    return bool(confirmed_new)


def needs_manual_review(row: dict[str, Any], match: ResumeJobMatch) -> bool:
    """Surface large/risk-only changes without ever writing them to DuckDB."""
    if not (
        row["prompt_version"] == MATCHER_PROMPT_VERSION
        and row["description_complete"]
        and row["description_is_current"]
        and match.description_complete
    ):
        return False
    if should_downgrade(row, match):
        return False
    return (
        PRIORITY[match.recommendation] < PRIORITY[row["recommendation"]]
        or row["overall_score"] - match.overall_score >= 10
    )


def read_rows(connection: duckdb.DuckDBPyConnection, limit: int | None) -> list[dict[str, Any]]:
    sql = """
        SELECT
            s.*, j.title, j.company_name, j.description, j.location,
            p.profile_json,
            (s.scored_at >= coalesce(
                j.description_updated_at,
                TIMESTAMPTZ '1970-01-01 00:00:00+00'
            )) AS description_is_current
        FROM resume_job_scores AS s
        INNER JOIN raw_jobs AS j ON s.record_key = j.record_key
        INNER JOIN resume_profiles AS p ON p.resume_hash = s.resume_hash
        WHERE s.prompt_version = ?
        ORDER BY s.scored_at DESC
    """
    arguments: list[Any] = [MATCHER_PROMPT_VERSION]
    if limit is not None:
        sql += " LIMIT ?"
        arguments.append(limit)
    cursor = connection.execute(sql, arguments)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, record)) for record in cursor.fetchall()]


def update_one(connection: duckdb.DuckDBPyConnection, row: dict[str, Any], match: ResumeJobMatch) -> None:
    values: list[Any] = [
        match.overall_score,
        match.recommendation,
        *[getattr(match.analysis, name) for name in COMPONENTS],
        match.analysis.confidence,
        *[
            json.dumps(getattr(match.analysis, name), ensure_ascii=False)
            for name in JSON_FIELDS
        ],
        match.analysis.summary,
        match.description_word_count,
        match.description_complete,
        row["resume_hash"],
        row["record_key"],
        row["overall_score"],
        row["recommendation"],
        row["prompt_version"],
    ]
    changed = connection.execute(
        """
        UPDATE resume_job_scores SET
            overall_score = ?, recommendation = ?,
            title_fit = ?, skills_fit = ?, experience_fit = ?,
            seniority_fit = ?, industry_fit = ?, location_fit = ?,
            confidence = ?, matching_strengths = ?,
            hard_requirements_missing = ?,
            preferred_qualifications_missing = ?, risk_factors = ?,
            summary = ?, description_word_count = ?,
            description_complete = ?, scored_at = CURRENT_TIMESTAMP
        WHERE resume_hash = ? AND record_key = ?
          AND overall_score = ? AND recommendation = ?
          AND prompt_version = ?
        RETURNING record_key
        """,
        values,
    ).fetchall()
    if len(changed) != 1:
        raise RuntimeError("Concurrent/changed score detected; transaction rolled back.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Back up DB then apply only newly confirmed hard-gap SKIP corrections")
    parser.add_argument("--limit", type=int, default=None, help="Maximum cached rows to inspect")
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    arguments = parser.parse_args()
    if arguments.limit is not None and arguments.limit < 1:
        parser.error("--limit must be positive")
    db = arguments.database.resolve()
    if not db.is_file():
        parser.error(f"Database does not exist: {db}")

    # Calculate first with an explicit read-only connection.
    with duckdb.connect(str(db), read_only=True) as connection:
        rows = read_rows(connection, arguments.limit)

    proposed = []
    manual_review = []
    failures = []
    for row in rows:
        try:
            match = replay(row)
            if should_downgrade(row, match):
                proposed.append((row, match))
            elif needs_manual_review(row, match):
                manual_review.append((row, match))
        except Exception as error:
            failures.append((row.get("record_key"), str(error)))

    print(f"Audited {len(rows)} cached evaluations.")
    for row, match in proposed:
        gaps = match.analysis.hard_requirements_missing
        print(
            f"{row['company_name']} | {row['title']}: "
            f"{row['overall_score']}% {row['recommendation']} -> "
            f"{match.overall_score}% {match.recommendation}; "
            f"hard gaps: {gaps or 'none'}"
        )
    print(f"Confirmed new hard-gap downgrades (eligible for --apply): {len(proposed)}")
    print(f"Other material/recommendation changes (REVIEW ONLY): {len(manual_review)}")
    for row, match in manual_review[:20]:
        print(
            f"  REVIEW ONLY: {row['company_name']} | {row['title']}: "
            f"{row['overall_score']}% {row['recommendation']} -> "
            f"{match.overall_score}% {match.recommendation}"
        )
    if len(manual_review) > 20:
        print(f"  ...and {len(manual_review) - 20} more")
    print(f"Replay errors: {len(failures)}")
    for key, error in failures[:5]:
        print(f"  {key}: {error}")
    if failures and arguments.apply:
        raise SystemExit("No changes: audit errors must be resolved before applying.")

    if not arguments.apply:
        print("READ ONLY — no scores changed. Review hard gaps before considering --apply.")
        return
    if not proposed:
        print("No downgrades to apply.")
        return

    backup_dir = db.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup = backup_dir / f"{db.stem}-before-fit-audit-{suffix}{db.suffix}"

    # No connection is open during backup; a live app holding DuckDB should be stopped.
    shutil.copy2(db, backup)
    if backup.stat().st_size <= 0:
        raise RuntimeError("Backup unexpectedly empty; update cancelled.")
    print(f"Database backup created: {backup}")

    with duckdb.connect(str(db)) as connection:
        connection.execute("BEGIN TRANSACTION")
        try:
            for row, match in proposed:
                update_one(connection, row, match)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    print(f"Committed {len(proposed)} deterministic downgrades. No AI API calls.")


if __name__ == "__main__":
    main()
