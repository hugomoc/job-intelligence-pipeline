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
)


from src.description_quality import MINIMUM_FULL_DESCRIPTION_WORDS, assess_description_quality
















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
) -> str:
    """Build the full-JD, AI-first fit-scoring prompt."""
    completeness = (
        "COMPLETE"
        if description_complete
        else "INCOMPLETE"
    )

    return f"""
You are an expert technical recruiter evaluating one candidate against one
specific job. Read the ENTIRE job description and the ENTIRE candidate profile
before deciding.

Your job is to make the same holistic judgment a strong recruiter would make,
not to count keywords.

Rules:

1. Use only evidence in the candidate profile and job information. Never invent
   experience, skills, eligibility, location, language fluency, or credentials.
2. Distinguish professional production experience from portfolio/project work.
3. Identify the role's CORE RESPONSIBILITIES even when a responsibility bullet
   does not contain words such as "required" or "must". A central hands-on duty
   can be a major gap if the candidate has no demonstrated experience with it.
4. Distinguish core/required qualifications from preferred or nice-to-have
   qualifications. Do not penalize heavily for preferred-only technologies.
5. Evaluate alternatives correctly. "A or B", "such as", examples, and
   "or equivalent" do not require every named technology.
6. Consider genuinely transferable experience, but do not treat a different
   tool as equivalent when the job specifically requires hands-on use of a
   technology, language, platform, or domain that is central to the work.
7. Evaluate practical eligibility from the full JD, including explicit
   geography, required language fluency, travel, clearance, and work
   authorization constraints. personal_eligibility contains explicit
   candidate-supplied facts and must be treated as authoritative.
8. Never turn an UNKNOWN personal fact into a failed requirement. Unknown
   location, language, work authorization, clearance, relocation, or travel
   information may create REVIEW uncertainty, but UNKNOWN by itself must never
   produce SKIP. When an explicit personal_eligibility fact satisfies the
   requirement, do not list that requirement as a gap. Confirmed incompatibility
   may be treated as a real eligibility gap.
9. Do not let a matching title or many generic skills overwhelm one major
   missing core responsibility.
10. Treat years-of-experience requirements as minimums unless the JD gives a
    maximum or bounded range. More experience is not a negative by itself.
11. Put missing explicit requirements AND missing central hands-on
    responsibilities in hard_requirements_missing. Put preferred-only gaps in
    preferred_qualifications_missing.
12. Return overall_score as your HOLISTIC final fit score. Do not derive it from
    a fixed weighted average of the component scores.
13. Use these final decision rules consistently:
      APPLY  = 75-100 and no major core/eligibility blocker
      REVIEW = 50-100 when meaningful gaps or uncertainty make APPLY premature;
               scores of 75+ may still be REVIEW when the fit is strong but a
               material concern needs human confirmation
      SKIP   = 0-49, including clear practical ineligibility or a major mismatch
14. recommendation must agree with overall_score and the stated gaps.
15. Keep strengths, gaps, risks, and summary concise and specific enough that a
    human can understand why the score was assigned.
16. Populate requirement_matches with the 6-10 most important requirements,
    core responsibilities, and explicit eligibility constraints from the JD.
    For each row, compare the requirement with concise candidate evidence and
    label it excellent, strong, partial, or gap. Do not invent evidence. For
    personal eligibility, use PARTIAL when the fact is unknown and GAP only for
    a confirmed mismatch. Keep these rows explanatory only: do not calculate
    overall_score by counting or averaging the row labels.

CANDIDATE PROFILE:

{resume_profile.model_dump_json(indent=2)}

JOB INFORMATION:

Title: {job.get("title") or "Not provided"}
Company: {job.get("company_name") or "Not provided"}
Location: {job.get("location") or "Not provided"}
Salary: {job.get("salary_text") or "Not provided"}
Source: {job.get("source") or "Not provided"}

Job-description completeness: {completeness}

FULL JOB DESCRIPTION:

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


def normalize_v5_personal_eligibility(
    analysis: JobMatchAnalysis,
    resume_profile: ResumeProfile,
    description: str,
) -> tuple[JobMatchAnalysis, bool, bool]:
    """Remove fabricated personal gaps and identify personal eligibility state."""
    hard_missing: list[str] = []
    risks = list(analysis.risk_factors)
    saw_nonfailing_personal = False
    saw_confirmed_personal_mismatch = False

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
            saw_confirmed_personal_mismatch = True
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
        saw_confirmed_personal_mismatch,
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

    # v4 production scoring is AI-first: the full JD and candidate profile are
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
            saw_confirmed_personal_mismatch,
        ) = normalize_v5_personal_eligibility(
            analysis=analysis,
            resume_profile=resume_profile,
            description=description,
        )

        # UNKNOWN personal eligibility is review uncertainty, never proof of
        # ineligibility. If it was the only hard-gap basis for SKIP, normalize
        # the result to the REVIEW band rather than inventing a failure.
        if (
            recommendation == "skip"
            and saw_nonfailing_personal
            and not saw_confirmed_personal_mismatch
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
