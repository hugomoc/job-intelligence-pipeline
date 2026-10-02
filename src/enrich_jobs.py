"""CLI and helpers for enriching incomplete job descriptions.

Ingested email alerts often contain thin descriptions. This module tries to
fetch better public descriptions from the apply URL and records every attempt
so failed/blocked pages can be retried intentionally.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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
from src.enrichment.job_identity import (
    JobIdentityValidation,
    validate_job_identity,
)
from src.enrichment.official_job_resolver import (
    OFFICIAL_FOUND_VERIFIED,
    OfficialJobResolutionResult,
    resolve_official_job,
    should_attempt_official_resolution,
)
from src.job_title_filter import EXCLUDED_TITLE_SQL_REGEX
from src.repositories.recommendation_repository import (
    apply_admission_gate,
    calculate_enrichment_priority,
    description_has_quality_signals,
    description_state,
    register_exact_posting_identity_function,
)


FAILED_STATUSES = {
    "blocked",
    "fetch_error",
    "invalid_url",
    "no_description",
    "not_improved",
    "resolution_rejected",
}

AGGREGATOR_SOURCES_REQUIRING_IDENTITY = {
    "lensa",
    "jobleads",
}


@dataclass(frozen=True)
class EnrichmentProcessingResult:
    result: JobDescriptionResult
    stored_status: str
    stored_error: str | None
    identity_validation: JobIdentityValidation | None
    official_resolution: OfficialJobResolutionResult | None
    updated: bool


def initialize_enrichment_tables() -> None:
    """Add enrichment metadata tables/columns without changing raw job shape."""
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

        for statement in (
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS resolved_candidate_title VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS resolved_candidate_company VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS resolved_candidate_location VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS identity_confidence DOUBLE
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS title_similarity DOUBLE
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS company_similarity DOUBLE
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS identity_validation_reason VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_job_url VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_url_status VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_url_source VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_url_resolved_at TIMESTAMPTZ
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_url_confidence DOUBLE
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_url_validation_reason VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_resolved_title VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_resolved_company VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS official_resolved_location VARCHAR
            """,
        ):
            connection.execute(statement)


def requires_identity_validation(
    job: dict[str, Any],
    result: JobDescriptionResult,
) -> bool:
    source = str(job.get("source") or "").casefold()
    final_url = str(result.final_url or "").casefold()

    return (
        source in AGGREGATOR_SOURCES_REQUIRING_IDENTITY
        or "jobleads.com" in final_url
        or "lensa.com" in final_url
    )


