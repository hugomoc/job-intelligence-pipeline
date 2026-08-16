"""Service layer for resume upload and recommendation generation.

Streamlit gives this module an uploaded file and display limits. The service
extracts the resume in memory, reuses or creates a cached profile, screens jobs,
scores eligible candidates, refreshes dbt, and returns safe UI summaries.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.ai.resume_matcher import (
    MATCHER_PROMPT_VERSION,
    ResumeMatcherError,
    get_openai_model_name,
    score_resume_against_job,
    score_resume_against_job_openai,
)
from src.ai.resume_profiler import (
    ResumeProfile,
    get_model_name,
    profile_resume,
)
from src.resume.extractor import ExtractedResume, ResumeExtractionError, extract_resume
from src.job_screening import screen_unscreened_jobs
from src.repositories.recommendation_repository import (
    count_cached_canonical_scores,
    load_candidate_jobs,
    load_recommendations,
)
from src.score_jobs_ai import (
    initialize_ai_tables,
    load_cached_profile,
    save_job_score,
    save_resume_profile,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class ScoringSummary:
    resume_hash: str
    filename: str
    word_count: int
    page_count: int | None
    model_name: str
    profile_was_cached: bool
    candidate_count: int
    cached_score_count: int
    successful_score_count: int
    failed_score_count: int
    gemini_score_count: int
    openai_score_count: int
    new_score_count: int
    dbt_was_run: bool


class JobRecommendationServiceError(Exception):
    """Raised when the Streamlit service cannot complete the workflow."""


def get_or_create_resume_profile(
    resume: ExtractedResume,
    model_name: str,
) -> tuple[ResumeProfile, bool]:
    """Reuse a cached profile for the resume hash, or create one once."""
    cached_profile = load_cached_profile(
        resume_hash=resume.resume_hash,
        model_name=model_name,
    )

    if cached_profile:
        return cached_profile, True

    profiled_resume = profile_resume(
        resume=resume,
        model_name=model_name,
    )

    save_resume_profile(
        resume_hash=resume.resume_hash,
        filename=resume.filename,
        model_name=model_name,
        profile=profiled_resume.profile,
    )

    return profiled_resume.profile, False


def run_dbt_build() -> None:
    """Refresh dbt analytics after new scores change mart output."""
    command = [
        str(PROJECT_ROOT / "scripts" / "dbt_jobs.sh"),
        "build",
    ]

    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    if result.returncode != 0:
        print("dbt build failed.")
        print("STDOUT:")
        print(result.stdout)
        print("STDERR:")
        print(result.stderr)
        raise JobRecommendationServiceError(
            "The analytics refresh failed. Please try again later."
        )


def is_gemini_quota_exhausted(error: ResumeMatcherError) -> bool:
    error_message = str(error)

    return (
        "429 RESOURCE_EXHAUSTED"
        in error_message
        and (
            "GenerateRequestsPerDay"
            in error_message
            or "quota exceeded"
            in error_message.casefold()
        )
    )


def is_openai_quota_exhausted(error: ResumeMatcherError) -> bool:
    error_message = str(error)

    return (
        "insufficient_quota" in error_message
        or (
            "429" in error_message
            and "quota" in error_message.casefold()
            and "openai" in error_message.casefold()
        )
    )


def process_resume_upload(
    uploaded_file: Any,
    limit: int,
    minimum_rule_score: int,
) -> ScoringSummary:
    """Profile a resume and score a bounded set of eligible jobs."""
    try:
        resume = extract_resume(uploaded_file)
        model_name = get_model_name()

        initialize_ai_tables()

        resume_profile, profile_was_cached = get_or_create_resume_profile(
            resume=resume,
            model_name=model_name,
        )

        cached_score_count = count_cached_canonical_scores(
            resume_hash=resume.resume_hash,
            model_name=None,
            prompt_version=MATCHER_PROMPT_VERSION,
            reuse_any_model=True,
        )

        screen_unscreened_jobs(
            resume_hash=resume.resume_hash,
            resume_profile=resume_profile,
            limit=limit,
            model_name=model_name,
        )

        candidate_jobs = load_candidate_jobs(
            resume_hash=resume.resume_hash,
            model_name=model_name,
            limit=limit,
            minimum_rule_score=minimum_rule_score,
            prompt_version=MATCHER_PROMPT_VERSION,
            reuse_any_model=True,
        )

        successful_score_count = 0
        failed_score_count = 0
        gemini_score_count = 0
        openai_score_count = 0
        use_openai_fallback = False

        for job in candidate_jobs:
            try:
                if use_openai_fallback:
                    match = score_resume_against_job_openai(
                        resume_profile=resume_profile,
                        job=job,
                        model_name=get_openai_model_name(),
                    )
                    openai_score_count += 1
                else:
                    match = score_resume_against_job(
                        resume_profile=resume_profile,
                        job=job,
                        model_name=model_name,
                    )
                    gemini_score_count += 1

                save_job_score(
                    resume_hash=resume.resume_hash,
                    match=match,
                )

                successful_score_count += 1

            except ResumeMatcherError as error:
                error_message = str(error)

                print(
                    "Failed to score job "
                    f"{job.get('record_key', '<missing>')}: "
                    f"{error_message}"
                )

                quota_exhausted = (
                    "429 RESOURCE_EXHAUSTED"
                    in error_message
                    and (
                        "GenerateRequestsPerDay"
                        in error_message
                        or "quota exceeded"
                        in error_message.casefold()
                    )
                )

                if quota_exhausted:
                    print(
                        "Gemini quota is exhausted. "
                        "Switching to OpenAI fallback."
                    )

                    use_openai_fallback = True

                    try:
                        match = score_resume_against_job_openai(
                            resume_profile=resume_profile,
                            job=job,
                            model_name=get_openai_model_name(),
                        )

                        save_job_score(
                            resume_hash=resume.resume_hash,
                            match=match,
                        )

                        successful_score_count += 1
                        openai_score_count += 1

                    except ResumeMatcherError as fallback_error:
                        failed_score_count += 1
                        print(
                            "OpenAI fallback failed for job "
                            f"{job.get('record_key', '<missing>')}: "
                            f"{fallback_error}"
                        )

                        if is_openai_quota_exhausted(
                            fallback_error
                        ):
                            print(
                                "OpenAI quota is exhausted. "
                                "Stopping the remaining scoring requests."
                            )
                            break

                    continue

                if (
                    use_openai_fallback
                    and is_openai_quota_exhausted(error)
                ):
                    print(
                        "OpenAI quota is exhausted. "
                        "Stopping the remaining scoring requests."
                    )
                    failed_score_count += 1
                    break

                failed_score_count += 1
                continue

        if successful_score_count:
            run_dbt_build()

        return ScoringSummary(
            resume_hash=resume.resume_hash,
            filename=resume.filename,
            word_count=resume.word_count,
            page_count=resume.page_count,
            model_name=model_name,
            profile_was_cached=profile_was_cached,
            candidate_count=len(candidate_jobs),
            cached_score_count=cached_score_count,
            successful_score_count=successful_score_count,
            failed_score_count=failed_score_count,
            gemini_score_count=gemini_score_count,
            openai_score_count=openai_score_count,
            new_score_count=successful_score_count,
            dbt_was_run=bool(successful_score_count),
        )

    except (
        ResumeExtractionError,
        JobRecommendationServiceError,
    ):
        raise

    except Exception as error:
        print(f"Unexpected Streamlit workflow error: {type(error).__name__}: {error}")
        raise JobRecommendationServiceError(
            "The scoring workflow could not be completed. Please try again later."
        ) from error
