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


def validate_job_identity(
    original_title: str | None,
    original_company: str | None,
    resolved_title: str | None,
    resolved_company: str | None,
    original_location: str | None = None,
    resolved_location: str | None = None,
) -> JobIdentityValidation:
    del original_location, resolved_location

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
        confidence = (
            (title_similarity or 0.0) * 0.90
            + (0.10 if occupation_match is not False else 0.0)
        )
        accepted = (
            confidence >= ACCEPT_WITHOUT_COMPANY_THRESHOLD
            and title_similarity is not None
            and title_similarity >= ACCEPT_WITHOUT_COMPANY_THRESHOLD
            and "primary occupation mismatch" not in reasons
        )

    if accepted:
        reason = "identity accepted"
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
    )
