"""Job visibility, assessment eligibility and verification, kept separate.

- Visibility: every relevant discovered job is shown, from every source.
  Relevance is the existing admission/title gate (recommendation_repository).
- Source trust gate (passes_source_trust_gate): employer-verified or direct
  source. Defines "awaiting employer verification" for Pending Verification
  and hidden-job recovery; never a visibility requirement.
- Assessment eligibility (is_assessment_eligible_job): which jobs the paid
  screening/scoring queues may consider. Those queues still require a FULL_JD.
- Employer verification: a trusted posting key (src.actionability.trusted_key).
"""

from __future__ import annotations

from typing import Any
from src.job_title_filter import classify_job_title

from src.enrichment.official_job_resolver import (
    OFFICIAL_FOUND_VERIFIED,
    is_aggregator_domain,
    is_aggregator_job,
)


def has_verified_non_aggregator_official_url(
    job: dict[str, Any],
) -> bool:
    official_url = str(job.get("official_job_url") or "").strip()

    return (
        str(job.get("official_url_status") or "").strip()
        == OFFICIAL_FOUND_VERIFIED
        and bool(official_url)
        and not is_aggregator_domain(official_url)
    )


def passes_source_trust_gate(
    job: dict[str, Any],
) -> bool:
    """Formerly is_user_reviewable_job: employer-verified or direct-source job."""
    if str(job.get("application_status") or "new").lower() in {"applied", "removed"}:
        return True
    from src.job_domains import BUILT_IN_DOMAINS, is_domain_in
    if job.get("official_url_status") == OFFICIAL_FOUND_VERIFIED and is_domain_in(job.get("official_job_url"), BUILT_IN_DOMAINS):
        return True  # Legacy diagnostic card; never employer evidence or ACTIVE.
    if str(job.get("source") or "").lower() == "builtin":
        from src.repositories.recommendation_repository import has_current_complete_ai_score
        if has_current_complete_ai_score(job):
            return True  # Existing scored diagnostic record; gate still requires ACTIVE.
    if not is_aggregator_job(job):
        return True

    return has_verified_non_aggregator_official_url(job)


def is_assessment_eligible_job(
    job: dict[str, Any],
) -> bool:
    """May the screening/scoring queues consider this job?

    Trusted-source jobs keep today's behaviour. An unverified aggregator job is
    eligible only when its own stored description is a FULL_JD under the
    existing quality rules and was not rejected by identity validation (raw
    descriptions are stored only from the alert itself or an identity-accepted
    fetch). This never marks the job verified, ACTIVE or Recommended.
    """
    if passes_source_trust_gate(job):
        return True
    from src.description_attachment import enrichment_rejection_applies
    if enrichment_rejection_applies(job):  # Option B: active user paste is judged on its text
        return False
    from src.repositories.recommendation_repository import description_state
    return description_state(job) == "FULL_JD"


