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

    # Employer-first discovery has already queried the published recruitment
    # path. Once its platform is established, guessing unrelated ATS tenants is
    # redundant; preserve public-search fallback for missing positions.
    from src.enrichment.employer_careers import CAREERS_CACHE, CACHE_TTL_SECONDS
    from src.enrichment.job_identity import normalize_company_name
    import time
    known = CAREERS_CACHE.get(normalize_company_name(job.get('company_name')))
    if known and time.monotonic() - known[0] < CACHE_TTL_SECONDS and known[1].get('platforms'):
        job.setdefault('employer_discovery_trace', []).append(dict(
            reason='unrelated_ats_guesses_skipped_verified_platform',
            platforms=list(known[1]['platforms'])))
        return []

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

    # An unsuccessful probe is not a durable statement about an employer's
    # inventory. In particular, do not cache transient blocking as no jobs.
    if discovered:
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
