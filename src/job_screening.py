"""Shared orchestration for AI job-eligibility screening.

Both Streamlit and the backlog CLI call this module so the same cache and
provider-fallback behavior applies everywhere. The decisions are saved before
full scoring, which keeps unrelated jobs out of later expensive steps.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.ai.job_eligibility import (
    ELIGIBILITY_PROMPT_VERSION,
    evaluate_job_eligibility,
    evaluate_job_eligibility_openai,
    get_openai_screening_model_name,
)
from src.ai.resume_matcher import ResumeMatcherError
from src.ai.resume_profiler import ResumeProfile
from src.repositories.recommendation_repository import (
    load_unscreened_job_eligibility_candidates,
    description_state,
    save_job_eligibility_decision,
)


@dataclass(frozen=True)
class JobScreeningSummary:
    candidates_selected: int
    screened: int
    eligible: int
    needs_description: int
    excluded: int
    failed: int
    gemini_screened: int
    openai_screened: int
    quota_exhausted: bool


def is_gemini_quota_exhausted(error: ResumeMatcherError) -> bool:
    error_message = str(error)

    return (
        "429 RESOURCE_EXHAUSTED" in error_message
        and (
            "GenerateRequestsPerDay" in error_message
            or "quota exceeded" in error_message.casefold()
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


def should_switch_to_openai(error: ResumeMatcherError) -> bool:
    """Use OpenAI when Gemini cannot screen a job."""
    error_message = str(error)

    return (
        is_gemini_quota_exhausted(error)
        or "PERMISSION_DENIED" in error_message
        or "denied access" in error_message.casefold()
    )


def screen_unscreened_jobs(
    resume_hash: str,
    resume_profile: ResumeProfile,
    limit: int,
    model_name: str,
) -> JobScreeningSummary:
    """Screen uncached canonical jobs and persist their eligibility decisions."""
    jobs = load_unscreened_job_eligibility_candidates(
        resume_hash=resume_hash,
        limit=limit,
        prompt_version=ELIGIBILITY_PROMPT_VERSION,
    )

    # JD resolution precedes every AI stage, including lightweight screening.
    jobs = [job for job in jobs if description_state({
        **job,
        "raw_description_word_count": len((job.get("description") or "").split()),
    }) == "FULL_JD"]

    screened = 0
    eligible = 0
    needs_description = 0
    excluded = 0
    failed = 0
    gemini_screened = 0
    openai_screened = 0
    quota_exhausted = False
    use_openai_fallback = False

    if jobs:
        print(f"Screening {len(jobs)} jobs for resume fit before scoring.")

    for index, job in enumerate(jobs, start=1):
        print(
            f"Screening {index}/{len(jobs)}: "
            f"{job.get('title') or 'Untitled job'} | "
            f"{job.get('company_name') or 'Unknown company'}"
        )

        try:
            if use_openai_fallback:
                decision = evaluate_job_eligibility_openai(
                    resume_hash=resume_hash,
                    resume_profile=resume_profile,
                    job=job,
                    model_name=get_openai_screening_model_name(),
                )
                openai_screened += 1
            else:
                decision = evaluate_job_eligibility(
                    resume_hash=resume_hash,
                    resume_profile=resume_profile,
                    job=job,
                    model_name=model_name,
                )
                gemini_screened += 1

        except ResumeMatcherError as error:
            if should_switch_to_openai(error):
                if is_gemini_quota_exhausted(error):
                    quota_exhausted = True
                    print(
                        "Gemini quota is exhausted during screening. "
                        "Switching to OpenAI fallback."
                    )
                else:
                    print(
                        "Gemini is unavailable during screening. "
                        "Switching to OpenAI fallback."
                    )

                use_openai_fallback = True

                try:
                    decision = evaluate_job_eligibility_openai(
                        resume_hash=resume_hash,
                        resume_profile=resume_profile,
                        job=job,
                        model_name=get_openai_screening_model_name(),
                    )
                    openai_screened += 1
                except ResumeMatcherError as fallback_error:
                    failed += 1
                    print("OpenAI fallback failed during screening.")

                    if is_openai_quota_exhausted(fallback_error):
                        print(
                            "OpenAI quota is exhausted. "
                            "Stopping eligibility screening."
                        )
                        break

                    continue
            else:
                failed += 1
                print("Eligibility screening failed for this job.")
                continue

        save_job_eligibility_decision(decision)
        screened += 1

        if decision.analysis.decision == "eligible":
            eligible += 1
        elif decision.analysis.decision == "needs_description":
            needs_description += 1
        else:
            excluded += 1

        print(
            "Screened as "
            f"{decision.analysis.decision.upper()} "
            f"({decision.analysis.confidence} confidence)."
        )

    return JobScreeningSummary(
        candidates_selected=len(jobs),
        screened=screened,
        eligible=eligible,
        needs_description=needs_description,
        excluded=excluded,
        failed=failed,
        gemini_screened=gemini_screened,
        openai_screened=openai_screened,
        quota_exhausted=quota_exhausted,
    )
