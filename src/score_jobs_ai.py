"""Full resume-to-job AI scoring workflow.

This module owns durable resume profiles and detailed AI fit scores. Streamlit
and CLI workflows reuse these helpers so scores are cached by resume/job instead
of recomputed on every click.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.ai.resume_matcher import (
    MATCHER_PROMPT_VERSION,
    ResumeJobMatch,
    ResumeMatcherError,
    score_resume_against_job,
)
from src.ai.resume_profiler import (
    ResumeProfile,
    ResumeProfilerError,
    get_model_name,
    profile_resume,
)
from src.database import (
    get_connection,
    initialize_database,
)
from src.resume.extractor import (
    ResumeExtractionError,
    extract_resume_file,
)


def initialize_ai_tables() -> None:
    """Create cached resume-profile and job-score tables."""
    initialize_database()

    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS resume_profiles (
                resume_hash VARCHAR PRIMARY KEY,
                filename VARCHAR NOT NULL,
                model_name VARCHAR NOT NULL,
                profile_json VARCHAR NOT NULL,
                created_at TIMESTAMPTZ
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS resume_job_scores (
                resume_hash VARCHAR NOT NULL,
                record_key VARCHAR NOT NULL,

                overall_score INTEGER NOT NULL,
                recommendation VARCHAR NOT NULL,

                title_fit INTEGER NOT NULL,
                skills_fit INTEGER NOT NULL,
                experience_fit INTEGER NOT NULL,
                seniority_fit INTEGER NOT NULL,
                industry_fit INTEGER NOT NULL,
                location_fit INTEGER NOT NULL,

                confidence VARCHAR NOT NULL,

                matching_strengths VARCHAR,
                hard_requirements_missing VARCHAR,
                preferred_qualifications_missing VARCHAR,
                risk_factors VARCHAR,
                summary VARCHAR,

                description_word_count INTEGER NOT NULL,
                description_complete BOOLEAN NOT NULL,

                model_name VARCHAR NOT NULL,
                prompt_version VARCHAR NOT NULL
                    DEFAULT 'v1',
                scored_at TIMESTAMPTZ
                    DEFAULT CURRENT_TIMESTAMP,

                PRIMARY KEY (
                    resume_hash,
                    record_key
                )
            )
            """
        )

        connection.execute(
            """
            ALTER TABLE resume_job_scores
            ADD COLUMN IF NOT EXISTS
                prompt_version VARCHAR
                DEFAULT 'v1'
            """
        )


def load_cached_profile(
    resume_hash: str,
    model_name: str,
) -> ResumeProfile | None:
    initialize_ai_tables()

    with get_connection() as connection:
        result = connection.execute(
            """
            SELECT profile_json
            FROM resume_profiles
            WHERE resume_hash = ?
              AND model_name = ?
            """,
            [
                resume_hash,
                model_name,
            ],
        ).fetchone()

    if not result:
        return None

    return ResumeProfile.model_validate_json(
        result[0]
    )


def save_resume_profile(
    resume_hash: str,
    filename: str,
    model_name: str,
    profile: ResumeProfile,
) -> None:
    initialize_ai_tables()

    with get_connection() as connection:
        connection.execute(
            """
            DELETE FROM resume_profiles
            WHERE resume_hash = ?
            """,
            [resume_hash],
        )

        connection.execute(
            """
            INSERT INTO resume_profiles (
                resume_hash,
                filename,
                model_name,
                profile_json
            )
            VALUES (?, ?, ?, ?)
            """,
            [
                resume_hash,
                filename,
                model_name,
                profile.model_dump_json(),
            ],
        )


def load_candidate_jobs(
    limit: int,
    minimum_rule_score: int,
) -> list[dict[str, Any]]:
    """
    Uses the existing rule-based matcher only to decide
    which jobs should receive the more expensive AI review.
    """
    with get_connection() as connection:
        cursor = connection.execute(
            """
            WITH ranked_rule_matches AS (
                SELECT
                    jobs.record_key,
                    jobs.title,
                    jobs.company_name,
                    jobs.location,
                    jobs.salary_text,
                    jobs.description,
                    jobs.source,
                    jobs.apply_url,

                    matches.match_score
                        AS rule_score,
                    matches.search_id,
                    matches.is_recommended,
                    matches.needs_review,

                    ROW_NUMBER() OVER (
                        PARTITION BY jobs.record_key
                        ORDER BY
                            matches.is_recommended DESC,
                            matches.needs_review DESC,
                            matches.match_score DESC
                    ) AS rule_rank

                FROM raw_jobs AS jobs

                INNER JOIN job_matches AS matches
                    ON jobs.record_key =
                       matches.record_key
            )

            SELECT
                record_key,
                title,
                company_name,
                location,
                salary_text,
                description,
                source,
                apply_url,
                rule_score,
                search_id
            FROM ranked_rule_matches
            WHERE rule_rank = 1
              AND rule_score >= ?
            ORDER BY
                rule_score DESC,
                title,
                company_name
            LIMIT ?
            """,
            [
                minimum_rule_score,
                limit,
            ],
        )

        columns = [
            description[0]
            for description
            in cursor.description
        ]

        rows = cursor.fetchall()

    return [
        dict(zip(columns, row))
        for row in rows
    ]


