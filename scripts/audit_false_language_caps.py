"""Read-only audit of v8 job scores capped by personal eligibility logic.

Usage:
    python -m scripts.audit_false_language_caps --output data/false-language-caps-dry-run.json

No provider calls, migrations, score updates, or database writes.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import duckdb

from src.ai.resume_matcher import confirmed_mandatory_personal_mismatches
from src.ai.resume_profiler import ResumeProfile
from src.database import DATABASE_PATH


def audit(database_path: Path) -> dict:
    if not database_path.is_file():
        raise FileNotFoundError(f"Database not found: {database_path}")

    # Never use get_connection()/initialize_ai_tables(): they permit writes.
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        rows = connection.execute("""
            SELECT s.resume_hash, s.record_key, s.overall_score,
                   s.recommendation, s.title_fit, s.skills_fit,
                   s.experience_fit, s.seniority_fit, s.industry_fit,
                   s.hard_requirements_missing, s.model_name,
                   s.prompt_version, j.title, j.company_name,
                   j.description, p.profile_json
            FROM resume_job_scores AS s
            JOIN raw_jobs AS j ON j.record_key = s.record_key
            JOIN resume_profiles AS p ON p.resume_hash = s.resume_hash
            WHERE s.overall_score = 49
              AND s.recommendation = 'skip'
              AND s.prompt_version LIKE 'v8%'
            ORDER BY j.company_name, j.title, s.record_key
        """).fetchall()
    finally:
        connection.close()

    results = []
    for row in rows:
        (resume_hash, key, final_score, recommendation, title_fit,
         skills_fit, experience_fit, seniority_fit, industry_fit,
         old_gaps_json, model, prompt_version, title, company,
         description, profile_json) = row
        item = {
            "resume_hash": resume_hash,
            "record_key": key,
            "company": company,
            "title": title,
            "provider": model,
            "prompt_version": prompt_version,
            "saved_score": final_score,
            "saved_recommendation": recommendation,
            "pre_adjustment_score": None,
            "proposed_score": None,
            "proposed_recommendation": None,
            "status": "needs_review",
            "remaining_personal_mismatches": [],
            "old_hard_gaps": [],
        }
        try:
            item["old_hard_gaps"] = json.loads(old_gaps_json or "[]")
            components = (title_fit, skills_fit, experience_fit,
                          seniority_fit, industry_fit)
            # The v8 adapter sets every component to the same raw model score.
            if any(v is None for v in components) or len(set(components)) != 1:
                item["reason"] = "Cannot verify original model score from components"
            elif not description:
                item["reason"] = "Missing saved job description"
            else:
                raw_score = int(components[0])
                item["pre_adjustment_score"] = raw_score
                profile = ResumeProfile.model_validate_json(profile_json)
                remaining = confirmed_mandatory_personal_mismatches(profile, description)
                item["remaining_personal_mismatches"] = remaining
                if remaining:
                    item["status"] = "keep_cap"
                    item["reason"] = "Current parser still confirms personal ineligibility"
                elif raw_score <= 49:
                    item["status"] = "keep_score"
                    item["reason"] = "Underlying model score is 49 or lower"
                else:
                    item["status"] = "candidate_restore"
                    item["proposed_score"] = raw_score
                    item["proposed_recommendation"] = (
                        "apply" if raw_score >= 75 else "review"
                    )
                    item["reason"] = (
                        "No mandatory personal mismatch remains; proposed only, "
                        "not verified for every other possible scoring adjustment"
                    )
        except (ValueError, TypeError, KeyError) as exc:
            item["reason"] = f"Cannot safely replay saved evidence: {type(exc).__name__}"
        results.append(item)

    counts = dict(sorted(Counter(item["status"] for item in results).items()))
    return {
        "mode": "read_only_no_changes",
        "database": str(database_path),
        "scanned_49_v8_rows": len(rows),
        "status_counts": counts,
        "note": ("Candidate restoration is a proposal, not a database update. "
                 "Review eligibility evidence before any separate apply step."),
        "rows": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    args = parser.parse_args()
    result = audit(args.database)
    print(json.dumps(
        {"scanned_49_v8_rows": result["scanned_49_v8_rows"],
         "status_counts": result["status_counts"], "mode": result["mode"]},
        indent=2,
    ))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"Report written: {args.output}")


if __name__ == "__main__":
    main()
