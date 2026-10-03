"""Thin iCIMS candidate discovery using static listings when available."""

from __future__ import annotations

from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from src.enrichment.ats.base import (
    OfficialJobCandidate,
    anchors_from_html,
    clean_text,
    company_slug_candidates,
    filter_and_rank_candidates,
    make_candidate,
    safe_get,
)


SOURCE = "icims"


def parse_icims_jobs_from_html(
    html_text: str,
    base_url: str,
    company_name: str | None,
) -> list[OfficialJobCandidate]:
    soup = BeautifulSoup(
        html_text,
        "html.parser",
    )
    candidates: list[OfficialJobCandidate] = []

    for element in soup.select("[data-job-title], [data-title]"):
        title = (
            element.get("data-job-title")
            or element.get("data-title")
            or element.get_text(" ", strip=True)
        )
        anchor = element.find("a", href=True)

        if anchor is None and element.name == "a" and element.has_attr("href"):
            anchor = element

        if anchor is None:
            continue

        parent_text = clean_text(
            element.get_text(" ", strip=True)
        )
        location = parent_text.replace(
            clean_text(title),
            " ",
        ).strip()
        candidate = make_candidate(
            url=anchor.get("href"),
            title=title,
            company=company_name,
            location=location,
            source=SOURCE,
            base_url=base_url,
        )

        if candidate:
            candidates.append(candidate)

    candidates.extend(
        anchors_from_html(
            html_text=html_text,
            base_url=base_url,
            source=SOURCE,
            expected_company=company_name,
        )
    )

    return candidates


def discover_candidates(
    job: dict,
    client: httpx.Client,
) -> list[OfficialJobCandidate]:
    company_name = str(job.get("company_name") or "")
    title = str(job.get("title") or "")
    candidates: list[OfficialJobCandidate] = []
    encoded_title = quote_plus(title)

    for slug in company_slug_candidates(company_name):
        search_urls = [
            (
                f"https://careers-{slug}.icims.com/jobs/search"
                f"?ss=1&searchKeyword={encoded_title}"
            ),
            (
                f"https://{slug}.icims.com/jobs/search"
                f"?ss=1&searchKeyword={encoded_title}"
            ),
        ]

        for search_url in search_urls:
            response = safe_get(client, search_url)

            if response is None:
                continue

            candidates.extend(
                parse_icims_jobs_from_html(
                    html_text=response.text,
                    base_url=search_url,
                    company_name=company_name,
                )
            )

            if candidates:
                break

        if candidates:
            break

    return filter_and_rank_candidates(
        job=job,
        candidates=candidates,
    )
