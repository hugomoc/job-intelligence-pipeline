"""Audit existing enrichment attempts for identity mismatches."""

from __future__ import annotations

import argparse

from src.database import get_connection, initialize_database
from src.enrich_jobs import initialize_enrichment_tables
from src.enrichment.job_identity import validate_job_identity


def audit_enrichment_identity(
    limit: int | None = None,
) -> dict[str, int]:
    """Report suspicious enrichments without mutating job or score data."""
    initialize_database()
    initialize_enrichment_tables()

    query = """
        SELECT
            jobs.record_key,
            jobs.title,
            jobs.company_name,
            jobs.location,
            attempts.final_url,
            attempts.resolved_candidate_title,
            attempts.resolved_candidate_company,
            attempts.resolved_candidate_location
        FROM raw_jobs AS jobs
        INNER JOIN job_enrichment_attempts AS attempts
            ON jobs.record_key = attempts.record_key
        WHERE attempts.resolved_candidate_title IS NOT NULL
           OR attempts.resolved_candidate_company IS NOT NULL
        ORDER BY attempts.last_attempted_at DESC NULLS LAST
    """
    parameters: list[int] = []

    if limit is not None:
        query += " LIMIT ?"
        parameters.append(limit)

    totals = {
        "checked": 0,
        "valid": 0,
        "invalid": 0,
        "unknown": 0,
    }

    with get_connection() as connection:
        rows = connection.execute(
            query,
            parameters,
        ).fetchall()

    for row in rows:
        (
            record_key,
            title,
            company_name,
            location,
            final_url,
            resolved_title,
            resolved_company,
            resolved_location,
        ) = row

        validation = validate_job_identity(
            original_title=title,
            original_company=company_name,
            resolved_title=resolved_title,
            resolved_company=resolved_company,
            original_location=location,
            resolved_location=resolved_location,
        )

        totals["checked"] += 1

        if validation.accepted:
            totals["valid"] += 1
            continue

        totals["invalid"] += 1
        print(
            "Suspicious enrichment\n"
            f"record_key={record_key}\n"
            f'original="{title} | {company_name}"\n'
            f'candidate="{resolved_title} | {resolved_company}"\n'
            f"url={final_url}\n"
            f"reason={validation.reason}\n"
        )

    return totals


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Audit stored enrichment candidate metadata for identity "
            "mismatches. This does not delete descriptions or AI scores."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of enrichment attempts to inspect.",
    )
    args = parser.parse_args()

    totals = audit_enrichment_identity(
        limit=args.limit,
    )

    print(
        "Enrichment identity audit summary\n"
        f"Checked: {totals['checked']}\n"
        f"Valid: {totals['valid']}\n"
        f"Invalid/Suspicious: {totals['invalid']}\n"
        f"Unknown: {totals['unknown']}"
    )


if __name__ == "__main__":
    main()
