"""Verified cross-source posting identity helpers.

Exact posting identity stays source-specific. The helpers here support a second
identity layer that can link records from different sources only after an
official employer/ATS posting has been verified.
"""

from __future__ import annotations

from src.job_domains import is_third_party_url

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

VERIFIED_KEY_TRUST_TRUSTED = "trusted"
VERIFIED_KEY_TRUST_UNTRUSTED = "untrusted"
VERIFIED_KEY_TRUST_CONFLICT = "conflict"

VERIFIED_KEY_SOURCE_OFFICIAL_FOUND_VERIFIED = "official_found_verified"
VERIFIED_KEY_SOURCE_DIRECT_ATS_SOURCE = "direct_ats_source"
VERIFIED_KEY_SOURCE_SOURCE_REDIRECT_VERIFIED = "source_redirect_verified"
VERIFIED_KEY_SOURCE_EMBEDDED_OFFICIAL_DESTINATION = (
    "embedded_official_destination"
)
VERIFIED_KEY_SOURCE_ACCEPTED_DIRECT_CANDIDATE = "accepted_direct_candidate"
VERIFIED_KEY_SOURCE_EXPLICIT_REQUISITION_ID = "explicit_requisition_id"
VERIFIED_KEY_SOURCE_VERIFIED_REUSE = "verified_reuse"

TRUSTED_VERIFIED_KEY_SOURCES = {
    VERIFIED_KEY_SOURCE_OFFICIAL_FOUND_VERIFIED,
    VERIFIED_KEY_SOURCE_DIRECT_ATS_SOURCE,
    VERIFIED_KEY_SOURCE_SOURCE_REDIRECT_VERIFIED,
    VERIFIED_KEY_SOURCE_EMBEDDED_OFFICIAL_DESTINATION,
    VERIFIED_KEY_SOURCE_ACCEPTED_DIRECT_CANDIDATE,
    VERIFIED_KEY_SOURCE_EXPLICIT_REQUISITION_ID,
    VERIFIED_KEY_SOURCE_VERIFIED_REUSE,
}

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


def normalize_verified_key_source(value: object) -> str:
    """Normalize a stored verified-key provenance label."""
    return str(value or "").strip().casefold()


def normalize_verified_key_trust(value: object) -> str:
    """Normalize a stored verified-key trust state."""
    return str(value or "").strip().casefold()


def verified_key_metadata_is_trusted(
    source: object,
    trust: object,
) -> bool:
    """Return true only for explicitly trusted provenance metadata."""
    return (
        normalize_verified_key_trust(trust) == VERIFIED_KEY_TRUST_TRUSTED
        and normalize_verified_key_source(source)
        in TRUSTED_VERIFIED_KEY_SOURCES
    )


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


def workday_posting_scope(url: str) -> tuple[str, str, str] | None:
    """Parse tenant/site/posting, without granting fetched identity trust."""
    p = urlsplit(url)
    host = (p.hostname or '').casefold()
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
        return None
    if not host.endswith(('.myworkdayjobs.com', '.myworkdaysite.com', '.workdayjobs.com')):
        return None
    parts = p.path.strip('/').split('/')
    if parts and re.fullmatch(r'[a-z]{2}(?:-[a-z]{2})?', parts[0], re.I):
        parts = parts[1:]
    if len(parts) not in (3, 4) or parts[1] != 'job' or not all(parts):
        return None
    return host, parts[0], parts[-1]


def is_short_workday_posting(url: str) -> bool:
    scope = workday_posting_scope(url)
    if not scope:
        return False
    parts = urlsplit(url).path.strip('/').split('/')
    return len(parts[parts.index('job') + 1:]) == 1


def breezy_posting_scope(url: str) -> tuple[str, str] | None:
    p = urlsplit(url)
    host = (p.hostname or '').casefold()
    if (p.scheme != 'https' or p.username or p.password or p.port not in (None, 443)
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]*\.breezy\.hr', host)):
        return None
    match = re.fullmatch(r'/p/([a-f0-9]{12})(?:-[a-zA-Z0-9_-]+)?(?:/apply)?/?', p.path)
    return (host, match.group(1)) if match else None


