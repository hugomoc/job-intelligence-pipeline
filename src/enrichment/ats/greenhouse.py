"""Thin Greenhouse job-board candidate discovery."""

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


SOURCE = "greenhouse"


def job_from_greenhouse_payload(
    item: dict,
    board_slug: str,
    company_name: str | None,
) -> OfficialJobCandidate | None:
    job_id = item.get("id") or item.get("internal_job_id")
    url = (
        item.get("absolute_url")
        or item.get("url")
        or (
            f"https://boards.greenhouse.io/{board_slug}/jobs/{job_id}"
            if job_id
            else None
        )
    )

    return make_candidate(
        url=url,
        title=item.get("title"),
        company=company_name,
        location=location_from_value(item.get("location")),
        source=SOURCE,
        job_id=str(job_id) if job_id else None,
    )


def parse_greenhouse_jobs(
    payload: object,
    board_slug: str,
    company_name: str | None,
) -> list[OfficialJobCandidate]:
    if not isinstance(payload, dict):
        return []

    jobs = payload.get("jobs")

    if not isinstance(jobs, list):
        return []

    return [
        candidate
        for item in jobs
        if isinstance(item, dict)
        for candidate in [
            job_from_greenhouse_payload(
                item=item,
                board_slug=board_slug,
                company_name=company_name,
            )
        ]
        if candidate
    ]


def discover_candidates(
    job: dict,
    client: httpx.Client,
) -> list[OfficialJobCandidate]:
    company_name = str(job.get("company_name") or "")
    candidates: list[OfficialJobCandidate] = []

    for board_slug in company_slug_candidates(company_name):
        api_url = (
            "https://boards-api.greenhouse.io/v1/boards/"
            f"{board_slug}/jobs?content=true"
        )
        response = safe_get(client, api_url)

        if response is None:
            continue

        candidates.extend(
            parse_greenhouse_jobs(
                payload=response_json(response),
                board_slug=board_slug,
                company_name=company_name,
            )
        )

        if candidates:
            break

    return filter_and_rank_candidates(
        job=job,
        candidates=candidates,
    )
