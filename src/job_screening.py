"""Shared orchestration for AI job-eligibility screening.

Both Streamlit and the backlog CLI call this module so the same cache and
provider-fallback behavior applies everywhere. The decisions are saved before
full scoring, which keeps unrelated jobs out of later expensive steps.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from src.ai.job_eligibility import (
    ELIGIBILITY_PROMPT_VERSION,
    evaluate_job_eligibility,
    evaluate_job_eligibility_openai,
    get_openai_screening_model_name,
)
from src.ai.resume_matcher import ResumeMatcherError
from src.scoring_diagnostics import log_scoring_failure
from src.ai.resume_profiler import ResumeProfile
from src.repositories.recommendation_repository import (
    load_unscreened_job_eligibility_candidates,
    description_state,
    save_job_eligibility_decision,
)


# Temporary global safety guard (Screening-1). Saved "exclude" decisions are
# persistent, so AI eligibility screening is off unless deliberately enabled:
# the default is 0 new screening attempts per workflow invocation.
SCREENING_MAX_PER_RUN_ENV = "SCREENING_MAX_PER_RUN"
DEFAULT_SCREENING_MAX_PER_RUN = 0


class ScreeningConfigurationError(ValueError):
    """SCREENING_MAX_PER_RUN is not a non-negative integer."""


def screening_max_per_run(environ=None) -> int:
    """Validated cap; unset or blank means 0. Never falls back to a positive value."""
    source = os.environ if environ is None else environ
    raw = source.get(SCREENING_MAX_PER_RUN_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_SCREENING_MAX_PER_RUN
    text = str(raw).strip()
    if not re.fullmatch(r"[0-9]+", text):
        raise ScreeningConfigurationError(
            f"{SCREENING_MAX_PER_RUN_ENV} must be a non-negative integer (0 disables "
            f"eligibility screening); got {raw!r}.")
    return int(text)


@dataclass
class ScreeningBudget:
    """New screening attempts allowed for one complete workflow invocation.

    Pass the same budget to every screening call in a workflow; attempts are
    counted per job (a Gemini attempt plus its OpenAI fallback is one attempt).
    """
    limit: int
    used: int = 0

    def __post_init__(self):
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 0:
            raise ScreeningConfigurationError(
                f"Screening limit must be a non-negative integer; got {self.limit!r}.")

    @classmethod
    def from_config(cls, environ=None) -> "ScreeningBudget":
        return cls(screening_max_per_run(environ))

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)


def screening_status_line(limit: int, attempts: int, saved: int) -> str:
    if limit == 0:
        return f"Eligibility screening: disabled by {SCREENING_MAX_PER_RUN_ENV}=0"
    return (f"Eligibility screening: limit {limit} per run ({SCREENING_MAX_PER_RUN_ENV}); "
            f"attempts {attempts}; new decisions saved {saved}")


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
    failed_record_keys: tuple[str, ...] = ()
    failed_posting_keys: tuple[str, ...] = ()
    configured_limit: int = DEFAULT_SCREENING_MAX_PER_RUN
    attempts: int = 0
    disabled: bool = False


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
    record_keys: tuple[str, ...] | None = None,
    budget: ScreeningBudget | None = None,
) -> JobScreeningSummary:
    """Screen uncached canonical jobs and persist their eligibility decisions.

    At most ``budget.remaining`` jobs are screened. Without an explicit budget
    the validated SCREENING_MAX_PER_RUN configuration applies (default 0).
    """
    budget = ScreeningBudget.from_config() if budget is None else budget
    if budget.remaining <= 0:
        print(screening_status_line(budget.limit, budget.used, 0) if budget.limit == 0 else
              f"Eligibility screening: per-run limit of {budget.limit} already used; no new screening.")
        return JobScreeningSummary(
            candidates_selected=0, screened=0, eligible=0, needs_description=0, excluded=0,
            failed=0, gemini_screened=0, openai_screened=0, quota_exhausted=False,
            configured_limit=budget.limit, attempts=0, disabled=budget.limit == 0)

    jobs = load_unscreened_job_eligibility_candidates(
        resume_hash=resume_hash,
        limit=limit,
        prompt_version=ELIGIBILITY_PROMPT_VERSION,
        **({"record_keys": record_keys} if record_keys is not None else {}),
    )

    # JD resolution precedes every AI stage, including lightweight screening.
    jobs = [job for job in jobs if description_state({
        **job,
        "raw_description_word_count": len((job.get("description") or "").split()),
    }) == "FULL_JD"]
    # Existing selection order, bounded by the workflow's remaining budget.
    jobs = jobs[:budget.remaining]
    attempts = 0

    screened = 0
    eligible = 0
    needs_description = 0
    excluded = 0
    failed = 0
    failed_records = []
    failed_postings = []

    def record_failure(job, index, provider, error):
        failed_records.append(job["record_key"])
        failed_postings.append(job.get("canonical_job_key") or job["record_key"])
        log_scoring_failure(job,index,len(jobs),provider,error,stage="Eligibility screening")
    gemini_screened = 0
    openai_screened = 0
    quota_exhausted = False
    use_openai_fallback = False

    if jobs:
        print(f"Screening {len(jobs)} jobs for resume fit before scoring.")

    for index, job in enumerate(jobs, start=1):
        budget.used += 1
        attempts += 1
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
            log_scoring_failure(job,index,len(jobs),"OpenAI" if use_openai_fallback else "Gemini",error,stage="Eligibility screening attempt")
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
                    record_failure(job,index,"OpenAI",fallback_error)
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
                record_failure(job,index,"OpenAI" if use_openai_fallback else "Gemini",error)
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
        failed_record_keys=tuple(failed_records),
        failed_posting_keys=tuple(failed_postings),
        configured_limit=budget.limit,
        attempts=attempts,
    )


def screening_failed_for_job(summary, job):
    """Failures are a run-local hold, never a cached eligibility decision."""
    records = getattr(summary, 'failed_record_keys', ())
    postings = getattr(summary, 'failed_posting_keys', ())
    # Older orchestration adapters may provide only the original summary fields.
    records = records if isinstance(records, (tuple,list,set,frozenset)) else ()
    postings = postings if isinstance(postings, (tuple,list,set,frozenset)) else ()
    return (job.get('record_key') in records
            or (job.get('canonical_job_key') or job.get('record_key')) in postings)
