"""Detailed AI resume-to-job fit scoring.

This is the expensive matcher that produces APPLY/REVIEW/SKIP, component fit
scores, strengths, gaps, risks, and a summary. It expects jobs to have already
passed ingestion, rule matching, eligibility screening, and description checks.
"""

from __future__ import annotations

from src.ai.matcher_contract import (
    MINIMUM_COMPLETE_DESCRIPTION_WORDS,
    MATCHER_PROMPT_VERSION,
    SIGNIFICANT_TERM_PATTERN,
    TECH_ALIASES,
    CLOUD_GROUPS,
    WAREHOUSE_GROUPS,
    DATA_PROCESSING_GROUPS,
    BI_PLATFORM_GROUPS,
    INTERCHANGEABLE_TECH_GROUPS,
    SIGNAL_ALIAS_GROUPS,
    CriticalCapabilitySignal,
    CriticalRequirementGap,
    CRITICAL_CAPABILITY_SIGNALS,
    HARD_MANDATORY_CONTEXT_PATTERNS,
    MANDATORY_REQUIREMENT_CONTEXT_PATTERNS,
    CRITICAL_REQUIREMENT_CONTEXT_PATTERNS,
    OPTIONAL_REQUIREMENT_CONTEXT_PATTERNS,
)
from src.ai.matcher_evidence import (
    normalize_match_text,
    contains_phrase,
    profile_to_text,
    profile_fields_to_text,
    regex_present,
    regex_count,
    interchangeable_family_for_group,
    production_resume_text,
    project_resume_text,
    profile_has_alias_group,
    mentioned_alias_groups,
    requirement_uses_alternatives,
    requirement_allows_open_equivalent,
    requirement_requires_all,
    acceptable_alternative_groups,
    significant_terms,
    requirement_is_demonstrated,
    remove_demonstrated_missing_items,
)
from src.ai.matcher_capabilities import (
    requirement_has_satisfied_alternative,
    capability_mentions_with_context,
    capability_mandatory_mentions,
    capability_is_central_to_job,
    detect_critical_requirement_gaps,
)


import json
import os
import re
import time

from bs4 import BeautifulSoup
from dataclasses import dataclass
from typing import Any, Literal

from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from src.ai.resume_profiler import (
    ResumeProfile,
    apply_candidate_eligibility_overrides,
    get_gemini_client,
    get_model_name,
    redact_personal_information,
)


from src.description_quality import MINIMUM_FULL_DESCRIPTION_WORDS, assess_description_quality
















class FitAnalysis(BaseModel):
    """Simple resume-to-JD assessment used by v7 production scoring."""

    fit_score: int = Field(
        ge=0,
        le=100,
        description="Direct holistic resume-to-JD requirements fit score.",
    )
    required_matches: list[str] = Field(default_factory=list)
    required_partial: list[str] = Field(default_factory=list)
    required_gaps: list[str] = Field(default_factory=list)
    preferred_matches: list[str] = Field(default_factory=list)
    preferred_partial: list[str] = Field(default_factory=list)
    preferred_gaps: list[str] = Field(default_factory=list)
    work_arrangement: str = Field(
        description=(
            "Remote/hybrid/onsite arrangement and city/schedule when stated. "
            "This is informational only and must not affect fit_score."
        )
    )
    summary: str = Field(
        description=(
            "Concise evidence-based explanation consistent with the classifications."
        )
    )


class RequirementMatch(BaseModel):
    job_requirement: str = Field(
        description="Important job requirement or core responsibility from the JD.",
    )
    candidate_background: str = Field(
        description=(
            "Concise candidate evidence that matches the requirement, or a clear "
            "statement that the experience is not demonstrated."
        ),
    )
    match: Literal["excellent", "strong", "partial", "gap"] = Field(
        description=(
            "Human-readable fit label for this requirement. This is explanatory "
            "only and must not be used as a formula for the overall score."
        ),
    )


