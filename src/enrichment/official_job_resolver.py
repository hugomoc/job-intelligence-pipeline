"""Find verified employer/ATS postings when source URLs are unusable.

The resolver is intentionally bounded and deterministic. It starts with a small
public search for the title/company, prefers known ATS or official company
career URLs, fetches only a handful of candidates, and accepts a URL only after
the existing job-identity validator confirms that it is the same posting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from src.enrichment.public_search import (
    PublicSearchError, selected_provider, available_providers, serper_search, tavily_search, metric,
    cached_response, reserve_search_request, SearchBudgetExhausted,
)
from src.browser.playwright_client import PlaywrightClient
from src.enrichment.ats import (
    OfficialJobCandidate,
    discover_ats_candidates,
    workday,
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
OFFICIAL_INSUFFICIENT_IDENTITY = "INSUFFICIENT_IDENTITY"
OFFICIAL_AMBIGUOUS = "AMBIGUOUS"
OFFICIAL_BLOCKED = "BLOCKED"
OFFICIAL_ERROR = "ERROR"
OFFICIAL_SEARCH_DEFERRED = "SEARCH_DEFERRED"


class SearchDeferredError(RuntimeError):
    pass


class PublicSearchCircuit:
    """Try Tavily, then Serper; independently disable unhealthy providers."""
    def __init__(self, threshold: int = 5):
        self.threshold = threshold
        self.states = {}
        self.calls = 0
        self.errors = 0
        self.deferred = 0
        self.budget_deferred = 0

    def state(self, provider=None):
        return self.states.setdefault(provider or selected_provider(),
                                     {"open": False, "consecutive_errors": 0, "calls": 0, "errors": 0})

    @property
    def open(self):
        configured = available_providers()
        return not configured or all(self.state(provider)["open"] for provider in configured)

    @open.setter
    def open(self, value):
        self.state()["open"] = value

    def search(self, job, client):
        attempted = False
        last_error = None
        for provider in available_providers():
            state = self.state(provider)
            if state["open"]:
                continue
            attempted = True
            self.calls += 1
            state["calls"] += 1
            try:
                candidates = search_candidates(job, client, provider=provider)
            except SearchBudgetExhausted:
                self.deferred += 1
                self.budget_deferred += 1
                raise SearchDeferredError("public search deferred: request budget exhausted") from None
            except Exception as error:
                self.errors += 1
                state["errors"] += 1
                state["consecutive_errors"] += 1
                state["open"] = (getattr(error, "stop_provider", False)
                                 or state["consecutive_errors"] >= self.threshold)
                if not isinstance(error, PublicSearchError):
                    raise
                last_error = error
                continue
            state["consecutive_errors"] = 0
            return candidates
        if not attempted:
            self.deferred += 1
            raise SearchDeferredError("public search deferred: no available configured providers")
        # No provider succeeded. Avoid masking an actual provider error.
        if last_error is not None:
            raise last_error
        self.deferred += 1
        raise SearchDeferredError("public search deferred: all providers unavailable")

    def summary(self):
        return {"opened": self.open, "calls_attempted": self.calls,
                "errors": self.errors, "jobs_deferred": self.deferred}


from src.job_domains import THIRD_PARTY_DOMAINS

AGGREGATOR_SOURCES = {
    "builtin",
    "bebee",
    "glassdoor",
    "jobleads",
    "joblookup",
    "jobright",
    "lensa",
}

AGGREGATOR_DOMAINS = THIRD_PARTY_DOMAINS

# Domains controlled by each ATS. Matches require a hostname label boundary.
KNOWN_ATS_DOMAINS = {
    "breezy.hr": "breezy",
    "pinpointhq.com": "pinpoint",
    # Only Rippling's hosted job-board host, not its corporate rippling.com site.
    "ats.rippling.com": "rippling",
    "ashbyhq.com": "ashby",
    "greenhouse.io": "greenhouse",
    "icims.com": "icims",
    "lever.co": "lever",
    "smartrecruiters.com": "smartrecruiters",
    "myworkdayjobs.com": "workday",
    "myworkdaysite.com": "workday",
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
OFFICIAL_RESOLUTION_BUDGET_SECONDS = 60.0
CANDIDATE_HTTP_TIMEOUT_SECONDS = 8.0
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
    posting_identity_evidence: dict | None = None
    employer_provenance: dict | None = None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalized_source(value: object) -> str:
    return str(value or "").strip().casefold()


def hostname(value: str | None) -> str:
    if not value:
        return ""

    try:
        return (urlparse(value).hostname or "").casefold().removeprefix("www.").rstrip(".")
    except ValueError:
        return ""


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
    """Decide whether a job needs official-site discovery.

    Source type is a strategy signal, not an eligibility gate: any source with
    a missing, blocked, rejected, or incomplete description can fall back to an
    official employer/ATS search. Aggregator sources still keep their stricter
    preference for verified official URLs.
    """
    official_status = str(
        job.get("official_url_status") or ""
    ).strip()

    # Explicit daily selection separates missing identity from JD completeness.
    # False also prevents a fresh source fetch from bypassing an official cooldown.
    if job.get("needs_official_resolution") is False:
        return False
    if job.get("needs_official_resolution") and job.get("requires_trusted_posting_key"):
        return True

    if official_status == OFFICIAL_FOUND_VERIFIED:
        return False

    if (
        enrichment_result.status == "enriched"
        and identity_validation is not None
        and identity_validation.accepted
    ):
        return (
            is_aggregator_job(job)
            or is_aggregator_domain(enrichment_result.final_url)
            or not (
                enrichment_result.resolved_title
                and enrichment_result.resolved_company
            )
        )

    if identity_validation is not None and not identity_validation.accepted:
        return True

    if is_aggregator_job(job):
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

    host = host.rstrip(".")
    for domain, ats_type in KNOWN_ATS_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return ats_type

    return None


def company_domain_tokens(company_name: str | None) -> set[str]:
    normalized = normalize_company_name(company_name)

    return {
        token
        for token in normalized.split()
        if len(token) >= 3
    }


_LOCALE_SEGMENT = re.compile(r"[a-z]{2}(?:[-_][a-z]{2})?")


def _is_locale_career_job_detail(segments: list[str]) -> bool:
    """/<locale>/careers/JobDetail/[<slug>/]<numeric id>, e.g. en_US or en-US.

    Narrow: the locale, careers root, JobDetail marker and numeric requisition
    ID are all required, so locale-prefixed home/search pages stay rejected.
    """
    return (len(segments) in {4, 5}
            and _LOCALE_SEGMENT.fullmatch(segments[0]) is not None
            and segments[1] in {"career", "careers"}
            and segments[2] == "jobdetail"
            and re.fullmatch(r"[0-9]+", segments[-1]) is not None)


def looks_like_individual_posting_url(url: str) -> bool:
    """Require an individual job URL, not an ATS root or generic career page.

    This is discovery filtering only, not independent identity evidence.
    """
    parsed = urlparse(url)
    segments = [part.casefold() for part in parsed.path.strip("/").split("/") if part]
    if not segments:
        return False
    path = parsed.path.casefold()
    # Corporate careers navigation is not an individual requisition, even
    # when its final slug contains words such as "join our team".
    if any(part in {"join-our-team", "join-us", "work-with-us",
                    "our-team", "life-at", "career-opportunities"}
           for part in segments):
        return False
    if (
        re.search(r"/jobs?/(?:q[-_/]|search|category|categories|browse|filter)", path)
        or any(part in {"job-search", "search-jobs", "jobsearch", "job-listings",
                        "all-jobs", "search-results"} for part in segments)
    ):
        return False

    ats = ats_type_for_url(url)
    if ats == 'breezy':
        from src.verified_posting_identity import breezy_posting_scope
        return breezy_posting_scope(url) is not None
    if ats in {'pinpoint', 'rippling'}:
        from src.verified_posting_identity import hosted_tenant_posting_scope
        return hosted_tenant_posting_scope(url) is not None
    if ats in {"workday", "greenhouse", "icims"}:
        return any(part in {"job", "jobs", "job-detail", "details"}
                   and i + 1 < len(segments) for i, part in enumerate(segments))
    if ats in {"lever", "ashby", "smartrecruiters"}:
        return len(segments) >= 2 and segments[-1] not in {
            "jobs", "job", "careers", "openings", "search"
        }

    if re.search(r"/(?:job|jobs|job-detail|requisition|posting|positions?)/[^/]+", path):
        return True
    if _is_locale_career_job_detail(segments):
        return True
    # Support employer /careers/specific-opening paths, not /about/careers.
    return (len(segments) >= 2 and segments[0] in {"career", "careers"}
            and segments[-1] not in {
                "jobs", "openings", "teams", "digital", "engineering", "all"
            })


def discovery_url_decision(url: str, company_name: str | None) -> tuple[bool, str]:
    """Explain the existing discovery gate; acceptance/verification is unchanged."""
    if not is_valid_http_url(url):
        return False, "invalid_http_url"
    if is_aggregator_domain(url):
        return False, "third_party_domain"
    host = hostname(url)
    if host == "dice.com" or host.endswith(".dice.com"):
        return False, "third_party_domain"
    if not looks_like_individual_posting_url(url):
        return False, "unsupported_posting_path"
    if ats_type_for_url(url):
        return True, "known_ats_discovery_only"
    if any(token in host for token in company_domain_tokens(company_name)):
        return True, "employer_hostname_token_discovery_only"
    return False, "unrecognized_ats_or_employer_domain"


def looks_like_official_or_ats_url(url: str, company_name: str | None) -> bool:
    return discovery_url_decision(url, company_name)[0]


def record_search_diagnostic(job, *, provider, query, urls=(), selected_urls=(),
                             result_count=0, cache_hit=False, error_category=None):
    """Safe structured discovery trace, never identity evidence or DB content.

    Batch processing copies these events into its existing operational log;
    direct callers can also capture the logger. No headers or response bodies.
    """
    from src.enrich_jobs import safe_enrichment_log_message, safe_posting_url_diagnostic
    selected = set(selected_urls)
    seen = set()
    decisions = []
    for url in urls:
        accepted, reason = discovery_url_decision(url, job.get('company_name'))
        if url in seen:
            reason = 'duplicate_result'
        elif accepted and url not in selected:
            reason = 'candidate_limit'
        elif (not accepted and ats_type_for_url(url) == 'workday'
              and not looks_like_individual_posting_url(url)):
            reason = 'workday_listing_lead_not_individual_posting'
        # Decode reflected credentials before the existing URL/log redactors.
        decoded = str(url or '')
        for _ in range(3):
            decoded = unquote(decoded)
        decisions.append(dict(url=safe_posting_url_diagnostic(decoded),
                              reason=reason, selected_for_verification=url in selected))
        seen.add(url)
    event = dict(provider=provider, query=safe_enrichment_log_message(query),
                 search_results=result_count, cache_hit=cache_hit,
                 error_category=error_category, returned_urls=decisions)
    job.setdefault('public_search_diagnostics', []).append(event)
    logging.getLogger(__name__).info('Employer search diagnostic: %s', json.dumps(event))
    # CLI/server logs retain diagnostics even when a caller discards the job
    # object or has not enabled INFO logging. No raw URL/header/body is printed.
    print('Employer search diagnostic: ' + json.dumps({k: v for k, v in event.items() if k != 'returned_urls'}))
    for decision in decisions:
        print('Employer search URL decision: ' + json.dumps(decision))
    return event


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


def duckduckgo_candidates(
    job: dict,
    client: httpx.Client,
) -> list[OfficialJobCandidate]:
    title = str(job.get("title") or "").strip()
    company = str(job.get("company_name") or "").strip()

    if not title or not company:
        return []

    query = quote_plus(f'"{title}" "{company}" careers jobs')
    search_url = f"https://duckduckgo.com/html/?q={query}"

    reserve_search_request(client, "duckduckgo")
    metric(client, "duckduckgo", "search_requests")
    response = client.get(search_url, follow_redirects=False)

    if response.status_code in {401, 403, 429}:
        raise PermissionError(f"search blocked with HTTP {response.status_code}")

    if response.status_code >= 300:
        raise RuntimeError(f"search failed with HTTP {response.status_code}")

    if any(marker in response.text.lower() for marker in (
        'id="challenge-form"', 'class="anomaly-modal', 'bots use duckduckgo too'
    )):
        raise PermissionError("public search returned a bot challenge")

    metric(client, "duckduckgo", "successful_responses")
    if "no-results" in response.text:
        metric(client, "duckduckgo", "no_result_searches")
    job["public_search_trace"] = {"provider": "duckduckgo", "healthy": bool(
        BeautifulSoup(response.text, "html.parser").select(".result__a, .no-results, .no-results__message"))}
    candidates = extract_search_result_candidates(response.text, company_name=company)
    soup = BeautifulSoup(response.text, "html.parser")
    anchors = soup.select(".result__a") or soup.find_all("a", href=True)
    returned_urls = [unwrap_search_url(urljoin("https://duckduckgo.com", a.get("href", ""))) for a in anchors]
    record_search_diagnostic(job, provider="duckduckgo",
        query=f'"{title}" "{company}" careers jobs', urls=returned_urls,
        selected_urls=[c.url for c in candidates], result_count=len(returned_urls))
    return candidates


def search_candidates(
    job: dict, client: httpx.Client, provider: str | None = None
) -> list[OfficialJobCandidate]:
    """Only official/ATS URLs become untrusted candidates, never inferred identity.

    Direct callers use Tavily then Serper. A shared circuit supplies one provider
    at a time and prevents repeated attempts against unavailable providers.
    """
    if provider is None:
        previous_error = None
        for name in available_providers():
            try:
                return search_candidates(job, client, provider=name)
            except SearchBudgetExhausted:
                raise
            except PublicSearchError as error:
                previous_error = error
        if previous_error:
            raise previous_error
        raise SearchDeferredError("public search unavailable: configure TAVILY_API_KEY or SERPER_API_KEY")

    trace = job.get("public_search_trace") or {}
    attempted = [*trace.get("attempted_providers", []), provider]
    job["public_search_trace"] = {
        "provider": provider, "attempted_providers": attempted,
        "healthy": False, "candidates": 0,
    }
    title = str(job.get("title") or "").strip()
    company = str(job.get("company_name") or "").strip()
    if not title or not company:
        return []
    if provider == "duckduckgo":
        candidates = duckduckgo_candidates(job, client)
    elif provider in {"serper", "tavily"}:
        # Strip alert-card separators only; never rewrite stored identity.
        query_title = re.sub(r"\s*\|\s*$", "", title).strip() or title
        query = f'{company} {query_title} jobs'
        job["public_search_trace"]["query"] = query
        job["public_search_trace"]["cache_hit"] = cached_response(
            client, provider, query
        ) is not None
        search_started = time.monotonic()
        try:
            response = (
                tavily_search(query, client) if provider == "tavily"
                else serper_search(query, client)
            )
        except PublicSearchError as error:
            job["public_search_trace"]["error_category"] = error.category
            record_search_diagnostic(job, provider=provider, query=query,
                                     error_category=error.category)
            raise
        finally:
            timing = job.setdefault("enrichment_timing", {})
            timing["public_search_ms"] = round(
                timing.get("public_search_ms", 0)
                + 1000 * (time.monotonic() - search_started), 1
            )
        # Search titles are untrusted ranking hints only; fetched posting
        # metadata is still required by validate_candidate_result().
        hints = {hit["url"]: str(hit.get("title") or "") for hit in response.untrusted_results}
        candidates = [
            OfficialJobCandidate(
                url=url, label=hints.get(url, ""),
                source=ats_type_for_url(url) or "search"
            )
            for url in response.urls if looks_like_official_or_ats_url(url, company)
        ][:MAX_SEARCH_RESULTS]
        audit = []
        for hit in response.untrusted_results:
            url = hit["url"]
            eligible = looks_like_official_or_ats_url(url, company)
            audit.append({"title": hit["title"], "domain": hostname(url), "url": url,
                          "candidate": eligible,
                          "discovery_reason": "candidate-only; requires fetched posting identity" if eligible
                          else "non-official/non-ATS domain or no employer careers/job path"})
        job["public_search_trace"].update(
            healthy=True, search_results=response.result_count,
            organic_results=audit,
        )
        record_search_diagnostic(job, provider=provider, query=query, urls=response.urls,
            selected_urls=[c.url for c in candidates], result_count=response.result_count,
            cache_hit=job['public_search_trace']['cache_hit'])
    else:
        raise ValueError("Unknown configured search provider")
    job["public_search_trace"]["candidates"] = len(candidates)
    return candidates


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
    *,
    require_full_jd: bool = False,
) -> JobDescriptionResult:
    result = fetch_job_description(
        candidate.url, client, timeout_seconds=CANDIDATE_HTTP_TIMEOUT_SECONDS
    )

    incomplete = False
    if require_full_jd and result.status == "enriched":
        from src.repositories.recommendation_repository import (
            MINIMUM_FULL_DESCRIPTION_WORDS, description_has_quality_signals,
        )
        incomplete = (result.word_count < MINIMUM_FULL_DESCRIPTION_WORDS
                      or not description_has_quality_signals(result.description))
    if result.status != "no_description" and not incomplete:
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


def candidate_specific_posting_key(
    job: dict, candidate: OfficialJobCandidate, result: JobDescriptionResult,
) -> str:
    """Use the persistence URL policy before claiming verified resolution.

    Discovery source labels and matching fetched metadata cannot establish a
    posting-specific URL or upgrade search provenance to employer provenance.
    Check the final URL, so redirects cannot borrow the candidate URL's ID.
    """
    from src.enrichment.verified_identity_recovery import supported_specific_posting_key
    from src.verified_posting_identity import fetched_posting_scope_valid
    if not fetched_posting_scope_valid(result.final_url or candidate.url, result, candidate.employer_provenance):
        return ''
    from src.enrich_jobs import official_status_value, stored_verified_key_is_trusted
    allow_employer = candidate.source == "employer" or (
        official_status_value(job) == OFFICIAL_FOUND_VERIFIED and stored_verified_key_is_trusted(job)
    )
    if candidate.employer_provenance:
        from src.verified_posting_identity import posting_urls_equivalent
        if not posting_urls_equivalent(candidate.url, result.final_url or candidate.url, result.resolved_job_id):
            return ''
    return supported_specific_posting_key(
        result.final_url or candidate.url, allow_employer=allow_employer,
    )


def validate_candidate_result(
    job: dict,
    candidate: OfficialJobCandidate,
    result: JobDescriptionResult,
) -> VerifiedOfficialJob | None:
    if result.status != "enriched" or not candidate_specific_posting_key(job, candidate, result):
        return None

    from src.enrichment.job_identity import compare_posting_evidence
    if compare_posting_evidence(job, result)['requisition_conflict']:
        return None
    from src.verified_posting_identity import posting_urls_equivalent
    if result.canonical_url and not posting_urls_equivalent(
            result.final_url or candidate.url, result.canonical_url, result.resolved_job_id):
        return None

    # Candidate fields are discovery/ranking hints, including expected-company
    # values supplied by ATS adapters. Only fetched posting metadata is evidence.
    from src.enrich_jobs import trusted_identity_proof
    validation = validate_job_identity(
        original_title=job.get("title"),
        original_company=job.get("company_name"),
        original_location=job.get("location"),
        resolved_title=result.resolved_title,
        resolved_company=result.resolved_company,
        resolved_location=result.resolved_location,
        resolved_location_source=result.resolved_location_source,
        **trusted_identity_proof(job, result),
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
                resolved_company=best.result.resolved_company,
                resolved_location=best.result.resolved_location,
            )

    return OfficialJobResolutionResult(
        status=OFFICIAL_FOUND_VERIFIED,
        official_job_url=best.result.final_url or best.candidate.url,
        official_url_source=best.candidate.source,
        official_url_resolved_at=now_utc(),
        official_url_confidence=best.validation.confidence,
        official_url_validation_reason=best.validation.reason,
        resolved_title=best.result.resolved_title,
        resolved_company=best.result.resolved_company,
        resolved_location=best.result.resolved_location,
        description_result=best.result,
        identity_validation=best.validation,
    )


def resolve_from_candidates(
    job: dict,
    client: httpx.Client,
    candidates: Iterable[OfficialJobCandidate],
    source: str,
    *,
    deadline: float | None = None,
) -> OfficialJobResolutionResult:
    job.setdefault("candidate_validation_trace", [])
    timing = job.setdefault("enrichment_timing", {})
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
    skipped_count = 0
    blocked_hosts: set[str] = set()
    last_rejection_reason: str | None = None

    for candidate in ranked:
        # A partially checked candidate set cannot establish a unique match:
        # defer and retry rather than select an unchecked competitor.
        if deadline is not None and time.monotonic() >= deadline:
            return OfficialJobResolutionResult(
                status=OFFICIAL_SEARCH_DEFERRED, official_job_url=None,
                official_url_source=source, official_url_resolved_at=now_utc(),
                official_url_confidence=None,
                official_url_validation_reason="official resolution time budget exceeded; retry safely",
                resolved_title=None, resolved_company=None, resolved_location=None,
            )
        host = hostname(candidate.url)
        if host and host in blocked_hosts and not ats_type_for_url(candidate.url):
            blocked_count += 1
            skipped_count += 1
            timing["blocked_host_skips"] = timing.get("blocked_host_skips", 0) + 1
            job["candidate_validation_trace"].append({
                "url": candidate.url, "accepted": False,
                "fetch_status": "skipped_blocked_host",
                "reason": "another URL on this host was blocked for this job",
                "elapsed_ms": 0,
            })
            continue

        fetch_started = time.monotonic()
        result = fetch_candidate_description(candidate, client)
        from src.verified_posting_identity import breezy_posting_scope
        if breezy_posting_scope(result.final_url or '') and result.status == 'enriched':
            from dataclasses import replace
            from src.enrichment.ats.breezy import employer_provenance
            proof = employer_provenance(job, result, client)
            if proof:
                candidate = replace(candidate, source='employer', employer_provenance=proof)
        from src.verified_posting_identity import hosted_tenant_posting_scope
        if (hosted_tenant_posting_scope(result.final_url or '') and result.status == 'enriched'
                and not candidate.employer_provenance):
            # Search-found Pinpoint/Rippling postings still need the employer's
            # own site to publish this exact tenant; the posting cannot vouch.
            from dataclasses import replace
            from src.enrichment.ats.hosted_tenants import employer_provenance as hosted_provenance
            proof = hosted_provenance(job, result, client)
            if proof:
                candidate = replace(candidate, source='employer', employer_provenance=proof)
        elapsed_ms = round(1000 * (time.monotonic() - fetch_started), 1)
        timing["candidate_fetch_ms"] = round(
            timing.get("candidate_fetch_ms", 0) + elapsed_ms, 1
        )
        timing["candidate_fetches"] = timing.get("candidate_fetches", 0) + 1
        if result.status == "blocked":
            job["candidate_validation_trace"].append({"url": candidate.url, "accepted": False,
                "fetch_status": result.status, "elapsed_ms": elapsed_ms,
                "reason": "identity not evaluated: posting fetch blocked"})
            blocked_count += 1
            if host:
                blocked_hosts.add(host)
            continue

        verified = validate_candidate_result(
            job=job,
            candidate=candidate,
            result=result,
        )

        job["candidate_validation_trace"].append({"url": candidate.url,
            "accepted": verified is not None, "fetch_status": result.status,
            "final_url": result.final_url, "official_url_source": candidate.source,
            "elapsed_ms": elapsed_ms,
            "reason": verified.validation.reason if verified else "fetched posting did not pass existing identity validation",
            "http_status": result.http_status, "word_count": result.word_count,
            "resolved_title": result.resolved_title, "resolved_company": result.resolved_company})
        if verified is None:
            from src.enrichment.job_identity import compare_posting_evidence
            from src.verified_posting_identity import posting_urls_equivalent
            if compare_posting_evidence(job, result)['requisition_conflict']:
                last_rejection_reason = 'explicit employer requisition conflict'
                job['candidate_validation_trace'][-1]['reason'] = last_rejection_reason
                continue
            if result.canonical_url and not posting_urls_equivalent(
                    result.final_url or candidate.url, result.canonical_url, result.resolved_job_id):
                last_rejection_reason = 'conflicting fetched canonical posting identity'
                job['candidate_validation_trace'][-1]['reason'] = last_rejection_reason
                continue
            if result.status == "enriched" and not candidate_specific_posting_key(job, candidate, result):
                last_rejection_reason = "fetched URL lacks supported specific posting identity/provenance"
                job["candidate_validation_trace"][-1]["reason"] = last_rejection_reason
                continue
            if result.status == "enriched" or result.resolved_title or result.resolved_company:
                from src.enrich_jobs import trusted_identity_proof
                validation = validate_job_identity(
                    original_title=job.get("title"),
                    original_company=job.get("company_name"),
                    original_location=job.get("location"),
                    resolved_title=result.resolved_title,
                    resolved_company=result.resolved_company,
                    resolved_location=result.resolved_location,
                    resolved_location_source=result.resolved_location_source,
                    **trusted_identity_proof(job, result),
                )
                last_rejection_reason = validation.reason
                job["candidate_validation_trace"][-1]["reason"] = validation.reason
            continue

        verified_jobs.append(verified)

    # Do not return even a seemingly valid match if there are unchecked
    # candidates due to deadline expiry; ambiguity must remain detectable.
    if deadline is not None and time.monotonic() >= deadline:
        return OfficialJobResolutionResult(
            status=OFFICIAL_SEARCH_DEFERRED, official_job_url=None,
            official_url_source=source, official_url_resolved_at=now_utc(),
            official_url_confidence=None,
            official_url_validation_reason="official resolution time budget exceeded; retry safely",
            resolved_title=None, resolved_company=None, resolved_location=None,
        )

    if verified_jobs and skipped_count:
        # Do not promote a match when another potentially competing posting
        # was skipped rather than actually fetched and checked.
        return OfficialJobResolutionResult(
            status=OFFICIAL_SEARCH_DEFERRED, official_job_url=None,
            official_url_source=source, official_url_resolved_at=now_utc(),
            official_url_confidence=None,
            official_url_validation_reason="potential competing posting skipped on blocked host",
            resolved_title=None, resolved_company=None, resolved_location=None,
        )

    if verified_jobs:
        chosen = choose_verified_candidate(verified_jobs)
        if chosen.description_result:
            from dataclasses import replace
            from src.enrichment.job_identity import compare_posting_evidence
            selected = next(v for v in verified_jobs if v.result is chosen.description_result)
            identity_evidence = compare_posting_evidence(job, chosen.description_result)
            chosen = replace(chosen,
                posting_identity_evidence=identity_evidence,
                employer_provenance=selected.candidate.employer_provenance,
                # This existing persisted reason carries the distinction even
                # where callers do not retain the additional diagnostic fields.
                official_url_validation_reason=(
                    chosen.official_url_validation_reason + '; current employer posting verified; historical match: '
                    + identity_evidence['historical_match']
                    if selected.candidate.employer_provenance else chosen.official_url_validation_reason))
        trace = job.get("public_search_trace", {})
        if trace.get("provider") and chosen.status == OFFICIAL_FOUND_VERIFIED:
            metric(client, trace["provider"], "verified_official_resolutions")
            from src.repositories.recommendation_repository import description_state
            description = chosen.description_result
            if description and description_state({"description": description.description,
                    "raw_description_word_count": description.word_count}) == "FULL_JD":
                metric(client, trace["provider"], "descriptions_recovered")
        return chosen

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
        status=(OFFICIAL_INSUFFICIENT_IDENTITY if last_rejection_reason
                and "independent same-posting proof unavailable" in last_rejection_reason
                and "resolved title missing" not in last_rejection_reason
                else OFFICIAL_NOT_FOUND),
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
    search_circuit: PublicSearchCircuit | None = None,
) -> OfficialJobResolutionResult:
    """Discover direct ATS postings first, under a cooperative per-job budget."""
    timing = job.setdefault("enrichment_timing", {})
    deadline = time.monotonic() + OFFICIAL_RESOLUTION_BUDGET_SECONDS
    from src.enrichment.employer_careers import discover_employer_candidates
    employer_candidates = discover_employer_candidates(job, client, deadline=deadline)
    if employer_candidates:
        employer_result = resolve_from_candidates(job, client, employer_candidates, 'employer', deadline=deadline)
        if employer_result.status in {OFFICIAL_FOUND_VERIFIED, OFFICIAL_AMBIGUOUS, OFFICIAL_SEARCH_DEFERRED}:
            return employer_result
    ats_started = time.monotonic()
    try:
        ats_candidates = discover_ats_candidates(
            job=job,
            client=client,
        )
    except Exception:
        ats_candidates = []
    finally:
        timing["ats_discovery_ms"] = round(
            1000 * (time.monotonic() - ats_started), 1
        )

    if ats_candidates:
        ats_result = resolve_from_candidates(
            job=job,
            client=client,
            candidates=ats_candidates,
            source="ats",
            deadline=deadline,
        )

        if ats_result.status in {
            OFFICIAL_FOUND_VERIFIED,
            OFFICIAL_AMBIGUOUS,
            OFFICIAL_SEARCH_DEFERRED,
        }:
            return ats_result

    if time.monotonic() >= deadline:
        return OfficialJobResolutionResult(
            status=OFFICIAL_SEARCH_DEFERRED, official_job_url=None,
            official_url_source="search", official_url_resolved_at=now_utc(),
            official_url_confidence=None,
            official_url_validation_reason="official resolution time budget exceeded before public search",
            resolved_title=None, resolved_company=None, resolved_location=None,
        )

    try:
        candidates = (search_circuit.search(job, client) if search_circuit
                      else search_candidates(job, client))
    except SearchDeferredError as error:
        return OfficialJobResolutionResult(
            status=OFFICIAL_SEARCH_DEFERRED, official_job_url=None,
            official_url_source="search", official_url_resolved_at=now_utc(),
            official_url_confidence=None, official_url_validation_reason=str(error),
            resolved_title=None, resolved_company=None, resolved_location=None,
            error_message=str(error),
        )
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

    # Tavily may find the employer's real Workday career-site homepage
    # rather than a specific posting. Query that public tenant's CXS listing
    # without extra Tavily credits, then fetch/validate matching postings.
    # Untrusted search data never becomes accepted title/company evidence.
    seen_urls = {candidate.url for candidate in candidates}
    search_trace = job.get("public_search_trace") or {}
    # Search may supply a corporate-site lead. It must pass fetched branding
    # and careers-link verification just like a domain probe; the search label
    # itself never grants employer provenance.
    corporate_leads = [hit.get('url', '') for hit in search_trace.get('organic_results', [])
                       if not looks_like_individual_posting_url(hit.get('url', ''))
                       and not ats_type_for_url(hit.get('url', ''))][:2]
    if corporate_leads and time.monotonic() < deadline:
        for lead in discover_employer_candidates(job, client, deadline=deadline, corporate_leads=corporate_leads):
            if lead.url not in seen_urls:
                candidates.append(lead)
                seen_urls.add(lead.url)
    workday_homepages = [
        hit["url"] for hit in search_trace.get("organic_results", [])
        if ats_type_for_url(hit.get("url", "")) == "workday"
        and not looks_like_individual_posting_url(hit.get("url", ""))
    ][:1]
    for homepage in workday_homepages:
        if time.monotonic() >= deadline:
            break
        lead_started = time.monotonic()
        leads = workday.discover_from_search_homepage(job, client, homepage)
        timing["workday_listing_discovery_ms"] = round(
            1000 * (time.monotonic() - lead_started), 1
        )
        search_trace["workday_listing_candidates_found"] = len(leads)
        for lead in leads:
            if lead.url not in seen_urls:
                candidates.append(lead)
                seen_urls.add(lead.url)

    return resolve_from_candidates(
        job=job,
        client=client,
        candidates=candidates,
        source="search",
        deadline=deadline,
    )