def fetched_posting_scope_valid(url, result, employer_provenance=None):
    """Additional fetched evidence required by the newly supported URL forms.

    URL-derived identifiers and discovery hints cannot license new trust.
    Existing long-form Workday keys retain their historical representation.
    """
    host = (urlsplit(url).hostname or '').casefold()
    if (host == 'breezy.hr' or host.endswith('.breezy.hr')) and not breezy_posting_scope(url):
        return False
    if is_short_workday_posting(url) or breezy_posting_scope(url):
        explicit_ids = {v.casefold() for k, v in parse_qsl(urlsplit(url).query)
                        if k.casefold() in JOB_ID_QUERY_PARAMETERS and v}
        if explicit_ids and explicit_ids != {str(result.resolved_job_id or '').casefold()}:
            return False
    if is_short_workday_posting(url):
        scope = workday_posting_scope(url)
        req = str(result.resolved_job_id or '')
        return bool(req and result.resolved_job_id_source in {
            'jobposting_json_ld', 'workday_posting_api',
        } and re.search(r'_' + re.escape(req) + r'(?:-\d+)?$', scope[2], re.I))
    scope = breezy_posting_scope(url)
    if scope:
        from src.enrichment.job_identity import normalize_company_name
        proof = employer_provenance or {}
        return bool(result.resolved_job_id == scope[1]
                    and result.resolved_job_id_source == 'breezy_embedded_position'
                    and proof.get('company') == normalize_company_name(result.resolved_company)
                    and urlsplit(proof.get('careers_url', '')).hostname == scope[0]
                    and proof.get('basis') in {
                        'fetched_corporate_brand_and_published_careers_link',
                        'fetched_reciprocal_employer_careers_link',
                        'previously_verified_employer',
                    })
    return True


def is_specific_job_posting_url(value: str | None) -> bool:
    """Return true only when a URL appears to identify one posting."""
    normalized_url = normalize_official_url(value)

    if not normalized_url:
        return False

    parsed = urlsplit(normalized_url)
    hostname = (parsed.hostname or "").casefold()
    path = parsed.path or "/"
    if hostname == 'breezy.hr' or hostname.endswith('.breezy.hr'):
        return breezy_posting_scope(normalized_url) is not None
    if is_short_workday_posting(normalized_url):
        return bool(re.search(r'_(?:JR|REQ|R|J)[-_]?\d+(?:-\d+)?$', path, re.I))
    query = {
        key.casefold(): query_value
        for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
        if query_value
    }

    if any(query.get(key) for key in JOB_ID_QUERY_PARAMETERS):
        return True

    if "greenhouse.io" in hostname:
        return bool(re.search(r"/jobs/[0-9]+(?:/|$)", path))

    if "lever.co" in hostname:
        path_parts = [part for part in path.split("/") if part]
        return len(path_parts) >= 2

    if hostname == "ashbyhq.com" or hostname.endswith(".ashbyhq.com"):
        return bool(re.search(r"/(?:job|jobs)/[^/]+(?:/|$)", path)
                    or re.fullmatch(r"/[^/]+/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/?", path, re.I))

    if "smartrecruiters.com" in hostname:
        path_parts = [part for part in path.split("/") if part]
        return len(path_parts) >= 2

    if "icims.com" in hostname:
        return bool(re.search(r"/jobs/[0-9]+(?:/|$)", path))

    if "workday" in hostname or "myworkdayjobs.com" in hostname:
        return bool(re.search(r"/(?:job|jobs)/[^/]+/[^/]+(?:/|$)", path))

    generic_paths = {
        "",
        "/",
        "/career",
        "/careers",
        "/careers/jobs",
        "/job",
        "/jobs",
        "/openings",
    }

    if path.casefold().rstrip("/") in generic_paths:
        return False

    path_parts = [part for part in path.split("/") if part]
    if len(path_parts) < 2:
        return False

    return bool(
        re.search(r"[0-9]", path_parts[-1])
        or re.search(r"\b(req|job|jr|id)[-_]?[0-9a-z]+", path_parts[-1], re.I)
    )


