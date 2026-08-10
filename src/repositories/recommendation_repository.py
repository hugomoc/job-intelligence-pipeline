from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from src.ai.resume_matcher import MATCHER_PROMPT_VERSION
from src.database import get_connection, initialize_database


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []

    if not isinstance(parsed, list):
        return []

    return [str(item) for item in parsed]


def parse_email_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def load_all_jobs(
    resume_hash: str | None = None,
) -> list[dict[str, Any]]:
    initialize_database()

    with get_connection() as connection:
        cursor = connection.execute(
            """
            WITH jobs AS (
                SELECT
                    *,
                    COALESCE(
                        NULLIF(job_fingerprint, ''),
                        record_key
                    ) AS canonical_job_key
                FROM raw_jobs
            ),

            best_matches AS (
                SELECT
                    record_key,
                    search_title,
                    match_score,
                    row_number() over (
                        partition by record_key
                        order by
                            is_recommended desc,
                            needs_review desc,
                            match_score desc,
                            title_score desc,
                            search_id
                    ) as match_rank
                FROM job_matches
            ),

            latest_application_status AS (
                SELECT
                    status_jobs.canonical_job_key,
                    status.status,
                    row_number() over (
                        partition by status_jobs.canonical_job_key
                        order by
                            status.updated_at desc nulls last,
                            status.record_key
                    ) as status_rank
                FROM application_status as status
                INNER JOIN jobs as status_jobs
                    ON status.record_key = status_jobs.record_key
            )

            SELECT
                jobs.record_key,
                jobs.canonical_job_key,
                jobs.title,
                jobs.company_name,
                jobs.location,
                jobs.salary_text,
                jobs.source,
                jobs.apply_url,
                jobs.posted_age_text,
                jobs.email_date,
                jobs.discovered_at,
                matches.search_title as best_search_title,
                matches.match_score as rule_score,
                coalesce(
                    canonical_status.status,
                    'new'
                ) as application_status,
                recommendations.ai_score,
                recommendations.recommendation,
                recommendations.confidence,
                recommendations.title_fit,
                recommendations.skills_fit,
                recommendations.experience_fit,
                recommendations.seniority_fit,
                recommendations.industry_fit,
                recommendations.location_fit,
                recommendations.matching_strengths,
                recommendations.hard_requirements_missing,
                recommendations.preferred_qualifications_missing,
                recommendations.risk_factors,
                recommendations.summary,
                recommendations.description_word_count,
                recommendations.has_incomplete_description,
                recommendations.ai_scored_at
            FROM jobs
            LEFT JOIN best_matches as matches
                ON jobs.record_key = matches.record_key
               AND matches.match_rank = 1
            LEFT JOIN latest_application_status
                as canonical_status
                ON jobs.canonical_job_key =
                    canonical_status.canonical_job_key
               AND canonical_status.status_rank = 1
            LEFT JOIN analytics.mart_job_recommendations as recommendations
                ON jobs.canonical_job_key =
                   recommendations.canonical_job_key
               AND recommendations.resume_hash = ?
               AND recommendations.ai_prompt_version = ?
            ORDER BY
                jobs.discovered_at desc nulls last,
                jobs.title,
                jobs.company_name
            """,
            [
                resume_hash or "",
                MATCHER_PROMPT_VERSION,
            ],
        )

        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()

    jobs = [dict(zip(columns, row)) for row in rows]

    for job in jobs:
        email_datetime = parse_email_datetime(
            job.get("email_date")
        )

        if email_datetime is None:
            email_datetime = job.get("discovered_at")

            if (
                isinstance(email_datetime, datetime)
                and email_datetime.tzinfo is None
            ):
                email_datetime = email_datetime.replace(
                    tzinfo=timezone.utc
                )

        if isinstance(email_datetime, datetime):
            job["sent_at"] = email_datetime
        else:
            job["sent_at"] = None

        job["matching_strengths"] = parse_json_list(
            job.get("matching_strengths")
        )
        job["hard_requirements_missing"] = parse_json_list(
            job.get("hard_requirements_missing")
        )
        job["preferred_qualifications_missing"] = parse_json_list(
            job.get("preferred_qualifications_missing")
        )
        job["risk_factors"] = parse_json_list(
            job.get("risk_factors")
        )

    return sorted(
        jobs,
        key=lambda job: (
            {
                "new": 2,
                "applied": 1,
                "removed": 0,
            }.get(
                job.get("application_status", "new"),
                2,
            ),
            job.get("ai_score") is not None,
            job.get("ai_score") or -1,
            job.get("sent_at") or datetime.min.replace(
                tzinfo=timezone.utc
            ),
            job.get("title") or "",
            job.get("company_name") or "",
        ),
        reverse=True,
    )


