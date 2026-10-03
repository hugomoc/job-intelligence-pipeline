"""Best-effort Workday candidate discovery from static/embedded listing data."""

from __future__ import annotations

import json
import re

import httpx
from bs4 import BeautifulSoup

from src.enrichment.ats.base import (
    MAX_ATS_PAGES,
    MAX_ATS_RESULTS,
    OfficialJobCandidate,
    anchors_from_html,
    clean_text,
    company_slug_candidates,
    filter_and_rank_candidates,
    location_from_value,
    make_candidate,
    response_json,
    safe_get,
    walk_json,
)


SOURCE = "workday"


def workday_url_from_node(
    node: dict,
) -> str | None:
    return (
        node.get("externalPath")
        or node.get("url")
        or node.get("jobPostingUrl")
        or node.get("postingUrl")
    )


def workday_title_from_node(
    node: dict,
) -> str | None:
    return (
        clean_text(node.get("title"))
        or clean_text(node.get("titleWithoutTitleSuffix"))
        or clean_text(node.get("jobTitle"))
    )


def workday_location_from_node(
    node: dict,
) -> str | None:
    return (
        clean_text(node.get("locationsText"))
        or location_from_value(node.get("primaryLocation"))
        or location_from_value(node.get("location"))
    )


def parse_embedded_json_payloads(
    html_text: str,
) -> list[object]:
    soup = BeautifulSoup(
        html_text,
        "html.parser",
    )
    payloads: list[object] = []

    for script in soup.find_all("script"):
        raw_text = script.string or script.get_text(strip=True)

        if not raw_text:
            continue

        if script.get("type") == "application/json":
            try:
                payloads.append(json.loads(raw_text))
            except json.JSONDecodeError:
                pass

        for match in re.finditer(r"\{[^<]{80,}\}", raw_text):
            candidate = match.group(0)

            if "externalPath" not in candidate and "jobPosting" not in candidate:
                continue

            try:
                payloads.append(json.loads(candidate))
            except json.JSONDecodeError:
                continue

    return payloads


def parse_workday_jobs(
    payloads: list[object],
    base_url: str,
    company_name: str | None,
) -> list[OfficialJobCandidate]:
    candidates: list[OfficialJobCandidate] = []

    for payload in payloads:
        for node in walk_json(payload):
            title = workday_title_from_node(node)
            url = workday_url_from_node(node)

            if not title or not url:
                continue

            candidate = make_candidate(
                url=url,
                title=title,
                company=company_name,
                location=workday_location_from_node(node),
                source=SOURCE,
                base_url=base_url,
                job_id=(
                    node.get("jobPostingId")
                    or node.get("id")
                    or node.get("requisitionId")
                ),
            )

            if candidate:
                candidates.append(candidate)

            if len(candidates) >= MAX_ATS_RESULTS:
                break

        if len(candidates) >= MAX_ATS_RESULTS:
            break

    return candidates


def workday_listing_urls(
    company_name: str | None,
) -> list[str]:
    urls: list[str] = []

    for slug in company_slug_candidates(company_name):
        urls.extend(
            [
                f"https://{slug}.wd1.myworkdaysite.com/en-US/{slug}/jobs",
                f"https://{slug}.wd3.myworkdaysite.com/en-US/{slug}/jobs",
                f"https://{slug}.myworkdayjobs.com/{slug}",
            ]
        )

    return urls[:MAX_ATS_PAGES * 3]


def discover_candidates(
    job: dict,
    client: httpx.Client,
) -> list[OfficialJobCandidate]:
    company_name = str(job.get("company_name") or "")
    candidates: list[OfficialJobCandidate] = []

    for url in workday_listing_urls(company_name):
        response = safe_get(client, url)

        if response is None:
            continue

        payload = response_json(response)
        payloads = [
            payload,
        ] if payload is not None else parse_embedded_json_payloads(response.text)
        candidates.extend(
            parse_workday_jobs(
                payloads=payloads,
                base_url=url,
                company_name=company_name,
            )
        )

        if not candidates and response.text:
            candidates.extend(
                anchors_from_html(
                    html_text=response.text,
                    base_url=url,
                    source=SOURCE,
                    expected_company=company_name,
                )
            )

        if len(candidates) >= MAX_ATS_RESULTS:
            break

    return filter_and_rank_candidates(
        job=job,
        candidates=candidates[:MAX_ATS_RESULTS],
    )