def validate_enrichment_identity(
    job: dict[str, Any],
    result: JobDescriptionResult,
) -> JobIdentityValidation:
    if not requires_identity_validation(job, result):
        return JobIdentityValidation(
            accepted=True,
            confidence=1.0,
            title_similarity=None,
            company_similarity=None,
            occupation_match=None,
            reason="identity validation not required for this source",
        )

    if not result.resolved_title and not result.resolved_company:
        return JobIdentityValidation(
            accepted=False,
            confidence=0.0,
            title_similarity=None,
            company_similarity=None,
            occupation_match=None,
            reason="resolved identity metadata unavailable",
        )

    return validate_job_identity(
        original_title=job.get("title"),
        original_company=job.get("company_name"),
        resolved_title=result.resolved_title,
        resolved_company=result.resolved_company,
        original_location=job.get("location"),
        resolved_location=result.resolved_location,
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
    resume_hash: str | None = None,
) -> list[dict[str, Any]]:
    initialize_enrichment_tables()

    with get_connection() as connection:
        register_exact_posting_identity_function(connection)
        cursor = connection.execute(
    """
    WITH jobs AS (
        SELECT
            raw_jobs.*,
            COALESCE(
                NULLIF(job_fingerprint, ''),
                record_key
            ) AS duplicate_fingerprint,
            exact_posting_identity(
                source,
                source_job_id,
                apply_url,
                record_key
            ) AS exact_posting_key,
            exact_posting_identity(
                source,
                source_job_id,
                apply_url,
                record_key
            ) AS posting_status_key
        FROM raw_jobs
    ),

    best_rule_match AS (
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
    ),

    latest_application_status AS (
        SELECT
            status_jobs.posting_status_key,
            status.status,
            ROW_NUMBER() OVER (
                PARTITION BY status_jobs.posting_status_key
                ORDER BY
                    status.updated_at DESC NULLS LAST,
                    status.record_key
            ) AS status_rank
        FROM application_status AS status
        INNER JOIN jobs AS status_jobs
            ON status.record_key = status_jobs.record_key
    ),

    scored_jobs AS (
        SELECT DISTINCT
            score_jobs.exact_posting_key,
            max(scores.overall_score) AS ai_score
        FROM resume_job_scores AS scores
        INNER JOIN jobs AS score_jobs
            ON scores.record_key = score_jobs.record_key
        WHERE scores.description_complete = true
          AND scores.scored_at >= coalesce(
              score_jobs.description_updated_at,
              TIMESTAMPTZ '1970-01-01 00:00:00+00'
          )
        GROUP BY score_jobs.exact_posting_key
    )

    SELECT
        jobs.record_key,
        jobs.exact_posting_key,
        jobs.duplicate_fingerprint,
        jobs.source,
        jobs.title,
        jobs.company_name,
        jobs.location,
        jobs.description,
        jobs.apply_url,
        jobs.email_date,
        jobs.discovered_at,
        jobs.description_updated_at,
        COALESCE(
            latest_application_status.status,
            'new'
        ) AS application_status,
        COALESCE(
            jobs.title_classification,
            CASE
                WHEN regexp_matches(
                    lower(coalesce(jobs.title, '')),
                    ?
                )
                THEN 'FILTERED_OUT'
                ELSE 'POSSIBLE_MATCH'
            END
        ) AS title_classification,
        jobs.title_match_score,
        scored_jobs.ai_score,

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

    FROM jobs AS jobs

    LEFT JOIN job_enrichment_attempts
        AS attempts
        ON jobs.record_key =
           attempts.record_key

    LEFT JOIN best_rule_match AS matches
        ON jobs.record_key =
           matches.record_key
       AND matches.match_rank = 1

    LEFT JOIN latest_application_status
        ON jobs.posting_status_key =
           latest_application_status.posting_status_key
       AND latest_application_status.status_rank = 1

    LEFT JOIN scored_jobs
        ON jobs.exact_posting_key = scored_jobs.exact_posting_key

    WHERE jobs.apply_url IS NOT NULL

    ORDER BY
        jobs.discovered_at DESC,
        jobs.title,
        jobs.company_name
    """,
            [EXCLUDED_TITLE_SQL_REGEX],
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

    for job in jobs:
        current_word_count = count_words(
            job.get("description")
        )
        job["current_word_count"] = current_word_count
        job["raw_description_word_count"] = current_word_count
        job["description_quality_signals"] = (
            description_has_quality_signals(job.get("description"))
        )
        job["description_state"] = description_state(job)

    if resume_hash:
        jobs = apply_admission_gate(
            jobs=jobs,
            resume_hash=resume_hash,
            include_low_priority=True,
            keep_filtered_out=True,
        )

    for job in jobs:
        priority = calculate_enrichment_priority(job)
        job["enrichment_priority_score"] = priority.score
        job["enrichment_priority_tier"] = priority.tier
        job["enrichment_priority_reason"] = priority.reason

    jobs.sort(
        key=lambda job: (
            job["enrichment_priority_score"],
            job.get("discovered_at") or datetime.min.replace(
                tzinfo=timezone.utc
            ),
            job.get("title") or "",
            job.get("company_name") or "",
        ),
        reverse=True,
    )

    selected: list[dict[str, Any]] = []
    selected_exact_keys: set[str] = set()
    selected_duplicate_keys: set[str] = set()

    for job in jobs:
        exact_key = str(
            job.get("exact_posting_key")
            or job.get("record_key")
            or ""
        )

        if exact_key in selected_exact_keys:
            continue

        duplicate_key = str(
            job.get("duplicate_fingerprint")
            or exact_key
        )

        if duplicate_key in selected_duplicate_keys:
            continue

        if (
            source
            and job["source"].casefold()
            != source.casefold()
        ):
            continue

        previous_status = job.get(
            "previous_status"
        )

        if not force:
            if (
                job["current_word_count"] >= minimum_words
                and job["description_state"] == "FULL_JD"
            ):
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
        selected_exact_keys.add(exact_key)
        selected_duplicate_keys.add(duplicate_key)

        if len(selected) >= limit:
            break

    return selected


def save_enrichment_attempt(
    job: dict[str, Any],
    result: JobDescriptionResult,
    status: str | None = None,
    error_message: str | None = None,
    identity_validation: JobIdentityValidation | None = None,
    official_resolution: OfficialJobResolutionResult | None = None,
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
                resolved_candidate_title,
                resolved_candidate_company,
                resolved_candidate_location,
                identity_confidence,
                title_similarity,
                company_similarity,
                identity_validation_reason,
                official_job_url,
                official_url_status,
                official_url_source,
                official_url_resolved_at,
                official_url_confidence,
                official_url_validation_reason,
                official_resolved_title,
                official_resolved_company,
                official_resolved_location,
                attempt_count,
                last_attempted_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
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
                result.resolved_title,
                result.resolved_company,
                result.resolved_location,
                (
                    identity_validation.confidence
                    if identity_validation
                    else None
                ),
                (
                    identity_validation.title_similarity
                    if identity_validation
                    else None
                ),
                (
                    identity_validation.company_similarity
                    if identity_validation
                    else None
                ),
                (
                    identity_validation.reason
                    if identity_validation
                    else None
                ),
                (
                    official_resolution.official_job_url
                    if official_resolution
                    else None
                ),
                (
                    official_resolution.status
                    if official_resolution
                    else None
                ),
                (
                    official_resolution.official_url_source
                    if official_resolution
                    else None
                ),
                (
                    official_resolution.official_url_resolved_at
                    if official_resolution
                    else None
                ),
                (
                    official_resolution.official_url_confidence
                    if official_resolution
                    else None
                ),
                (
                    official_resolution.official_url_validation_reason
                    if official_resolution
                    else None
                ),
                (
                    official_resolution.resolved_title
                    if official_resolution
                    else None
                ),
                (
                    official_resolution.resolved_company
                    if official_resolution
                    else None
                ),
                (
                    official_resolution.resolved_location
                    if official_resolution
                    else None
                ),
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
        register_exact_posting_identity_function(connection)
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

        # description_updated_at makes older AI scores stale without deleting
        # historical scoring rows.

    return True


def process_enrichment_job(
    job: dict[str, Any],
    client,
) -> EnrichmentProcessingResult:
    """Fetch/validate a description, falling back to official pages if needed."""
    source_result = fetch_job_description(
        url=job["apply_url"],
        client=client,
    )

    result = source_result
    updated = False
    stored_status = result.status
    stored_error = result.error_message
    identity_validation: JobIdentityValidation | None = None
    official_resolution: OfficialJobResolutionResult | None = None

    if result.status == "enriched":
        identity_validation = validate_enrichment_identity(
            job=job,
            result=result,
        )

        if not identity_validation.accepted:
            stored_status = "resolution_rejected"
            stored_error = (
                "Resolved candidate rejected: "
                f"{identity_validation.reason}"
            )
        else:
            updated = update_job_description(
                job=job,
                result=result,
            )

            if not updated:
                stored_status = "not_improved"
                stored_error = (
                    "The extracted description was not longer than the "
                    "existing description."
                )

    if should_attempt_official_resolution(
        job=job,
        enrichment_result=source_result,
        identity_validation=identity_validation,
    ):
        official_resolution = resolve_official_job(
            job=job,
            client=client,
        )

        if (
            official_resolution.status == OFFICIAL_FOUND_VERIFIED
            and official_resolution.description_result is not None
        ):
            result = official_resolution.description_result
            identity_validation = official_resolution.identity_validation
            stored_status = result.status
            stored_error = result.error_message
            updated = update_job_description(
                job=job,
                result=result,
            )

            if not updated:
                stored_status = "not_improved"
                stored_error = (
                    "The verified official description was not longer than "
                    "the existing description."
                )

    return EnrichmentProcessingResult(
        result=result,
        stored_status=stored_status,
        stored_error=stored_error,
        identity_validation=identity_validation,
        official_resolution=official_resolution,
        updated=updated,
    )


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
        "resolution_rejected": 0,
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

            processed = process_enrichment_job(
                job=job,
                client=client,
            )

            result = processed.result
            stored_status = processed.stored_status
            stored_error = processed.stored_error
            identity_validation = processed.identity_validation
            updated = processed.updated

            if processed.official_resolution:
                official = processed.official_resolution
                print(
                    "Official resolution: "
                    f"{official.status}; "
                    f"url={official.official_job_url or '<none>'}; "
                    f"reason={official.official_url_validation_reason}"
                )

            if updated:
                updated_count += 1

            save_enrichment_attempt(
                job=job,
                result=result,
                status=stored_status,
                error_message=stored_error,
                identity_validation=identity_validation,
                official_resolution=processed.official_resolution,
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
