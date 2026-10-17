"""Audit existing 49% job scores without changing DuckDB or calling AI APIs.

Unlike audit_cached_fit, this tool inspects ALREADY-SKIPPED 49% records,
including those for which no new hard-gap downgrade would be proposed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from src.ai.requirement_policy import (
    required_platform_tenure_gaps,
    validate_hard_gap_clause,
)
from src.ai.resume_matcher import (
    MATCHER_PROMPT_VERSION,
    JobMatchAnalysis,
    calculate_overall_score,
)
from src.ai.resume_profiler import ResumeProfile
from ops.audit_cached_fit import COMPONENTS, JSON_FIELDS, decode_list, replay
from src.database import DATABASE_PATH
from src.fit_gap_evidence import gap_clause_pattern, platform_tenure_gap


def local_day(value: datetime, timezone_name: str) -> date:
    """DuckDB TIMESTAMPTZ values are aware; protect naive legacy values."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(timezone_name)).date()


def read_scores(connection: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info('raw_jobs')").fetchall()
    }
    freshness = (
        "(s.scored_at >= coalesce(j.description_updated_at, "
        "TIMESTAMPTZ '1970-01-01 00:00:00+00'))"
        if "description_updated_at" in columns else "FALSE"
    )
    cursor = connection.execute(
        f"""
        SELECT s.*, j.title, j.company_name, j.description, j.location,
               j.source, j.email_date, p.profile_json,
               {freshness} AS description_is_current
        FROM resume_job_scores AS s
        JOIN raw_jobs AS j ON s.record_key = j.record_key
        LEFT JOIN resume_profiles AS p ON p.resume_hash = s.resume_hash
        ORDER BY s.scored_at DESC
        """
    )
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, record)) for record in cursor.fetchall()]


def gap_evidence(
    gap: str, description: str, profile: ResumeProfile
) -> dict[str, Any]:
    """Expose stored JD support without declaring any gap automatically true."""
    tenure = platform_tenure_gap(gap)
    if tenure is not None:
        missing, uncertain = required_platform_tenure_gaps(description, profile)
        if any(gap == item for item in missing):
            status = "still_missing_under_tenure_policy"
        elif any(tenure[0].lower() in item.lower() for item in uncertain):
            status = "tenure_unverified_review_not_skip"
        else:
            status = "not_confirmed_by_current_tenure_policy"
        pattern = gap_clause_pattern(gap)
        from src.ai.requirement_policy import (
            _platform_tenure_requirement,
            description_requirement_clauses,
        )
        matched = []
        for row in description_requirement_clauses(description):
            if pattern is None or not pattern.search(row["clause"]):
                continue
            parsed = _platform_tenure_requirement(row["clause"])
            if parsed is not None and (parsed[0], parsed[2]) == tenure[:2]:
                matched.append(row)
        clauses = matched
    else:
        check = validate_hard_gap_clause(gap, description, profile)
        status = (
            "still_hard_under_current_policy" if check["keep_hard_gap"]
            else "not_confirmed_by_current_policy"
        )
        clauses = check.get("matched_clauses", [])
    return {
        "stored_gap": gap,
        "policy_status": status,
        "matching_saved_jd_clauses": [
            {
                "text": str(item["clause"])[:550],
                "mandatory": bool(item["mandatory"]),
                "optional": bool(item["optional"]),
                "section": item["section"],
            }
            for item in clauses[:5]
        ],
        "more_matching_clauses": max(0, len(clauses) - 5),
    }


def audit_score(row: dict[str, Any]) -> dict[str, Any]:
    """Audit one saved score without database writes or AI calls."""
    stored_gaps = decode_list(row.get("hard_requirements_missing"))
    result = {
        "company": row["company_name"],
        "title": row["title"],
        "source": row.get("source"),
        "record_key": row["record_key"],
        "stored_score": row["overall_score"],
        "stored_recommendation": row["recommendation"],
        "scored_at": row["scored_at"].isoformat(),
        "prompt_version": row.get("prompt_version"),
        "description_is_current": bool(row.get("description_is_current")),
        "description_complete": bool(row.get("description_complete")),
        "stored_hard_gap_count": len(stored_gaps),
        "component_score_without_final_49_cap": None,
        "gaps": [],
        "current_replay": None,
        "replay_error": None,
    }
    try:
        base_analysis = JobMatchAnalysis(
            **{name: row[name] for name in COMPONENTS},
            confidence=row["confidence"],
            **{name: decode_list(row.get(name)) for name in JSON_FIELDS},
            summary=row.get("summary") or "Stored AI analysis",
        )
        result["component_score_without_final_49_cap"] = calculate_overall_score(
            base_analysis
        )
        if row.get("profile_json"):
            profile = ResumeProfile.model_validate_json(row["profile_json"])
            result["gaps"] = [
                gap_evidence(str(gap), str(row.get("description") or ""), profile)
                for gap in stored_gaps
            ]
        elif stored_gaps:
            result["replay_error"] = "Saved resume profile unavailable"
        if (
            row.get("prompt_version") == MATCHER_PROMPT_VERSION
            and row.get("profile_json")
        ):
            updated = replay(row)
            result["current_replay"] = {
                "score": updated.overall_score,
                "recommendation": updated.recommendation,
                "hard_gaps": updated.analysis.hard_requirements_missing,
                "risk_factors": updated.analysis.risk_factors[:10],
            }
    except Exception as error:
        result["replay_error"] = f"{type(error).__name__}: {error}"
    result["needs_attention"] = (
        not stored_gaps
        or any(
            item["policy_status"].startswith("not_confirmed")
            or item["policy_status"] == "tenure_unverified_review_not_skip"
            for item in result["gaps"]
        )
        or bool(result["current_replay"] and (
            result["current_replay"]["score"] > result["stored_score"]
            or result["current_replay"]["recommendation"] != result["stored_recommendation"]
        ))
        or bool(result["replay_error"])
    )
    return result


