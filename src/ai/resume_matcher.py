from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from src.ai.resume_profiler import (
    ResumeProfile,
    get_gemini_client,
    get_model_name,
)

import json


MINIMUM_COMPLETE_DESCRIPTION_WORDS = 80


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
    """Raised when Gemini cannot evaluate a job."""


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
        word_count
        >= MINIMUM_COMPLETE_DESCRIPTION_WORDS
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
12. Keep strengths, gaps and risks concise and specific.

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


def determine_recommendation(
    overall_score: int,
    analysis: JobMatchAnalysis,
    description_complete: bool,
) -> Literal[
    "apply",
    "review",
    "skip",
]:
    # Incomplete postings cannot be confidently approved
    # or rejected unless the basic fit is extremely weak.
    if not description_complete:
        if overall_score >= 50:
            return "review"

        return "skip"

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

    if not isinstance(payload, dict):
        raise ResumeMatcherError(
            "Gemini returned an unexpected "
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
            "Gemini returned an invalid job-match "
            "response.\n\n"
            f"Validation details:\n{error}\n\n"
            "Response preview:\n"
            f"{response_preview}"
        ) from error


def score_resume_against_job(
    resume_profile: ResumeProfile,
    job: dict[str, Any],
    model_name: str | None = None,
) -> ResumeJobMatch:
    record_key = str(
        job.get("record_key") or ""
    )

    if not record_key:
        raise ResumeMatcherError(
            "The job record_key is missing."
        )

    (
        description,
        description_word_count,
        description_complete,
    ) = get_description_information(job)

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

    overall_score = calculate_overall_score(
        analysis
    )

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

    # A title-only or partial posting must never appear
    # as a confirmed high-confidence match.
    if not description_complete:
        overall_score = min(
            overall_score,
            69,
        )

        analysis = analysis.model_copy(
            update={
                "confidence": "low",
            }
        )

    recommendation = determine_recommendation(
        overall_score=overall_score,
        analysis=analysis,
        description_complete=(
            description_complete
        ),
    )

    return ResumeJobMatch(
        record_key=record_key,
        model_name=selected_model,
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