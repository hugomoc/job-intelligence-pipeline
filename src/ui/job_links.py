"""Choose safe job links for the Streamlit job card."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.enrichment.official_job_resolver import is_aggregator_job
from src.ui.job_visibility import has_verified_non_aggregator_official_url


OFFICIAL_FOUND_VERIFIED = "FOUND_VERIFIED"


@dataclass(frozen=True)
class JobOpenTarget:
    label: str
    url: str | None
    note: str | None = None


def identity_validation_passed(job: dict[str, Any]) -> bool:
    if job.get("enrichment_status") != "enriched":
        return False

    confidence = job.get("identity_confidence")

    if confidence is None:
        return True

    try:
        return float(confidence) >= 0.78
    except (TypeError, ValueError):
        return False


def select_job_open_target(job: dict[str, Any]) -> JobOpenTarget:
    official_url = str(job.get("official_job_url") or "").strip()
    official_status = str(job.get("official_url_status") or "").strip()

    if has_verified_non_aggregator_official_url(job):
        return JobOpenTarget(
            label="Open official job",
            url=official_url,
            note="Verified official employer/ATS posting.",
        )

    if is_aggregator_job(job):
        return JobOpenTarget(
            label="Official job unavailable",
            url=None,
            note="Aggregator source hidden until an official employer/ATS link is verified.",
        )

    resolved_url = str(job.get("resolved_candidate_url") or "").strip()

    if resolved_url and identity_validation_passed(job):
        return JobOpenTarget(
            label="Open verified job",
            url=resolved_url,
            note="Resolved page passed identity validation.",
        )

    source_url = str(job.get("apply_url") or "").strip()

    if official_status and official_status != OFFICIAL_FOUND_VERIFIED:
        return JobOpenTarget(
            label="Open source page",
            url=source_url or None,
            note=f"Official job link status: {official_status}.",
        )

    return JobOpenTarget(
        label="Open job",
        url=source_url or None,
    )
