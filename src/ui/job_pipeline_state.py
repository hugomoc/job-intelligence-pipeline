"""Derived UI labels for a job's enrichment and scoring pipeline state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


OFFICIAL_RETRY_COOLDOWN_DAYS = 7
OFFICIAL_RETRY_STATUSES = {
    "AMBIGUOUS",
    "BLOCKED",
    "ERROR",
    "OFFICIAL_NOT_FOUND",
    "SEARCH_DEFERRED",
}


@dataclass(frozen=True)
class JobPipelineState:
    code: str
    label: str
    details: tuple[tuple[str, str], ...]


def as_utc_datetime(value: Any) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value.astimezone(timezone.utc)

    try:
        parsed = datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def is_official_retry_in_cooldown(
    job: dict[str, Any],
    now: datetime | None = None,
) -> bool:
    official_status = str(
        job.get("official_url_status") or ""
    ).strip()

    if official_status not in OFFICIAL_RETRY_STATUSES:
        return False

    resolved_at = as_utc_datetime(
        job.get("official_url_resolved_at")
        or job.get("enrichment_attempted_at")
    )

    if resolved_at is None:
        return False

    reference_time = now or datetime.now(timezone.utc)

    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)

    return resolved_at > (
        reference_time.astimezone(timezone.utc)
        - timedelta(days=OFFICIAL_RETRY_COOLDOWN_DAYS)
    )


def derive_job_pipeline_state(
    job: dict[str, Any],
    now: datetime | None = None,
) -> JobPipelineState:
    """Return concise UI text without implying an AI failure too early."""
    if job.get("ai_score") is not None:
        code = "AI_SCORED"
        label = "AI-scored"
    else:
        description_state = str(
            job.get("description_state") or "NEEDS_ENRICHMENT"
        )
        enrichment_status = str(
            job.get("enrichment_status") or ""
        ).strip()
        official_status = str(
            job.get("official_url_status") or ""
        ).strip()

        if description_state == "FULL_JD":
            code = "SCORING_PENDING"
            label = "Awaiting AI scoring"
        elif is_official_retry_in_cooldown(job, now=now):
            code = "ENRICHMENT_COOLDOWN"
            label = "Official lookup retry after cooldown"
        elif (
            enrichment_status == "blocked"
            and official_status != "FOUND_VERIFIED"
        ):
            code = "ENRICHMENT_PENDING"
            label = "Source fetch blocked - official lookup pending"
        elif official_status in OFFICIAL_RETRY_STATUSES:
            code = "ENRICHMENT_PENDING"
            label = "Official job lookup pending"
        elif int(job.get("enrichment_attempt_count") or 0) == 0:
            code = "NEEDS_ENRICHMENT"
            label = "Awaiting enrichment"
        else:
            code = "UNRESOLVED_DESCRIPTION"
            label = "No verified full job description found"

    return JobPipelineState(
        code=code,
        label=label,
        details=build_enrichment_details(job),
    )


def build_enrichment_details(
    job: dict[str, Any],
) -> tuple[tuple[str, str], ...]:
    details: list[tuple[str, str]] = []

    add_detail(
        details,
        "Description state",
        job.get("description_state"),
    )
    add_detail(
        details,
        "Current word count",
        (
            job.get("description_word_count")
            or job.get("raw_description_word_count")
        ),
    )
    add_detail(
        details,
        "Enrichment attempt count",
        job.get("enrichment_attempt_count"),
    )
    add_detail(
        details,
        "Last attempt status",
        job.get("enrichment_status"),
    )
    add_detail(
        details,
        "Last HTTP status",
        job.get("enrichment_http_status"),
    )
    add_detail(
        details,
        "Last attempted",
        job.get("enrichment_attempted_at"),
    )
    add_detail(
        details,
        "Official-resolution status",
        job.get("official_url_status"),
    )
    add_detail(
        details,
        "Official job URL",
        job.get("official_job_url"),
    )
    add_detail(
        details,
        "Official-resolution source",
        job.get("official_url_source"),
    )
    add_detail(
        details,
        "Approximate enrichment queue position",
        job.get("approximate_enrichment_queue_rank")
        or job.get("enrichment_queue_rank"),
    )
    add_detail(
        details,
        "Priority reason",
        job.get("enrichment_priority_reason"),
    )

    if job.get("enrichment_error_message"):
        add_detail(
            details,
            "Last error",
            job.get("enrichment_error_message"),
        )

    return tuple(details)


def add_detail(
    details: list[tuple[str, str]],
    label: str,
    value: Any,
) -> None:
    if value is None:
        return

    text = str(value).strip()

    if not text:
        return

    details.append((label, text))
