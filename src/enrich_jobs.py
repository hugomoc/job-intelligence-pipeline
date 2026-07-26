from __future__ import annotations

import argparse
import time
from typing import Any

from src.database import (
    get_connection,
    initialize_database,
)
from src.enrichment.job_description import (
    JobDescriptionResult,
    count_words,
    create_http_client,
    fetch_job_description,
)


FAILED_STATUSES = {
    "blocked",
    "fetch_error",
    "invalid_url",
    "no_description",
    "not_improved",
}


def initialize_enrichment_tables() -> None:
    initialize_database()

    with get_connection() as connection:
        connection.execute(
            """
            ALTER TABLE raw_jobs
            ADD COLUMN IF NOT EXISTS
                description_updated_at TIMESTAMPTZ
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS
                job_enrichment_attempts (
                    record_key VARCHAR PRIMARY KEY,
                    source VARCHAR NOT NULL,

                    requested_url VARCHAR NOT NULL,
                    final_url VARCHAR,

                    status VARCHAR NOT NULL,
                    http_status INTEGER,
                    extraction_method VARCHAR,

                    description_word_count INTEGER
                        NOT NULL,
                    error_message VARCHAR,

                    attempt_count INTEGER
                        NOT NULL DEFAULT 1,

                    last_attempted_at TIMESTAMPTZ
                        DEFAULT CURRENT_TIMESTAMP
                )
            """
        )


def table_exists(
    connection,
    table_name: str,
) -> bool:
    result = connection.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_name = ?
        """,
        [table_name],
    ).fetchone()

    return bool(
        result
        and result[0]
    )


def load_jobs_to_enrich(
    limit: int,
    minimum_words: int,
    source: str | None,
    retry_failed: bool,
    force: bool,
) -> list[dict[str, Any]]:
    initialize_enrichment_tables()

    with get_connection() as connection:
        cursor = connection.execute(
    """
    WITH best_rule_match AS (
        SELECT
            record_key,
            match_score,
            is_recommended,
            needs_review,

            ROW_NUMBER() OVER (
                PARTITION BY record_key
                ORDER BY
                    is_recommended DESC,
                    needs_review DESC,
                    match_score DESC
            ) AS match_rank

        FROM job_matches
    )

    SELECT
        jobs.record_key,
        jobs.source,
        jobs.title,
        jobs.company_name,
        jobs.location,
        jobs.description,
        jobs.apply_url,
        jobs.discovered_at,

        attempts.status
            AS previous_status,
        attempts.attempt_count,

        COALESCE(
            matches.match_score,
            0
        ) AS rule_score,

        COALESCE(
            matches.is_recommended,
            FALSE
        ) AS is_recommended,

        COALESCE(
            matches.needs_review,
            FALSE
        ) AS needs_review

    FROM raw_jobs AS jobs

    LEFT JOIN job_enrichment_attempts
        AS attempts
        ON jobs.record_key =
           attempts.record_key

    LEFT JOIN best_rule_match AS matches
        ON jobs.record_key =
           matches.record_key
       AND matches.match_rank = 1

    WHERE jobs.apply_url IS NOT NULL

    ORDER BY
        matches.is_recommended DESC,
        matches.needs_review DESC,
        matches.match_score DESC,
        jobs.discovered_at DESC,
        jobs.title,
        jobs.company_name
    """
    )

        columns = [
            description[0]
            for description
            in cursor.description
        ]

        rows = cursor.fetchall()

    jobs = [
        dict(zip(columns, row))
        for row in rows
    ]

    selected: list[dict[str, Any]] = []

    for job in jobs:
        if (
            source
            and job["source"].casefold()
            != source.casefold()
        ):
            continue

        current_word_count = count_words(
            job.get("description")
        )

        job["current_word_count"] = (
            current_word_count
        )

        previous_status = job.get(
            "previous_status"
        )

        if not force:
            if current_word_count >= minimum_words:
                continue

            if previous_status:
                should_retry = (
                    retry_failed
                    and previous_status
                    in FAILED_STATUSES
                )

                if not should_retry:
                    continue

        selected.append(job)

        if len(selected) >= limit:
            break

    return selected


def save_enrichment_attempt(
    job: dict[str, Any],
    result: JobDescriptionResult,
    status: str | None = None,
    error_message: str | None = None,
) -> None:
    previous_attempt_count = int(
        job.get("attempt_count") or 0
    )

    final_status = status or result.status

    final_error = (
        error_message
        if error_message is not None
        else result.error_message
    )

    with get_connection() as connection:
        connection.execute(
            """
            DELETE FROM job_enrichment_attempts
            WHERE record_key = ?
            """,
            [job["record_key"]],
        )

        connection.execute(
            """
            INSERT INTO job_enrichment_attempts (
                record_key,
                source,
                requested_url,
                final_url,
                status,
                http_status,
                extraction_method,
                description_word_count,
                error_message,
                attempt_count,
                last_attempted_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                CURRENT_TIMESTAMP
            )
            """,
            [
                job["record_key"],
                job["source"],
                result.requested_url,
                result.final_url,
                final_status,
                result.http_status,
                result.extraction_method,
                result.word_count,
                final_error,
                previous_attempt_count + 1,
            ],
        )


def update_job_description(
    job: dict[str, Any],
    result: JobDescriptionResult,
) -> bool:
    current_word_count = int(
        job.get("current_word_count") or 0
    )

    if result.word_count <= current_word_count:
        return False

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE raw_jobs
            SET
                description = ?,
                description_updated_at =
                    CURRENT_TIMESTAMP
            WHERE record_key = ?
            """,
            [
                result.description,
                job["record_key"],
            ],
        )

        # Existing AI scores are stale after the
        # description changes.
        if table_exists(
            connection,
            "resume_job_scores",
        ):
            connection.execute(
                """
                DELETE FROM resume_job_scores
                WHERE record_key = ?
                """,
                [job["record_key"]],
            )

    return True