class JobMatchAnalysis(BaseModel):
    title_fit: int = Field(
        ge=0,
        le=100,
        description=(
            "How well the job title and function align "
            "with the candidate's demonstrated background."
        ),
    )

    skills_fit: int = Field(
        ge=0,
        le=100,
        description=(
            "How well the candidate's demonstrated skills "
            "match the job's required and preferred skills."
        ),
    )

    experience_fit: int = Field(
        ge=0,
        le=100,
        description=(
            "How well the candidate's professional "
            "experience matches the responsibilities."
        ),
    )

    seniority_fit: int = Field(
        ge=0,
        le=100,
        description=(
            "How well the candidate's seniority and scope "
            "match the position."
        ),
    )

    industry_fit: int = Field(
        ge=0,
        le=100,
        description=(
            "How relevant the candidate's industry and "
            "business-domain experience is."
        ),
    )

    location_fit: int = Field(
        ge=0,
        le=100,
        description=(
            "How well the job location or work arrangement "
            "matches the available candidate information."
        ),
    )

    overall_score: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "Holistic candidate-to-job fit score returned by the model after "
            "considering the full JD, core responsibilities, requirements, "
            "eligibility constraints, and transferable experience."
        ),
    )

    recommendation: Literal[
        "apply",
        "review",
        "skip",
    ] | None = Field(
        default=None,
        description=(
            "Holistic model recommendation. APPLY requires a realistically "
            "actionable fit, REVIEW is plausible with meaningful gaps or "
            "uncertainty, and SKIP is a poor or practically ineligible fit."
        ),
    )

    confidence: Literal[
        "high",
        "medium",
        "low",
    ] = Field(
        description=(
            "Confidence in the evaluation based on the "
            "completeness of the job description."
        )
    )

    matching_strengths: list[str] = Field(
        default_factory=list,
        description=(
            "Important candidate qualifications that "
            "directly match the job."
        ),
    )

    hard_requirements_missing: list[str] = Field(
        default_factory=list,
        description=(
            "Explicit requirements or central hands-on responsibilities "
            "that the candidate does not demonstrate."
        ),
    )

    preferred_qualifications_missing: list[str] = Field(
        default_factory=list,
        description=(
            "Preferred qualifications that are not shown "
            "in the candidate profile."
        ),
    )

    risk_factors: list[str] = Field(
        default_factory=list,
        description=(
            "Important concerns such as location, required "
            "clearance, seniority mismatch or missing data."
        ),
    )

    requirement_matches: list[RequirementMatch] = Field(
        default_factory=list,
        max_length=10,
        description=(
            "Concise comparison of the most important job requirements against "
            "the candidate's demonstrated background. Explanatory only; it does "
            "not determine the score."
        ),
    )

    summary: str = Field(
        description=(
            "A concise explanation of the candidate's fit."
        )
    )


@dataclass(frozen=True)
class ResumeJobMatch:
    record_key: str
    model_name: str
    prompt_version: str

    overall_score: int
    recommendation: Literal[
        "apply",
        "review",
        "skip",
    ]

    description_word_count: int
    description_complete: bool

    analysis: JobMatchAnalysis


class ResumeMatcherError(Exception):
    """Raised when an AI provider cannot evaluate a job."""


def calculate_overall_score(
    analysis: JobMatchAnalysis,
) -> int:
    """
    Final score is calculated in Python so that every
    job uses the same weighting.

    Skills and professional experience receive the
    greatest weight.
    """
    weighted_score = (
        analysis.title_fit * 0.15
        + analysis.skills_fit * 0.30
        + analysis.experience_fit * 0.25
        + analysis.seniority_fit * 0.15
        + analysis.industry_fit * 0.05
        + analysis.location_fit * 0.10
    )

    return round(weighted_score)


def get_description_information(
    job: dict[str, Any],
) -> tuple[str, int, bool]:
    description = str(
        job.get("description") or ""
    ).strip()

    word_count = len(
        description.split()
    )

    description_complete = (
        assess_description_quality(description).full_jd
        and job.get("enrichment_status") != "resolution_rejected"
    )

    return (
        description,
        word_count,
        description_complete,
    )