def update_application_status(
    record_key: str,
    status: str,
) -> None:
    if status not in {"new", "applied", "removed"}:
        raise ValueError(
            "Application status must be new, applied or removed."
        )

    initialize_database()

    updated_at = datetime.now(timezone.utc)

    with get_connection() as connection:
        canonical_row = connection.execute(
            """
            SELECT COALESCE(
                NULLIF(job_fingerprint, ''),
                record_key
            ) AS canonical_job_key
            FROM raw_jobs
            WHERE record_key = ?
            """,
            [record_key],
        ).fetchone()

        if not canonical_row:
            return

        related_rows = connection.execute(
            """
            SELECT record_key
            FROM raw_jobs
            WHERE COALESCE(
                NULLIF(job_fingerprint, ''),
                record_key
            ) = ?
            """,
            [canonical_row[0]],
        ).fetchall()

        connection.executemany(
            """
            INSERT INTO application_status (
                record_key,
                status,
                updated_at
            )
            VALUES (?, ?, ?)
            ON CONFLICT (record_key) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            [
                [
                    related_row[0],
                    status,
                    updated_at,
                ]
                for related_row in related_rows
            ],
        )


def load_candidate_jobs(
    resume_hash: str,
    model_name: str | None,
    limit: int,
    minimum_rule_score: int,
    prompt_version: str = MATCHER_PROMPT_VERSION,
    reuse_any_model: bool = False,
) -> list[dict[str, Any]]:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            WITH jobs AS (
                SELECT
                    *,
                    COALESCE(
                        NULLIF(job_fingerprint, ''),
                        record_key
                    ) AS canonical_job_key
                FROM raw_jobs
            ),

            existing_scores AS (
                SELECT DISTINCT
                    scored_jobs.canonical_job_key
                FROM resume_job_scores AS scores
                INNER JOIN jobs AS scored_jobs
                    ON scores.record_key =
                       scored_jobs.record_key
                WHERE scores.resume_hash = ?
                  AND (
                      ? = true
                      OR scores.model_name = ?
                  )
                  AND coalesce(scores.prompt_version, 'v1') = ?
            ),

            latest_application_status AS (
                SELECT
                    status_jobs.canonical_job_key,
                    status.status,
                    ROW_NUMBER() OVER (
                        PARTITION BY status_jobs.canonical_job_key
                        ORDER BY
                            status.updated_at DESC NULLS LAST,
                            status.record_key
                    ) AS status_rank
                FROM application_status AS status
                INNER JOIN jobs AS status_jobs
                    ON status.record_key =
                       status_jobs.record_key
            ),

            ranked_candidates AS (
                SELECT
                    jobs.canonical_job_key,
                    jobs.record_key,
                    jobs.title,
                    jobs.company_name,
                    jobs.location,
                    jobs.salary_text,
                    jobs.description,
                    jobs.source,
                    jobs.apply_url,
                    matches.match_score AS rule_score,
                    matches.search_id,
                    matches.search_title,
                    matches.is_recommended,
                    matches.needs_review,
                    ROW_NUMBER() OVER (
                        PARTITION BY jobs.canonical_job_key
                        ORDER BY
                            matches.is_recommended DESC,
                            matches.needs_review DESC,
                            matches.match_score DESC,
                            matches.title_score DESC,
                            CASE
                                WHEN jobs.description IS NOT NULL
                                 AND TRIM(jobs.description) <> ''
                                THEN 1
                                ELSE 0
                            END DESC,
                            jobs.description_updated_at DESC NULLS LAST,
                            jobs.discovered_at DESC NULLS LAST,
                            jobs.record_key
                    ) AS candidate_rank
                FROM jobs
                INNER JOIN job_matches AS matches
                    ON jobs.record_key = matches.record_key
                LEFT JOIN existing_scores
                    ON jobs.canonical_job_key =
                       existing_scores.canonical_job_key
                LEFT JOIN latest_application_status
                    ON jobs.canonical_job_key =
                       latest_application_status.canonical_job_key
                   AND latest_application_status.status_rank = 1
                WHERE existing_scores.canonical_job_key IS NULL
                  AND matches.match_score >= ?
                  AND jobs.description IS NOT NULL
                  AND TRIM(jobs.description) <> ''
                  AND array_length(
                      regexp_split_to_array(
                          trim(jobs.description),
                          '\\s+'
                      )
                  ) >= 80
                  AND COALESCE(
                      latest_application_status.status,
                      'new'
                  ) = 'new'
            )

            SELECT
                canonical_job_key,
                record_key,
                title,
                company_name,
                location,
                salary_text,
                description,
                source,
                apply_url,
                rule_score,
                search_id,
                search_title
            FROM ranked_candidates
            WHERE candidate_rank = 1
            ORDER BY
                rule_score DESC,
                title,
                company_name
            LIMIT ?
            """,
            [
                resume_hash,
                reuse_any_model,
                model_name,
                prompt_version,
                minimum_rule_score,
                limit,
            ],
        )

        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()

    return [dict(zip(columns, row)) for row in rows]


