"""Thin Lever candidate discovery using the public postings endpoint."""

from __future__ import annotations

import httpx

from src.enrichment.ats.base import (
    OfficialJobCandidate,
    company_slug_candidates,
    filter_and_rank_candidates,
    location_from_value,
    make_candidate,
    response_json,
    safe_get,
)


SOURCE = "lever"


def lever_location(item: dict) -> str | None:
    categories = item.get("categories")

    if isinstance(categories, dict):
        return location_from_value(categories.get("location"))

    return location_from_value(item.get("location"))


def parse_lever_jobs(
    payload: object,
    company_name: str | None,
) -> list[OfficialJobCandidate]:
    if not isinstance(payload, list):
        return []

    candidates: list[OfficialJobCandidate] = []

    for item in payload:
        if not isinstance(item, dict):
            continue

        candidate = make_candidate(
            url=item.get("hostedUrl") or item.get("applyUrl") or item.get("url"),
            title=item.get("text") or item.get("title"),
            company=company_name,
            location=lever_location(item),
            source=SOURCE,
            job_id=item.get("id") or item.get("lever_id"),
        )

        if candidate:
            candidates.append(candidate)

    return candidates


def discover_candidates(
    job: dict,
    client: httpx.Client,
) -> list[OfficialJobCandidate]:
    company_name = str(job.get("company_name") or "")
    candidates: list[OfficialJobCandidate] = []

    for slug in company_slug_candidates(company_name):
        api_url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        response = safe_get(client, api_url)

        if response is None:
            continue

        candidates.extend(
            parse_lever_jobs(
                payload=response_json(response),
                company_name=company_name,
            )
        )

        if candidates:
            break

    return filter_and_rank_candidates(
        job=job,
        candidates=candidates,
    )
