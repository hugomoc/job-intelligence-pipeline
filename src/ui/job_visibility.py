"""User-facing job visibility rules.

Raw aggregator alerts are useful discovery inputs, but they are not actionable
review cards until an employer/ATS URL has been verified.
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


def is_user_reviewable_job(
    job: dict[str, Any],
) -> bool:
    if not is_aggregator_job(job):
        return True

    return has_verified_non_aggregator_official_url(job)


def filter_user_reviewable_jobs(
    jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        job
        for job in jobs
        if is_user_reviewable_job(job)
    ]


REVIEW_BUCKET_RECOMMENDED = "recommended"
REVIEW_BUCKET_NEEDS_REVIEW = "needs_review"
REVIEW_BUCKET_LOW_FIT = "low_fit"

REVIEW_FILTER_RECOMMENDED = "Recommended"
REVIEW_FILTER_NEEDS_REVIEW = "Needs review"
REVIEW_FILTER_ALL = "All"
REVIEW_FILTER_LOW_FIT = "Low fit"

REVIEW_FILTER_OPTIONS = (
    REVIEW_FILTER_RECOMMENDED,
    REVIEW_FILTER_NEEDS_REVIEW,
    REVIEW_FILTER_ALL,
    REVIEW_FILTER_LOW_FIT,
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


def classify_job_review_bucket(
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
                or classification.matched_pattern is None
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


def count_job_review_buckets(
    jobs: list[dict[str, Any]],
) -> dict[str, int]:
    counts = {
        REVIEW_BUCKET_RECOMMENDED: 0,
        REVIEW_BUCKET_NEEDS_REVIEW: 0,
        REVIEW_BUCKET_LOW_FIT: 0,
    }

    for job in jobs:
        counts[classify_job_review_bucket(job)] += 1

    return counts


def is_visible_for_review_filter(
    job: dict[str, Any],
    review_filter: str,
) -> bool:
    if review_filter == REVIEW_FILTER_ALL:
        return True

    bucket = classify_job_review_bucket(job)

    if review_filter == REVIEW_FILTER_LOW_FIT:
        return bucket == REVIEW_BUCKET_LOW_FIT

    if review_filter == REVIEW_FILTER_NEEDS_REVIEW:
        return bucket == REVIEW_BUCKET_NEEDS_REVIEW

    return bucket == REVIEW_BUCKET_RECOMMENDED


def filter_jobs_by_review_filter(
    jobs: list[dict[str, Any]],
    review_filter: str,
) -> list[dict[str, Any]]:
    return [
        job
        for job in jobs
        if is_visible_for_review_filter(job, review_filter)
    ]
