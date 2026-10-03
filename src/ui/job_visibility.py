"""User-facing job visibility rules.

Raw aggregator alerts are useful discovery inputs, but they are not actionable
review cards until an employer/ATS URL has been verified.
"""

from __future__ import annotations

from typing import Any

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
