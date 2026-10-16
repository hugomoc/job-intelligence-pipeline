"""Best-effort Workday candidate discovery from static/embedded listing data."""

from __future__ import annotations

import json
import re
from urllib.parse import quote, urlparse

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


def discover_from_search_homepage(
    job: dict,
    client: httpx.Client,
    root_url: str,
) -> list[OfficialJobCandidate]:
    """Read a known Workday tenant's public CXS listing for exact job leads.

    A search result landing on a Workday homepage supplies only the tenant and
    public career-site slug. CXS posting titles/links are untrusted hints;
    final resolution still fetches and independently validates the posting.
    """
    parsed = urlparse(root_url)
    host = (parsed.hostname or "").casefold()
    if (parsed.scheme != "https" or not
            (host.endswith(".myworkdayjobs.com") or
             host.endswith(".myworkdaysite.com"))):
        return []
    tenant = host.split(".")[0]
    segments = [part for part in parsed.path.strip("/").split("/") if part]
    if segments and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segments[0]):
        segments = segments[1:]
    # The search hit must be a career-site root; a posting URL is already
    # handled by ordinary candidate resolution.
    if len(segments) != 1 or not tenant:
        return []
    site = segments[0]
    key = (host, site.casefold(), str(job.get("title") or "").strip().casefold())
    cache = getattr(client, "_workday_listing_cache", None)
    if cache is None:
        cache = {}
        client._workday_listing_cache = cache
    if key in cache:
        return list(cache[key])

    endpoint = (
        f"https://{host}/wday/cxs/{quote(tenant, safe='')}/"
        f"{quote(site, safe='')}/jobs"
    )
    try:
        response = client.post(
            endpoint,
            json={"appliedFacets": {}, "limit": 20, "offset": 0,
                  "searchText": str(job.get("title") or "")},
            timeout=8.0, follow_redirects=False,
        )
        if response.status_code != 200:
            return []
        payload = response.json()
    except (httpx.RequestError, ValueError):
        return []

    postings = payload.get("jobPostings") if isinstance(payload, dict) else None
    if not isinstance(postings, list):
        return []
    base = f"https://{host}/en-US/{site}"
    candidates = []
    for posting in postings[:20]:
        if not isinstance(posting, dict):
            continue
        path = posting.get("externalPath")
        if not isinstance(path, str) or not path.startswith("/job/"):
            continue
        candidate = make_candidate(
            url=base + path, title=posting.get("title"),
            company=job.get("company_name"),
            location=posting.get("locationsText"), source=SOURCE,
        )
        if candidate:
            candidates.append(candidate)
    ranked = filter_and_rank_candidates(job, candidates, max_results=5)
    cache[key] = tuple(ranked)
    return ranked