def filter_assessment_eligible_jobs(
    jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [job for job in jobs if is_assessment_eligible_job(job)]


def is_employer_verified(job: dict[str, Any]) -> bool:
    from src.actionability import trusted_key
    return bool(trusted_key(job))


JOB_STATE_VERIFIED_SCORED = "Verified · Scored"
JOB_STATE_VERIFIED_NEEDS_DESCRIPTION = "Verified · Needs description"
JOB_STATE_VERIFIED_NOT_SCORED = "Verified · Not scored"
JOB_STATE_UNVERIFIED_SCORED = "Unverified · Scored"
JOB_STATE_UNVERIFIED_JD_AVAILABLE = "Unverified · JD available"
JOB_STATE_UNVERIFIED_NEEDS_DESCRIPTION = "Unverified · Needs description"


def job_card_state(job: dict[str, Any]) -> str:
    """Verification x description x score state shown on every job card."""
    from src.repositories.recommendation_repository import description_state, has_current_complete_ai_score
    verified = is_employer_verified(job)
    if has_current_complete_ai_score(job):
        return JOB_STATE_VERIFIED_SCORED if verified else JOB_STATE_UNVERIFIED_SCORED
    full = (job.get("description_state") or description_state(job)) == "FULL_JD"
    if verified:
        return JOB_STATE_VERIFIED_NOT_SCORED if full else JOB_STATE_VERIFIED_NEEDS_DESCRIPTION
    return JOB_STATE_UNVERIFIED_JD_AVAILABLE if full else JOB_STATE_UNVERIFIED_NEEDS_DESCRIPTION


REVIEW_BUCKET_RECOMMENDED = "recommended"
REVIEW_BUCKET_NEEDS_REVIEW = "needs_review"
REVIEW_BUCKET_LOW_FIT = "low_fit"

REVIEW_FILTER_RECOMMENDED = "Recommended"
REVIEW_FILTER_NEEDS_REVIEW = "Needs review"
REVIEW_FILTER_ALL = "All"
REVIEW_FILTER_LOW_FIT = "Low fit"
# Default: Recommended + Needs review buckets (every relevant job, any source,
# verified or not). Low fit and the unfiltered All view stay optional.
REVIEW_FILTER_ALL_RELEVANT = "All relevant"

REVIEW_FILTER_OPTIONS = (
    REVIEW_FILTER_ALL_RELEVANT,
    REVIEW_FILTER_RECOMMENDED,
    REVIEW_FILTER_NEEDS_REVIEW,
    REVIEW_FILTER_LOW_FIT,
    REVIEW_FILTER_ALL,
)


def _normalized(value: Any) -> str:
    return str(value or "").strip().casefold()


def _score_value(value: Any) -> int | None:
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_job_fit_bucket(
    job: dict[str, Any],
) -> str:
    """Classify how a reviewable job should appear in the UI workflow.

    This is deliberately a presentation bucket. Hard title/admission/source
    screening happens before this helper sees a job.
    """
    application_status = _normalized(
        job.get("application_status") or "new"
    )

    if application_status in {"applied", "removed"} and bool(
        job.get("has_current_complete_ai_assessment")
    ):
        return REVIEW_BUCKET_RECOMMENDED

    if not bool(job.get("has_current_complete_ai_assessment")):
        classification = classify_job_title(job.get("title"))
        if (classification.category == "FILTERED_OUT"
                or (classification.matched_pattern is None
                    and job.get("admission_decision") != "include")
                or job.get("critical_skill_gaps")):
            return REVIEW_BUCKET_LOW_FIT
        return REVIEW_BUCKET_NEEDS_REVIEW

    score = _score_value(job.get("ai_score"))
    recommendation = _normalized(job.get("recommendation"))
    confidence = _normalized(job.get("confidence"))
    fit_priority = _normalized(job.get("fit_priority_label"))
    priority_source = _normalized(job.get("fit_priority_source"))

    if recommendation == "apply":
        return REVIEW_BUCKET_RECOMMENDED

    if recommendation in {"consider", "review"} and (
        score is not None and score >= 50
    ):
        return REVIEW_BUCKET_RECOMMENDED

    if score is not None and score >= 60:
        return REVIEW_BUCKET_RECOMMENDED

    if recommendation == "skip" and confidence == "high":
        return REVIEW_BUCKET_LOW_FIT

    if score is not None and score < 50:
        return REVIEW_BUCKET_LOW_FIT

    if fit_priority == "low" and priority_source == "ai assessment":
        return REVIEW_BUCKET_LOW_FIT

    return REVIEW_BUCKET_RECOMMENDED


def classify_job_review_bucket(job, actionability_config=None, now=None):
    """Apply current actionability only to otherwise recommended NEW jobs."""
    fit = classify_job_fit_bucket(job)
    if str(job.get("application_status") or "new").lower() != "new" or fit != REVIEW_BUCKET_RECOMMENDED:
        return fit
    from src.actionability import effective_status
    return REVIEW_BUCKET_RECOMMENDED if effective_status(job, config=actionability_config, now=now) == "ACTIVE" else REVIEW_BUCKET_NEEDS_REVIEW


def count_job_review_buckets(
    jobs: list[dict[str, Any]],
) -> dict[str, int]:
    from src.actionability import settings, now_utc
    config, now = settings(), now_utc()
    counts = {
        REVIEW_BUCKET_RECOMMENDED: 0,
        REVIEW_BUCKET_NEEDS_REVIEW: 0,
        REVIEW_BUCKET_LOW_FIT: 0,
    }

    for job in jobs:
        counts[classify_job_review_bucket(job, actionability_config=config, now=now)] += 1

    return counts


def is_visible_for_review_filter(
    job: dict[str, Any],
    review_filter: str,
    actionability_config=None, now=None,
) -> bool:
    if review_filter == REVIEW_FILTER_ALL:
        return True

    bucket = classify_job_review_bucket(job, actionability_config=actionability_config, now=now)

    if review_filter == REVIEW_FILTER_ALL_RELEVANT:
        return bucket != REVIEW_BUCKET_LOW_FIT

    if review_filter == REVIEW_FILTER_LOW_FIT:
        return bucket == REVIEW_BUCKET_LOW_FIT

    if review_filter == REVIEW_FILTER_NEEDS_REVIEW:
        return bucket == REVIEW_BUCKET_NEEDS_REVIEW

    return bucket == REVIEW_BUCKET_RECOMMENDED


def filter_jobs_by_review_filter(
    jobs: list[dict[str, Any]],
    review_filter: str,
) -> list[dict[str, Any]]:
    from src.actionability import settings, now_utc
    config, now = settings(), now_utc()
    return [
        job
        for job in jobs
        if is_visible_for_review_filter(job, review_filter, actionability_config=config, now=now)
    ]
