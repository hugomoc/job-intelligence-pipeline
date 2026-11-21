"""Deterministic identity validation for enriched job pages.

Enrichment may follow redirects or aggregator links. This validator answers a
single question before any description is accepted: is the resolved page the
same job that came from the original email alert?
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from src.job_title_filter import normalize_job_title


ACCEPT_WITH_COMPANY_THRESHOLD = 0.78
ACCEPT_WITHOUT_COMPANY_THRESHOLD = 0.92
COMPANY_MATCH_THRESHOLD = 0.82
TITLE_REASONABLE_THRESHOLD = 0.68

LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "corp",
    "corporation",
    "company",
    "co",
}

OCCUPATION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("data_engineer", r"\bdata\s+engineer\b"),
    ("analytics_engineer", r"\banalytics\s+engineer\b"),
    (
        "business_intelligence_engineer",
        r"\b(business intelligence|bi)\s+engineer\b",
    ),
    (
        "business_intelligence_developer",
        r"\b(business intelligence|bi)\s+developer\b",
    ),
    ("data_analyst", r"\bdata\s+analyst\b"),
    ("analytics_analyst", r"\banalytics\s+analyst\b"),
    ("software_engineer", r"\bsoftware\s+(engineer|developer)\b"),
    (
        "machine_learning_engineer",
        r"\b(machine learning|ml|ai)\s+engineer\b",
    ),
    ("data_scientist", r"\bdata\s+scientist\b"),
    ("database_administrator", r"\b(database administrator|dba)\b"),
)


@dataclass(frozen=True)
class JobIdentityValidation:
    accepted: bool
    confidence: float
    title_similarity: float | None
    company_similarity: float | None
    occupation_match: bool | None
    reason: str
    location_compatibility: str = "unknown"
    location_reason: str = "authoritative location unavailable"


def description_similarity(original: str | None, resolved: str | None) -> float | None:
    """Formatting-insensitive supporting evidence, excluding generic boilerplate.

    This diagnostic never overrides employer, geography or requisition conflicts.
    Use word shingles to retain order without relying on PDF line wrapping.
    """
    from src.verified_posting_identity import normalize_loose_text
    def shingles(value):
        from html import unescape
        value = unescape(value or '')
        value = re.sub(r'<[^>]+>', ' ', value or '')
        sentences = re.split(r'[.!?;\n]+', value)
        boilerplate = r'equal opportunity|reasonable accommodation|all rights reserved|privacy policy|regardless of race|protected veteran'
        words = normalize_loose_text(' '.join(s for s in sentences if not re.search(boilerplate, s, re.I))).split()
        return {tuple(words[i:i + 4]) for i in range(len(words) - 3)}
    a, b = shingles(original), shingles(resolved)
    if not a or not b:
        return None
    return round(len(a & b) / len(a | b), 4)


def compare_posting_evidence(job, result):
    """Keep current employer-posting verification distinct from historical proof.

    Source listing IDs are never compared to requisition IDs: callers must
    explicitly identify an employer requisition when they possess one.
    """
    old_id = str(job.get('original_requisition_id') or job.get('official_resolved_job_id') or '').strip()
    new_id = str(result.resolved_job_id or '').strip()
    conflict = bool(old_id and new_id and old_id.casefold() != new_id.casefold())
    original = job.get('description') or job.get('raw_description')
    sim = description_similarity(original, result.description)
    matched_id = bool(old_id and new_id and not conflict)
    return dict(original_requisition_id=old_id or None, current_requisition_id=new_id or None,
                requisition_conflict=conflict, description_similarity=sim,
                historical_match=('conflicting_requisition' if conflict else
                                  'explicit_requisition_match' if matched_id else
                                  'description_supported_not_exact_identity' if sim is not None else
                                  'unproven_original_jd_and_requisition_unavailable'),
                description_caution=('material_description_difference' if sim is not None and sim < .35 else None))


def normalize_company_name(value: str | None) -> str:
    if not value:
        return ""

    normalized = unicodedata.normalize("NFKD", value)
    normalized = normalized.casefold()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    tokens = [
        token
        for token in normalized.split()
        if token not in LEGAL_SUFFIXES
    ]

    return " ".join(tokens)


def similarity(left: str | None, right: str | None) -> float | None:
    left_text = (left or "").strip()
    right_text = (right or "").strip()

    if not left_text or not right_text:
        return None

    if left_text == right_text:
        return 1.0

    left_tokens = set(left_text.split())
    right_tokens = set(right_text.split())

    if left_tokens and right_tokens:
        token_overlap = len(left_tokens & right_tokens) / max(
            len(left_tokens),
            len(right_tokens),
        )
    else:
        token_overlap = 0.0

    sequence = SequenceMatcher(
        None,
        left_text,
        right_text,
    ).ratio()

    return max(token_overlap, sequence)


def primary_occupation(title: str | None) -> str | None:
    normalized = normalize_job_title(title)

    for occupation, pattern in OCCUPATION_PATTERNS:
        if re.search(pattern, normalized):
            return occupation

    return None


AUTHORITATIVE_LOCATION_SOURCES = {
    "breezy_embedded_position",
    "jobposting_json_ld", "greenhouse_remix_jobPost", "workday_posting_api",
}
US_REGIONS = dict(pair.split(":") for pair in (
    "AL:alabama|AK:alaska|AZ:arizona|AR:arkansas|CA:california|CO:colorado|CT:connecticut|"
    "DE:delaware|DC:district of columbia|FL:florida|GA:georgia|HI:hawaii|ID:idaho|IL:illinois|"
    "IN:indiana|IA:iowa|KS:kansas|KY:kentucky|LA:louisiana|ME:maine|MD:maryland|MA:massachusetts|"
    "MI:michigan|MN:minnesota|MS:mississippi|MO:missouri|MT:montana|NE:nebraska|NV:nevada|"
    "NH:new hampshire|NJ:new jersey|NM:new mexico|NY:new york|NC:north carolina|ND:north dakota|"
    "OH:ohio|OK:oklahoma|OR:oregon|PA:pennsylvania|RI:rhode island|SC:south carolina|"
    "SD:south dakota|TN:tennessee|TX:texas|UT:utah|VT:vermont|VA:virginia|WA:washington|"
    "WV:west virginia|WI:wisconsin|WY:wyoming"
).split("|"))


def location_compatibility(original, resolved, source):
    """Veto explicit geography conflicts only with independently sourced evidence.

    Bare Remote is never interpreted as US. URL slugs and discovery candidates
    are never authoritative inputs. Unknown geography does not lower scores.
    """
    if source not in AUTHORITATIVE_LOCATION_SOURCES or not resolved:
        return "unknown", "authoritative resolved location unavailable"
    if not original:
        return "unknown", "original posting geography unavailable"

    def geography(text):
        raw = str(text)
        normalized = re.sub(r"[^a-z0-9]+", " ", raw.casefold()).strip()
        countries = set()
        for code, pattern in {
            "US": r"\b(united states(?: of america)?|usa|us|u s a?)\b",
            "GB": r"\b(united kingdom|great britain|gb|uk|u k)\b",
            "CA": r"\b(canada)\b", "AU": r"\b(australia)\b",
        }.items():
            if re.search(pattern, normalized):
                countries.add(code)
        regions = set()
        for code, name in US_REGIONS.items():
            # Two-letter regions must be comma-delimited, avoiding ordinary words.
            if re.search(r"(?:^|,)\s*" + code + r"(?:\s*(?:[,;/)]|$))", raw) or re.search(r"\b" + name + r"\b", normalized):
                regions.add(code)
        if regions:
            countries.add("US")
        remote = bool(re.search(r"\bremote\b", normalized))
        return countries, regions, remote, normalized

    oc, os, remote_original, original_text = geography(original)
    rc, rs, remote_resolved, resolved_text = geography(resolved)
    if oc and rc and not oc.intersection(rc):
        return "mismatch", "authoritative country conflicts with original posting geography"
    if remote_original and not oc:
        if rc == {"US"} and remote_resolved:
            return "compatible", "remote posting with authoritative US remote scope"
        return "unknown", "original Remote has no country scope; geographic identity unproven"
    if oc and rc and oc.intersection(rc) and remote_resolved:
        return "compatible", "remote scope includes original posting country"
    if os and rs and not os.intersection(rs):
        return "mismatch", "authoritative US state conflicts with original posting geography"
    if os and rs and os.intersection(rs):
        def cities(text, regions):
            aliases = "|".join(re.escape(value) for code in regions for value in (code.lower(), US_REGIONS[code]))
            return {match.group(1).strip().casefold() for match in re.finditer(
                r"(?:^|[;,])\s*([^,;]+),\s*(?:" + aliases + r")\b", str(text), re.I
            )}
        original_cities = cities(original, os)
        resolved_cities = cities(resolved, rs)
        if original_cities and resolved_cities:
            if original_cities.intersection(resolved_cities):
                return "compatible", "matching authoritative city and state"
            return "mismatch", "authoritative city conflicts with original posting geography"
        return "unknown", "same state; city identity not established"
    if oc and rc and oc.intersection(rc):
        return "compatible", "authoritative country includes original posting country"
    return "unknown", "insufficient geographic scope to establish compatibility"


def validate_job_identity(
    original_title: str | None,
    original_company: str | None,
    resolved_title: str | None,
    resolved_company: str | None,
    original_location: str | None = None,
    resolved_location: str | None = None,
    resolved_location_source: str | None = None,
    trusted_original_posting_key: str | None = None,
    resolved_posting_key: str | None = None,
) -> JobIdentityValidation:
    location_status, location_reason = location_compatibility(
        original_location, resolved_location, resolved_location_source
    )

    normalized_original_title = normalize_job_title(original_title)
    normalized_resolved_title = normalize_job_title(resolved_title)
    normalized_original_company = normalize_company_name(original_company)
    normalized_resolved_company = normalize_company_name(resolved_company)

    title_similarity = similarity(
        normalized_original_title,
        normalized_resolved_title,
    )
    company_similarity = similarity(
        normalized_original_company,
        normalized_resolved_company,
    )

    original_occupation = primary_occupation(original_title)
    resolved_occupation = primary_occupation(resolved_title)
    occupation_match = None

    if original_occupation and resolved_occupation:
        occupation_match = original_occupation == resolved_occupation

    reasons: list[str] = []

    if title_similarity is None:
        reasons.append("resolved title missing")
    elif title_similarity < TITLE_REASONABLE_THRESHOLD:
        reasons.append("title mismatch")

    company_available = bool(
        normalized_original_company
        and normalized_resolved_company
    )

    if company_available:
        if (
            company_similarity is None
            or company_similarity < COMPANY_MATCH_THRESHOLD
        ):
            reasons.append("company mismatch")
    else:
        reasons.append("company unavailable")

    if occupation_match is False:
        reasons.append("primary occupation mismatch")

    if company_available:
        confidence = (
            (title_similarity or 0.0) * 0.45
            + (company_similarity or 0.0) * 0.45
            + (0.10 if occupation_match is not False else 0.0)
        )
        accepted = (
            confidence >= ACCEPT_WITH_COMPANY_THRESHOLD
            and "company mismatch" not in reasons
            and "primary occupation mismatch" not in reasons
            and "title mismatch" not in reasons
        )
    else:
        exact_posting_proof = bool(
            trusted_original_posting_key
            and resolved_posting_key
            and trusted_original_posting_key == resolved_posting_key
        )
        confidence = (
            (title_similarity or 0.0) * (0.75 if exact_posting_proof else 0.45)
            + (0.10 if occupation_match is not False else 0.0)
        )
        accepted = (
            exact_posting_proof
            and title_similarity is not None
            and title_similarity >= ACCEPT_WITHOUT_COMPANY_THRESHOLD
            and "primary occupation mismatch" not in reasons
        )
        if not exact_posting_proof:
            reasons.append("independent same-posting proof unavailable")

    if location_status == "mismatch":
        accepted = False
        reasons.append("location mismatch: " + location_reason)

    if accepted:
        reason = ("identity accepted" if company_available else
                  "identity accepted with trusted exact posting proof; company unavailable")
    else:
        reason = "; ".join(reasons) or "identity confidence below threshold"

    return JobIdentityValidation(
        accepted=accepted,
        confidence=round(confidence, 4),
        title_similarity=(
            None
            if title_similarity is None
            else round(title_similarity, 4)
        ),
        company_similarity=(
            None
            if company_similarity is None
            else round(company_similarity, 4)
        ),
        occupation_match=occupation_match,
        reason=reason,
        location_compatibility=location_status,
        location_reason=location_reason,
    )
