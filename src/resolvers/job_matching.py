"""Fuzzy title/company matching for resolver candidate pages.

Some job-alert links open a page with many jobs. These helpers score each card
against the expected title/company and reject ambiguous matches instead of
clicking the wrong job.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher


REMOTE_TITLE_WORDS = {
    "remote",
    "hybrid",
    "onsite",
    "on",
    "site",
}

AMBIGUITY_MARGIN = 8
MINIMUM_SELECTION_SCORE = 75


@dataclass(frozen=True)
class CandidateMatch:
    title: str
    company: str
    score: int
    title_score: int
    company_score: int
    reason: str


def normalize_text(value: str | None) -> str:
    if not value:
        return ""

    normalized = unicodedata.normalize("NFKD", value)
    normalized = normalized.casefold()
    normalized = "".join(
        " "
        if unicodedata.category(character).startswith("P")
        else character
        for character in normalized
    )
    normalized = re.sub(r"\s+", " ", normalized)

    return normalized.strip()


def token_set(value: str | None) -> set[str]:
    return {
        token
        for token in normalize_text(value).split()
        if token
    }


def title_tokens(value: str | None) -> set[str]:
    return token_set(value) - REMOTE_TITLE_WORDS


def ratio(left: str | None, right: str | None) -> int:
    left_normalized = normalize_text(left)
    right_normalized = normalize_text(right)

    if not left_normalized or not right_normalized:
        return 0

    if left_normalized == right_normalized:
        return 100

    return round(
        SequenceMatcher(
            None,
            left_normalized,
            right_normalized,
        ).ratio()
        * 100
    )


def token_overlap_score(
    expected: set[str],
    candidate: set[str],
) -> int:
    if not expected or not candidate:
        return 0

    overlap = len(expected & candidate)

    return round(
        overlap
        / max(len(expected), len(candidate))
        * 100
    )


def score_title(
    expected: str,
    candidate: str,
) -> int:
    expected_normalized = normalize_text(expected)
    candidate_normalized = normalize_text(candidate)

    if expected_normalized == candidate_normalized:
        return 100

    expected_tokens = title_tokens(expected)
    candidate_tokens = title_tokens(candidate)
    overlap = token_overlap_score(expected_tokens, candidate_tokens)

    if expected_tokens and expected_tokens <= candidate_tokens:
        overlap = max(overlap, 92)

    return max(
        ratio(expected, candidate),
        overlap,
    )


def score_company(
    expected: str,
    candidate: str,
) -> int:
    expected_normalized = normalize_text(expected)
    candidate_normalized = normalize_text(candidate)

    if expected_normalized == candidate_normalized:
        return 100

    expected_tokens = token_set(expected)
    candidate_tokens = token_set(candidate)
    overlap = token_overlap_score(expected_tokens, candidate_tokens)

    return max(
        ratio(expected, candidate),
        overlap,
    )


def score_candidate(
    expected_title: str,
    expected_company: str,
    candidate_title: str,
    candidate_company: str,
) -> CandidateMatch:
    title_score = score_title(
        expected=expected_title,
        candidate=candidate_title,
    )
    company_score = score_company(
        expected=expected_company,
        candidate=candidate_company,
    )

    if company_score < 70:
        combined_score = min(title_score, 45)
        reason = "company mismatch"
    else:
        combined_score = round(
            title_score * 0.55
            + company_score * 0.45
        )

        if title_score == 100 and company_score == 100:
            reason = "exact normalized title/company match"
        elif title_score >= 90 and company_score == 100:
            reason = "very strong title match and exact company"
        elif title_score >= 80 and company_score >= 80:
            reason = "strong title/company match"
        else:
            reason = "weak match"

    return CandidateMatch(
        title=candidate_title,
        company=candidate_company,
        score=combined_score,
        title_score=title_score,
        company_score=company_score,
        reason=reason,
    )


def select_best_candidate(
    matches: list[CandidateMatch],
) -> CandidateMatch | None:
    if not matches:
        return None

    ranked = sorted(
        matches,
        key=lambda match: (
            match.score,
            match.company_score,
            match.title_score,
        ),
        reverse=True,
    )
    best = ranked[0]

    if best.score < MINIMUM_SELECTION_SCORE:
        return None

    if best.title_score == 100 and best.company_score == 100:
        return best

    if len(ranked) > 1:
        second = ranked[1]

        if (
            second.score >= MINIMUM_SELECTION_SCORE
            and best.score - second.score < AMBIGUITY_MARGIN
        ):
            return None

    return best
