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
    get_gemini_client,
    get_model_name,
)


from src.description_quality import MINIMUM_FULL_DESCRIPTION_WORDS, assess_description_quality
















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
            "Explicitly required qualifications that the "
            "candidate does not demonstrate."
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
    """Build the full evidence-bound prompt for fit scoring."""
    completeness = (
        "COMPLETE"
        if description_complete
        else "INCOMPLETE"
    )

    return f"""
You are an expert technical recruiter evaluating a candidate
against a specific job.

Evaluate only evidence contained in the candidate profile and
job information below.

Rules:

1. Do not invent candidate experience or qualifications.
2. Distinguish production experience from portfolio-project
   experience.
3. Portfolio experience is useful evidence, but it must not
   be treated as years of professional production experience.
4. Distinguish explicitly required qualifications from
   preferred qualifications.
5. Add an item to hard_requirements_missing only when the job
   explicitly requires it and the candidate profile does not
   demonstrate it.
6. Do not penalize the candidate for every technology that is
   merely preferred.
7. Consider equivalent and transferable experience.
8. Do not reward keyword repetition.
9. If the job description is incomplete, use low confidence
   and do not infer requirements that are not provided.
10. Required security clearance, work authorization,
    mandatory location or travel requirements should be
    identified as risk factors when relevant.
11. Evaluate seniority based on demonstrated scope,
    responsibilities and years of relevant experience.
12. Treat years-of-experience requirements as minimum
    requirements unless the job explicitly specifies a
    maximum or a bounded range.

13. A candidate exceeding a minimum experience requirement
    should normally be treated as meeting or exceeding the
    requirement, not as a risk.

14. Do not identify excess years of experience as a risk by
    itself. Only identify possible overqualification when
    there is explicit evidence of a meaningful mismatch in
    seniority, responsibilities, compensation, or role scope.

15. When possible overqualification is uncertain, do not add
    it to risk_factors.

16. Keep strengths, gaps and risks concise and specific.
17. Before adding any item to hard_requirements_missing or
    preferred_qualifications_missing, inspect the complete
    candidate profile, including professional experience,
    skills, projects, certifications, tools, platforms,
    methodologies and summarized experience.
18. Do not mark a qualification missing when the candidate
    demonstrates the exact qualification, a recognized alias,
    or a clearly equivalent or transferable qualification.
19. When a requirement lists examples in parentheses, such as
    "cloud platforms (AWS, Azure, GCP)" or "data warehouses
    (Snowflake, Redshift, BigQuery)", treat those examples as
    alternatives unless the job explicitly says all are required.
20. A cloud-platform requirement can be satisfied by
    demonstrated production use of relevant cloud services,
    infrastructure, databases or data warehouses. Do not check
    only certifications when work-history evidence exists.
21. Treat Git-based development plus Jenkins, GitHub Actions,
    GitLab CI, Azure DevOps, CircleCI or equivalent automation
    tools as relevant evidence for CI/CD and Git requirements.
22. Do not declare a qualification missing merely because the
    candidate profile uses different wording from the job.
23. A missing qualification must be genuinely absent. Do not
    use missing qualifications to describe experience that is
    present but could be stronger, newer or more detailed.
24. Preferred technologies should not all be treated as
    mandatory.
25. Agile methodologies may be missing only when the job asks
    for Agile and the complete candidate profile contains no
    evidence of Agile, Scrum, sprint-based delivery or an
    equivalent methodology.
26. Before returning the final JSON, verify every item in
    hard_requirements_missing and preferred_qualifications_missing
    against the complete candidate profile one final time.
27. Identify critical specializations that define the role, such
    as production RAG/agent systems, Databricks, Domo, healthcare
    claims, production ML deployment, ledger/accounting domain
    expertise, or Staff/Principal organizational scope. Missing
    one central specialization is a major gap even when the
    candidate is an excellent generic data-engineering fit.
28. Do not treat personal projects, AI-assisted coding, coursework
    or generic tool usage as equivalent to production ownership
    when the job asks for production systems serving real users.
29. When a critical specialization is missing, lower the relevant
    component score substantially and explain it as a major gap.
    Do not describe it as a minor or preferred gap.
30. Do not apply a major penalty for interchangeable alternatives
    or preferred-only skills, such as Snowflake/BigQuery/Redshift
    or similar when the candidate has one equivalent platform.

CANDIDATE PROFILE:

{resume_profile.model_dump_json(indent=2)}

JOB INFORMATION:

Title: {job.get("title") or "Not provided"}
Company: {job.get("company_name") or "Not provided"}
Location: {job.get("location") or "Not provided"}
Salary: {job.get("salary_text") or "Not provided"}
Source: {job.get("source") or "Not provided"}

Job-description completeness: {completeness}

JOB DESCRIPTION:

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
            elif decision["reason"] == "central responsibility unsatisfied":
                risks.append(f"Major central responsibility gap: {gap}")
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

    if any(
        risk.startswith("Major central responsibility gap:")
        for risk in analysis.risk_factors
    ):
        return "review"

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
    schema["additionalProperties"] = False
    schema["required"] = list(
        schema.get("properties", {}).keys()
    )

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

    central_responsibility_gaps = [
        risk.removeprefix("Major central responsibility gap:").strip()
        for risk in analysis.risk_factors
        if risk.startswith("Major central responsibility gap:")
    ]
    if central_responsibility_gaps:
        analysis = analysis.model_copy(
            update={
                "skills_fit": min(analysis.skills_fit, 65),
                "experience_fit": min(analysis.experience_fit, 70),
                "summary": (
                    "Major central responsibility gap: "
                    + "; ".join(central_responsibility_gaps)
                    + ". "
                    + analysis.summary
                ),
            }
        )

    overall_score = calculate_overall_score(
        analysis
    )
    if central_responsibility_gaps:
        overall_score = min(overall_score, 69)

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
                        "Return only valid JSON with these keys: "
                        "title_fit, skills_fit, experience_fit, "
                        "seniority_fit, industry_fit, location_fit, "
                        "confidence, matching_strengths, "
                        "hard_requirements_missing, "
                        "preferred_qualifications_missing, "
                        "risk_factors, summary. The six *_fit "
                        "fields must be integer numbers from 0 "
                        "to 100, never words."
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
