"""Thin Ashby job-board candidate discovery."""

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
    walk_json,
)


SOURCE = "ashby"


def ashby_job_url(
    item: dict,
    board_slug: str,
) -> str | None:
    return (
        item.get("jobUrl")
        or item.get("url")
        or item.get("externalLink")
        or (
            f"https://jobs.ashbyhq.com/{board_slug}/{item.get('id')}"
            if item.get("id")
            else None
        )
    )


def ashby_location(item: dict) -> str | None:
    return (
        location_from_value(item.get("locationName"))
        or location_from_value(item.get("location"))
        or location_from_value(item.get("primaryLocation"))
    )


def parse_ashby_jobs(
    payload: object,
    board_slug: str,
    company_name: str | None,
) -> list[OfficialJobCandidate]:
    candidates: list[OfficialJobCandidate] = []

    for node in walk_json(payload):
        title = node.get("title")

        if not title:
            continue

        url = ashby_job_url(
            item=node,
            board_slug=board_slug,
        )

        candidate = make_candidate(
            url=url,
            title=title,
            company=company_name,
            location=ashby_location(node),
            source=SOURCE,
            job_id=node.get("id") or node.get("jobId"),
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
        api_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        response = safe_get(client, api_url)

        if response is None:
            continue

        candidates.extend(
            parse_ashby_jobs(
                payload=response_json(response),
                board_slug=slug,
                company_name=company_name,
            )
        )

        if candidates:
            break

    return filter_and_rank_candidates(
        job=job,
        candidates=candidates,
    )