def print_result(
    job: dict[str, Any],
    result: JobDescriptionResult,
    updated: bool,
) -> None:
    print(
        f"\n{job['title']} | "
        f"{job['company_name']}"
    )

    print(
        f"Source: {job['source']}"
    )

    print(
        "Existing description: "
        f"{job['current_word_count']} words"
    )

    print(
        f"Status: {result.status}"
    )

    if result.http_status is not None:
        print(
            f"HTTP status: "
            f"{result.http_status}"
        )

    if result.final_url:
        print(
            f"Final URL: {result.final_url}"
        )

    if result.extraction_method:
        print(
            "Extraction method: "
            f"{result.extraction_method}"
        )

    print(
        "Extracted description: "
        f"{result.word_count} words"
    )

    print(
        "Database updated: "
        f"{'yes' if updated else 'no'}"
    )

    if result.error_message:
        print(
            f"Details: {result.error_message}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Retrieve complete descriptions "
            "for stored jobs."
        )
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help=(
            "Maximum jobs to process. "
            "Default: 5."
        ),
    )

    parser.add_argument(
        "--min-words",
        type=int,
        default=80,
        help=(
            "Jobs with fewer words than this "
            "are considered incomplete. "
            "Default: 80."
        ),
    )

    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help=(
            "Process one source only, such as "
            "indeed or glassdoor."
        ),
    )

    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "Retry jobs with previous failed "
            "enrichment attempts."
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Process jobs even when already "
            "attempted or complete."
        ),
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help=(
            "Seconds between requests. "
            "Default: 2."
        ),
    )

    arguments = parser.parse_args()

    if arguments.limit < 1:
        print("--limit must be at least 1.")
        raise SystemExit(1)

    if arguments.min_words < 1:
        print("--min-words must be at least 1.")
        raise SystemExit(1)

    if arguments.delay < 0:
        print("--delay cannot be negative.")
        raise SystemExit(1)

    jobs = load_jobs_to_enrich(
        limit=arguments.limit,
        minimum_words=arguments.min_words,
        source=arguments.source,
        retry_failed=arguments.retry_failed,
        force=arguments.force,
    )

    if not jobs:
        print(
            "No jobs currently require "
            "description enrichment."
        )
        return

    print(
        f"Jobs selected for enrichment: "
        f"{len(jobs)}"
    )

    totals = {
        "enriched": 0,
        "blocked": 0,
        "no_description": 0,
        "fetch_error": 0,
        "invalid_url": 0,
        "not_improved": 0,
    }

    updated_count = 0

    with create_http_client() as client:
        for index, job in enumerate(
            jobs,
            start=1,
        ):
            print(
                f"\nProcessing job "
                f"{index} of {len(jobs)}..."
            )

            result = fetch_job_description(
                url=job["apply_url"],
                client=client,
            )

            updated = False
            stored_status = result.status
            stored_error = result.error_message

            if result.status == "enriched":
                updated = update_job_description(
                    job=job,
                    result=result,
                )

                if updated:
                    updated_count += 1

                else:
                    stored_status = (
                        "not_improved"
                    )

                    stored_error = (
                        "The extracted description "
                        "was not longer than the "
                        "existing description."
                    )

            save_enrichment_attempt(
                job=job,
                result=result,
                status=stored_status,
                error_message=stored_error,
            )

            totals[stored_status] = (
                totals.get(
                    stored_status,
                    0,
                )
                + 1
            )

            print_result(
                job=job,
                result=result,
                updated=updated,
            )

            if index < len(jobs):
                time.sleep(
                    arguments.delay
                )

    print("\nEnrichment complete")
    print(
        f"Descriptions updated: "
        f"{updated_count}"
    )

    for status, count in totals.items():
        if count:
            print(
                f"{status}: {count}"
            )

    if updated_count:
        print(
            "\nDescriptions changed. "
            "Refresh rule scores with:"
        )

        print(
            "python -m src.score_jobs"
        )

        print(
            "\nThen rerun personalized "
            "AI scoring with:"
        )

        print(
            "python -m src.score_jobs_ai "
            '"src/HugoBatista_Resume.pdf" '
            "--limit 5"
        )


if __name__ == "__main__":
    main()