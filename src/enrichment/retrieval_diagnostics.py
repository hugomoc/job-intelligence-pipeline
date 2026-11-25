"""Per-attempt JD retrieval diagnostics (run-local, never identity evidence).

Each source/candidate/listing fetch appends one dict to
``job["retrieval_diagnostics"]``; enrich_jobs writes one sanitized run-log
line per entry. No HTML, cookies, credentials or non-identity query strings
are retained.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

# Failure categories (distinct by design).
ACCESS_DENIED = "access_denied"            # HTTP 401/403
RATE_LIMITED = "rate_limited"              # HTTP 429
BOT_CHECK = "bot_check"                    # 200 page with a human/bot check
JAVASCRIPT_REQUIRED = "javascript_required"  # 200 interstitial needing JS/cookies
POSTING_NOT_FOUND = "posting_not_found"    # 404/410
HTTP_ERROR = "http_error"                  # other HTTP >= 400
NETWORK_ERROR = "network_error"            # timeout / connection error
INVALID_URL = "invalid_url"
EXTRACTION_FAILED = "description_extraction_failed"  # accessible, no usable JD
IDENTITY_REJECTED = "identity_rejected"


# Generic posting-id parameters (e.g. /view_job?id=39611) are identity, not
# tracking; kept only when the value is a short plain identifier.
_POSTING_ID_PARAMETERS = {"id", "jobid", "job_id", "reqid", "req_id", "requisitionid", "postingid", "posting_id"}


def sanitize_url(value: Any) -> str | None:
    """Host + path, plus only job-identity query parameters. No credentials."""
    if not value:
        return None
    try:
        from src.verified_posting_identity import JOB_ID_QUERY_PARAMETERS
        parsed = urlsplit(str(value))
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return "<malformed>"
        query = urlencode([(k, v) for k, v in parse_qsl(parsed.query)
                           if k.casefold() in JOB_ID_QUERY_PARAMETERS
                           or (k.casefold() in _POSTING_ID_PARAMETERS
                               and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", v))])
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}{parsed.path}" + (f"?{query}" if query else "")
    except (TypeError, ValueError):
        return "<malformed>"


def access_classification(result) -> str:
    """Why a fetch did or did not yield page content."""
    status = getattr(result, "status", None)
    signal = str(getattr(result, "access_signal", "") or "")
    http = getattr(result, "http_status", None)
    if status == "invalid_url":
        return INVALID_URL
    if signal in {"http_401", "http_403"} or (status == "blocked" and http in (401, 403) and not signal):
        return ACCESS_DENIED
    if signal == "http_429" or http == 429:
        return RATE_LIMITED
    if signal.startswith("javascript_required"):
        return JAVASCRIPT_REQUIRED
    if signal.startswith("bot_check") or status == "blocked":
        return BOT_CHECK
    if status == "fetch_error":
        if http in (404, 410):
            return POSTING_NOT_FOUND
        return HTTP_ERROR if http else NETWORK_ERROR
    return "accessible"


def description_quality(result) -> str:
    """Existing JD-quality rules: full / partial / none."""
    words = int(getattr(result, "word_count", 0) or 0)
    if words <= 0:
        return "none"
    from src.repositories.recommendation_repository import (
        MINIMUM_FULL_DESCRIPTION_WORDS, description_has_quality_signals,
    )
    full = (words >= MINIMUM_FULL_DESCRIPTION_WORDS
            and description_has_quality_signals(getattr(result, "description", "") or ""))
    return "full" if full else "partial"


def posting_detail_endpoint_status(url: Any, result=None) -> str:
    """Whether a supported ATS posting-detail endpoint was used or exists."""
    recorded = getattr(result, "posting_detail", None)
    if recorded:
        return recorded
    from src.enrichment.official_job_resolver import ats_type_for_url
    ats = ats_type_for_url(str(url or ""))
    if not ats:
        return "not applicable (not a known ATS URL)"
    if ats in {"greenhouse", "icims"}:
        return f"not needed or not permitted ({ats} fallback runs only when the page lacks a description)"
    return f"not attempted: no {ats} posting-detail endpoint is implemented (HTML page fetch only)"


def failure_category(result, identity: str | None) -> str | None:
    access = access_classification(result)
    if access != "accessible":
        return access
    if getattr(result, "status", None) == "no_description" or not getattr(result, "word_count", 0):
        return EXTRACTION_FAILED
    if identity and identity.startswith("rejected"):
        return IDENTITY_REJECTED
    return None


def uncertainty(result, access: str) -> str | None:
    if access == ACCESS_DENIED:
        return "refusal cause (WAF, IP reputation, client policy) is not identifiable from the status code"
    if access == RATE_LIMITED and not getattr(result, "retry_after", None):
        return "no Retry-After header; safe retry time unknown"
    if access == BOT_CHECK:
        return "human/bot check page; content not retrievable without passing it"
    if access == JAVASCRIPT_REQUIRED:
        return "page may render in a browser; requires an allowed browser fetch to confirm"
    if access == NETWORK_ERROR:
        return "no HTTP response; transient or network-level failure"
    return None


def record(job: dict, *, stage: str, url: Any, result, method: str = "http_get",
           identity: str | None = None, note: str | None = None) -> dict:
    """Append one sanitized diagnostic entry for a retrieval attempt."""
    access = access_classification(result)
    entry = {
        "record_key": job.get("record_key"),
        "stage": stage,
        "candidate_url": sanitize_url(url),
        "final_url": sanitize_url(getattr(result, "final_url", None)),
        "method": method,
        "http_status": getattr(result, "http_status", None),
        "fetch_status": getattr(result, "status", None),
        "access": access,
        "access_signal": getattr(result, "access_signal", None),
        "retry_after": getattr(result, "retry_after", None),
        "browser_fallback": getattr(result, "browser_fallback", None) or "not evaluated",
        "posting_detail_endpoint": posting_detail_endpoint_status(url, result),
        "extraction_method": getattr(result, "extraction_method", None),
        "word_count": int(getattr(result, "word_count", 0) or 0),
        "description_quality": description_quality(result),
        "identity": identity or "not evaluated",
        "failure": failure_category(result, identity),
        "uncertainty": uncertainty(result, access),
    }
    if note:
        entry["note"] = note
    job.setdefault("retrieval_diagnostics", []).append(entry)
    return entry


def record_listing(job: dict, *, stage: str, url: Any, http_status: int | None,
                   outcome: str, candidates: int = 0) -> dict:
    """ATS listing/search endpoints (no description content by design)."""
    entry = {"record_key": job.get("record_key"), "stage": stage, "candidate_url": sanitize_url(url),
             "method": "ats_listing_api", "http_status": http_status, "outcome": outcome,
             "candidates_found": candidates}
    job.setdefault("retrieval_diagnostics", []).append(entry)
    return entry


LINE_FIELDS = ("stage", "candidate_url", "final_url", "method", "http_status", "fetch_status", "access",
               "access_signal", "retry_after", "browser_fallback", "posting_detail_endpoint",
               "extraction_method", "word_count", "description_quality", "identity", "failure",
               "uncertainty", "outcome", "candidates_found", "note")


def format_line(entry: dict) -> str:
    """One run-log line; URLs keep identity but are escaped like other diagnostics."""
    parts = [f"record_key={entry.get('record_key')}"]
    for field in LINE_FIELDS:
        if field in entry and entry[field] not in (None, ""):
            value = str(entry[field]).replace("://", "[:]//")
            parts.append(f"{field}={value}")
    return "Retrieval diagnostic: " + "; ".join(parts)
