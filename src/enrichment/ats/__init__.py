"""Thin ATS candidate discovery adapters for official job resolution."""

from __future__ import annotations

import httpx

from src.enrichment.ats import (
    ashby,
    greenhouse,
    icims,
    lever,
    smartrecruiters,
    workday,
)
from src.enrichment.ats.base import (
    MAX_ATS_PAGES,
    MAX_ATS_RESULTS,
    ATS_REQUEST_TIMEOUT,
    OfficialJobCandidate,
)


ADAPTERS = (
    greenhouse,
    lever,
    ashby,
    smartrecruiters,
    icims,
    workday,
)


ATS_DISCOVERY_CACHE: dict[str, tuple[OfficialJobCandidate, ...]] = {}


def normalized_company_key(
    company_name: str | None,
) -> str:
    return " ".join(str(company_name or "").casefold().split())


def normalized_job_cache_key(
    job: dict,
) -> str:
    company = normalized_company_key(
        str(job.get("company_name") or "")
    )
    title = " ".join(str(job.get("title") or "").casefold().split())

    return f"{company}|{title}"


def discover_ats_candidates(
    job: dict,
    client: httpx.Client,
) -> list[OfficialJobCandidate]:
    company_key = normalized_job_cache_key(job)

    if company_key == "|":
        return []

    cached = ATS_DISCOVERY_CACHE.get(company_key)

    if cached is not None:
        return list(cached)

    discovered: list[OfficialJobCandidate] = []

    for adapter in ADAPTERS:
        try:
            adapter_candidates = adapter.discover_candidates(
                job=job,
                client=client,
            )
        except Exception:
            continue

        discovered.extend(adapter_candidates)

        if discovered:
            break

    ATS_DISCOVERY_CACHE[company_key] = tuple(discovered[:MAX_ATS_RESULTS])

    return discovered[:MAX_ATS_RESULTS]


__all__ = [
    "ADAPTERS",
    "ATS_DISCOVERY_CACHE",
    "ATS_REQUEST_TIMEOUT",
    "MAX_ATS_PAGES",
    "MAX_ATS_RESULTS",
    "OfficialJobCandidate",
    "discover_ats_candidates",
]