def build_job_match_prompt(
    resume_profile: ResumeProfile,
    job: dict[str, Any],
    description: str,
    description_complete: bool,
    resume_text: str | None = None,
) -> str:
    """Build the v7 actual-resume, requirements-first scoring prompt."""
    completeness = (
        "COMPLETE"
        if description_complete
        else "INCOMPLETE"
    )

    evidence_text = (
        redact_personal_information(resume_text).strip()
        if resume_text
        else resume_profile.model_dump_json(indent=2)
    )

    eligibility_text = (
        resume_profile.personal_eligibility.model_dump_json(
            indent=2
        )
    )

    return f"""
Evaluate ONE RESUME against ONE JOB DESCRIPTION.

Answer one question:

HOW STRONGLY DOES THE EXPERIENCE ACTUALLY DEMONSTRATED IN THIS RESUME
MATCH WHAT THIS JOB DESCRIPTION ACTUALLY REQUIRES?

Return a direct fit_score from 0 to 100.

============================================================
PRIMARY RULE
============================================================

Judge only evidence actually demonstrated in the resume.

Do not infer stronger experience simply because related keywords appear
in different places.

Do not calculate the score from a rigid point formula.

============================================================
WHAT MATTERS MOST
============================================================

Base fit_score primarily on:

- required qualifications
- required technical skills
- core responsibilities
- required experience and seniority

Preferred qualifications should have only a small effect.

Do not score whether the candidate personally wants the job.

Do not penalize employer industry unless prior domain experience is
explicitly required by the JD.

============================================================
WHOLE-REQUIREMENT EVIDENCE
============================================================

A requirement is satisfied only when the resume supports the COMPLETE
meaning of the requirement.

Do not match isolated keywords.

Examples:

"deep hands-on experience in dbt"

is NOT fully satisfied merely because:

- dbt appears in Skills
- dbt appears in portfolio projects
- the candidate has many years of general data experience

If dbt is demonstrated but deep professional production usage is not,
classify it as PARTIAL.


"Python scripting within SCADA/BMS frameworks"

is NOT satisfied merely because Python appears in the resume.

The resume must demonstrate Python specifically in the requested
SCADA/BMS context.


"PySpark experience"

is NOT automatically satisfied by combining Python and Spark unless
the resume actually demonstrates PySpark or clearly demonstrates Python
being used with Spark.

If adjacent evidence exists but the requested specialization is not fully
demonstrated, classify it as PARTIAL.

============================================================
DO NOT MANUFACTURE EXPERIENCE
============================================================

Never combine unrelated resume facts to manufacture stronger experience.

Examples:

12 years data engineering + dbt in Skills
does NOT equal 12 years of dbt experience.

Python + Spark does NOT automatically equal production PySpark expertise.

Python + automation does NOT equal Python automation in SCADA/BMS.

A technology appearing in a skills list proves familiarity only. It does
not automatically prove deep, extensive, expert, production, or multi-year
experience.

============================================================
GAP VS PARTIAL — STRICT
============================================================

Use GAP only when the resume provides essentially NO meaningful evidence
for the required capability.

Use PARTIAL when the underlying capability is demonstrated, but the JD
asks for greater:

- scope
- seniority
- organizational influence
- depth
- duration
- specialization
- context

Do not use GAP merely because the resume does not prove the full requested
scope.

============================================================
OR / AND — STRICT
============================================================

Respect logical alternatives exactly as written.

If a requirement says "A OR B", demonstrating either A or B satisfies it.
Do not require both and do not create a gap for the other side.

If a requirement says "A AND B", both must be demonstrated.

============================================================
REQUIRED VS PREFERRED — STRICT
============================================================

Before placing anything in required_partial or required_gaps, verify that
the JD actually makes it required.

If the JD uses wording such as:

- preferred
- ideally
- a plus
- bonus
- nice to have
- desirable
- highly desirable
- comparable experience welcome

then it MUST NOT appear in required_gaps.

Optional qualifications belong only in preferred_matches,
preferred_partial, or preferred_gaps.

============================================================
EQUIVALENT TECHNOLOGIES
============================================================

If the JD explicitly permits comparable, equivalent, adjacent, or similar
technologies, evaluate the underlying capability rather than requiring the
exact named example.

Do not create a gap for an example technology when an explicitly accepted
equivalent is demonstrated.

============================================================
NO DOUBLE COUNTING
============================================================

Classify each qualification once.

Do not put the same requirement into MATCH and PARTIAL, PARTIAL and GAP,
or both required and preferred categories.

Do not represent a comparable capability as PARTIAL and separately create
a GAP for the exact example technology when the JD accepts equivalents.

============================================================
CLASSIFICATION
============================================================

required_matches:
Actually required qualifications or core responsibilities demonstrated at
the requested level, depth, duration, specialization, scope, and context.

required_partial:
Actually required items where meaningful evidence exists, but the requested
depth, duration, specialization, scope, organizational influence, or context
is only partially demonstrated.

required_gaps:
Actually required qualifications where the underlying capability is
materially absent.

preferred_matches:
Preferred or optional items fully demonstrated.

preferred_partial:
Preferred or optional items with related evidence but not fully demonstrated.

preferred_gaps:
Preferred or optional items not demonstrated.

============================================================
FIT SCORE
============================================================

Use professional recruiting judgment.

90-100:
Exceptional match. Nearly all important requirements are directly and
convincingly demonstrated.

80-89:
Strong match. Most requirements are demonstrated, with one or a few
meaningful partial areas.

70-79:
Good but imperfect match. Several meaningful requirements are partial or
one significant required area is weak.

60-69:
Moderate match. Transferable experience exists, but multiple important
requirements are not demonstrated strongly enough.

40-59:
Weak match. Some relevant experience exists, but major required areas are
missing.

0-39:
Poor match. The background substantially differs from the role's core
requirements.

============================================================
WORK ARRANGEMENT
============================================================

Do NOT include any of these in fit_score:

- remote / hybrid / onsite
- city
- commute
- relocation
- salary
- compensation
- schedule

Report the work arrangement separately in work_arrangement, including city
and required office days when stated.

Examples:

Remote

Hybrid — San Francisco, CA — Tuesdays and Wednesdays

Onsite — San Diego, CA

============================================================
PERSONAL ELIGIBILITY
============================================================

The explicit candidate eligibility facts below are supplied separately so
you do not invent personal facts.

Do not use location, hybrid/onsite schedule, commute, relocation, salary, or
travel logistics to increase or decrease fit_score.

Do not convert UNKNOWN eligibility facts into resume-skill gaps.

============================================================
SUMMARY CONSISTENCY
============================================================

The summary MUST be consistent with every classification.

If an item is absent, it cannot be described as a match.

If an item is PARTIAL, do not describe it as fully demonstrated, deep,
extensive, expert, or production-grade unless the resume evidence supports
that stronger wording.

Do not add stronger adjectives in the summary than the resume supports.

============================================================
FINAL REVIEW
============================================================

Before returning:

1. Re-read the entire JD.
2. Re-read the entire resume.
3. Verify every MATCH against the complete requirement.
4. Respect OR and AND exactly as written.
5. Do not use keyword overlap alone.
6. Do not combine unrelated facts to manufacture experience.
7. Verify depth and duration with actual evidence.
8. Verify context-specific requirements with context-specific evidence.
9. Distinguish professional work from portfolio/project experience.
10. Verify every required_partial and required_gap is actually required.
11. Move optional qualifications into the preferred section.
12. Verify GAP vs PARTIAL using the underlying-capability rule.
13. Ensure the summary does not contradict or exaggerate classifications.
14. Keep work arrangement completely outside fit_score.

Return only the final reviewed assessment.

============================================================
ACTUAL RESUME
============================================================

{evidence_text}

============================================================
EXPLICIT CANDIDATE ELIGIBILITY FACTS
============================================================

{eligibility_text}

============================================================
JOB INFORMATION
============================================================

Title: {job.get("title") or "Not provided"}
Company: {job.get("company_name") or "Not provided"}
Location: {job.get("location") or "Not provided"}
Source: {job.get("source") or "Not provided"}
Job-description completeness: {completeness}

============================================================
FULL JOB DESCRIPTION
============================================================

{description or "No complete job description is available."}
""".strip()



