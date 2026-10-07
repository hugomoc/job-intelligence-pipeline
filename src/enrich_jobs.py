"""CLI and helpers for enriching incomplete job descriptions.

Ingested email alerts often contain thin descriptions. This module tries to
fetch better public descriptions from the apply URL and records every attempt
so failed/blocked pages can be retried intentionally.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    OFFICIAL_AMBIGUOUS,
    OFFICIAL_BLOCKED,
    OFFICIAL_ERROR,
    OFFICIAL_FOUND_VERIFIED,
    OFFICIAL_NOT_FOUND,
    OfficialJobResolutionResult,
    is_aggregator_job,
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
from src.verified_posting_identity import (
    candidate_duplicate_fingerprint,
    description_hash,
    is_known_official_or_ats_url,
    verified_posting_key_from_url,
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
    "glassdoor",
    "lensa",
    "jobleads",
}

OFFICIAL_RETRY_COOLDOWN_DAYS = 7
OFFICIAL_RETRY_STATUSES = {
    OFFICIAL_AMBIGUOUS,
    OFFICIAL_BLOCKED,
    OFFICIAL_ERROR,
    OFFICIAL_NOT_FOUND,
}
DEFAULT_ENRICHMENT_LIMIT = 25
ENRICHMENT_DIAGNOSTIC_QUEUE_LIMIT = 10_000


@dataclass(frozen=True)
class EnrichmentProcessingResult:
    result: JobDescriptionResult
    stored_status: str
    stored_error: str | None
    identity_validation: JobIdentityValidation | None
    official_resolution: OfficialJobResolutionResult | None
    updated: bool


@dataclass(frozen=True)
class EnrichmentBatchSummary:
    jobs_selected: int
    jobs_processed: int
    descriptions_updated: int
    totals: dict[str, int]
    stopped_for_time_budget: bool
    elapsed_seconds: float
    log_lines: tuple[str, ...]


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
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS verified_posting_key VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS description_hash VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS description_source VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS posting_status VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS matched_prior_record_key VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS matched_prior_source VARCHAR
            """,
            """
            ALTER TABLE job_enrichment_attempts
            ADD COLUMN IF NOT EXISTS reused_description BOOLEAN
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


def as_utc_datetime(
    value: Any,
) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)

    return None


def official_retry_cutoff(
    now: datetime | None = None,
) -> datetime:
    reference = now or datetime.now(timezone.utc)

    return reference - timedelta(days=OFFICIAL_RETRY_COOLDOWN_DAYS)


def official_status_value(
    job: dict[str, Any],
) -> str:
    return str(job.get("official_url_status") or "").strip()


def needs_official_resolution(
    job: dict[str, Any],
    force: bool = False,
    now: datetime | None = None,
) -> bool:
    status = official_status_value(job)

    if status == OFFICIAL_FOUND_VERIFIED:
        return False

    if force:
        return True

    if (
        job.get("description_state") == "FULL_JD"
        and not is_aggregator_job(job)
    ):
        return False

    if not status:
        return True

    if status not in OFFICIAL_RETRY_STATUSES:
        return True

    last_official_attempt = (
        as_utc_datetime(job.get("official_url_resolved_at"))
        or as_utc_datetime(job.get("previous_attempted_at"))
    )

    if last_official_attempt is None:
        return True

    return last_official_attempt <= official_retry_cutoff(now)


def needs_description_enrichment(
    job: dict[str, Any],
    minimum_words: int,
    retry_failed: bool,
    force: bool = False,
) -> bool:
    if force:
        return True

    if (
        job["current_word_count"] >= minimum_words
        and job["description_state"] == "FULL_JD"
    ):
        return False

    previous_status = job.get("previous_status")

    if previous_status:
        return (
            retry_failed
            and previous_status in FAILED_STATUSES
        )

    return True


def load_verified_reuse_candidates(
    minimum_words: int,
) -> list[dict[str, Any]]:
    """Load verified historical postings that can seed safe local reuse."""
    with get_connection() as connection:
        cursor = connection.execute(
            """
            SELECT
                jobs.record_key,
                jobs.source,
                jobs.title,
                jobs.company_name,
                jobs.location,
                jobs.description,
                jobs.description_updated_at,
                attempts.verified_posting_key,
                attempts.description_hash,
                attempts.official_job_url,
                attempts.official_url_status,
                attempts.official_url_source,
                attempts.official_url_confidence,
                attempts.official_url_validation_reason,
                attempts.official_resolved_title,
                attempts.official_resolved_company,
                attempts.official_resolved_location,
                attempts.last_attempted_at,
                attempts.posting_status
            FROM raw_jobs AS jobs
            INNER JOIN job_enrichment_attempts AS attempts
                ON jobs.record_key = attempts.record_key
            WHERE attempts.verified_posting_key IS NOT NULL
              AND TRIM(attempts.verified_posting_key) <> ''
              AND attempts.status = 'enriched'
              AND jobs.description IS NOT NULL
              AND TRIM(jobs.description) <> ''
              AND array_length(
                  regexp_split_to_array(
                      TRIM(jobs.description),
                      '\\s+'
                  )
              ) >= ?
            """,
            [minimum_words],
        )
        columns = [
            description[0]
            for description in cursor.description
        ]
        rows = cursor.fetchall()

    return [
        dict(zip(columns, row))
        for row in rows
    ]


def find_verified_reuse_candidate(
    job: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], JobIdentityValidation] | None:
    """Find a verified historical posting that safely matches this job."""
    job_candidate_key = candidate_duplicate_fingerprint(
        str(job.get("title") or ""),
        str(job.get("company_name") or ""),
        str(job.get("location") or ""),
    )

    if not job_candidate_key.strip("|"):
        return None

    for candidate in candidates:
        if candidate.get("record_key") == job.get("record_key"):
            continue

        candidate_key = candidate_duplicate_fingerprint(
            str(candidate.get("title") or ""),
            str(candidate.get("company_name") or ""),
            str(candidate.get("location") or ""),
        )

        if candidate_key != job_candidate_key:
            continue

        validation = validate_job_identity(
            original_title=str(job.get("title") or ""),
            original_company=str(job.get("company_name") or ""),
            resolved_title=str(
                candidate.get("official_resolved_title")
                or candidate.get("title")
                or ""
            ),
            resolved_company=str(
                candidate.get("official_resolved_company")
                or candidate.get("company_name")
                or ""
            ),
            original_location=str(job.get("location") or ""),
            resolved_location=str(
                candidate.get("official_resolved_location")
                or candidate.get("location")
                or ""
            ),
        )

        if validation.accepted:
            return candidate, validation

    return None


def reuse_verified_description(
    job: dict[str, Any],
    candidate: dict[str, Any],
    validation: JobIdentityValidation,
) -> None:
    """Copy a verified description/official identity onto a source duplicate."""
    description = str(candidate.get("description") or "")
    word_count = count_words(description)
    official_url = str(candidate.get("official_job_url") or "")
    now = datetime.now(timezone.utc)

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE raw_jobs
            SET
                description = ?,
                description_updated_at = CURRENT_TIMESTAMP
            WHERE record_key = ?
            """,
            [
                description,
                job["record_key"],
            ],
        )

    job["description"] = description
    job["description_updated_at"] = now
    job["current_word_count"] = word_count
    job["raw_description_word_count"] = word_count
    job["description_quality_signals"] = description_has_quality_signals(
        description
    )
    job["verified_posting_key"] = candidate.get("verified_posting_key")
    job["description_hash"] = (
        candidate.get("description_hash")
        or description_hash(description)
    )
    job["description_source"] = "verified_reuse"
    job["posting_status"] = candidate.get("posting_status") or "unknown"
    job["matched_prior_record_key"] = candidate.get("record_key")
    job["matched_prior_source"] = candidate.get("source")
    job["reused_description"] = True
    job["official_job_url"] = official_url
    job["official_url_status"] = OFFICIAL_FOUND_VERIFIED
    job["official_url_source"] = candidate.get("official_url_source")
    job["official_url_confidence"] = candidate.get(
        "official_url_confidence"
    )
    job["official_url_validation_reason"] = (
        "reused verified posting identity from historical duplicate"
    )
    job["official_resolved_title"] = (
        candidate.get("official_resolved_title")
        or candidate.get("title")
    )
    job["official_resolved_company"] = (
        candidate.get("official_resolved_company")
        or candidate.get("company_name")
    )
    job["official_resolved_location"] = (
        candidate.get("official_resolved_location")
        or candidate.get("location")
    )

    result = JobDescriptionResult(
        requested_url=str(job.get("apply_url") or ""),
        final_url=official_url or str(candidate.get("final_url") or ""),
        status="enriched",
        http_status=None,
        extraction_method="verified_reuse",
        description=description,
        word_count=word_count,
        error_message=None,
        resolved_title=str(job["official_resolved_title"] or ""),
        resolved_company=str(job["official_resolved_company"] or ""),
        resolved_location=str(job["official_resolved_location"] or ""),
    )
    official_resolution = OfficialJobResolutionResult(
        status=OFFICIAL_FOUND_VERIFIED,
        official_job_url=official_url,
        official_url_source=str(candidate.get("official_url_source") or ""),
        official_url_resolved_at=now,
        official_url_confidence=float(validation.confidence),
        official_url_validation_reason=validation.reason,
        resolved_title=result.resolved_title,
        resolved_company=result.resolved_company,
        resolved_location=result.resolved_location,
        description_result=result,
        identity_validation=validation,
    )

    save_enrichment_attempt(
        job=job,
        result=result,
        status="enriched",
        error_message=None,
        identity_validation=validation,
        official_resolution=official_resolution,
    )


