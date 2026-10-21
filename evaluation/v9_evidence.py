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


class Requirement(StrictModel):
    group_id: str = Field(min_length=1)
    jd_quote: str = Field(min_length=1)
    kind: Literal["mandatory", "preferred", "responsibility"]
    logic: GroupLogic
    component: str = Field(min_length=1)
    min_years: int | None = Field(default=None, ge=0)
    resume_quote: str | None = None
    evidence_context: Literal["employment", "project", "skills_list", "none"]
    status: Literal["met", "partial", "not_demonstrated", "unclear"]
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


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.]+", value.casefold()))


def validate_ledger(ledger: Ledger | dict, jd: str, resume: str) -> dict:
    """Return failures and effective statuses without rewriting model output.

    Quote checks are exact apart from whitespace/case. Group conclusions use
    AND/OR semantics. Failed structural/fidelity checks block acceptance.
    This does not pretend to prove that a model extracted *all* requirements.
    """
    obj = ledger if isinstance(ledger, Ledger) else Ledger.model_validate(ledger)
    failures: list[str] = []
    warnings: list[str] = []
    groups: dict[str, list[tuple[int, Requirement, str]]] = {}
    for i, r in enumerate(obj.requirements):
        status = r.status
        if _norm(r.jd_quote) not in _norm(jd):
            failures.append(f"row {i}: JD quote not found")
        if r.resume_quote and _norm(r.resume_quote) not in _norm(resume):
            failures.append(f"row {i}: resume quote not found")
        if r.kind == "mandatory" and re.search(r"\b(preferred|nice.to.have|bonus)\b", r.jd_quote, re.I):
            failures.append(f"row {i}: preferred clause called mandatory")
        if r.min_years is not None and status == "met":
            if r.evidence_context != "employment" or not re.search(
                r"\b(?:\d+\s*\+?\s*years?|20\d{2}\s*[-–]\s*(?:20\d{2}|present))\b",
                r.resume_quote or "", re.I
            ):
                status = "unclear"
                warnings.append(f"row {i}: tenure not independently evidenced")
        if r.status == "met" and r.logic in {GroupLogic.SINGLE, GroupLogic.ANY_OF, GroupLogic.ALL_OF}:
            if r.component.casefold() not in (r.resume_quote or "").casefold():
                # A literal term is intentionally required for exact named tech;
                # generic equivalence belongs under OPEN_EQUIVALENT.
                failures.append(f"row {i}: claimed component absent from resume quote")
        if r.status == "met" and r.logic == GroupLogic.OPEN_EQUIVALENT:
            if not re.search(r"\b(or similar|or equivalent|equivalent tools?|such as)\b", r.jd_quote, re.I):
                failures.append(f"row {i}: open equivalence not licensed by JD quote")
        groups.setdefault(r.group_id, []).append((i, r, status))
    summaries = []
    for group_id, rows in groups.items():
        logics = {r.logic for _, r, _ in rows}
        kinds = {r.kind for _, r, _ in rows}
        quotes = {_norm(r.jd_quote) for _, r, _ in rows}
        if len(logics) != 1 or len(kinds) != 1 or len(quotes) != 1:
            failures.append(f"group {group_id}: inconsistent logic/kind/source")
        if len({r.component.casefold() for _, r, _ in rows}) != len(rows):
            failures.append(f"group {group_id}: repeated component")
        logic = rows[0][1].logic
        if logic == GroupLogic.SINGLE and len(rows) != 1:
            failures.append(f"group {group_id}: single requires exactly one row")
        if logic in {GroupLogic.ANY_OF, GroupLogic.ALL_OF} and len(rows) < 2:
            failures.append(f"group {group_id}: compound group requires multiple rows")
        statuses = [status for _, _, status in rows]
        if logic == GroupLogic.ALL_OF:
            group_status = "met" if all(s == "met" for s in statuses) else (
                "not_demonstrated" if "not_demonstrated" in statuses else "unclear")
        elif logic in {GroupLogic.ANY_OF, GroupLogic.OPEN_EQUIVALENT}:
            group_status = "met" if "met" in statuses else (
                "not_demonstrated" if all(s == "not_demonstrated" for s in statuses) else "unclear")
        else:
            group_status = statuses[0]
        summaries.append({
            "group_id": group_id,
            "kind": rows[0][1].kind,
            "materiality": rows[0][1].materiality,
            "logic": logic.value,
            "status": group_status,
        })
    for group in summaries:
        if group["kind"] != "mandatory" or group["materiality"] != "central":
            continue
        if obj.fit_score >= 90 and group["status"] == "not_demonstrated":
            failures.append(f"group {group['group_id']}: unsupported central mandatory requirement at 90+")
        elif obj.fit_score >= 90 and group["status"] == "unclear":
            warnings.append(f"group {group['group_id']}: uncertain central requirement at 90+")
        if obj.fit_score >= 75 and group["status"] == "not_demonstrated":
            failures.append(f"group {group['group_id']}: APPLY contradicts central mandatory gap")
    return {
        "valid": not failures,
        "failures": list(dict.fromkeys(failures)),
        "warnings": list(dict.fromkeys(warnings)),
        "groups": summaries,
        "proposed_score": obj.fit_score,
    }
