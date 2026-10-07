"""Verified cross-source posting identity helpers.

Exact posting identity stays source-specific. The helpers here support a second
identity layer that can link records from different sources only after an
official employer/ATS posting has been verified.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_QUERY_PARAMETERS = {
    "ao",
    "campaign",
    "cb",
    "clickid",
    "cs",
    "fbclid",
    "gclid",
    "guid",
    "igshid",
    "imp_id",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "ref",
    "ref_src",
    "referrer",
    "s",
    "src",
    "t",
    "tr",
    "uid",
    "uido",
    "utm_campaign",
    "utm_content",
    "utm_id",
    "utm_medium",
    "utm_source",
    "utm_term",
    "vt",
}

JOB_ID_QUERY_PARAMETERS = {
    "gh_jid",
    "jid",
    "job_id",
    "jobid",
    "postingid",
    "requisitionid",
    "reqid",
}

KNOWN_OFFICIAL_HOST_MARKERS = (
    "ashbyhq.com",
    "greenhouse.io",
    "icims.com",
    "lever.co",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "workdayjobs.com",
)

TITLE_REPLACEMENTS = (
    (r"\bsr\.?\b", "senior"),
    (r"\bjr\.?\b", "junior"),
    (r"\bbi\b", "business intelligence"),
    (r"\beng\b", "engineer"),
)

COMPANY_SUFFIXES = {
    "co",
    "company",
    "corp",
    "corporation",
    "inc",
    "incorporated",
    "llc",
    "ltd",
}


def normalize_verified_source(value: object) -> str:
    """Normalize a source label for source-independent metadata display."""
    return normalize_loose_text(str(value or ""))


def normalize_loose_text(value: str | None) -> str:
    if not value:
        return ""

    normalized = unicodedata.normalize("NFKD", value)
    normalized = normalized.casefold()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_candidate_title(value: str | None) -> str:
    """Normalize titles for likely-duplicate candidate discovery only."""
    normalized = normalize_loose_text(value)

    for pattern, replacement in TITLE_REPLACEMENTS:
        normalized = re.sub(pattern, replacement, normalized)

    return re.sub(r"\s+", " ", normalized).strip()


def normalize_candidate_company(value: str | None) -> str:
    """Normalize company names for candidate discovery, not proof."""
    tokens = [
        token
        for token in normalize_loose_text(value).split()
        if token not in COMPANY_SUFFIXES
    ]

    return " ".join(tokens)


def normalize_candidate_location(value: str | None) -> str:
    """Normalize common remote-US location variants for duplicate discovery."""
    normalized = normalize_loose_text(value)

    if not normalized:
        return ""

    has_remote = bool(re.search(r"\b(remote|remote only|work from home)\b", normalized))
    has_us = bool(
        re.search(
            r"\b(united states|usa|u s|us|america|remote us|us remote)\b",
            normalized,
        )
    )

    if has_remote and has_us:
        return "remote-us"

    if has_remote:
        return "remote"

    normalized = re.sub(r"\bunited states\b", "us", normalized)
    normalized = re.sub(r"\busa\b", "us", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def candidate_duplicate_fingerprint(
    title: str | None,
    company: str | None,
    location: str | None,
) -> str:
    """Return a fuzzy candidate key for discovery, never status propagation."""
    return "|".join(
        (
            normalize_candidate_title(title),
            normalize_candidate_company(company),
            normalize_candidate_location(location),
        )
    )


def normalize_official_url(value: str | None) -> str:
    """Normalize official URLs while preserving identity-bearing parameters."""
    if not value:
        return ""

    raw_url = str(value).strip()
    if not raw_url:
        return ""

    parsed = urlsplit(raw_url)
    scheme = parsed.scheme.casefold() or "https"
    hostname = (parsed.hostname or "").casefold()
    netloc = f"{hostname}:{parsed.port}" if parsed.port else hostname
    path = re.sub(r"/+", "/", parsed.path or "/")

    if path != "/":
        path = path.rstrip("/")

    kept_query_parameters = [
        (key, query_value)
        for key, query_value in parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
        if key.casefold() not in TRACKING_QUERY_PARAMETERS
        and not key.casefold().startswith("utm_")
    ]
    kept_query_parameters.sort(key=lambda item: (item[0].casefold(), item[1]))

    return urlunsplit(
        (
            scheme,
            netloc,
            path,
            urlencode(kept_query_parameters, doseq=True),
            "",
        )
    )


def _path_match(pattern: str, url: str) -> str:
    match = re.search(pattern, url, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def verified_posting_key_from_url(value: str | None) -> str:
    """Create a source-independent key from verified official URL evidence."""
    normalized_url = normalize_official_url(value)
    if not normalized_url:
        return ""

    parsed = urlsplit(normalized_url)
    hostname = (parsed.hostname or "").casefold()
    query = {
        key.casefold(): query_value
        for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
        if query_value
    }

    if "greenhouse.io" in hostname:
        job_id = query.get("gh_jid") or _path_match(r"/jobs/([0-9]+)", normalized_url)
        if job_id:
            return f"greenhouse:{job_id}"

    if "lever.co" in hostname:
        job_id = _path_match(r"/([^/?#]+)$", normalized_url)
        if job_id:
            return f"lever:{job_id}"

    if "ashbyhq.com" in hostname:
        job_id = _path_match(r"/(?:job/)?([^/?#]+)$", normalized_url)
        if job_id:
            return f"ashby:{job_id}"

    if "smartrecruiters.com" in hostname:
        job_id = _path_match(r"/(?:[^/?#]+/)?([^/?#]+)$", normalized_url)
        if job_id:
            return f"smartrecruiters:{job_id}"

    if "icims.com" in hostname:
        job_id = _path_match(r"/jobs/([0-9]+)", normalized_url)
        if job_id:
            return f"icims:{job_id}"

    if "workday" in hostname or "myworkdayjobs.com" in hostname:
        for key in JOB_ID_QUERY_PARAMETERS:
            if query.get(key):
                return f"workday:{query[key]}"

        job_id = _path_match(r"/(?:job|jobs)/[^/?#]*/([^/?#]+)", normalized_url)
        if job_id:
            return f"workday:{job_id}"

    for key in JOB_ID_QUERY_PARAMETERS:
        if query.get(key):
            return f"{hostname}:{key}:{query[key]}"

    return f"official-url:{normalized_url}"


def is_known_official_or_ats_url(value: str | None) -> bool:
    """Return true for URLs that can safely produce verified posting keys."""
    normalized_url = normalize_official_url(value)
    if not normalized_url:
        return False

    hostname = (urlsplit(normalized_url).hostname or "").casefold()
    return any(marker in hostname for marker in KNOWN_OFFICIAL_HOST_MARKERS)


def description_hash(value: str | None) -> str:
    """Hash normalized description content for safe AI score reuse."""
    normalized = normalize_loose_text(value)

    if not normalized:
        return ""

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