def build_report(rows: list[dict[str, Any]], day: date, tz: str) -> dict[str, Any]:
    selected = [
        row for row in rows
        if isinstance(row.get("scored_at"), datetime)
        and local_day(row["scored_at"], tz) == day
    ]
    low = [row for row in selected if row["overall_score"] == 49]
    output = [audit_score(row) for row in low]
    gap_counts = Counter(
        gap["stored_gap"]
        for item in output for gap in item["gaps"]
    )
    status_counts = Counter(
        gap["policy_status"]
        for item in output for gap in item["gaps"]
    )
    return {
        "version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "date": day.isoformat(),
        "timezone": tz,
        "read_only": True,
        "note": (
            "A JD keyword/section alone never proves a mandatory skill. "
            "Replayed scores are diagnostic only, not permission to rewrite saved scores. "
            "This report contains selected saved JD snippets, not full JDs or resumes."
        ),
        "total_scored_on_date": len(selected),
        "score_49_count": len(output),
        "score_49_with_stored_hard_gap": sum(bool(x["stored_hard_gap_count"]) for x in output),
        "score_49_without_stored_hard_gap": sum(not x["stored_hard_gap_count"] for x in output),
        "score_49_needing_attention": sum(x["needs_attention"] for x in output),
        "top_stored_hard_gaps": [
            {"gap": gap, "count": count} for gap, count in gap_counts.most_common()
        ],
        "policy_status_counts": dict(status_counts),
        "jobs": output,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date", default=None,
        help="Local scored-at date, YYYY-MM-DD (default today in --timezone)",
    )
    parser.add_argument("--timezone", default="America/Los_Angeles")
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument(
        "--output", type=Path,
        help="Optional JSON report file (must not already exist)",
    )
    args = parser.parse_args()
    try:
        zone = ZoneInfo(args.timezone)
        day = date.fromisoformat(args.date) if args.date else datetime.now(zone).date()
    except (ValueError, KeyError) as error:
        parser.error(f"Invalid date/timezone: {error}")
    db = args.database.expanduser().resolve()
    if not db.is_file():
        parser.error(f"Database does not exist: {db}")
    if args.output:
        target = args.output.expanduser().resolve()
        if target.exists() or target == db:
            parser.error("Refusing to overwrite an existing file or the database")
    with duckdb.connect(str(db), read_only=True) as connection:
        rows = read_scores(connection)
    report = build_report(rows, day, args.timezone)
    print(
        f"Read-only 49% audit for {day.isoformat()} ({args.timezone}): "
        f"{report['total_scored_on_date']} scored, {report['score_49_count']} at 49%."
    )
    print(
        f"49% with stored hard gaps: {report['score_49_with_stored_hard_gap']}; "
        f"without: {report['score_49_without_stored_hard_gap']}; "
        f"flagged for review: {report['score_49_needing_attention']}."
    )
    for group in report["top_stored_hard_gaps"][:12]:
        print(f"  {group['count']} × {group['gap']}")
    for item in report["jobs"][:30]:
        flags = ", ".join(sorted({g["policy_status"] for g in item["gaps"]}))
        updated = item["current_replay"]
        replay_hint = (
            f"; replay {updated['score']}% {updated['recommendation']}"
            if updated else ""
        )
        print(
            f"  {item['company']} | {item['title']}: 49% "
            f"({item['stored_hard_gap_count']} saved hard gaps; "
            f"{flags or 'none'}{replay_hint})"
        )
    if len(report["jobs"]) > 30:
        print(f"  ...and {len(report['jobs']) - 30} more (see JSON)")
    if args.output:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Saved evidence report: {target}")
    print("READ ONLY — no database changes, no AI API calls.")


if __name__ == "__main__":
    main()
