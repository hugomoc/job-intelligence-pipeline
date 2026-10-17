"""Backfill deterministic title classification for existing jobs."""

from __future__ import annotations

import argparse

from src.database import get_connection, initialize_database
from src.job_title_filter import classify_job_title


def reclassify_titles(
    limit: int | None = None,
    log_filtered: bool = False,
) -> int:
    """Recompute title-filter fields without enrichment or AI calls."""
    initialize_database()

    with get_connection() as connection:
        query = """
            SELECT record_key, title
            FROM raw_jobs
            ORDER BY discovered_at DESC NULLS LAST, record_key
        """

        parameters: list[int] = []

        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)

        rows = connection.execute(
            query,
            parameters,
        ).fetchall()

        updates = []

        for record_key, title in rows:
            classification = classify_job_title(title)
            updates.append(
                [
                    classification.normalized_title,
                    classification.category,
                    classification.score,
                    classification.reason,
                    classification.matched_pattern,
                    record_key,
                ]
            )

            if log_filtered and classification.category == "FILTERED_OUT":
                print(
                    f'Filtered: "{title or "<missing title>"}"\n'
                    f"Reason: {classification.reason}"
                )

        connection.executemany(
            """
            UPDATE raw_jobs
            SET
                normalized_title = ?,
                title_classification = ?,
                title_match_score = ?,
                title_filter_reason = ?,
                title_matched_pattern = ?
            WHERE record_key = ?
            """,
            updates,
        )

    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute deterministic title classifications for existing "
            "DuckDB raw_jobs rows."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of jobs to reclassify.",
    )
    parser.add_argument(
        "--show-filtered",
        action="store_true",
        help="Print each filtered job and reason during backfill.",
    )
    args = parser.parse_args()

    updated = reclassify_titles(
        limit=args.limit,
        log_filtered=args.show_filtered,
    )

    print(f"Title classifications updated: {updated}")


if __name__ == "__main__":
    main()
