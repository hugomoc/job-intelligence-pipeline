"""Shared types and helpers for thin ATS candidate discovery adapters."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Protocol
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from src.resolvers.job_matching import (
    normalize_text,
    score_candidate,
)


MAX_ATS_RESULTS = 20
MAX_ATS_PAGES = 2
ATS_REQUEST_TIMEOUT = 20


@dataclass(frozen=True)
class OfficialJobCandidate:
    url: str
    label: str
    source: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    job_id: str | None = None


class ATSAdapter(Protocol):
    source: str

    def discover_candidates(
        self,
        job: dict,
        client: httpx.Client,
    ) -> list[OfficialJobCandidate]:
        ...


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def company_slug_candidates(company_name: str | None) -> list[str]:
    normalized = normalize_text(company_name)

    if not normalized:
        return []

    stop_words = {
        "and",
        "co",
        "company",
        "corp",
        "corporation",
        "group",
        "holdings",
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "solutions",
        "technologies",
        "technology",
        "the",
    }
    tokens = [
        token
        for token in normalized.split()
        if token not in stop_words
    ]

    compact = "".join(tokens)
    hyphenated = "-".join(tokens)
    underscored = "_".join(tokens)

    candidates = [
        value
        for value in (
            compact,
            hyphenated,
            underscored,
            normalized.replace(" ", ""),
            normalized.replace(" ", "-"),
        )
        if value
    ]

    return list(dict.fromkeys(candidates))


def response_json(response: httpx.Response) -> Any | None:
    try:
        return response.json()
    except Exception:
        try:
            return json.loads(response.text)
        except Exception:
            return None


def safe_get(
    client: httpx.Client,
    url: str,
) -> httpx.Response | None:
    try:
        response = client.get(url)
    except httpx.RequestError:
        return None

    if response.status_code >= 400:
        return None

    return response


def walk_json(value: Any):
    if isinstance(value, dict):
        yield value

        for child in value.values():
            yield from walk_json(child)

    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def string_from_path(
    value: dict[str, Any],
    *path: str,
) -> str | None:
    current: Any = value

    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)

    cleaned = clean_text(current)
    return cleaned or None


def location_from_value(value: Any) -> str | None:
    if isinstance(value, str):
        return clean_text(value) or None

    if isinstance(value, dict):
        pieces = [
            value.get("name"),
            value.get("locationName"),
            value.get("city"),
            value.get("region"),
            value.get("state"),
            value.get("country"),
        ]

        cleaned = [
            clean_text(piece)
            for piece in pieces
            if clean_text(piece)
        ]

        return ", ".join(dict.fromkeys(cleaned)) or None

    return None


def make_candidate(
    url: str | None,
    title: str | None,
    company: str | None,
    location: str | None,
    source: str,
    base_url: str | None = None,
    job_id: str | None = None,
) -> OfficialJobCandidate | None:
    if not url:
        return None

    full_url = urljoin(base_url, url) if base_url else url
    title_text = clean_text(title)

    if not title_text:
        return None

    company_text = clean_text(company) or company
    location_text = clean_text(location) or None
    label_parts = [
        title_text,
        company_text,
        location_text,
    ]
    label = " | ".join(
        str(part)
        for part in label_parts
        if part
    )

    return OfficialJobCandidate(
        url=full_url,
        label=label,
        source=source,
        title=title_text,
        company=company_text,
        location=location_text,
        job_id=clean_text(job_id) or None,
    )


def unique_candidates(
    candidates: Iterable[OfficialJobCandidate | None],
) -> list[OfficialJobCandidate]:
    unique: list[OfficialJobCandidate] = []
    seen: set[str] = set()

    for candidate in candidates:
        if candidate is None or candidate.url in seen:
            continue

        seen.add(candidate.url)
        unique.append(candidate)

    return unique


def filter_and_rank_candidates(
    job: dict,
    candidates: Iterable[OfficialJobCandidate | None],
    max_results: int = MAX_ATS_RESULTS,
) -> list[OfficialJobCandidate]:
    expected_title = str(job.get("title") or "")
    expected_company = str(job.get("company_name") or "")
    scored: list[tuple[int, int, OfficialJobCandidate]] = []

    for candidate in unique_candidates(candidates):
        match = score_candidate(
            expected_title=expected_title,
            expected_company=expected_company,
            candidate_title=candidate.title or candidate.label,
            candidate_company=candidate.company or expected_company,
        )

        if match.score < 70 or match.title_score < 75:
            continue

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

        scored.append(
            (
                match.score,
                metadata_bonus,
                candidate,
            )
        )

    scored.sort(
        key=lambda item: (
            item[0],
            item[1],
        ),
        reverse=True,
    )

    return [
        candidate
        for _, _, candidate in scored[:max_results]
    ]


def anchors_from_html(
    html_text: str,
    base_url: str,
    source: str,
    expected_company: str | None,
) -> list[OfficialJobCandidate]:
    soup = BeautifulSoup(
        html_text,
        "html.parser",
    )
    candidates: list[OfficialJobCandidate] = []

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        text = clean_text(anchor.get_text(" ", strip=True))

        if not text or not href:
            continue

        if not re.search(r"/job|jobs|posting|requisition", href, re.I):
            continue

        parent_text = clean_text(
            anchor.parent.get_text(" ", strip=True)
            if anchor.parent
            else ""
        )
        location = None

        if parent_text and parent_text != text:
            location = parent_text.replace(text, " ").strip()

        candidate = make_candidate(
            url=href,
            title=text,
            company=expected_company,
            location=location,
            source=source,
            base_url=base_url,
        )

        if candidate:
            candidates.append(candidate)

    return unique_candidates(candidates)