from src.ai.requirement_policy import (
    description_requirement_clauses, clause_groups, validate_hard_gap_clause,
    explicit_production_ai_requirements,
    personal_fact_kind, personal_requirement_evidence,
    required_platform_tenure_gaps,
)


def validate_missing_qualifications(
    analysis: JobMatchAnalysis,
    resume_profile: ResumeProfile,
    description: str | None = None,
) -> JobMatchAnalysis:
    hard_missing = remove_demonstrated_missing_items(
        analysis.hard_requirements_missing,
        resume_profile,
    )
    risks = list(analysis.risk_factors)
    if description is not None:
        hard_missing = []
        for gap in analysis.hard_requirements_missing:
            decision = validate_hard_gap_clause(gap, description, resume_profile)
            if decision["keep_hard_gap"]:
                hard_missing.append(gap)
            elif decision.get("personal_evidence", {}).get("state") == "UNKNOWN":
                risks.append(f"Needs confirmation (UNKNOWN personal eligibility): {decision['personal_evidence']['kind']}; no explicit candidate fact supplied.")
            elif decision["reason"] in {"mandatory status not established", "vendor examples not mandatory; broader capability needs review"}:
                risks.append(f"Unconfirmed qualification (mandatory JD evidence unavailable): {gap}")
    preferred_missing = remove_demonstrated_missing_items(
        analysis.preferred_qualifications_missing,
        resume_profile,
    )

    if (
        hard_missing == analysis.hard_requirements_missing
        and preferred_missing
        == analysis.preferred_qualifications_missing
        and risks == analysis.risk_factors
    ):
        return analysis

    return analysis.model_copy(
        update={
            "hard_requirements_missing": hard_missing,
            "risk_factors": list(dict.fromkeys(risks)),
            "preferred_qualifications_missing": (
                preferred_missing
            ),
        }
    )


def determine_recommendation(
    overall_score: int,
    analysis: JobMatchAnalysis,
    description_complete: bool,
) -> Literal[
    "apply",
    "review",
    "skip",
]:
    # Incomplete postings must not be approved from title
    # or email metadata alone. They can still be skipped
    # when the limited evidence is clearly weak.
    if not description_complete:
        if overall_score < 50:
            return "skip"

        return "review"

    if analysis.hard_requirements_missing:
        return "skip"

    if overall_score < 50:
        return "skip"

    if (
        overall_score >= 75
        and analysis.confidence in {
            "high",
            "medium",
        }
    ):
        return "apply"

    return "review"

