"""Backfill verified cross-source reuse without external network calls.

This command intentionally does not run source fetches, official search, or AI
scoring. It invokes the enrichment loader's Stage 1 local reuse so existing
records can copy verified descriptions from historical duplicates when the same
verified_posting_key can be assigned safely.
"""

from __future__ import annotations

import argparse

from src.database import get_connection
from src.enrich_jobs import (
    DEFAULT_ENRICHMENT_LIMIT,
    initialize_enrichment_tables,
    load_jobs_to_enrich,
)


def reused_description_count() -> int:
    with get_connection() as connection:
        result = connection.execute(
            """
            SELECT count(*)
            FROM job_enrichment_attempts
            WHERE coalesce(reused_description, false) = true
            """
        ).fetchone()

    return int(result[0] or 0) if result else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill verified cross-source description reuse without "
            "network enrichment."
        )
    )
    parser.add_argument(
        "--minimum-words",
        type=int,
        default=80,
        help="Minimum full-description word count.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10_000,
        help=(
            "Maximum jobs to scan through the existing enrichment loader. "
            "No external requests are made by this command."
        ),
    )
    parser.add_argument(
        "--resume-hash",
        default=None,
        help="Optional resume hash for the existing admission gate.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    initialize_enrichment_tables()

    before = reused_description_count()
    queue_jobs = load_jobs_to_enrich(
        limit=max(args.limit, DEFAULT_ENRICHMENT_LIMIT),
        minimum_words=args.minimum_words,
        source=None,
        retry_failed=False,
        force=False,
        resume_hash=args.resume_hash,
    )
    after = reused_description_count()

    print("Verified local reuse backfill complete.")
    print(f"Descriptions reused before: {before}")
    print(f"Descriptions reused after: {after}")
    print(f"New descriptions reused: {after - before}")
    print(f"Remaining external enrichment candidates: {len(queue_jobs)}")


if __name__ == "__main__":
    main()
