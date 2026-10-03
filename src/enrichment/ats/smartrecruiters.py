"""Thin SmartRecruiters candidate discovery."""

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


SOURCE = "smartrecruiters"


def smartrecruiters_url(
    item: dict,
    company_slug: str,
) -> str | None:
    job_id = (
        item.get("id")
        or item.get("uuid")
        or item.get("ref")
        or item.get("jobAdId")
    )

    return (
        item.get("referralUrl")
        or item.get("applyUrl")
        or item.get("url")
        or (
            f"https://jobs.smartrecruiters.com/{company_slug}/{job_id}"
            if job_id
            else None
        )
    )


def smartrecruiters_location(item: dict) -> str | None:
    location = item.get("location")

    if isinstance(location, dict):
        pieces = [
            location.get("city"),
            location.get("region"),
            location.get("country"),
        ]

        return ", ".join(
            str(piece)
            for piece in pieces
            if piece
        ) or None

    return location_from_value(location)


def parse_smartrecruiters_jobs(
    payload: object,
    company_slug: str,
    company_name: str | None,
) -> list[OfficialJobCandidate]:
    if not isinstance(payload, dict):
        return []

    postings = payload.get("content") or payload.get("jobs")

    if not isinstance(postings, list):
        return []

    candidates: list[OfficialJobCandidate] = []

    for item in postings:
        if not isinstance(item, dict):
            continue

        job_id = (
            item.get("id")
            or item.get("uuid")
            or item.get("ref")
            or item.get("jobAdId")
        )
        candidate = make_candidate(
            url=smartrecruiters_url(
                item=item,
                company_slug=company_slug,
            ),
            title=item.get("name") or item.get("title"),
            company=company_name,
            location=smartrecruiters_location(item),
            source=SOURCE,
            job_id=job_id,
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
        api_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
        response = safe_get(client, api_url)

        if response is None:
            continue

        candidates.extend(
            parse_smartrecruiters_jobs(
                payload=response_json(response),
                company_slug=slug,
                company_name=company_name,
            )
        )

        if candidates:
            break

    return filter_and_rank_candidates(
        job=job,
        candidates=candidates,
    )
