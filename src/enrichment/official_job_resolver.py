"""Find verified employer/ATS postings when aggregator URLs are unusable.

The resolver is intentionally bounded and deterministic. It starts with a small
public search for the title/company, prefers known ATS or official company
career URLs, fetches only a handful of candidates, and accepts a URL only after
the existing job-identity validator confirms that it is the same posting.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from src.browser.playwright_client import PlaywrightClient
from src.enrichment.ats import (
    OfficialJobCandidate,
    discover_ats_candidates,
)
from src.enrichment.job_description import (
    JobDescriptionResult,
    extract_job_description_from_html,
    fetch_job_description,
    is_valid_http_url,
)
from src.enrichment.job_identity import (
    JobIdentityValidation,
    normalize_company_name,
    validate_job_identity,
)
from src.resolvers.job_matching import (
    score_candidate,
)


OFFICIAL_FOUND_VERIFIED = "FOUND_VERIFIED"
OFFICIAL_NOT_FOUND = "OFFICIAL_NOT_FOUND"
OFFICIAL_AMBIGUOUS = "AMBIGUOUS"
OFFICIAL_BLOCKED = "BLOCKED"
OFFICIAL_ERROR = "ERROR"

AGGREGATOR_SOURCES = {
    "bebee",
    "glassdoor",
    "jobleads",
    "joblookup",
    "jobright",
    "lensa",
}

AGGREGATOR_DOMAINS = {
    "bebee.com",
    "glassdoor.com",
    "jobleads.com",
    "joblookup.com",
    "jobright.ai",
    "lensa.com",
}

KNOWN_ATS_HOST_PARTS = {
    "ashbyhq.com": "ashby",
    "boards.greenhouse.io": "greenhouse",
    "careers-": "icims",
    "greenhouse.io": "greenhouse",
    "icims.com": "icims",
    "jobs.ashbyhq.com": "ashby",
    "jobs.lever.co": "lever",
    "jobs.smartrecruiters.com": "smartrecruiters",
    "lever.co": "lever",
    "myworkdayjobs.com": "workday",
    "smartrecruiters.com": "smartrecruiters",
    "wd1.myworkdaysite.com": "workday",
    "wd3.myworkdaysite.com": "workday",
    "workdayjobs.com": "workday",
}

CAREER_PATH_WORDS = {
    "career",
    "careers",
    "job",
    "jobs",
    "openings",
    "opportunities",
    "positions",
    "recruiting",
    "workday",
}

MAX_SEARCH_RESULTS = 12
MAX_CANDIDATE_FETCHES = 5
AMBIGUOUS_CONFIDENCE_MARGIN = 0.05
DYNAMIC_RENDER_WAIT_MS = 1_500


@dataclass(frozen=True)
class VerifiedOfficialJob:
    candidate: OfficialJobCandidate
    result: JobDescriptionResult
    validation: JobIdentityValidation


@dataclass(frozen=True)
class OfficialJobResolutionResult:
    status: str
    official_job_url: str | None
    official_url_source: str | None
    official_url_resolved_at: datetime
    official_url_confidence: float | None
    official_url_validation_reason: str | None
    resolved_title: str | None
    resolved_company: str | None
    resolved_location: str | None
    description_result: JobDescriptionResult | None = None
    identity_validation: JobIdentityValidation | None = None
    error_message: str | None = None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalized_source(value: object) -> str:
    return str(value or "").strip().casefold()


def hostname(value: str | None) -> str:
    if not value:
        return ""

    try:
        parsed = urlparse(value)
    except ValueError:
        return ""

    return parsed.netloc.casefold().removeprefix("www.")


def is_aggregator_domain(url: str | None) -> bool:
    host = hostname(url)

    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in AGGREGATOR_DOMAINS
    )


def is_aggregator_job(job: dict) -> bool:
    source = normalized_source(job.get("source"))

    return source in AGGREGATOR_SOURCES or is_aggregator_domain(
        str(job.get("apply_url") or "")
    )


def should_attempt_official_resolution(
    job: dict,
    enrichment_result: JobDescriptionResult,
    identity_validation: JobIdentityValidation | None,
) -> bool:
    """Decide whether an aggregator job needs official-site discovery."""
    if is_aggregator_job(job):
        return (
            str(job.get("official_url_status") or "").strip()
            != OFFICIAL_FOUND_VERIFIED
        )

    if (
        enrichment_result.status == "enriched"
        and identity_validation is not None
        and identity_validation.accepted
    ):
        return False

    if identity_validation is not None and not identity_validation.accepted:
        return True

    if enrichment_result.status != "enriched":
        return True

    if is_aggregator_domain(enrichment_result.final_url):
        return True

    return not (
        enrichment_result.resolved_title
        and enrichment_result.resolved_company
    )


def ats_type_for_url(url: str) -> str | None:
    host = hostname(url)

    for host_part, ats_type in KNOWN_ATS_HOST_PARTS.items():
        if host_part in host:
            return ats_type

    return None


def company_domain_tokens(company_name: str | None) -> set[str]:
    normalized = normalize_company_name(company_name)

    return {
        token
        for token in normalized.split()
        if len(token) >= 3
    }


def looks_like_official_or_ats_url(
    url: str,
    company_name: str | None,
) -> bool:
    if not is_valid_http_url(url):
        return False

    if is_aggregator_domain(url):
        return False

    if ats_type_for_url(url):
        return True

    parsed = urlparse(url)
    host = parsed.netloc.casefold().removeprefix("www.")
    path = parsed.path.casefold()
    company_tokens = company_domain_tokens(company_name)

    host_matches_company = any(
        token in host
        for token in company_tokens
    )
    path_suggests_careers = any(
        word in path
        for word in CAREER_PATH_WORDS
    )

    return host_matches_company and path_suggests_careers


def unwrap_search_url(url: str) -> str:
    parsed = urlparse(url)
    parameters = parse_qs(parsed.query)

    for key in ("uddg", "url", "u", "q"):
        values = parameters.get(key)

        if not values:
            continue

        candidate = unquote(values[0])

        if is_valid_http_url(candidate):
            return candidate

    return url


def extract_search_result_candidates(
    html: str,
    company_name: str | None,
) -> list[OfficialJobCandidate]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[OfficialJobCandidate] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        url = unwrap_search_url(urljoin("https://duckduckgo.com", anchor["href"]))

        if not looks_like_official_or_ats_url(url, company_name):
            continue

        if url in seen_urls:
            continue

        seen_urls.add(url)
        label = anchor.get_text(" ", strip=True)
        candidates.append(
            OfficialJobCandidate(
                url=url,
                label=label,
                source=ats_type_for_url(url) or "search",
                title=label,
                company=company_name,
            )
        )

        if len(candidates) >= MAX_SEARCH_RESULTS:
            break

    return candidates


def search_candidates(
    job: dict,
    client: httpx.Client,
) -> list[OfficialJobCandidate]:
    title = str(job.get("title") or "").strip()
    company = str(job.get("company_name") or "").strip()

    if not title or not company:
        return []

    query = quote_plus(f'"{title}" "{company}" careers jobs')
    search_url = f"https://duckduckgo.com/html/?q={query}"

    response = client.get(search_url)

    if response.status_code in {401, 403, 429}:
        raise PermissionError(f"search blocked with HTTP {response.status_code}")

    if response.status_code >= 400:
        raise RuntimeError(f"search failed with HTTP {response.status_code}")

    return extract_search_result_candidates(
        response.text,
        company_name=company,
    )


async def fetch_dynamic_job_description_async(
    url: str,
) -> JobDescriptionResult:
    async with PlaywrightClient(timeout_ms=20_000) as browser:
        page = await browser.new_page()
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=30_000,
        )

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=8_000,
            )
        except Exception:
            pass

        await page.wait_for_timeout(
            DYNAMIC_RENDER_WAIT_MS
        )

        html = await page.content()
        http_status = response.status if response else None

        return extract_job_description_from_html(
            requested_url=url,
            final_url=page.url,
            http_status=http_status,
            html_text=html,
            extraction_method_prefix="playwright",
        )


def fetch_dynamic_job_description(
    url: str,
) -> JobDescriptionResult | None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return None

    try:
        return asyncio.run(
            fetch_dynamic_job_description_async(url)
        )
    except Exception:
        return None


def fetch_candidate_description(
    candidate: OfficialJobCandidate,
    client: httpx.Client,
) -> JobDescriptionResult:
    result = fetch_job_description(candidate.url, client)

    if result.status != "no_description":
        return result

    dynamic_result = fetch_dynamic_job_description(
        candidate.url
    )

    if dynamic_result is None:
        return result

    if dynamic_result.status == "enriched":
        return dynamic_result

    if dynamic_result.word_count > result.word_count:
        return dynamic_result

    return result


def candidate_order_key(
    job: dict,
    candidate: OfficialJobCandidate,
) -> tuple[int, int]:
    expected_title = str(job.get("title") or "")
    expected_company = str(job.get("company_name") or "")
    match = score_candidate(
        expected_title=expected_title,
        expected_company=expected_company,
        candidate_title=candidate.title or candidate.label or candidate.url,
        candidate_company=candidate.company or expected_company,
    )

    ats_bonus = 10 if ats_type_for_url(candidate.url) else 0
    metadata_bonus = sum(
        1
        for value in (
            candidate.title,
            candidate.company,
            candidate.location,
            candidate.job_id,
        )
        if value
    )

    return (
        match.title_score + ats_bonus,
        metadata_bonus,
    )


def ranked_candidates(
    job: dict,
    candidates: Iterable[OfficialJobCandidate],
) -> list[OfficialJobCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: candidate_order_key(job, candidate),
        reverse=True,
    )


def validate_candidate_result(
    job: dict,
    candidate: OfficialJobCandidate,
    result: JobDescriptionResult,
) -> VerifiedOfficialJob | None:
    if result.status != "enriched":
        return None

    validation = validate_job_identity(
        original_title=job.get("title"),
        original_company=job.get("company_name"),
        original_location=job.get("location"),
        resolved_title=result.resolved_title or candidate.title,
        resolved_company=result.resolved_company or candidate.company,
        resolved_location=result.resolved_location or candidate.location,
    )

    if not validation.accepted:
        return None

    return VerifiedOfficialJob(
        candidate=candidate,
        result=result,
        validation=validation,
    )


def choose_verified_candidate(
    verified_jobs: list[VerifiedOfficialJob],
) -> OfficialJobResolutionResult:
    ranked = sorted(
        verified_jobs,
        key=lambda item: (
            item.validation.confidence,
            item.result.word_count,
        ),
        reverse=True,
    )
    best = ranked[0]

    if len(ranked) > 1:
        second = ranked[1]
        if (
            best.validation.confidence - second.validation.confidence
            < AMBIGUOUS_CONFIDENCE_MARGIN
        ):
            return OfficialJobResolutionResult(
                status=OFFICIAL_AMBIGUOUS,
                official_job_url=None,
                official_url_source=best.candidate.source,
                official_url_resolved_at=now_utc(),
                official_url_confidence=best.validation.confidence,
                official_url_validation_reason=(
                    "multiple verified official candidates were too close "
                    "to choose safely"
                ),
                resolved_title=best.result.resolved_title,
                resolved_company=best.result.resolved_company or best.candidate.company,
                resolved_location=best.result.resolved_location or best.candidate.location,
            )

    return OfficialJobResolutionResult(
        status=OFFICIAL_FOUND_VERIFIED,
        official_job_url=best.result.final_url or best.candidate.url,
        official_url_source=best.candidate.source,
        official_url_resolved_at=now_utc(),
        official_url_confidence=best.validation.confidence,
        official_url_validation_reason=best.validation.reason,
        resolved_title=best.result.resolved_title or best.candidate.title,
        resolved_company=best.result.resolved_company or best.candidate.company,
        resolved_location=best.result.resolved_location or best.candidate.location,
        description_result=best.result,
        identity_validation=best.validation,
    )


def resolve_from_candidates(
    job: dict,
    client: httpx.Client,
    candidates: Iterable[OfficialJobCandidate],
    source: str,
) -> OfficialJobResolutionResult:
    ranked = ranked_candidates(
        job,
        candidates,
    )[:MAX_CANDIDATE_FETCHES]

    if not ranked:
        return OfficialJobResolutionResult(
            status=OFFICIAL_NOT_FOUND,
            official_job_url=None,
            official_url_source=source,
            official_url_resolved_at=now_utc(),
            official_url_confidence=None,
            official_url_validation_reason="no official or known ATS candidates found",
            resolved_title=None,
            resolved_company=None,
            resolved_location=None,
        )

    verified_jobs: list[VerifiedOfficialJob] = []
    blocked_count = 0
    last_rejection_reason: str | None = None

    for candidate in ranked:
        result = fetch_candidate_description(candidate, client)

        if result.status == "blocked":
            blocked_count += 1
            continue

        verified = validate_candidate_result(
            job=job,
            candidate=candidate,
            result=result,
        )

        if verified is None:
            if result.resolved_title or result.resolved_company:
                validation = validate_job_identity(
                    original_title=job.get("title"),
                    original_company=job.get("company_name"),
                    original_location=job.get("location"),
                    resolved_title=result.resolved_title or candidate.title,
                    resolved_company=result.resolved_company or candidate.company,
                    resolved_location=result.resolved_location or candidate.location,
                )
                last_rejection_reason = validation.reason
            continue

        verified_jobs.append(verified)

    if verified_jobs:
        return choose_verified_candidate(verified_jobs)

    if blocked_count == len(ranked):
        return OfficialJobResolutionResult(
            status=OFFICIAL_BLOCKED,
            official_job_url=None,
            official_url_source=source,
            official_url_resolved_at=now_utc(),
            official_url_confidence=None,
            official_url_validation_reason="all official candidates were blocked",
            resolved_title=None,
            resolved_company=None,
            resolved_location=None,
        )

    return OfficialJobResolutionResult(
        status=OFFICIAL_NOT_FOUND,
        official_job_url=None,
        official_url_source=source,
        official_url_resolved_at=now_utc(),
        official_url_confidence=None,
        official_url_validation_reason=(
            last_rejection_reason
            or "official candidates did not validate as the same job"
        ),
        resolved_title=None,
        resolved_company=None,
        resolved_location=None,
    )


def resolve_official_job(
    job: dict,
    client: httpx.Client,
) -> OfficialJobResolutionResult:
    """Find and verify an official employer/ATS posting for an aggregator job."""
    try:
        ats_candidates = discover_ats_candidates(
            job=job,
            client=client,
        )
    except Exception:
        ats_candidates = []

    if ats_candidates:
        ats_result = resolve_from_candidates(
            job=job,
            client=client,
            candidates=ats_candidates,
            source="ats",
        )

        if ats_result.status in {
            OFFICIAL_FOUND_VERIFIED,
            OFFICIAL_AMBIGUOUS,
        }:
            return ats_result

    try:
        candidates = search_candidates(job, client)
    except PermissionError as error:
        return OfficialJobResolutionResult(
            status=OFFICIAL_BLOCKED,
            official_job_url=None,
            official_url_source="search",
            official_url_resolved_at=now_utc(),
            official_url_confidence=None,
            official_url_validation_reason=str(error),
            resolved_title=None,
            resolved_company=None,
            resolved_location=None,
            error_message=str(error),
        )
    except Exception as error:
        return OfficialJobResolutionResult(
            status=OFFICIAL_ERROR,
            official_job_url=None,
            official_url_source="search",
            official_url_resolved_at=now_utc(),
            official_url_confidence=None,
            official_url_validation_reason=str(error),
            resolved_title=None,
            resolved_company=None,
            resolved_location=None,
            error_message=f"{type(error).__name__}: {error}",
        )

    return resolve_from_candidates(
        job=job,
        client=client,
        candidates=candidates,
        source="search",
    )