def load_cached_job_score(
    resume_hash: str,
    record_key: str,
    model_name: str,
    prompt_version: str = MATCHER_PROMPT_VERSION,
) -> dict[str, Any] | None:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            SELECT
                overall_score,
                recommendation,
                confidence,
                matching_strengths,
                hard_requirements_missing,
                preferred_qualifications_missing,
                risk_factors,
                summary,
                description_word_count,
                description_complete
            FROM resume_job_scores
            WHERE resume_hash = ?
              AND record_key = ?
              AND model_name = ?
              AND coalesce(prompt_version, 'v1') = ?
            """,
            [
                resume_hash,
                record_key,
                model_name,
                prompt_version,
            ],
        )

        row = cursor.fetchone()

        if not row:
            return None

        columns = [
            description[0]
            for description
            in cursor.description
        ]

    return dict(zip(columns, row))


def save_job_score(
    resume_hash: str,
    match: ResumeJobMatch,
) -> None:
    """Replace one resume/job score so the latest model output is canonical."""
    analysis = match.analysis

    values = [
        resume_hash,
        match.record_key,
        match.overall_score,
        match.recommendation,
        analysis.title_fit,
        analysis.skills_fit,
        analysis.experience_fit,
        analysis.seniority_fit,
        analysis.industry_fit,
        analysis.location_fit,
        analysis.confidence,
        json.dumps(
            analysis.matching_strengths,
            ensure_ascii=False,
        ),
        json.dumps(
            analysis.hard_requirements_missing,
            ensure_ascii=False,
        ),
        json.dumps(
            analysis
            .preferred_qualifications_missing,
            ensure_ascii=False,
        ),
        json.dumps(
            analysis.risk_factors,
            ensure_ascii=False,
        ),
        analysis.summary,
        match.description_word_count,
        match.description_complete,
        match.model_name,
        match.prompt_version,
    ]

    with get_connection() as connection:
        connection.execute(
            """
            DELETE FROM resume_job_scores
            WHERE resume_hash = ?
              AND record_key = ?
            """,
            [
                resume_hash,
                match.record_key,
            ],
        )

        connection.execute(
            """
            INSERT INTO resume_job_scores (
                resume_hash,
                record_key,
                overall_score,
                recommendation,
                title_fit,
                skills_fit,
                experience_fit,
                seniority_fit,
                industry_fit,
                location_fit,
                confidence,
                matching_strengths,
                hard_requirements_missing,
                preferred_qualifications_missing,
                risk_factors,
                summary,
                description_word_count,
                description_complete,
                model_name,
                prompt_version
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            values,
        )


def parse_json_list(
    value: str | None,
) -> list[str]:
    if not value:
        return []

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []

    if not isinstance(parsed, list):
        return []

    return [
        str(item)
        for item in parsed
    ]


def print_match(
    job: dict[str, Any],
    match: ResumeJobMatch,
    cached: bool = False,
) -> None:
    cache_label = (
        " — cached"
        if cached
        else ""
    )

    print(
        f"\n[{match.recommendation.upper()}] "
        f"{match.overall_score}%{cache_label}"
    )

    print(
        f"{job['title']} | "
        f"{job['company_name']}"
    )

    print(
        "Location: "
        f"{job.get('location') or 'Not provided'}"
    )

    print(
        "Confidence: "
        f"{match.analysis.confidence}"
    )

    print(
        "Description: "
        f"{match.description_word_count} words"
    )

    print(
        "Summary: "
        f"{match.analysis.summary}"
    )

    if match.analysis.matching_strengths:
        print("Strengths:")

        for strength in (
            match.analysis.matching_strengths
        ):
            print(f"- {strength}")

    if match.analysis.hard_requirements_missing:
        print("Missing required qualifications:")

        for requirement in (
            match.analysis
            .hard_requirements_missing
        ):
            print(f"- {requirement}")

    if match.analysis.risk_factors:
        print("Risks:")

        for risk in match.analysis.risk_factors:
            print(f"- {risk}")

    print(f"Apply: {job['apply_url']}")


