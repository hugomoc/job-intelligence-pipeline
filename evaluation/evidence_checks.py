"""Pure offline requirement-evidence consistency checks; never invoke models or databases.

This module evaluates *supplied, human-reviewed evidence*. It does not infer
requirements or experience from free text, nor does it alter stored scores.
"""
from __future__ import annotations

from collections import Counter

EVIDENCE_STATUSES = frozenset({"met", "partial", "not_demonstrated", "unclear"})
REVIEW_LABELS = frozenset({"strongly_supported", "questionable", "inflated"})
SEVERITIES = {"not_demonstrated": "high", "partial": "review", "unclear": "review"}


def validate_case(case: dict) -> dict:
    """Normalize a private label record and fail closed on malformed evidence."""
    if not isinstance(case, dict):
        raise ValueError("Case must be an object")
    key = case.get("case_id")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("case_id is required")
    score = case.get("stored_score")
    if type(score) not in (int, float) or not 0 <= score <= 100:
        raise ValueError("stored_score must be numeric from 0 to 100")
    label = case.get("review_label")
    if label not in REVIEW_LABELS:
        raise ValueError("review_label must be an explicit review label")
    requirements = case.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("At least one reviewed requirement is required")
    normalized = []
    for req in requirements:
        if not isinstance(req, dict):
            raise ValueError("Requirements must be objects")
        if not isinstance(req.get("requirement"), str) or not req["requirement"].strip():
            raise ValueError("Requirement wording is required")
        if type(req.get("mandatory")) is not bool:
            raise ValueError("mandatory must be an explicit boolean")
        if req.get("status") not in EVIDENCE_STATUSES:
            raise ValueError("Unknown evidence status")
        evidence = req.get("resume_evidence")
        if not isinstance(evidence, str):
            raise ValueError("resume_evidence must be a string (possibly empty)")
        if req["status"] == "met" and not evidence.strip():
            raise ValueError("Met requirements need an explicit resume citation")
        normalized.append({**req})
    return {**case, "requirements": normalized}


def assess_case(case: dict, high_score_threshold: int = 95) -> dict:
    """Emit *review flags*, not new fit scores or recommendation changes."""
    case = validate_case(case)
    flags = []
    mandatory = [r for r in case["requirements"] if r["mandatory"]]
    for index, requirement in enumerate(case["requirements"]):
        status = requirement["status"]
        if requirement["mandatory"] and status in SEVERITIES:
            flags.append({
                "code": "mandatory_evidence_gap",
                "requirement_index": index,
                "evidence_status": status,
                "severity": SEVERITIES[status],
            })
    if case["stored_score"] >= high_score_threshold and any(
        r["mandatory"] and r["status"] == "not_demonstrated"
        for r in case["requirements"]
    ):
        flags.append({
            "code": "high_score_unsupported_mandatory",
            "severity": "high",
        })
    if case["stored_score"] >= high_score_threshold and any(
        r["mandatory"] and r["status"] in {"partial", "unclear"}
        for r in case["requirements"]
    ):
        flags.append({
            "code": "high_score_uncertain_mandatory",
            "severity": "review",
        })
    return {
        "case_id": case["case_id"],
        "review_label": case["review_label"],
        "flagged": bool(flags),
        "flags": flags,
        "mandatory_total": len(mandatory),
        "mandatory_met": sum(r["status"] == "met" for r in mandatory),
        "mandatory_not_demonstrated": sum(r["status"] == "not_demonstrated" for r in mandatory),
        "mandatory_uncertain": sum(r["status"] in {"partial", "unclear"} for r in mandatory),
    }


def evaluate_cases(cases: list[dict]) -> dict:
    """Deterministic descriptive metrics; no assumed numeric ground truth."""
    seen = set()
    results = []
    for case in cases:
        result = assess_case(case)
        if result["case_id"] in seen:
            raise ValueError("Duplicate case_id")
        seen.add(result["case_id"])
        results.append(result)
    by_label = Counter(r["review_label"] for r in results)
    inflated = [r for r in results if r["review_label"] == "inflated"]
    supported = [r for r in results if r["review_label"] == "strongly_supported"]
    return {
        "n": len(results),
        "review_label_counts": dict(sorted(by_label.items())),
        "known_inflated_detected": sum(r["flagged"] for r in inflated),
        "known_inflated_total": len(inflated),
        "supported_false_flags": sum(r["flagged"] for r in supported),
        "supported_total": len(supported),
        "questionable_flagged": sum(r["flagged"] for r in results if r["review_label"] == "questionable"),
        "questionable_total": by_label["questionable"],
        "cases": results,
    }