def verified_posting_key_from_url(value: str | None) -> str:
    """Create a source-independent key from verified official URL evidence."""
    if is_third_party_url(value):
        return ""
    normalized_url = normalize_official_url(value)
    if not normalized_url:
        return ""

    parsed = urlsplit(normalized_url)
    hostname = (parsed.hostname or "").casefold()
    breezy = breezy_posting_scope(normalized_url)
    if breezy:
        return f"breezy:{breezy[0]}:{breezy[1]}"
    if hostname == 'breezy.hr' or hostname.endswith('.breezy.hr'):
        return ''
    if is_short_workday_posting(normalized_url):
        scope = workday_posting_scope(normalized_url)
        return (f"workday:{scope[0]}:{scope[1]}:{scope[2]}"
                if is_specific_job_posting_url(normalized_url) else '')
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
        if job_id and is_specific_job_posting_url(normalized_url):
            return f"lever:{job_id}"

    if hostname == "ashbyhq.com" or hostname.endswith(".ashbyhq.com"):
        job_id = _path_match(r"/(?:job|jobs)/([^/?#]+)$", normalized_url)
        if not job_id:
            match = re.fullmatch(r"/[^/]+/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?", parsed.path, re.I)
            job_id = match.group(1).lower() if match else ""
        if job_id:
            return f"ashby:{job_id}"

    if "smartrecruiters.com" in hostname:
        job_id = _path_match(r"/(?:[^/?#]+/)?([^/?#]+)$", normalized_url)
        if job_id and is_specific_job_posting_url(normalized_url):
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

    if not is_specific_job_posting_url(normalized_url):
        return ""

    return f"official-url:{normalized_url}"


def is_known_official_or_ats_url(value: str | None) -> bool:
    """Return true for URLs that can safely produce verified posting keys."""
    if is_third_party_url(value):
        return False
    normalized_url = normalize_official_url(value)
    if not normalized_url:
        return False
    if is_short_workday_posting(normalized_url) or breezy_posting_scope(normalized_url):
        # These forms require fresh fetched identity, not URL-only admission.
        return False

    hostname = (urlsplit(normalized_url).hostname or "").casefold()
    return any(marker in hostname for marker in KNOWN_OFFICIAL_HOST_MARKERS)


def description_hash(value: str | None) -> str:
    """Hash normalized description content for safe AI score reuse."""
    normalized = normalize_loose_text(value)

    if not normalized:
        return ""

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def posting_urls_equivalent(left: str, right: str, requisition: str | None = None) -> bool:
    """Reconcile explicit URL identity/locale aliases without changing old keys.

    An identical title or requisition number on an unrelated host is not proof.
    Workday may omit its location segment, but tenant/site and JR suffix must
    still agree. Different UUIDs on employer pages remain conflicting.
    """
    a, b = urlsplit(normalize_official_url(left)), urlsplit(normalize_official_url(right))
    if not a.hostname or a.hostname != b.hostname:
        return False
    def parts(p):
        values = p.path.strip('/').split('/')
        if values and re.fullmatch(r'[a-z]{2}(?:-[a-z]{2})?', values[0], re.I):
            values = values[1:]
        return values
    ap, bp = parts(a), parts(b)
    if ap == bp and a.query == b.query:
        return True
    host = a.hostname
    if host.endswith(('.myworkdayjobs.com', '.myworkdaysite.com', '.workdayjobs.com')):
        return bool(requisition and len(ap) >= 3 and len(bp) >= 3
                    and ap[:2] == bp[:2] and ap[1] == 'job'
                    and ap[-1] == bp[-1]
                    and re.search(r'_' + re.escape(requisition) + r'(?:-\d+)?$', ap[-1], re.I)
                    and a.query == b.query)
    ka, kb = verified_posting_key_from_url(left), verified_posting_key_from_url(right)
    return bool(ka and ka == kb)


def url_requisition_ids(url: str) -> set[str]:
    """Only explicit requisition/job parameters and JR suffixes, not content IDs."""
    p = urlsplit(url)
    ids = {v.strip().casefold() for k, v in parse_qsl(p.query)
           if k.casefold() in JOB_ID_QUERY_PARAMETERS and v.strip()}
    ids.update(m.casefold() for m in re.findall(r'(?:_|/)(JR[-_]\d+)(?:/|$)', p.path, re.I))
    return ids