def count_cached_canonical_scores(
    resume_hash: str,
    model_name: str | None,
    prompt_version: str = MATCHER_PROMPT_VERSION,
    reuse_any_model: bool = False,
) -> int:
    with get_connection() as connection:
        result = connection.execute(
            """
            WITH jobs AS (
                SELECT
                    record_key,
                    COALESCE(
                        NULLIF(job_fingerprint, ''),
                        record_key
                    ) AS canonical_job_key
                FROM raw_jobs
            )

            SELECT COUNT(DISTINCT jobs.canonical_job_key)
            FROM resume_job_scores AS scores
            INNER JOIN jobs
                ON scores.record_key = jobs.record_key
            WHERE scores.resume_hash = ?
              AND (
                  ? = true
                  OR scores.model_name = ?
              )
              AND coalesce(scores.prompt_version, 'v1') = ?
            """,
            [
                resume_hash,
                reuse_any_model,
                model_name,
                prompt_version,
            ],
        ).fetchone()

    return int(result[0]) if result else 0


def count_unscored_candidate_jobs(
    resume_hash: str,
    model_name: str | None,
    minimum_rule_score: int,
    prompt_version: str = MATCHER_PROMPT_VERSION,
    reuse_any_model: bool = False,
) -> int:
    with get_connection() as connection:
        result = connection.execute(
            """
            WITH jobs AS (
                SELECT
                    *,
                    COALESCE(
                        NULLIF(job_fingerprint, ''),
                        record_key
                    ) AS canonical_job_key
                FROM raw_jobs
            ),

            existing_scores AS (
                SELECT DISTINCT
                    scored_jobs.canonical_job_key
                FROM resume_job_scores AS scores
                INNER JOIN jobs AS scored_jobs
                    ON scores.record_key =
                       scored_jobs.record_key
                WHERE scores.resume_hash = ?
                  AND (
                      ? = true
                      OR scores.model_name = ?
                  )
                  AND coalesce(scores.prompt_version, 'v1') = ?
            ),

            latest_application_status AS (
                SELECT
                    status_jobs.canonical_job_key,
                    status.status,
                    ROW_NUMBER() OVER (
                        PARTITION BY status_jobs.canonical_job_key
                        ORDER BY
                            status.updated_at DESC NULLS LAST,
                            status.record_key
                    ) AS status_rank
                FROM application_status AS status
                INNER JOIN jobs AS status_jobs
                    ON status.record_key =
                       status_jobs.record_key
            ),

            ranked_candidates AS (
                SELECT
                    jobs.canonical_job_key,
                    ROW_NUMBER() OVER (
                        PARTITION BY jobs.canonical_job_key
                        ORDER BY
                            matches.is_recommended DESC,
                            matches.needs_review DESC,
                            matches.match_score DESC,
                            matches.title_score DESC,
                            CASE
                                WHEN jobs.description IS NOT NULL
                                 AND TRIM(jobs.description) <> ''
                                THEN 1
                                ELSE 0
                            END DESC,
                            jobs.description_updated_at DESC NULLS LAST,
                            jobs.discovered_at DESC NULLS LAST,
                            jobs.record_key
                    ) AS candidate_rank
                FROM jobs
                INNER JOIN job_matches AS matches
                    ON jobs.record_key = matches.record_key
                LEFT JOIN existing_scores
                    ON jobs.canonical_job_key =
                       existing_scores.canonical_job_key
                LEFT JOIN latest_application_status
                    ON jobs.canonical_job_key =
                       latest_application_status.canonical_job_key
                   AND latest_application_status.status_rank = 1
                WHERE existing_scores.canonical_job_key IS NULL
                  AND matches.match_score >= ?
                  AND jobs.description IS NOT NULL
                  AND TRIM(jobs.description) <> ''
                  AND array_length(
                      regexp_split_to_array(
                          trim(jobs.description),
                          '\\s+'
                      )
                  ) >= 80
                  AND COALESCE(
                      latest_application_status.status,
                      'new'
                  ) = 'new'
            )

            SELECT COUNT(*)
            FROM ranked_candidates
            WHERE candidate_rank = 1
            """,
            [
                resume_hash,
                reuse_any_model,
                model_name,
                prompt_version,
                minimum_rule_score,
            ],
        ).fetchone()

    return int(result[0]) if result else 0


