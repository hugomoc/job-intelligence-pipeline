"""Service layer for resume upload and recommendation generation.

Streamlit gives this module an uploaded file and display limits. The service
extracts the resume in memory, reuses or creates a cached profile, screens jobs,
scores eligible candidates, refreshes dbt, and returns safe UI summaries.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.ai.resume_matcher import (
    MATCHER_PROMPT_VERSION,
    ResumeMatcherError,
    get_openai_model_name,
    is_gemini_daily_quota_error,
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
from src.scoring_diagnostics import log_scoring_failure
from src.scoring_run_repository import (
    finish_scoring_run,
    start_scoring_run,
)
from src.score_jobs_ai import (
    initialize_ai_tables,
    load_cached_profile,
    save_cached_resume_text,
    save_job_score,
    save_resume_profile,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class PreparedResume:
    """Resume/profile pair ready for screening or job scoring."""

    resume: ExtractedResume
    profile: ResumeProfile
    model_name: str
    profile_was_cached: bool


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
    log_lines: tuple[str, ...] = ()


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
        save_cached_resume_text(
            resume_hash=resume.resume_hash,
            resume_text=resume.text,
        )
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
        resume_text=resume.text,
    )

    return profiled_resume.profile, False


def prepare_resume_upload(
    uploaded_file: Any,
) -> PreparedResume:
    """Extract and activate an uploaded resume without scoring any jobs."""
    try:
        resume = extract_resume(uploaded_file)
        model_name = get_model_name()

        initialize_ai_tables()

        resume_profile, profile_was_cached = get_or_create_resume_profile(
            resume=resume,
            model_name=model_name,
        )

        return PreparedResume(
            resume=resume,
            profile=resume_profile,
            model_name=model_name,
            profile_was_cached=profile_was_cached,
        )

    except ResumeExtractionError:
        raise

    except Exception as error:
        print(
            "Unexpected resume preparation error: "
            f"{type(error).__name__}: {error}"
        )
        raise JobRecommendationServiceError(
            "The resume could not be prepared for scoring. "
            "Please try again later."
        ) from error


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
    """Return true only for explicit Gemini daily quota exhaustion."""
    return is_gemini_daily_quota_error(error)


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
    """Profile a resume, score a bounded set, and persist the scoring log."""
    prepared = prepare_resume_upload(
        uploaded_file
    )
    resume = prepared.resume
    model_name = prepared.model_name
    resume_profile = prepared.profile
    profile_was_cached = prepared.profile_was_cached

    run_id = start_scoring_run(
        resume_hash=resume.resume_hash,
        workflow="score_jobs",
    )
    output = io.StringIO()

    class _Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, value):
            for stream in self.streams:
                stream.write(value)
            return len(value)

        def flush(self):
            for stream in self.streams:
                stream.flush()

    successful_score_count = 0
    failed_score_count = 0
    gemini_score_count = 0
    openai_score_count = 0
    cached_score_count = 0
    candidate_jobs: list[dict[str, Any]] = []
    quota_exhausted = False
    dbt_was_run = False

    try:
        with contextlib.redirect_stdout(
            _Tee(sys.stdout, output)
        ):
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

            use_openai_fallback = False

            for index, job in enumerate(
                candidate_jobs,
                start=1,
            ):
                title = job.get("title") or "Untitled job"
                company = (
                    job.get("company_name")
                    or "Unknown company"
                )
                print(
                    f"Scoring {index}/{len(candidate_jobs)}: "
                    f"{title} | {company}"
                )

                try:
                    if use_openai_fallback:
                        match = score_resume_against_job_openai(
                            resume_profile=resume_profile,
                            resume_text=resume.text,
                            job=job,
                            model_name=get_openai_model_name(),
                        )
                    else:
                        match = score_resume_against_job(
                            resume_profile=resume_profile,
                            resume_text=resume.text,
                            job=job,
                            model_name=model_name,
                        )

                except ResumeMatcherError as error:
                    provider = (
                        "OpenAI"
                        if use_openai_fallback
                        else "Gemini"
                    )
                    log_scoring_failure(
                        job,
                        index,
                        len(candidate_jobs),
                        provider,
                        error,
                    )

                    if (
                        not use_openai_fallback
                        and is_gemini_quota_exhausted(error)
                    ):
                        quota_exhausted = True
                        print(
                            "Gemini daily quota is exhausted. "
                            "Switching to OpenAI fallback."
                        )
                        use_openai_fallback = True

                        try:
                            match = score_resume_against_job_openai(
                                resume_profile=resume_profile,
                                resume_text=resume.text,
                                job=job,
                                model_name=get_openai_model_name(),
                            )
                        except ResumeMatcherError as fallback_error:
                            log_scoring_failure(
                                job,
                                index,
                                len(candidate_jobs),
                                "OpenAI",
                                fallback_error,
                            )
                            failed_score_count += 1

                            if is_openai_quota_exhausted(
                                fallback_error
                            ):
                                print(
                                    "OpenAI quota is exhausted. "
                                    "Stopping the remaining scoring requests."
                                )
                                break

                            continue

                        save_job_score(
                            resume_hash=resume.resume_hash,
                            match=match,
                        )
                        successful_score_count += 1
                        openai_score_count += 1
                        print(
                            "Saved fallback score: "
                            f"{match.overall_score}% "
                            f"{match.recommendation.upper()} "
                            f"({match.analysis.confidence} confidence)"
                        )
                        continue

                    if (
                        use_openai_fallback
                        and is_openai_quota_exhausted(error)
                    ):
                        failed_score_count += 1
                        print(
                            "OpenAI quota is exhausted. "
                            "Stopping the remaining scoring requests."
                        )
                        break

                    failed_score_count += 1
                    continue

                save_job_score(
                    resume_hash=resume.resume_hash,
                    match=match,
                )
                successful_score_count += 1

                if use_openai_fallback:
                    openai_score_count += 1
                else:
                    gemini_score_count += 1

                print(
                    "Saved score: "
                    f"{match.overall_score}% "
                    f"{match.recommendation.upper()} "
                    f"({match.analysis.confidence} confidence)"
                )

            if successful_score_count:
                print("Refreshing dbt analytics...")
                run_dbt_build()
                dbt_was_run = True

        log_lines = tuple(
            line.strip()
            for line in output.getvalue().splitlines()
            if line.strip()
        )

        summary = ScoringSummary(
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
            dbt_was_run=dbt_was_run,
            log_lines=log_lines,
        )

        finish_scoring_run(
            run_id,
            status="completed",
            model_name=model_name,
            candidates_selected=len(candidate_jobs),
            already_scored=cached_score_count,
            scores_saved=successful_score_count,
            jobs_failed=failed_score_count,
            gemini_scores_saved=gemini_score_count,
            openai_scores_saved=openai_score_count,
            quota_exhausted=quota_exhausted,
            log_lines=log_lines,
        )

        return summary

    except (
        ResumeExtractionError,
        JobRecommendationServiceError,
    ):
        log_lines = tuple(
            line.strip()
            for line in output.getvalue().splitlines()
            if line.strip()
        )
        finish_scoring_run(
            run_id,
            status="failed",
            model_name=model_name,
            candidates_selected=len(candidate_jobs),
            already_scored=cached_score_count,
            scores_saved=successful_score_count,
            jobs_failed=failed_score_count,
            gemini_scores_saved=gemini_score_count,
            openai_scores_saved=openai_score_count,
            quota_exhausted=quota_exhausted,
            log_lines=log_lines,
        )
        raise

    except Exception as error:
        print(
            "Unexpected Streamlit workflow error: "
            f"{type(error).__name__}: {error}"
        )
        log_lines = tuple(
            line.strip()
            for line in output.getvalue().splitlines()
            if line.strip()
        )
        finish_scoring_run(
            run_id,
            status="failed",
            model_name=model_name,
            candidates_selected=len(candidate_jobs),
            already_scored=cached_score_count,
            scores_saved=successful_score_count,
            jobs_failed=failed_score_count,
            gemini_scores_saved=gemini_score_count,
            openai_scores_saved=openai_score_count,
            quota_exhausted=quota_exhausted,
            log_lines=log_lines,
        )
        raise JobRecommendationServiceError(
            "The scoring workflow could not be completed. "
            "Please try again later."
        ) from error

