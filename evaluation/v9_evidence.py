"""Isolated v9 evidence-ledger schema and deterministic checks.

No imports from scoring, database, or providers; no production integration.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroupLogic(str, Enum):
    SINGLE = "single"
    ANY_OF = "any_of"
    ALL_OF = "all_of"
    OPEN_EQUIVALENT = "open_equivalent"


Status = Literal["met", "partial", "not_demonstrated", "unclear"]


class Requirement(StrictModel):
    group_id: str = Field(min_length=1)
    jd_quote: str = Field(min_length=1)
    kind: Literal["mandatory", "preferred", "responsibility", "eligibility"]
    logic: GroupLogic
    component: str = Field(min_length=1)
    min_years: int | None = Field(default=None, ge=0)
    production_required: bool = False
    resume_quote: str | None = None
    evidence_context: Literal["employment", "project", "skills_list", "none"]
    status: Status
    materiality: Literal["central", "peripheral"] | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def coherent(self):
        if self.kind == "mandatory" and self.materiality is None:
            raise ValueError("mandatory rows require materiality")
        if self.kind != "mandatory" and self.materiality is not None:
            raise ValueError("nonmandatory rows must not carry materiality")
        if self.evidence_context == "none" and self.resume_quote is not None:
            raise ValueError("context none cannot cite resume text")
        if self.evidence_context != "none" and not self.resume_quote:
            raise ValueError("evidence context requires a resume quote")
        if self.status == "met" and self.resume_quote is None:
            raise ValueError("met requires a resume quote")
        return self


class Ledger(StrictModel):
    fit_score: int = Field(ge=0, le=100)
    requirements: list[Requirement] = Field(min_length=1)


PREFERRED_MARKERS = re.compile(
    r"\b(preferred|nice.to.have|bonus|a plus|desired|ideally)\b", re.I)
REQUIRED_MARKERS = re.compile(r"\b(required|must|minimum|mandatory)\b", re.I)
EQUIVALENCE_MARKERS = re.compile(
    r"\b(or similar|or equivalent|equivalent tools?|such as|comparable|e\.g\.)", re.I)
TENURE_EVIDENCE = re.compile(
    r"\b(?:\d+\s*\+?\s*years?"
    r"|(?:19|20)\d{2}\s*(?:[-–—]|to)\s*(?:(?:19|20)\d{2}|present|current|now))\b",
    re.I)


def _norm(value: str) -> str:
    # Hyphens compare as whitespace: extracted documents carry artifacts such
    # as "data -driven" that models legitimately quote as "data-driven", and
    # "end to end" must match "end-to-end". Both comparison sides receive the
    # same normalization, so no direction gains leniency the other lacks.
    return re.sub(r"[-\s]+", " ", value.casefold()).strip()


def _combine(logic: GroupLogic, statuses: list[str]) -> str:
    """AND/OR group status. ``partial`` is kept distinct from ``unclear``."""
    if logic == GroupLogic.ALL_OF:
        for status in ("not_demonstrated", "unclear", "partial"):
            if status in statuses:
                return status
        return "met"
    if logic in (GroupLogic.ANY_OF, GroupLogic.OPEN_EQUIVALENT):
        for status in ("met", "partial", "unclear"):
            if status in statuses:
                return status
        return "not_demonstrated"
    return statuses[0]


def validate_ledger(ledger: Ledger | dict, jd: str, resume: str) -> dict:
    """Return failures and effective statuses without rewriting model output.

    Quote checks are exact apart from whitespace/case. Each row gets an
    *effective* status: a claim the evidence cannot support (invented quote,
    portfolio/skills-list evidence for production or tenure, unlicensed
    equivalence) never counts as ``met``. Group conclusions use AND/OR
    semantics over effective statuses. This does not prove that a model
    extracted *all* requirements.
    """
    obj = ledger if isinstance(ledger, Ledger) else Ledger.model_validate(ledger)
    jd_n, resume_n = _norm(jd), _norm(resume)
    failures: list[str] = []
    warnings: list[str] = []
    rows: list[dict] = []
    groups: dict[str, list[tuple[int, Requirement, dict]]] = {}
    for i, r in enumerate(obj.requirements):
        status = r.status
        kind = r.kind
        jd_verified = _norm(r.jd_quote) in jd_n
        resume_verified = r.resume_quote is None or _norm(r.resume_quote) in resume_n
        integrity: list[str] = []
        if not jd_verified:
            integrity.append("jd_quote_not_found")
            failures.append(f"row {i}: JD quote not found")
        if not resume_verified:
            integrity.append("resume_quote_not_found")
            failures.append(f"row {i}: resume quote not found")
            if status in ("met", "partial"):
                # An invented citation is no evidence at all; we cannot
                # conclude the skill is missing either, so it is unclear.
                status = "unclear"
        if status == "partial" and r.resume_quote is None:
            status = "unclear"
            warnings.append(f"row {i}: partial claimed without resume evidence")
        if kind == "mandatory" and PREFERRED_MARKERS.search(r.jd_quote):
            failures.append(f"row {i}: preferred clause called mandatory")
            integrity.append("preferred_called_mandatory")
            kind = "preferred"
        if kind == "preferred" and REQUIRED_MARKERS.search(r.jd_quote) and not PREFERRED_MARKERS.search(r.jd_quote):
            warnings.append(f"row {i}: required-sounding clause labeled preferred")
        if status == "met" and r.logic in {GroupLogic.SINGLE, GroupLogic.ANY_OF, GroupLogic.ALL_OF}:
            if _norm(r.component) not in _norm(r.resume_quote or ""):
                # A literal term is intentionally required for exact named tech;
                # generic equivalence belongs under OPEN_EQUIVALENT.
                failures.append(f"row {i}: claimed component absent from resume quote")
                integrity.append("component_absent")
                status = "unclear"
        if status == "met" and r.logic == GroupLogic.OPEN_EQUIVALENT:
            if not EQUIVALENCE_MARKERS.search(r.jd_quote):
                failures.append(f"row {i}: open equivalence not licensed by JD quote")
                integrity.append("unlicensed_equivalence")
                status = "unclear"
        needs_professional = r.min_years is not None or r.production_required
        if needs_professional and status == "met" and r.evidence_context in ("project", "skills_list"):
            status = "partial"
            warnings.append(
                f"row {i}: {r.evidence_context} evidence cannot satisfy production/tenure requirement")
        if r.min_years is not None and status == "met":
            if r.evidence_context != "employment" or not TENURE_EVIDENCE.search(r.resume_quote or ""):
                status = "unclear"
                warnings.append(f"row {i}: tenure not independently evidenced")
        row = {
            "index": i,
            "group_id": r.group_id,
            "component": r.component,
            "kind": kind,
            "materiality": r.materiality if kind == "mandatory" else None,
            "claimed_status": r.status,
            "effective_status": status,
            "jd_verified": jd_verified,
            "resume_verified": resume_verified,
            "integrity": integrity,
        }
        rows.append(row)
        groups.setdefault(r.group_id, []).append((i, r, row))
    summaries = []
    for group_id, members in groups.items():
        logics = {r.logic for _, r, _ in members}
        kinds = {r.kind for _, r, _ in members}
        quotes = {_norm(r.jd_quote) for _, r, _ in members}
        if len(logics) != 1 or len(kinds) != 1 or len(quotes) != 1:
            failures.append(f"group {group_id}: inconsistent logic/kind/source")
        if len({r.component.casefold() for _, r, _ in members}) != len(members):
            failures.append(f"group {group_id}: repeated component")
        logic = members[0][1].logic
        if logic == GroupLogic.SINGLE and len(members) != 1:
            failures.append(f"group {group_id}: single requires exactly one row")
        if logic in {GroupLogic.ANY_OF, GroupLogic.ALL_OF} and len(members) < 2:
            failures.append(f"group {group_id}: compound group requires multiple rows")
        statuses = [row["effective_status"] for _, _, row in members]
        first = members[0][2]
        summaries.append({
            "group_id": group_id,
            "kind": first["kind"],
            "materiality": first["materiality"],
            "logic": logic.value,
            "status": _combine(logic, statuses),
            "claimed_status": _combine(logic, [r.status for _, r, _ in members]),
            "jd_verified": all(row["jd_verified"] for _, _, row in members),
            "components": [r.component for _, r, _ in members],
        })
    for group in summaries:
        if group["kind"] != "mandatory" or group["materiality"] != "central":
            continue
        if obj.fit_score >= 90 and group["status"] == "not_demonstrated":
            failures.append(f"group {group['group_id']}: unsupported central mandatory requirement at 90+")
        elif obj.fit_score >= 90 and group["status"] in ("unclear", "partial"):
            warnings.append(f"group {group['group_id']}: uncertain central requirement at 90+")
        if obj.fit_score >= 75 and group["status"] == "not_demonstrated":
            failures.append(f"group {group['group_id']}: APPLY contradicts central mandatory gap")
    return {
        "valid": not failures,
        "failures": list(dict.fromkeys(failures)),
        "warnings": list(dict.fromkeys(warnings)),
        "rows": rows,
        "groups": summaries,
        "proposed_score": obj.fit_score,
    }