def parse_job_match_payload(
    payload: Any,
    provider_name: str,
) -> JobMatchAnalysis:
    if not isinstance(payload, dict):
        raise ResumeMatcherError(
            f"{provider_name} returned an unexpected "
            f"response type: {type(payload).__name__}."
        )

    # Gemini may occasionally return null for an empty
    # collection. Convert those values to empty lists.
    list_fields = [
        "matching_strengths",
        "hard_requirements_missing",
        "preferred_qualifications_missing",
        "risk_factors",
    ]

    for field_name in list_fields:
        if payload.get(field_name) is None:
            payload[field_name] = []

    # Normalize confidence before Literal validation.
    confidence = payload.get("confidence")

    if isinstance(confidence, str):
        payload["confidence"] = (
            confidence.strip().casefold()
        )

    try:
        return JobMatchAnalysis.model_validate(
            payload
        )

    except ValidationError as error:
        response_preview = json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
        )[:3_000]

        raise ResumeMatcherError(
            f"{provider_name} returned an invalid job-match "
            "response.\n\n"
            f"Validation details:\n{error}\n\n"
            "Response preview:\n"
            f"{response_preview}"
        ) from error


def parse_job_match_response(
    response: Any,
) -> JobMatchAnalysis:
    """
    Converts Gemini's structured response into a validated
    JobMatchAnalysis and exposes useful validation errors.
    """
    if isinstance(
        response.parsed,
        JobMatchAnalysis,
    ):
        return response.parsed

    payload: Any = None

    if response.parsed is not None:
        if isinstance(
            response.parsed,
            BaseModel,
        ):
            payload = response.parsed.model_dump()

        else:
            payload = response.parsed

    elif response.text:
        try:
            payload = json.loads(
                response.text
            )

        except json.JSONDecodeError as error:
            preview = response.text[:2_000]

            raise ResumeMatcherError(
                "Gemini returned invalid JSON. "
                f"JSON error: {error}. "
                f"Response preview: {preview}"
            ) from error

    else:
        raise ResumeMatcherError(
            "Gemini returned an empty response."
        )

    return parse_job_match_payload(
        payload=payload,
        provider_name="Gemini",
    )


def get_openai_model_name() -> str:
    return os.getenv(
        "OPENAI_MODEL",
        "gpt-4.1-mini",
    )


