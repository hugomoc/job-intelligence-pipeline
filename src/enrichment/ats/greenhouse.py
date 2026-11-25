"""Thin Greenhouse job-board candidate discovery and public posting detail."""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import urlsplit

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
API_SOURCE = "greenhouse_job_board_api"
_POSTING_HOSTS = {"job-boards.greenhouse.io", "boards.greenhouse.io", "job-boards.eu.greenhouse.io",
                  "boards.eu.greenhouse.io"}
_API_HOSTS = {"boards-api.greenhouse.io": "boards-api.greenhouse.io",
              "boards-api.eu.greenhouse.io": "boards-api.eu.greenhouse.io"}


def greenhouse_posting_scope(url: str | None) -> tuple[str, str, str] | None:
    """(api_host, board slug, job id) for a Greenhouse-hosted posting URL."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return None
    host = (parts.hostname or "").casefold()
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        return None
    path = parts.path.strip("/")
    if host in _POSTING_HOSTS:
        match = re.fullmatch(r"([A-Za-z0-9_-]+)/jobs/(\d+)", path)
        api_host = "boards-api.eu.greenhouse.io" if ".eu." in host else "boards-api.greenhouse.io"
    elif host in _API_HOSTS:
        match = re.fullmatch(r"v1/boards/([A-Za-z0-9_-]+)/jobs/(\d+)", path)
        api_host = host
    else:
        return None
    return (api_host, match.group(1), match.group(2)) if match else None


def greenhouse_posting_url(board_slug: str, job_id: str) -> str:
    return f"https://job-boards.greenhouse.io/{board_slug}/jobs/{job_id}"


def fetch_job_board_api_posting(url: str, client: httpx.Client, *, timeout_seconds: float = 8.0):
    """Public Greenhouse Job Board API detail for the SAME posting as ``url``.

    The API URL is derived from the posting's own board and job id (same
    verified posting key), never from a search hint. Returns None when ``url``
    is not a Greenhouse posting. Identity is still validated by the caller.
    """
    from src.enrichment.job_description import (JobDescriptionResult, count_words,
                                                html_fragment_to_text)
    scope = greenhouse_posting_scope(url)
    if scope is None:
        return None
    api_host, slug, job_id = scope
    api_url = f"https://{api_host}/v1/boards/{slug}/jobs/{job_id}"

    def result(status, http_status=None, error=None, **fields):
        return JobDescriptionResult(requested_url=api_url, final_url=api_url, status=status,
                                    http_status=http_status, extraction_method=fields.pop("method", None),
                                    description=fields.pop("description", ""),
                                    word_count=fields.pop("word_count", 0), error_message=error, **fields)
    try:
        response = client.get(api_url, timeout=timeout_seconds, follow_redirects=False)
    except httpx.TimeoutException:
        return result("fetch_error", error="The request timed out.")
    except httpx.RequestError as error:
        return result("fetch_error", error=f"{type(error).__name__}: {error}")
    status = response.status_code
    if status in (401, 403, 429):
        return result("blocked", status, "The job site rejected or rate-limited the request.",
                      access_signal=f"http_{status}", retry_after=response.headers.get("retry-after") or None)
    if status != 200:
        return result("fetch_error", status, f"HTTP status {status}.")
    try:
        data = response.json()
    except ValueError:
        return result("no_description", status, "The response was not JSON.")
    if not isinstance(data, dict) or str(data.get("id") or "") != job_id:
        return result("no_description", status, "Job Board API response is not this posting.")
    text = html_fragment_to_text(html_lib.unescape(str(data.get("content") or "")))
    words = count_words(text)
    location = (data.get("location") or {}).get("name") if isinstance(data.get("location"), dict) else None
    company = str(data.get("company_name") or "").strip() or None
    return result(
        "enriched" if words else "no_description", status,
        None if words else "Job Board API returned no description.",
        method=API_SOURCE, description=text, word_count=words,
        resolved_title=str(data.get("title") or "").strip() or None,
        resolved_company=company, resolved_location=location, resolved_job_id=job_id,
        resolved_title_source=API_SOURCE, resolved_company_source=API_SOURCE if company else None,
        resolved_location_source=API_SOURCE if location else None, resolved_job_id_source=API_SOURCE,
    )


def job_from_greenhouse_payload(
    item: dict,
    board_slug: str,
    company_name: str | None,
) -> OfficialJobCandidate | None:
    job_id = item.get("id") or item.get("internal_job_id")
    url = item.get("absolute_url") or item.get("url")
    # Boards published on an employer domain return that mirror as
    # absolute_url (e.g. block.xyz/careers/jobs/1?gh_jid=1). A non-ATS mirror
    # cannot carry the Greenhouse posting key, so use the board's own posting.
    if item.get("id") and (not url or greenhouse_posting_scope(url) is None):
        url = greenhouse_posting_url(board_slug, str(item["id"]))

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
