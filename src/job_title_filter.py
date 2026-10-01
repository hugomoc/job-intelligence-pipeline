"""Deterministic job-title classification for pipeline triage.

This module is the cheap, explainable first pass before enrichment and AI
scoring. It classifies the occupation implied by a title, not every technology
word that appears in the title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


TitleCategory = Literal[
    "STRONG_MATCH",
    "POSSIBLE_MATCH",
    "FILTERED_OUT",
]

STRONG_MATCH = "STRONG_MATCH"
POSSIBLE_MATCH = "POSSIBLE_MATCH"
FILTERED_OUT = "FILTERED_OUT"

EXCLUDE_JUNIOR_ROLES = True
EXCLUDE_MANAGEMENT_ROLES = True

_LOCATION_NOISE = {
    "remote",
    "hybrid",
    "onsite",
    "on site",
    "usa",
    "us",
    "united states",
}

_ABBREVIATIONS: tuple[tuple[str, str], ...] = (
    (r"\bsr\b", "senior"),
    (r"\bjr\b", "junior"),
    (r"\bbi\b", "business intelligence"),
)


@dataclass(frozen=True)
class TitlePattern:
    regex: str
    matched_pattern: str
    reason: str
    score: int


@dataclass(frozen=True)
class TitleClassification:
    category: TitleCategory
    score: int
    reason: str
    matched_pattern: str | None
    normalized_title: str


JUNIOR_TITLE_PATTERNS: tuple[TitlePattern, ...] = (
    TitlePattern(
        r"\b(junior|entry level|entry-level|new grad|graduate|internship|intern)\b",
        "junior",
        "title indicates a junior or early-career role",
        10,
    ),
)

MANAGEMENT_TITLE_PATTERNS: tuple[TitlePattern, ...] = (
    TitlePattern(
        r"\b(engineering|analytics|business intelligence|data engineering|data|bi)\s+manager\b",
        "manager",
        "title is a management role rather than a hands-on IC role",
        20,
    ),
    TitlePattern(
        r"\b(manager|director|vp|vice president|head of)\b.*\b(data|analytics|business intelligence|bi|engineering)\b",
        "management",
        "title is a management role rather than a hands-on IC role",
        20,
    ),
    TitlePattern(
        r"\b(data|analytics|business intelligence|bi|engineering)\b.*\b(manager|director|vp|vice president)\b",
        "management",
        "title is a management role rather than a hands-on IC role",
        20,
    ),
)

EXCLUDED_TITLE_PATTERNS: tuple[TitlePattern, ...] = (
    TitlePattern(r"^[^a-z0-9]+$", "malformed punctuation", "title is malformed punctuation", 0),
    TitlePattern(r"^hi\s+there\b", "email greeting", "title is an email greeting", 0),
    TitlePattern(r"^\[", "malformed link text", "title is malformed link text", 0),
    TitlePattern(r"\bsoftware\s+(engineer|developer)\b", "software engineer", "primary job family is Software Engineering", 5),
    TitlePattern(r"\bapplication\s+developer\b", "application developer", "primary job family is Application Development", 10),
    TitlePattern(r"\b(java|oracle)\s+developer\b", "specialist developer", "primary job family is a non-target specialist developer role", 10),
    TitlePattern(r"\bbackend\s+(software\s+)?(engineer|developer)\b", "backend engineer", "primary job family is Software Engineering", 10),
    TitlePattern(r"\bfront\s*end\s+(engineer|developer)\b", "frontend engineer", "primary job family is Software Engineering", 10),
    TitlePattern(r"\bfull\s*stack\s+(engineer|developer)\b", "full stack engineer", "primary job family is Software Engineering", 10),
    TitlePattern(r"\b(mobile|ios|android)\s+(engineer|developer)\b", "mobile engineer", "primary job family is Mobile Engineering", 10),
    TitlePattern(r"\b(machine learning|ml|ai|generative ai|deep learning|nlp|computer vision|mlops)\s+engineer\b", "machine learning engineer", "primary job family is Machine Learning or AI Engineering", 10),
    TitlePattern(r"\b(data|applied|research)\s+scientist\b", "data scientist", "primary job family is Data Science", 10),
    TitlePattern(r"\b(devops|site reliability|sre|network|security|cybersecurity|cloud infrastructure|infrastructure)\s+(engineer|administrator|analyst)\b", "infrastructure", "primary job family is Infrastructure, IT or Security", 10),
    TitlePattern(r"\bsoc\s+analyst\b", "soc analyst", "primary job family is Security Operations", 10),
    TitlePattern(r"\b(database administrator|dba|oracle dba|sql server dba|mysql dba)\b", "database administrator", "primary job family is Database Administration", 10),
    TitlePattern(r"\b(technical\s+)?product\s+(manager|owner)\b", "product manager", "primary job family is Product Management", 10),
    TitlePattern(r"\b(project|program)\s+manager\b", "project manager", "primary job family is Project or Program Management", 10),
    TitlePattern(r"\bscrum\s+master\b", "scrum master", "primary job family is Scrum Master", 10),
    TitlePattern(r"\b(financial|finance|marketing|sales|hr|operations|investment)\s+analyst\b", "business analyst", "primary job family is not target data/BI engineering", 20),
    TitlePattern(r"\bbusiness\s+analyst\b", "business analyst", "primary job family is Business Analysis", 30),
    TitlePattern(r"\b(qa|test)\s+(engineer|analyst)\b", "qa engineer", "primary job family is QA or Testing", 10),
    TitlePattern(r"\b(help desk|desktop support|technical support specialist|network support)\b", "support", "primary job family is IT Support", 10),
    TitlePattern(r"\b(accountant|accounting manager)\b", "accounting", "primary job family is Accounting", 10),
    TitlePattern(r"\b(unqork|aep|rtcdp)\b", "excluded specialist platform", "title focuses on a non-target specialist platform", 20),
    TitlePattern(r"\badobe\s+experience\s+platform\b", "adobe experience platform", "title focuses on Adobe Experience Platform", 20),
)

STRONG_TITLE_PATTERNS: tuple[TitlePattern, ...] = (
    TitlePattern(r"\bdata\s+engineer\b", "data engineer", "Data Engineer is a primary target job family", 95),
    TitlePattern(r"\banalytics\s+engineer\b", "analytics engineer", "Analytics Engineer is a primary target job family", 95),
    TitlePattern(r"\b(business intelligence|bi)\s+engineer\b", "business intelligence engineer", "BI Engineer is a primary target job family", 92),
    TitlePattern(r"\b(business intelligence|bi)\s+developer\b", "business intelligence developer", "BI Developer is a primary target job family", 90),
    TitlePattern(r"\bdata\s+(warehouse|warehousing)\s+engineer\b", "data warehouse engineer", "Data Warehouse Engineering is a primary target job family", 92),
    TitlePattern(r"\bdata\s+integration\s+engineer\b", "data integration engineer", "Data Integration Engineering is a primary target job family", 90),
    TitlePattern(r"\b(etl|elt)\s+engineer\b", "etl engineer", "ETL/ELT Engineering is a primary target job family", 88),
    TitlePattern(r"\bsnowflake\s+(data\s+)?(engineer|developer)\b", "snowflake engineer", "Snowflake data role is a primary target job family", 90),
    TitlePattern(r"\bsnowflake\s+analytics\s+engineer\b", "snowflake analytics engineer", "Snowflake Analytics Engineering is a primary target job family", 94),
    TitlePattern(r"\blooker\s+(developer|engineer|architect|analytics\s+engineer)\b", "looker", "Looker role is a primary target job family", 88),
    TitlePattern(r"\bsemantic\s+(layer\s+)?(engineer|developer|modeler)\b", "semantic layer", "Semantic-layer role is a primary target job family", 88),
    TitlePattern(r"\bmetrics\s+engineer\b", "metrics engineer", "Metrics Engineering is a primary target job family", 86),
    TitlePattern(r"\b(analytics|business intelligence|bi|data analytics|data and analytics)\s+architect\b", "analytics architect", "Analytics/BI architecture is a target job family", 82),
    TitlePattern(r"\bdata\s+warehouse\s+architect\b", "data warehouse architect", "Data Warehouse Architecture is a target job family", 84),
    TitlePattern(r"\bdata\s+and\s+analytics\s+engineer\b", "data and analytics engineer", "Data and Analytics Engineering is a primary target job family", 92),
)

POSSIBLE_TITLE_PATTERNS: tuple[TitlePattern, ...] = (
    TitlePattern(r"\bdata\s+business\s+analyst\b", "data business analyst", "Data business analysis may be relevant", 65),
    TitlePattern(r"\b(data|analytics|business intelligence|bi|technical data)\s+analyst\b", "data analyst", "Analyst title may be relevant after description review", 65),
    TitlePattern(r"\breporting\s+(analyst|developer|engineer)\b", "reporting", "Reporting role may be relevant after description review", 62),
    TitlePattern(r"\b(data|cloud data|data solutions)\s+architect\b", "data architect", "Data architecture title may be relevant after description review", 70),
    TitlePattern(r"\bsolutions\s+architect\b.*\bdata\b", "solutions architect data", "Data-focused solutions architecture may be relevant", 60),
    TitlePattern(r"\bdata\s+platform\s+(engineer|developer)\b", "data platform", "Data platform role may be relevant after description review", 72),
    TitlePattern(r"\bdatabase\s+engineer\b", "database engineer", "Database Engineer may be relevant after description review", 60),
    TitlePattern(r"\b(staff|principal)\s+(data|analytics)\s+engineer\b", "staff/principal data engineer", "Very senior data role may be relevant after description review", 75),
)

EXCLUDED_TITLE_SQL_REGEX = "|".join(
    f"({pattern.regex})"
    for pattern in (
        JUNIOR_TITLE_PATTERNS
        + MANAGEMENT_TITLE_PATTERNS
        + EXCLUDED_TITLE_PATTERNS
    )
)


def normalize_job_title(title: str | None) -> str:
    if not title:
        return ""

    normalized = title.casefold().strip()
    normalized = normalized.replace("–", "-")
    normalized = normalized.replace("—", "-")
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[()\[\]{}|/,:;]+", " ", normalized)
    normalized = re.sub(r"\s+-\s+", " ", normalized)
    normalized = re.sub(r"[._]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    for pattern, replacement in _ABBREVIATIONS:
        normalized = re.sub(pattern, replacement, normalized)

    tokens = [
        token
        for token in normalized.split()
        if token not in _LOCATION_NOISE
    ]

    return " ".join(tokens)


def _match_first(
    normalized_title: str,
    patterns: tuple[TitlePattern, ...],
) -> TitlePattern | None:
    for pattern in patterns:
        if re.search(pattern.regex, normalized_title):
            return pattern

    return None


def classify_job_title(title: str | None) -> TitleClassification:
    raw_title = (title or "").strip()

    if raw_title.startswith("["):
        return TitleClassification(
            category=FILTERED_OUT,
            score=0,
            reason="title is malformed link text",
            matched_pattern="malformed link text",
            normalized_title=normalize_job_title(title),
        )

    if raw_title and not re.search(r"[a-z0-9]", raw_title.casefold()):
        return TitleClassification(
            category=FILTERED_OUT,
            score=0,
            reason="title is malformed punctuation",
            matched_pattern="malformed punctuation",
            normalized_title=normalize_job_title(title),
        )

    normalized_title = normalize_job_title(title)

    if not normalized_title:
        return TitleClassification(
            category=POSSIBLE_MATCH,
            score=50,
            reason="title is missing or blank",
            matched_pattern=None,
            normalized_title=normalized_title,
        )

    if EXCLUDE_JUNIOR_ROLES:
        junior_match = _match_first(
            normalized_title,
            JUNIOR_TITLE_PATTERNS,
        )
        if junior_match:
            return TitleClassification(
                category=FILTERED_OUT,
                score=junior_match.score,
                reason=junior_match.reason,
                matched_pattern=junior_match.matched_pattern,
                normalized_title=normalized_title,
            )

    if EXCLUDE_MANAGEMENT_ROLES:
        management_match = _match_first(
            normalized_title,
            MANAGEMENT_TITLE_PATTERNS,
        )
        if management_match:
            return TitleClassification(
                category=FILTERED_OUT,
                score=management_match.score,
                reason=management_match.reason,
                matched_pattern=management_match.matched_pattern,
                normalized_title=normalized_title,
            )

    data_business_analyst = _match_first(
        normalized_title,
        (POSSIBLE_TITLE_PATTERNS[0],),
    )
    if data_business_analyst:
        return TitleClassification(
            category=POSSIBLE_MATCH,
            score=data_business_analyst.score,
            reason=data_business_analyst.reason,
            matched_pattern=data_business_analyst.matched_pattern,
            normalized_title=normalized_title,
        )

    excluded_match = _match_first(
        normalized_title,
        EXCLUDED_TITLE_PATTERNS,
    )
    if excluded_match:
        return TitleClassification(
            category=FILTERED_OUT,
            score=excluded_match.score,
            reason=excluded_match.reason,
            matched_pattern=excluded_match.matched_pattern,
            normalized_title=normalized_title,
        )

    strong_match = _match_first(
        normalized_title,
        STRONG_TITLE_PATTERNS,
    )
    if strong_match:
        return TitleClassification(
            category=STRONG_MATCH,
            score=strong_match.score,
            reason=strong_match.reason,
            matched_pattern=strong_match.matched_pattern,
            normalized_title=normalized_title,
        )

    possible_match = _match_first(
        normalized_title,
        POSSIBLE_TITLE_PATTERNS,
    )
    if possible_match:
        return TitleClassification(
            category=POSSIBLE_MATCH,
            score=possible_match.score,
            reason=possible_match.reason,
            matched_pattern=possible_match.matched_pattern,
            normalized_title=normalized_title,
        )

    return TitleClassification(
        category=POSSIBLE_MATCH,
        score=50,
        reason="title did not match a configured target or exclusion family",
        matched_pattern=None,
        normalized_title=normalized_title,
    )


def excluded_job_title_reason(title: str | None) -> str | None:
    classification = classify_job_title(title)

    if classification.category == FILTERED_OUT:
        return classification.reason

    return None


def is_excluded_job_title(title: str | None) -> bool:
    return classify_job_title(title).category == FILTERED_OUT