def reuse_verified_descriptions_before_queue(
    jobs: list[dict[str, Any]],
    minimum_words: int,
) -> None:
    """Apply verified local reuse before external enrichment is ranked."""
    candidates = load_verified_reuse_candidates(
        minimum_words=minimum_words,
    )

    if not candidates:
        return

    for job in jobs:
        if int(job.get("current_word_count") or 0) >= minimum_words:
            continue

        if job.get("verified_posting_key"):
            continue

        match = find_verified_reuse_candidate(
            job=job,
            candidates=candidates,
        )

        if not match:
            continue

        candidate, validation = match
        reuse_verified_description(
            job=job,
            candidate=candidate,
            validation=validation,
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
        attempts.error_message
            AS previous_error_message,
        attempts.attempt_count,
        attempts.last_attempted_at
            AS previous_attempted_at,
        attempts.official_job_url,
        attempts.official_url_status,
        attempts.official_url_resolved_at,
        attempts.official_url_source,
        attempts.official_url_confidence,
        attempts.official_url_validation_reason,
        attempts.official_resolved_title,
        attempts.official_resolved_company,
        attempts.official_resolved_location,
        attempts.verified_posting_key,
        attempts.description_hash,
        attempts.description_source,
        attempts.posting_status,
        attempts.matched_prior_record_key,
        attempts.matched_prior_source,
        attempts.reused_description,

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

    reuse_verified_descriptions_before_queue(
        jobs=jobs,
        minimum_words=minimum_words,
    )

    for job in jobs:
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
        needs_description = needs_description_enrichment(
            job=job,
            minimum_words=minimum_words,
            retry_failed=retry_failed,
            force=force,
        )
        needs_official = needs_official_resolution(
            job=job,
            force=force,
        )
        job["needs_description_enrichment"] = needs_description
        job["needs_official_resolution"] = needs_official

        if not force:
            if not needs_description and not needs_official:
                continue

        job["enrichment_queue_rank"] = len(selected) + 1
        selected.append(job)
        selected_exact_keys.add(exact_key)
        selected_duplicate_keys.add(duplicate_key)

        if len(selected) >= limit:
            break

    return selected


def default_enrichment_totals() -> dict[str, int]:
    return {
        "enriched": 0,
        "blocked": 0,
        "no_description": 0,
        "fetch_error": 0,
        "invalid_url": 0,
        "not_improved": 0,
        "resolution_rejected": 0,
    }


def summarize_enrichment_queue(
    jobs: list[dict[str, Any]],
) -> dict[str, int]:
    """Summarize the active enrichment batch without mutating job state."""
    return {
        "eligible_for_enrichment": len(jobs),
        "never_attempted": sum(
            1
            for job in jobs
            if int(job.get("attempt_count") or 0) == 0
        ),
        "previously_attempted": sum(
            1
            for job in jobs
            if int(job.get("attempt_count") or 0) > 0
        ),
        "needs_description": sum(
            1
            for job in jobs
            if job.get("needs_description_enrichment")
        ),
        "needs_official_resolution": sum(
            1
            for job in jobs
            if job.get("needs_official_resolution")
        ),
    }


def failed_processing_result(
    job: dict[str, Any],
    error: Exception,
) -> EnrichmentProcessingResult:
    error_message = f"{type(error).__name__}: {error}"

    return EnrichmentProcessingResult(
        result=JobDescriptionResult(
            requested_url=str(job.get("apply_url") or ""),
            final_url=None,
            status="fetch_error",
            http_status=None,
            extraction_method=None,
            description="",
            word_count=0,
            error_message=error_message,
        ),
        stored_status="fetch_error",
        stored_error=error_message,
        identity_validation=None,
        official_resolution=None,
        updated=False,
    )


def process_enrichment_batch(
    jobs: list[dict[str, Any]],
    client,
    delay_seconds: float = 0.0,
    max_seconds: float | None = None,
    emit=None,
) -> EnrichmentBatchSummary:
    """Process a bounded batch and keep going after per-job failures."""
    totals = default_enrichment_totals()
    updated_count = 0
    processed_count = 0
    stopped_for_time_budget = False
    started_at = time.monotonic()
    log_lines: list[str] = []

    def log(message: str) -> None:
        log_lines.append(message)

        if emit:
            emit(message)

    for index, job in enumerate(jobs, start=1):
        if (
            max_seconds is not None
            and time.monotonic() - started_at >= max_seconds
        ):
            stopped_for_time_budget = True
            log(
                "Stopping enrichment because the max-seconds "
                f"budget was reached after {processed_count} jobs."
            )
            break

        log(
            "Enriching "
            f"{index}/{len(jobs)}: "
            f"{job.get('title') or 'Untitled job'} | "
            f"{job.get('company_name') or 'Unknown company'}"
        )

        try:
            processed = process_enrichment_job(
                job=job,
                client=client,
            )
        except Exception as error:
            processed = failed_processing_result(
                job=job,
                error=error,
            )

        result = processed.result
        stored_status = processed.stored_status
        stored_error = processed.stored_error
        identity_validation = processed.identity_validation

        if processed.official_resolution:
            official = processed.official_resolution
            log(
                "Official resolution: "
                f"{official.status}; "
                f"url={official.official_job_url or '<none>'}; "
                f"reason={official.official_url_validation_reason}"
            )

        if processed.updated:
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
            totals.get(stored_status, 0) + 1
        )
        processed_count += 1

        log(
            f"Status: {stored_status}; "
            f"words: {result.word_count}; "
            f"updated: {'yes' if processed.updated else 'no'}"
        )

        if (
            index < len(jobs)
            and delay_seconds > 0
        ):
            time.sleep(delay_seconds)

    return EnrichmentBatchSummary(
        jobs_selected=len(jobs),
        jobs_processed=processed_count,
        descriptions_updated=updated_count,
        totals=totals,
        stopped_for_time_budget=stopped_for_time_budget,
        elapsed_seconds=time.monotonic() - started_at,
        log_lines=tuple(log_lines),
    )


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

    verified_url = ""

    if (
        official_resolution
        and official_resolution.status == OFFICIAL_FOUND_VERIFIED
        and official_resolution.official_job_url
    ):
        verified_url = official_resolution.official_job_url
    elif (
        final_status == "enriched"
        and result.final_url
        and is_known_official_or_ats_url(result.final_url)
        and (
            identity_validation is None
            or identity_validation.accepted
        )
    ):
        verified_url = result.final_url

    verified_posting_key = (
        job.get("verified_posting_key")
        or verified_posting_key_from_url(verified_url)
        or None
    )
    stored_description_hash = (
        job.get("description_hash")
        or description_hash(result.description)
        or None
    )
    description_source = (
        job.get("description_source")
        or (
            official_resolution.official_url_source
            if official_resolution
            else None
        )
        or result.extraction_method
    )
    posting_status = job.get("posting_status") or "unknown"
    reused_description = bool(job.get("reused_description"))

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
                verified_posting_key,
                description_hash,
                description_source,
                posting_status,
                matched_prior_record_key,
                matched_prior_source,
                reused_description,
                attempt_count,
                last_attempted_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
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
                verified_posting_key,
                stored_description_hash,
                description_source,
                posting_status,
                job.get("matched_prior_record_key"),
                job.get("matched_prior_source"),
                reused_description,
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


