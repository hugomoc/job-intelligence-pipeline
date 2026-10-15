"""Read-only evidence for conservative cached-score corrections.

This report contains only selected saved JD clauses and limited skills evidence,
not the entire job database or candidate resume. Review it before approving.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from src.ai.requirement_policy import (
    AI_DELIVERY,
    AI_REQUIRED_TECH,
    description_requirement_clauses,
)
from src.ai.resume_profiler import ResumeProfile

GAP_RULES: tuple[tuple[str, str], ...] = (
    (r"\bdbt cloud\b", r"\bdbt[\s-]+cloud\b"),
    (r"\bdatabricks\b", r"\bdatabricks\b"),
    (r"\btableau\b", r"\btableau\b"),
    (r"\bdomo\b", r"\bdomo\b"),
    (r"\bledger\b|\baccounting\b", r"\b(?:ledger|accounting|financial close)\b"),
    (r"machine.learning model deployment", r"\b(?:mlops|machine[\s-]+learning|ml[\s-]+models?|model[\s-]+deployment|model[\s-]+serving)\b"),
)


def new_hard_gaps(row: dict[str, Any], match: Any) -> list[str]:
    try:
        stored = json.loads(row.get("hard_requirements_missing") or "[]")
    except (TypeError, ValueError):
        stored = []
    if not isinstance(stored, list):
        stored = []
    old = {str(x).strip().casefold() for x in stored}
    return [
        x for x in match.analysis.hard_requirements_missing
        if x.startswith("Major gap: ") and x.strip().casefold() not in old
    ]


def approval_fingerprint(row: dict[str, Any], match: Any) -> str:
    """Bind approval to the exact cached evaluation, profile and JD text."""
    fields = [
        row["resume_hash"],
        row["record_key"],
        row["overall_score"],
        row["recommendation"],
        row["description"],
        row["profile_json"],
        sorted(new_hard_gaps(row, match)),
    ]
    serialized = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def gap_clause_pattern(gap: str) -> re.Pattern[str] | None:
    if "production RAG" in gap or "agentic workflows" in gap:
        return AI_REQUIRED_TECH
    for label, pattern in GAP_RULES:
        if re.search(label, gap, re.I):
            return re.compile(pattern, re.I)
    return None


def candidate_evidence(row: dict[str, Any], match: Any) -> dict[str, Any]:
    """Extract verbatim saved clauses and provenance for new hard-gap claims."""
    profile = ResumeProfile.model_validate_json(row["profile_json"])
    clauses = description_requirement_clauses(row["description"])
    groups = []
    for gap in new_hard_gaps(row, match):
        pattern = gap_clause_pattern(gap)
        is_ai = ("production RAG" in gap or "agentic workflows" in gap)
        relevant = []
        if pattern:
            for clause in clauses:
                if not pattern.search(clause["clause"]):
                    continue
                item = {
                    "text": clause["clause"],
                    "section": clause["section"],
                    "mandatory": bool(clause["mandatory"]),
                    "optional": bool(clause["optional"]),
                }
                if is_ai:
                    item["production_ai_ownership_in_same_clause"] = bool(
                        AI_DELIVERY.search(clause["clause"])
                    )
                relevant.append(item)
        has_explicit = any(
            c["mandatory"] and not c["optional"]
            and (not is_ai or c.get("production_ai_ownership_in_same_clause"))
            for c in relevant
        )
        profile_matches = []
        if pattern:
            for field in (
                "production_skills", "ai_ml_experience",
                "data_engineering_capabilities", "bi_analytics_skills",
                "tools_platforms", "databases_warehouses",
                "industries",
            ):
                for value in getattr(profile, field, []) or []:
                    if pattern.search(str(value)):
                        profile_matches.append({
                            "field": field,
                            "text": str(value)[:350],
                        })
        groups.append({
            "new_hard_gap": gap,
            "matching_saved_jd_clauses": relevant,
            "explicit_mandatory_clause_found": has_explicit,
            "matching_profile_evidence": profile_matches[:12],
            "review_note": (
                "Check that the JD truly REQUIRES this specific capability; "
                "a matching keyword or mandatory section alone can be misleading."
            ),
        })
    return {
        "company": row["company_name"],
        "title": row["title"],
        "record_key": row["record_key"],
        "old_score": row["overall_score"],
        "old_recommendation": row["recommendation"],
        "new_score": match.overall_score,
        "new_recommendation": match.recommendation,
        "approval_fingerprint": approval_fingerprint(row, match),
        "all_new_gaps_have_explicit_mandatory_clause": bool(groups) and all(
            g["explicit_mandatory_clause_found"] for g in groups
        ),
        "gaps": groups,
    }


def approval_tokens(path: Any) -> set[str]:
    """Read an explicitly reviewed allowlist. Fail closed for malformed lists."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Could not read valid JSON approval file") from exc
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("Approval file must be a JSON object with version=1")
    allowed = document.get("approved_fingerprints")
    if not isinstance(allowed, list) or not allowed:
        raise ValueError("Approval file must contain a nonempty approved_fingerprints list")
    if not all(
        isinstance(x, str) and re.fullmatch(r"[a-f0-9]{64}", x)
        for x in allowed
    ) or len(allowed) != len(set(allowed)):
        raise ValueError("Approval fingerprints must be unique SHA-256 strings")
    return set(allowed)


def approved_candidates(
    proposed: list[tuple[dict[str, Any], Any]], selected: set[str]
) -> list[tuple[dict[str, Any], Any]]:
    """Require exact-match approval AND a clear mandatory matching JD clause."""
    by_token: dict[str, tuple[dict[str, Any], Any]] = {}
    for row, match in proposed:
        token = approval_fingerprint(row, match)
        if token in by_token:
            raise ValueError("Duplicate candidate fingerprint; refusing approval")
        by_token[token] = (row, match)
    unknown = selected - set(by_token)
    if unknown:
        raise ValueError(
            f"{len(unknown)} approval fingerprint(s) are unknown or stale; "
            "rerun the evidence report and review again"
        )
    approved = [pair for token, pair in by_token.items() if token in selected]
    for row, match in approved:
        evidence = candidate_evidence(row, match)
        if not evidence["all_new_gaps_have_explicit_mandatory_clause"]:
            raise ValueError(
                f"{row['company_name']} / {row['title']} has no explicit "
                "matching mandatory JD clause; refusing to apply"
            )
    return approved