def get_openai_job_match_schema() -> dict[str, Any]:
    schema = JobMatchAnalysis.model_json_schema()

    def make_strict(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                properties = node.get("properties")
                if isinstance(properties, dict):
                    node["required"] = list(properties.keys())

            for value in node.values():
                make_strict(value)

        elif isinstance(node, list):
            for value in node:
                make_strict(value)

    make_strict(schema)
    return schema


def parse_openai_job_match_content(
    content: str | None,
) -> JobMatchAnalysis:
    if not content:
        raise ResumeMatcherError(
            "OpenAI returned an empty response."
        )

    try:
        payload = json.loads(content)

    except json.JSONDecodeError as error:
        preview = content[:2_000]

        raise ResumeMatcherError(
            "OpenAI returned invalid JSON. "
            f"JSON error: {error}. "
            f"Response preview: {preview}"
        ) from error

    return parse_job_match_payload(
        payload=payload,
        provider_name="OpenAI",
    )


def confirmed_mandatory_personal_mismatches(
    resume_profile: ResumeProfile,
    description: str,
) -> list[str]:
    """Return mandatory personal requirements disproved by explicit facts."""
    mismatches: list[str] = []

    for row in description_requirement_clauses(description):
        if not row.get("mandatory"):
            continue

        clause = str(row.get("clause") or "").strip()
        kind = personal_fact_kind(clause)

        if not clause or kind is None:
            continue

        evidence = personal_requirement_evidence(
            clause,
            description,
            resume_profile,
            _kind=kind,
        )

        if evidence is not None and evidence["state"] == "DOES_NOT_MEET":
            mismatches.append(clause)

    return list(dict.fromkeys(mismatches))


def normalize_personal_eligibility(
    analysis: JobMatchAnalysis,
    resume_profile: ResumeProfile,
    description: str,
) -> tuple[JobMatchAnalysis, bool]:
    """Remove fabricated personal gaps and identify unresolved eligibility."""
    hard_missing: list[str] = []
    risks = list(analysis.risk_factors)
    saw_nonfailing_personal = False

    for gap in analysis.hard_requirements_missing:
        evidence = personal_requirement_evidence(
            gap,
            description,
            resume_profile,
        )

        if evidence is None:
            hard_missing.append(gap)
            continue

        state = evidence["state"]

        if state == "DOES_NOT_MEET":
            hard_missing.append(gap)
        elif state == "UNKNOWN":
            saw_nonfailing_personal = True
            risks.append(
                "Needs confirmation (UNKNOWN personal eligibility): "
                f"{evidence['kind']}; no explicit candidate fact supplied."
            )
        elif state == "MEETS":
            saw_nonfailing_personal = True
        # MEETS is intentionally removed from hard gaps.

    return (
        analysis.model_copy(
            update={
                "hard_requirements_missing": list(dict.fromkeys(hard_missing)),
                "risk_factors": list(dict.fromkeys(risks)),
            }
        ),
        saw_nonfailing_personal,
    )


def build_resume_job_match(
    resume_profile: ResumeProfile,
    job: dict[str, Any],
    model_name: str,
    analysis: JobMatchAnalysis,
    description_word_count: int,
    description_complete: bool,
) -> ResumeJobMatch:
    """Normalize model output into the durable score object stored in DuckDB."""
    record_key = str(
        job.get("record_key") or ""
    )

    if not record_key:
        raise ResumeMatcherError(
            "The job record_key is missing."
        )

    # v6 production scoring is AI-first: the full JD and candidate profile are
    # evaluated holistically by the model. Python validates consistency and
    # persistence shape; it does not re-interpret the JD as a second recruiter.
    if (
        analysis.overall_score is not None
        and analysis.recommendation is not None
    ):
        overall_score = analysis.overall_score
        recommendation = analysis.recommendation
        description = str(job.get("description") or "")

        (
            analysis,
            saw_nonfailing_personal,
        ) = normalize_personal_eligibility(
            analysis=analysis,
            resume_profile=resume_profile,
            description=description,
        )

        mandatory_personal_mismatches = (
            confirmed_mandatory_personal_mismatches(
                resume_profile=resume_profile,
                description=description,
            )
        )

        saw_confirmed_personal_mismatch = bool(
            mandatory_personal_mismatches
        )

        if mandatory_personal_mismatches:
            existing_confirmed_personal_kinds: set[str] = set()

            for gap in analysis.hard_requirements_missing:
                evidence = personal_requirement_evidence(
                    gap,
                    description,
                    resume_profile,
                )

                if (
                    evidence is not None
                    and evidence["state"] == "DOES_NOT_MEET"
                ):
                    existing_confirmed_personal_kinds.add(
                        evidence["kind"]
                    )

            additional_mismatches = [
                clause
                for clause in mandatory_personal_mismatches
                if personal_fact_kind(clause)
                not in existing_confirmed_personal_kinds
            ]

            if additional_mismatches:
                analysis = analysis.model_copy(
                    update={
                        "hard_requirements_missing": list(
                            dict.fromkeys(
                                analysis.hard_requirements_missing
                                + additional_mismatches
                            )
                        )
                    }
                )

        # Confirmed failure of a mandatory personal eligibility requirement is
        # practical ineligibility, even when the technical fit is excellent.
        if saw_confirmed_personal_mismatch:
            overall_score = min(overall_score, 49)
            recommendation = "skip"

        # UNKNOWN personal eligibility is review uncertainty, never proof of
        # ineligibility. If it was the only hard-gap basis for SKIP, normalize
        # the result to the REVIEW band rather than inventing a failure.
        elif (
            recommendation == "skip"
            and saw_nonfailing_personal
            and not analysis.hard_requirements_missing
        ):
            overall_score = max(overall_score, 50)
            recommendation = "review"

        invalid_score_band = (
            (recommendation == "apply" and overall_score < 75)
            or (recommendation == "review" and overall_score < 50)
            or (recommendation == "skip" and overall_score >= 50)
        )
        if invalid_score_band:
            raise ResumeMatcherError(
                "AI fit result is internally inconsistent: "
                f"score {overall_score} does not support "
                f"{recommendation.upper()}."
            )

        if (
            recommendation == "apply"
            and analysis.hard_requirements_missing
        ):
            raise ResumeMatcherError(
                "AI fit result is internally inconsistent: APPLY cannot include "
                "missing core or required qualifications."
            )

        if not description_complete:
            raise ResumeMatcherError(
                "A current FULL_JD is required before AI scoring."
            )

        return ResumeJobMatch(
            record_key=record_key,
            model_name=model_name,
            prompt_version=MATCHER_PROMPT_VERSION,
            overall_score=overall_score,
            recommendation=recommendation,
            description_word_count=description_word_count,
            description_complete=description_complete,
            analysis=analysis,
        )

    # Legacy fallback for older unit-test fixtures and historical callers that
    # construct JobMatchAnalysis without the v4 final decision fields.
    analysis = validate_missing_qualifications(
        analysis=analysis,
        resume_profile=resume_profile,
        description=str(job.get("description") or ""),
    )

    specialization_gap_labels: list[str] = []

    if description_complete:
        critical_gaps, critical_risks = (
            detect_critical_requirement_gaps(
                resume_profile=resume_profile,
                job=job,
                description=str(job.get("description") or ""),
            )
        )
        mandatory_gap_labels = [
            gap.label
            for gap in critical_gaps
            if gap.severity == "mandatory"
        ]
        specialization_gap_labels = [
            gap.label
            for gap in critical_gaps
            if gap.severity == "specialization"
        ]
        critical_components = [
            gap.skill_component
            for gap in critical_gaps
        ]

        if critical_gaps:
            hard_requirements_missing = (
                list(
                    dict.fromkeys(
                        analysis.hard_requirements_missing
                        + [
                            f"Major gap: {gap}"
                            for gap in mandatory_gap_labels
                        ]
                    )
                )
                if mandatory_gap_labels
                else analysis.hard_requirements_missing
            )
            risk_factors = list(
                dict.fromkeys(
                    analysis.risk_factors
                    + critical_risks
                    + [
                        f"Major specialization gap: {gap}"
                        for gap in specialization_gap_labels
                    ]
                )
            )
            gap_summary = (
                mandatory_gap_labels
                + specialization_gap_labels
            )
            if mandatory_gap_labels:
                gap_summary_text = (
                    "Major gap: the role requires "
                    f"{'; '.join(gap_summary)}. "
                )
            else:
                gap_summary_text = (
                    "Major specialization gap: the role is centered on "
                    f"{'; '.join(gap_summary)}. "
                )
            update: dict[str, Any] = {
                "hard_requirements_missing": hard_requirements_missing,
                "risk_factors": risk_factors,
                "summary": (
                    gap_summary_text +
                    "The resume does not show equivalent production "
                    "ownership for that specialization. "
                    f"{analysis.summary}"
                ),
            }

            if "skills" in critical_components:
                update["skills_fit"] = min(
                    analysis.skills_fit,
                    70,
                )

            if "experience" in critical_components:
                update["experience_fit"] = min(
                    analysis.experience_fit,
                    70,
                )

            if "industry" in critical_components:
                update["industry_fit"] = min(
                    analysis.industry_fit,
                    65,
                )

            if "seniority" in critical_components:
                update["seniority_fit"] = min(
                    analysis.seniority_fit,
                    65,
                )

            analysis = analysis.model_copy(
                update=update,
            )

    analysis = validate_missing_qualifications(analysis, resume_profile, str(job.get("description") or ""))

    tenure_uncertainties: list[str] = []
    if description_complete:
        tenure_missing, tenure_uncertainties = required_platform_tenure_gaps(
            str(job.get("description") or ""), resume_profile
        )
        if tenure_missing or tenure_uncertainties:
            analysis = analysis.model_copy(update={
                "hard_requirements_missing": list(dict.fromkeys(
                    analysis.hard_requirements_missing + tenure_missing
                )),
                "risk_factors": list(dict.fromkeys(
                    analysis.risk_factors + tenure_uncertainties
                )),
                "skills_fit": min(analysis.skills_fit, 70),
                "summary": (
                    "Professional platform-tenure requirement needs verification: "
                    + "; ".join(tenure_missing + tenure_uncertainties) + ". "
                    + analysis.summary
                ),
            })

    overall_score = calculate_overall_score(
        analysis
    )

    # Missing requirements found in an incomplete posting
    # are not confirmed hard requirements. Preserve them
    # as risks instead of automatically rejecting the job.
    if (
        not description_complete
        and analysis.hard_requirements_missing
    ):
        unconfirmed_requirements = [
            (
                "Potential missing qualification "
                f"(unconfirmed): {requirement}"
            )
            for requirement
            in analysis.hard_requirements_missing
        ]

        analysis = analysis.model_copy(
            update={
                "hard_requirements_missing": [],
                "risk_factors": list(
                    dict.fromkeys(
                        analysis.risk_factors
                        + unconfirmed_requirements
                    )
                ),
                "confidence": "low",
            }
        )

    # Confirmed mandatory gaps can reject a job only when
    # the full description is available.
    if (
        description_complete
        and analysis.hard_requirements_missing
    ):
        overall_score = min(
            overall_score,
            49,
        )

    if (
        description_complete
        and (specialization_gap_labels or tenure_uncertainties)
        and not analysis.hard_requirements_missing
    ):
        overall_score = min(
            overall_score,
            74,
        )

    # A title-only or partial posting must never appear
    # as a confirmed high-confidence match. Keep the
    # score itself uncapped so review jobs still sort
    # meaningfully instead of bunching at one value.
    if not description_complete:
        analysis = analysis.model_copy(
            update={
                "confidence": "low",
            }
        )

    # Unknown personal conditions are not failed requirements or confirmed fits.
    # Remove fabricated negative phrasing from model risks; retain confirmation.
    clean_risks = []
    for risk in analysis.risk_factors:
        personal = personal_requirement_evidence(risk, str(job.get("description") or ""), resume_profile)
        clean_risks.append(f"Needs confirmation (UNKNOWN personal eligibility): {personal['kind']}; no explicit candidate fact supplied." if personal and personal["state"] == "UNKNOWN" else risk)
    analysis = analysis.model_copy(update={"risk_factors": list(dict.fromkeys(clean_risks))})
    recommendation = determine_recommendation(
        overall_score=overall_score,
        analysis=analysis,
        description_complete=(
            description_complete
        ),
    )

    if recommendation == "apply" and any(risk.startswith("Needs confirmation (UNKNOWN personal eligibility)") for risk in analysis.risk_factors):
        recommendation = "review"

    return ResumeJobMatch(
        record_key=record_key,
        model_name=model_name,
        prompt_version=MATCHER_PROMPT_VERSION,
        overall_score=overall_score,
        recommendation=recommendation,
        description_word_count=(
            description_word_count
        ),
        description_complete=(
            description_complete
        ),
        analysis=analysis,
    )


def score_resume_against_job(
    resume_profile: ResumeProfile,
    job: dict[str, Any],
    model_name: str | None = None,
) -> ResumeJobMatch:
    resume_profile = apply_candidate_eligibility_overrides(
        resume_profile
    )

    (
        description,
        description_word_count,
        description_complete,
    ) = get_description_information(job)

    if not description_complete:
        raise ResumeMatcherError("A current FULL_JD is required before AI scoring.")

    selected_model = (
        model_name
        or get_model_name()
    )

    prompt = build_job_match_prompt(
        resume_profile=resume_profile,
        job=job,
        description=description,
        description_complete=(
            description_complete
        ),
    )

    client = get_gemini_client()

    try:
        response = client.models.generate_content(
            model=selected_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type=(
                    "application/json"
                ),
                response_schema=JobMatchAnalysis,
                temperature=0.1,
            ),
        )

    except Exception as error:
        raise ResumeMatcherError(
            "Gemini was unable to score the job. "
            f"{type(error).__name__}: {error}"
        ) from error

    analysis = parse_job_match_response(
        response
    )

    return build_resume_job_match(
        resume_profile=resume_profile,
        job=job,
        model_name=selected_model,
        analysis=analysis,
        description_word_count=description_word_count,
        description_complete=description_complete,
    )