def existing_description_result(
    job: dict[str, Any],
) -> JobDescriptionResult:
    return JobDescriptionResult(
        requested_url=job["apply_url"],
        final_url=None,
        status=(
            str(job.get("previous_status") or "").strip()
            or "official_resolution_only"
        ),
        http_status=None,
        extraction_method=None,
        description=str(job.get("description") or ""),
        word_count=int(job.get("current_word_count") or 0),
        error_message=(
            job.get("previous_error_message")
            or "Reused existing description while resolving official URL."
        ),
    )


def process_enrichment_job(
    job: dict[str, Any],
    client,
) -> EnrichmentProcessingResult:
    """Fetch/validate a description, falling back to official pages if needed."""
    needs_description = bool(
        job.get(
            "needs_description_enrichment",
            True,
        )
    )
    if needs_description:
        source_result = fetch_job_description(
            url=job["apply_url"],
            client=client,
        )
    else:
        source_result = existing_description_result(job)

    result = source_result
    updated = False
    stored_status = result.status
    stored_error = result.error_message
    identity_validation: JobIdentityValidation | None = None
    official_resolution: OfficialJobResolutionResult | None = None

    if needs_description and result.status == "enriched":
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

    if (
        should_attempt_official_resolution(
            job=job,
            enrichment_result=source_result,
            identity_validation=identity_validation,
        )
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
        default=DEFAULT_ENRICHMENT_LIMIT,
        help=(
            "Maximum jobs to process. "
            f"Default: {DEFAULT_ENRICHMENT_LIMIT}."
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

    parser.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help=(
            "Optional wall-clock processing budget. "
            "The batch stops before starting another job when reached."
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

    if (
        arguments.max_seconds is not None
        and arguments.max_seconds <= 0
    ):
        print("--max-seconds must be greater than 0.")
        raise SystemExit(1)

    queue_jobs = load_jobs_to_enrich(
        limit=max(
            arguments.limit,
            ENRICHMENT_DIAGNOSTIC_QUEUE_LIMIT,
        ),
        minimum_words=arguments.min_words,
        source=arguments.source,
        retry_failed=arguments.retry_failed,
        force=arguments.force,
    )
    jobs = queue_jobs[:arguments.limit]

    if not jobs:
        print(
            "No jobs currently require "
            "description enrichment."
        )
        return

    queue_summary = summarize_enrichment_queue(queue_jobs)

    print(
        f"Jobs selected for enrichment: "
        f"{len(jobs)}"
    )
    print(
        "Queue diagnostics: "
        f"eligible={queue_summary['eligible_for_enrichment']}; "
        f"never_attempted={queue_summary['never_attempted']}; "
        f"previously_attempted={queue_summary['previously_attempted']}; "
        f"needs_description={queue_summary['needs_description']}; "
        "needs_official_resolution="
        f"{queue_summary['needs_official_resolution']}"
    )

    with create_http_client() as client:
        batch_summary = process_enrichment_batch(
            jobs,
            client=client,
            delay_seconds=arguments.delay,
            max_seconds=arguments.max_seconds,
            emit=print,
        )

    print("\nEnrichment complete")
    print(
        f"Processed this run: "
        f"{batch_summary.jobs_processed}"
    )
    print(
        f"Descriptions updated: "
        f"{batch_summary.descriptions_updated}"
    )
    print(
        f"Elapsed seconds: "
        f"{batch_summary.elapsed_seconds:.1f}"
    )

    if batch_summary.stopped_for_time_budget:
        print("Stopped because max-seconds was reached.")

    for status, count in batch_summary.totals.items():
        if count:
            print(
                f"{status}: {count}"
            )

    if batch_summary.descriptions_updated:
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
