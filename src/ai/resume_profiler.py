from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from src.resume.extractor import ExtractedResume


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

load_dotenv(
    PROJECT_ROOT / ".env"
)


EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)

PHONE_PATTERN = re.compile(
    r"""
    (?:
        \+?1[\s.-]?
    )?
    (?:
        \(\d{3}\)
        |
        \d{3}
    )
    [\s.-]?
    \d{3}
    [\s.-]?
    \d{4}
    """,
    re.VERBOSE,
)

URL_PATTERN = re.compile(
    r"""
    (?:
        https?://
        |
        www\.
    )
    [^\s•|]+
    """,
    re.IGNORECASE | re.VERBOSE,
)

STREET_ADDRESS_PATTERN = re.compile(
    r"""
    ^\s*
    \d{1,6}
    \s+
    .+
    \b
    (
        street
        | st
        | avenue
        | ave
        | road
        | rd
        | drive
        | dr
        | lane
        | ln
        | boulevard
        | blvd
        | court
        | ct
        | circle
        | cir
        | parkway
        | pkwy
        | way
    )
    \b
    .*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


class QuantifiedAchievement(BaseModel):
    achievement: str = Field(
        description=(
            "Concise description of a measurable "
            "professional achievement."
        )
    )

    evidence: str = Field(
        description=(
            "Resume evidence supporting the achievement."
        )
    )


class EducationItem(BaseModel):
    degree: str = Field(
        description="Degree or education credential."
    )

    institution: str = Field(
        description="Education institution."
    )

    field_of_study: str | None = Field(
        default=None,
        description="Field of study when provided.",
    )


class ResumeProfile(BaseModel):
    professional_summary: str = Field(
        description=(
            "Brief professional summary based only "
            "on the resume."
        )
    )

    target_roles: list[str] = Field(
        default_factory=list,
        description=(
            "Roles the candidate is qualified to target."
        ),
    )

    current_or_recent_titles: list[str] = Field(
        default_factory=list,
        description=(
            "Most recent or most relevant job titles."
        ),
    )

    seniority: Literal[
        "entry",
        "mid",
        "senior",
        "lead",
        "manager",
        "executive",
        "mixed",
        "unknown",
    ] = Field(
        description=(
            "Candidate's demonstrated professional level."
        )
    )

    years_of_relevant_experience: int | None = Field(
        default=None,
        ge=0,
        le=60,
        description=(
            "Years of relevant experience supported "
            "by the resume. Use null when unclear."
        ),
    )

    production_skills: list[str] = Field(
        default_factory=list,
        description=(
            "Skills demonstrated in professional "
            "production experience."
        ),
    )

    project_skills: list[str] = Field(
        default_factory=list,
        description=(
            "Skills demonstrated primarily through "
            "personal or portfolio projects."
        ),
    )

    data_engineering_capabilities: list[str] = Field(
        default_factory=list,
        description=(
            "Data engineering methods and capabilities."
        ),
    )

    bi_analytics_skills: list[str] = Field(
        default_factory=list,
        description=(
            "Business intelligence and analytics skills."
        ),
    )

    programming_languages: list[str] = Field(
        default_factory=list,
        description="Programming and query languages.",
    )

    databases_warehouses: list[str] = Field(
        default_factory=list,
        description=(
            "Databases and data warehouses used."
        ),
    )

    cloud_platforms: list[str] = Field(
        default_factory=list,
        description="Cloud platforms used.",
    )

    tools_platforms: list[str] = Field(
        default_factory=list,
        description=(
            "Important frameworks, tools and platforms."
        ),
    )

    ai_ml_experience: list[str] = Field(
        default_factory=list,
        description=(
            "Artificial intelligence and machine "
            "learning experience."
        ),
    )

    industries: list[str] = Field(
        default_factory=list,
        description=(
            "Industries or business domains supported "
            "by the resume."
        ),
    )

    certifications: list[str] = Field(
        default_factory=list,
        description="Professional certifications.",
    )

    education: list[EducationItem] = Field(
        default_factory=list,
        description="Education history.",
    )

    quantified_achievements: list[
        QuantifiedAchievement
    ] = Field(
        default_factory=list,
        description=(
            "Important measurable professional results."
        ),
    )

    leadership_evidence: list[str] = Field(
        default_factory=list,
        description=(
            "Evidence of leadership, ownership, "
            "mentoring or cross-functional influence."
        ),
    )

    strengths_for_job_matching: list[str] = Field(
        default_factory=list,
        description=(
            "Candidate strengths that should receive "
            "weight during job matching."
        ),
    )

    profile_confidence: Literal[
        "high",
        "medium",
        "low",
    ] = Field(
        description=(
            "Confidence that the resume contains enough "
            "information for an accurate profile."
        )
    )


@dataclass(frozen=True)
class ProfiledResume:
    resume_hash: str
    filename: str
    model_name: str
    profile: ResumeProfile


class ResumeProfilerError(Exception):
    """Raised when Gemini cannot profile a resume."""


def redact_personal_information(
    resume_text: str,
) -> str:
    """
    Removes contact details that Gemini does not need
    for resume-to-job matching.
    """
    redacted_lines: list[str] = []

    for line in resume_text.splitlines():
        if STREET_ADDRESS_PATTERN.match(line):
            redacted_lines.append(
                "[STREET ADDRESS REDACTED]"
            )
            continue

        redacted_line = EMAIL_PATTERN.sub(
            "[EMAIL REDACTED]",
            line,
        )

        redacted_line = PHONE_PATTERN.sub(
            "[PHONE REDACTED]",
            redacted_line,
        )

        redacted_line = URL_PATTERN.sub(
            "[URL REDACTED]",
            redacted_line,
        )

        redacted_lines.append(redacted_line)

    return "\n".join(
        redacted_lines
    ).strip()


def build_resume_profile_prompt(
    redacted_resume_text: str,
) -> str:
    return f"""
You are an expert technical recruiter and resume analyst.

Convert the resume below into a structured candidate profile.

Rules:

1. Use only information supported by the resume.
2. Do not invent experience, skills, certifications,
   accomplishments or education.
3. Distinguish professional production experience from
   portfolio or personal-project experience.
4. Do not treat a technology listed only in a project as
   years of professional production experience.
5. Preserve measurable achievements such as cost savings,
   performance improvements and workload scale.
6. Use concise, normalized skill names.
7. Do not include names, addresses, phone numbers,
   email addresses, URLs or other contact information.
8. Do not calculate years of experience from unrelated
   early-career roles.
9. When years of relevant experience are unclear,
   return null.
10. Do not assume people-management experience unless the
    resume explicitly supports it.

RESUME:

{redacted_resume_text}
""".strip()


def get_gemini_client() -> genai.Client:
    api_key = os.getenv(
        "GEMINI_API_KEY"
    )

    if not api_key:
        raise ResumeProfilerError(
            "GEMINI_API_KEY is missing from .env."
        )

    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=60_000,
        ),
    )


def get_model_name() -> str:
    return os.getenv(
        "GEMINI_MODEL",
        "gemini-2.5-flash-lite",
    )


def profile_resume_text(
    resume_text: str,
    model_name: str | None = None,
) -> ResumeProfile:
    if not resume_text.strip():
        raise ResumeProfilerError(
            "Resume text is empty."
        )

    redacted_text = (
        redact_personal_information(
            resume_text
        )
    )

    prompt = build_resume_profile_prompt(
        redacted_resume_text=redacted_text
    )

    selected_model = (
        model_name
        or get_model_name()
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
                response_schema=ResumeProfile,
                temperature=0.1,
            ),
        )

    except Exception as error:
        raise ResumeProfilerError(
            "Gemini was unable to profile "
            "the resume. "
            f"{type(error).__name__}: {error}"
        ) from error

    # The SDK may automatically parse the response
    # when a Pydantic model is supplied.
    if isinstance(
        response.parsed,
        ResumeProfile,
    ):
        return response.parsed

    if response.parsed is not None:
        try:
            return ResumeProfile.model_validate(
                response.parsed
            )
        except ValidationError:
            pass

    if not response.text:
        raise ResumeProfilerError(
            "Gemini returned an empty response."
        )

    try:
        return ResumeProfile.model_validate_json(
            response.text
        )

    except ValidationError as error:
        raise ResumeProfilerError(
            "Gemini returned an invalid "
            "resume profile. "
            f"Validation error: {error}"
        ) from error


def profile_resume(
    resume: ExtractedResume,
    model_name: str | None = None,
) -> ProfiledResume:
    selected_model = (
        model_name
        or get_model_name()
    )

    profile = profile_resume_text(
        resume_text=resume.text,
        model_name=selected_model,
    )

    return ProfiledResume(
        resume_hash=resume.resume_hash,
        filename=resume.filename,
        model_name=selected_model,
        profile=profile,
    )