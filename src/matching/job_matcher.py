"""Rule-based job matcher used as the first cheap relevance pass.

This module does deterministic scoring from titles, descriptions, locations,
freshness, and configured keywords. AI scoring happens later only for jobs that
survive this inexpensive filter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


TOKEN_PATTERN = re.compile(r"[a-z0-9+#.]+")
STATE_PATTERN = re.compile(r",\s*([A-Z]{2})\b")


@dataclass
class MatchResult:
    record_key: str
    search_id: str
    search_title: str

    match_score: int
    title_score: int
    location_score: int
    keyword_score: int
    freshness_score: int

    matched_keywords: list[str]
    excluded_keywords: list[str]
    reasons: list[str]

    is_recommended: bool
    needs_review: bool


def normalize_text(value: str | None) -> str:
    if not value:
        return ""

    normalized = value.casefold()
    normalized = normalized.replace("–", "-")
    normalized = normalized.replace("—", "-")
    normalized = re.sub(
        r"[^a-z0-9+#.]+",
        " ",
        normalized,
    )

    return " ".join(normalized.split())


def tokenize(value: str | None) -> set[str]:
    return set(
        TOKEN_PATTERN.findall(
            normalize_text(value)
        )
    )


def contains_term(
    searchable_text: str,
    term: str,
) -> bool:
    normalized_text = normalize_text(
        searchable_text
    )
    normalized_term = normalize_text(term)

    if not normalized_term:
        return False

    return (
        f" {normalized_term} "
        in f" {normalized_text} "
    )


def unique_strings(
    values: list[str] | None,
) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()

    for value in values or []:
        cleaned = value.strip()

        if not cleaned:
            continue

        normalized = normalize_text(cleaned)

        if normalized in seen:
            continue

        seen.add(normalized)
        results.append(cleaned)

    return results


def merge_search_configuration(
    defaults: dict[str, Any],
    search: dict[str, Any],
) -> dict[str, Any]:
    default_keywords = defaults.get(
        "keywords",
        {},
    )

    search_keywords = search.get(
        "keywords",
        {},
    )

    all_keywords = unique_strings(
        default_keywords.get("all", [])
        + search_keywords.get("all", [])
    )

    any_keywords = unique_strings(
        default_keywords.get("any", [])
        + search_keywords.get("any", [])
    )

    exclude_keywords = unique_strings(
        defaults.get("exclude_keywords", [])
        + search.get("exclude_keywords", [])
    )

    return {
        **defaults,
        **search,
        "keywords": {
            "all": all_keywords,
            "any": any_keywords,
        },
        "exclude_keywords": exclude_keywords,
    }


def calculate_title_score(
    job_title: str | None,
    target_title: str | None,
) -> int:
    actual = normalize_text(job_title)
    target = normalize_text(target_title)

    if not actual or not target:
        return 0

    if actual == target:
        return 45

    # Examples:
    # "Senior Data Engineer" contains "Data Engineer"
    # "Data Engineer - Analytics" contains "Data Engineer"
    if contains_term(actual, target):
        return 40

    actual_tokens = tokenize(actual)
    target_tokens = tokenize(target)

    if not target_tokens:
        return 0

    matching_tokens = (
        actual_tokens
        & target_tokens
    )

    coverage = (
        len(matching_tokens)
        / len(target_tokens)
    )

    if coverage == 1:
        return 32

    if coverage >= 0.5:
        return round(24 * coverage)

    return 0


def extract_state(
    location: str | None,
) -> str | None:
    if not location:
        return None

    match = STATE_PATTERN.search(location)

    if not match:
        return None

    return match.group(1).upper()


def calculate_location_score(
    job_location: str | None,
    search_location: str | None,
    remote_requested: bool | None,
) -> int:
    actual = normalize_text(job_location)
    target = normalize_text(search_location)

    if not actual:
        return 0

    if remote_requested is True:
        if "remote" in actual:
            return 20

        if actual in {
            "united states",
            "usa",
            "us",
        }:
            return 15

        return 0

    if not target:
        return 10

    if actual == target:
        return 20

    target_state = extract_state(
        search_location
    )

    actual_state = extract_state(
        job_location
    )

    if (
        target_state
        and actual_state
        and target_state == actual_state
    ):
        return 10

    if actual in {
        "united states",
        "usa",
        "us",
    }:
        return 5

    return 0


def calculate_keyword_score(
    searchable_text: str,
    all_keywords: list[str],
    any_keywords: list[str],
) -> tuple[int, list[str]]:
    matched_all = [
        keyword
        for keyword in all_keywords
        if contains_term(
            searchable_text,
            keyword,
        )
    ]

    matched_any = [
        keyword
        for keyword in any_keywords
        if contains_term(
            searchable_text,
            keyword,
        )
    ]

    all_score = 0

    if all_keywords:
        all_score = round(
            10
            * len(matched_all)
            / len(all_keywords)
        )

    # Five points for each optional keyword,
    # capped at 20 points.
    any_score = min(
        20,
        len(matched_any) * 5,
    )

    matched_keywords = unique_strings(
        matched_all + matched_any
    )

    return (
        all_score + any_score,
        matched_keywords,
    )


def calculate_freshness_score(
    discovered_at: Any,
) -> int:
    if not discovered_at:
        return 0

    discovered: datetime | None = None

    if isinstance(discovered_at, datetime):
        discovered = discovered_at

    elif isinstance(discovered_at, str):
        try:
            discovered = datetime.fromisoformat(
                discovered_at.replace(
                    "Z",
                    "+00:00",
                )
            )
        except ValueError:
            return 0

    if discovered is None:
        return 0

    if discovered.tzinfo is None:
        discovered = discovered.replace(
            tzinfo=timezone.utc
        )

    now = datetime.now(timezone.utc)

    age_days = max(
        0,
        (
            now
            - discovered.astimezone(
                timezone.utc
            )
        ).total_seconds()
        / 86_400,
    )

    if age_days <= 1:
        return 5

    if age_days <= 3:
        return 4

    if age_days <= 7:
        return 2

    return 0


def find_excluded_keywords(
    searchable_text: str,
    exclusions: list[str],
) -> list[str]:
    return [
        keyword
        for keyword in exclusions
        if contains_term(
            searchable_text,
            keyword,
        )
    ]


def score_job_against_search(
    job: dict[str, Any],
    search: dict[str, Any],
    defaults: dict[str, Any],
) -> MatchResult:
    effective_search = (
        merge_search_configuration(
            defaults=defaults,
            search=search,
        )
    )

    job_title = job.get("title", "")
    job_location = job.get(
        "location",
        "",
    )
    job_description = job.get(
        "description",
        "",
    )
    company_name = job.get(
        "company_name",
        "",
    )

    searchable_text = " ".join(
        [
            str(job_title or ""),
            str(company_name or ""),
            str(job_location or ""),
            str(job_description or ""),
        ]
    )

    title_score = calculate_title_score(
        job_title=job_title,
        target_title=effective_search.get(
            "title"
        ),
    )

    location_score = calculate_location_score(
        job_location=job_location,
        search_location=effective_search.get(
            "location"
        ),
        remote_requested=effective_search.get(
            "remote"
        ),
    )

    keyword_config = effective_search.get(
        "keywords",
        {},
    )

    (
        keyword_score,
        matched_keywords,
    ) = calculate_keyword_score(
        searchable_text=searchable_text,
        all_keywords=keyword_config.get(
            "all",
            [],
        ),
        any_keywords=keyword_config.get(
            "any",
            [],
        ),
    )

    freshness_score = (
        calculate_freshness_score(
            job.get("discovered_at")
        )
    )

    excluded_keywords = (
        find_excluded_keywords(
            searchable_text=searchable_text,
            exclusions=effective_search.get(
                "exclude_keywords",
                [],
            ),
        )
    )

    match_score = min(
        100,
        title_score
        + location_score
        + keyword_score
        + freshness_score,
    )

    reasons = [
        f"Title alignment: {title_score}/45",
        (
            "Location alignment: "
            f"{location_score}/20"
        ),
        (
            "Keyword alignment: "
            f"{keyword_score}/30"
        ),
        (
            "Freshness: "
            f"{freshness_score}/5"
        ),
    ]

    if matched_keywords:
        reasons.append(
            "Matched keywords: "
            + ", ".join(matched_keywords)
        )

    if excluded_keywords:
        reasons.append(
            "Excluded keywords found: "
            + ", ".join(
                excluded_keywords
            )
        )

    threshold = int(
        effective_search.get(
            "recommendation_threshold",
            60,
        )
    )

    if excluded_keywords:
        match_score = 0

    description_available = bool(
        normalize_text(job_description)
    )

    has_skill_evidence = keyword_score > 0

    # Recommended:
    # Strong title, acceptable overall score,
    # and at least one confirmed skill match.
    is_recommended = (
        not excluded_keywords
        and title_score >= 20
        and match_score >= threshold
        and has_skill_evidence
    )

    # Review:
    # Strong title and remote/national location,
    # but not enough description or skill information.
    needs_review = (
        not excluded_keywords
        and not is_recommended
        and title_score >= 40
        and location_score >= 15
        and (
            not description_available
            or not has_skill_evidence
        )
    )

    return MatchResult(
        record_key=job["record_key"],
        search_id=effective_search[
            "search_id"
        ],
        search_title=effective_search[
            "title"
        ],
        match_score=match_score,
        title_score=title_score,
        location_score=location_score,
        keyword_score=keyword_score,
        freshness_score=freshness_score,
        matched_keywords=matched_keywords,
        excluded_keywords=excluded_keywords,
        reasons=reasons,
        is_recommended=is_recommended,
        needs_review=needs_review,
    )


def score_all_jobs(
    jobs: list[dict[str, Any]],
    searches: list[dict[str, Any]],
    defaults: dict[str, Any],
) -> list[MatchResult]:
    results: list[MatchResult] = []

    for job in jobs:
        for search in searches:
            results.append(
                score_job_against_search(
                    job=job,
                    search=search,
                    defaults=defaults,
                )
            )

    return results