def load_recommendations(
    resume_hash: str,
) -> list[dict[str, Any]]:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            SELECT
                resume_hash,
                canonical_job_key,
                canonical_record_key,
                scored_record_key,
                job_title,
                company_name,
                location,
                salary_text,
                source,
                apply_url,
                posted_age_text,
                discovered_at,
                rule_score,
                best_search_title,
                ai_score,
                recommendation,
                confidence,
                title_fit,
                skills_fit,
                experience_fit,
                seniority_fit,
                industry_fit,
                location_fit,
                matching_strengths,
                hard_requirements_missing,
                preferred_qualifications_missing,
                risk_factors,
                summary,
                description_word_count,
                has_incomplete_description,
                ai_model_name,
                ai_prompt_version,
                ai_scored_at,
                ai_score_rank
            FROM analytics.mart_job_recommendations
            WHERE resume_hash = ?
              AND ai_prompt_version = ?
            ORDER BY
                ai_score DESC,
                ai_score_rank
            """,
            [
                resume_hash,
                MATCHER_PROMPT_VERSION,
            ],
        )

        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()

    recommendations = [dict(zip(columns, row)) for row in rows]

    for recommendation in recommendations:
        recommendation["matching_strengths"] = parse_json_list(
            recommendation.get("matching_strengths")
        )
        recommendation["hard_requirements_missing"] = parse_json_list(
            recommendation.get("hard_requirements_missing")
        )
        recommendation["preferred_qualifications_missing"] = parse_json_list(
            recommendation.get("preferred_qualifications_missing")
        )
        recommendation["risk_factors"] = parse_json_list(
            recommendation.get("risk_factors")
        )

    return recommendations