def score_resume_against_job_openai(
    resume_profile: ResumeProfile,
    job: dict[str, Any],
    model_name: str | None = None,
) -> ResumeJobMatch:
    resume_profile = apply_candidate_eligibility_overrides(
        resume_profile
    )

    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise ResumeMatcherError(
            "OPENAI_API_KEY is missing from .env."
        )

    try:
        from openai import OpenAI
    except ImportError as error:
        raise ResumeMatcherError(
            "The OpenAI package is not installed. "
            "Run pip install -r requirements.txt."
        ) from error

    (
        description,
        description_word_count,
        description_complete,
    ) = get_description_information(job)

    if not description_complete:
        raise ResumeMatcherError("A current FULL_JD is required before AI scoring.")

    selected_model = (
        model_name
        or get_openai_model_name()
    )

    prompt = build_job_match_prompt(
        resume_profile=resume_profile,
        job=job,
        description=description,
        description_complete=(
            description_complete
        ),
    )

    client = OpenAI(api_key=api_key)

    try:
        response = client.chat.completions.create(
            model=selected_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Return only valid JSON matching the supplied schema. "
                        "You must populate overall_score and recommendation in addition "
                        "to the component fit scores, requirement comparisons, strengths, "
                        "gaps, risks, confidence, and summary. overall_score and all *_fit "
                        "fields must be integers "
                        "from 0 to 100. recommendation must be apply, review, or skip and "
                        "must agree with the score bands in the user prompt."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "job_match_analysis",
                    "schema": (
                        get_openai_job_match_schema()
                    ),
                    "strict": True,
                },
            },
            temperature=0.1,
        )

    except Exception as error:
        raise ResumeMatcherError(
            "OpenAI was unable to score the job. "
            f"{type(error).__name__}: {error}"
        ) from error

    content = response.choices[0].message.content
    analysis = parse_openai_job_match_content(
        content
    )

    return build_resume_job_match(
        resume_profile=resume_profile,
        job=job,
        model_name=selected_model,
        analysis=analysis,
        description_word_count=description_word_count,
        description_complete=description_complete,
    )