def print_cached_match(
    job: dict[str, Any],
    cached_score: dict[str, Any],
) -> None:
    print(
        f"\n[{cached_score['recommendation'].upper()}] "
        f"{cached_score['overall_score']}% — cached"
    )

    print(
        f"{job['title']} | "
        f"{job['company_name']}"
    )

    print(
        "Location: "
        f"{job.get('location') or 'Not provided'}"
    )

    print(
        "Confidence: "
        f"{cached_score['confidence']}"
    )

    print(
        "Description: "
        f"{cached_score['description_word_count']} words"
    )

    print(
        "Summary: "
        f"{cached_score['summary']}"
    )

    strengths = parse_json_list(
        cached_score["matching_strengths"]
    )

    if strengths:
        print("Strengths:")

        for strength in strengths:
            print(f"- {strength}")

    missing = parse_json_list(
        cached_score[
            "hard_requirements_missing"
        ]
    )

    if missing:
        print("Missing required qualifications:")

        for requirement in missing:
            print(f"- {requirement}")

    risks = parse_json_list(
        cached_score["risk_factors"]
    )

    if risks:
        print("Risks:")

        for risk in risks:
            print(f"- {risk}")

    print(f"Apply: {job['apply_url']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Use Gemini to compare a resume "
            "against stored jobs."
        )
    )

    parser.add_argument(
        "resume_path",
        type=Path,
        help="Path to a PDF or DOCX resume.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=2,
        help=(
            "Maximum number of jobs to AI-score. "
            "Default: 2."
        ),
    )

    parser.add_argument(
        "--min-rule-score",
        type=int,
        default=35,
        help=(
            "Minimum pre-filter score. "
            "Default: 35."
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Ignore cached AI scores and score "
            "the jobs again."
        ),
    )

    arguments = parser.parse_args()

    if arguments.limit < 1:
        print("--limit must be at least 1.")
        raise SystemExit(1)

    initialize_ai_tables()

    try:
        resume = extract_resume_file(
            arguments.resume_path
        )

        model_name = get_model_name()

        resume_profile = load_cached_profile(
            resume_hash=resume.resume_hash,
            model_name=model_name,
        )

        if resume_profile:
            print(
                "Using cached resume profile."
            )

        else:
            print(
                "Creating AI resume profile..."
            )

            profiled_resume = profile_resume(
                resume=resume,
                model_name=model_name,
            )

            resume_profile = (
                profiled_resume.profile
            )

            save_resume_profile(
                resume_hash=resume.resume_hash,
                filename=resume.filename,
                model_name=model_name,
                profile=resume_profile,
            )

        jobs = load_candidate_jobs(
            limit=arguments.limit,
            minimum_rule_score=(
                arguments.min_rule_score
            ),
        )

        if not jobs:
            print(
                "No jobs passed the rule-based "
                "pre-filter."
            )
            return

        print(
            f"Jobs selected for AI scoring: "
            f"{len(jobs)}"
        )

        for index, job in enumerate(
            jobs,
            start=1,
        ):
            print(
                f"\nScoring job "
                f"{index} of {len(jobs)}..."
            )

            cached_score = None

            if not arguments.force:
                cached_score = (
                    load_cached_job_score(
                        resume_hash=(
                            resume.resume_hash
                        ),
                        record_key=(
                            job["record_key"]
                        ),
                        model_name=model_name,
                        prompt_version=(
                            MATCHER_PROMPT_VERSION
                        ),
                    )
                )

            if cached_score:
                print_cached_match(
                    job=job,
                    cached_score=cached_score,
                )
                continue

            try:
                match = score_resume_against_job(
                    resume_profile=resume_profile,
                    job=job,
                    model_name=model_name,
                )

            except ResumeMatcherError as error:
                print(
                    "\nUnable to score this job."
                )
                print(
                    f"Job: {job['title']} | "
                    f"{job['company_name']}"
                )
                print(f"Reason: {error}")
                print(
                    "Continuing with the remaining jobs."
                )
                continue

            save_job_score(
                resume_hash=resume.resume_hash,
                match=match,
            )

            print_match(
                job=job,
                match=match,
            )

    except (
        ResumeExtractionError,
        ResumeProfilerError,
        ResumeMatcherError,
    ) as error:
        print(f"\nAI scoring failed: {error}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
