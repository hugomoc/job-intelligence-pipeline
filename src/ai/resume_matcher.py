from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from src.ai.resume_profiler import (
    ResumeProfile,
    get_gemini_client,
    get_model_name,
)


MINIMUM_COMPLETE_DESCRIPTION_WORDS = 80
MATCHER_PROMPT_VERSION = "v2"

SIGNIFICANT_TERM_PATTERN = re.compile(
    r"[a-z0-9+#.]+"
)

TECH_ALIASES: dict[str, set[str]] = {
    "aws": {
        "aws",
        "amazon web services",
        "redshift",
        "athena",
        "glue",
        "s3",
        "lambda",
        "cloudformation",
        "quicksight",
    },
    "azure": {
        "azure",
        "microsoft azure",
        "azure devops",
        "synapse",
    },
    "gcp": {
        "gcp",
        "google cloud",
        "google cloud platform",
        "bigquery",
    },
    "snowflake": {"snowflake"},
    "redshift": {"redshift", "amazon redshift"},
    "bigquery": {"bigquery", "google bigquery"},
    "git": {"git", "github", "gitlab", "bitbucket"},
    "ci_cd": {
        "ci/cd",
        "cicd",
        "continuous integration",
        "continuous delivery",
        "continuous deployment",
        "jenkins",
        "github actions",
        "gitlab ci",
        "azure devops",
        "circleci",
    },
    "agile": {
        "agile",
        "scrum",
        "sprint",
        "sprints",
        "kanban",
    },
}

CLOUD_GROUPS = {"aws", "azure", "gcp"}
WAREHOUSE_GROUPS = {
    "snowflake",
    "redshift",
    "bigquery",
}


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


def normalize_match_text(value: str | None) -> str:
    if not value:
        return ""

    normalized = value.casefold()
    normalized = normalized.replace("–", "-")
    normalized = normalized.replace("—", "-")
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(
        r"[^a-z0-9+#./]+",
        " ",
        normalized,
    )

    return " ".join(normalized.split())


def contains_phrase(
    text: str,
    phrase: str,
) -> bool:
    normalized_text = (
        f" {normalize_match_text(text)} "
    )
    normalized_phrase = normalize_match_text(phrase)

    if not normalized_phrase:
        return False

    return f" {normalized_phrase} " in normalized_text


def profile_to_text(
    resume_profile: ResumeProfile,
    include_certifications: bool = True,
) -> str:
    profile_data = resume_profile.model_dump()

    if not include_certifications:
        profile_data = {
            key: value
            for key, value in profile_data.items()
            if key != "certifications"
        }

    return json.dumps(
        profile_data,
        ensure_ascii=False,
    )


def profile_has_alias_group(
    resume_profile: ResumeProfile,
    group_name: str,
    include_certifications: bool = True,
) -> bool:
    evidence_text = profile_to_text(
        resume_profile=resume_profile,
        include_certifications=include_certifications,
    )

    return any(
        contains_phrase(evidence_text, alias)
        for alias in TECH_ALIASES[group_name]
    )


def mentioned_alias_groups(
    requirement: str,
) -> set[str]:
    return {
        group_name
        for group_name, aliases in TECH_ALIASES.items()
        if any(
            contains_phrase(requirement, alias)
            for alias in aliases
        )
    }


def requirement_uses_alternatives(
    requirement: str,
) -> bool:
    normalized = normalize_match_text(requirement)

    return any(
        marker in f" {normalized} "
        for marker in (
            " or ",
            " such as ",
            " including ",
            " e.g ",
            " eg ",
        )
    ) or "(" in requirement


def requirement_requires_all(
    requirement: str,
) -> bool:
    normalized = normalize_match_text(requirement)

    return any(
        marker in f" {normalized} "
        for marker in (
            " all ",
            " each ",
            " both ",
        )
    )


def significant_terms(value: str) -> set[str]:
    stop_words = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "for",
        "in",
        "of",
        "or",
        "the",
        "to",
        "with",
        "experience",
        "knowledge",
        "skill",
        "skills",
        "using",
    }

    return {
        term
        for term in SIGNIFICANT_TERM_PATTERN.findall(
            normalize_match_text(value)
        )
        if term not in stop_words and len(term) > 2
    }


def requirement_is_demonstrated(
    requirement: str,
    resume_profile: ResumeProfile,
) -> bool:
    normalized_requirement = normalize_match_text(
        requirement
    )
    groups = mentioned_alias_groups(
        requirement
    )
    non_cert_profile_text = profile_to_text(
        resume_profile=resume_profile,
        include_certifications=False,
    )

    if "cloud" in normalized_requirement:
        mentioned_clouds = groups & CLOUD_GROUPS

        if mentioned_clouds:
            if (
                requirement_uses_alternatives(requirement)
                and not requirement_requires_all(requirement)
            ):
                return any(
                    profile_has_alias_group(
                        resume_profile,
                        group,
                        include_certifications=False,
                    )
                    for group in mentioned_clouds
                )

            return all(
                profile_has_alias_group(
                    resume_profile,
                    group,
                    include_certifications=False,
                )
                for group in mentioned_clouds
            )

        return any(
            profile_has_alias_group(
                resume_profile,
                group,
                include_certifications=False,
            )
            for group in CLOUD_GROUPS
        )

    mentioned_warehouses = (
        groups & WAREHOUSE_GROUPS
    )

    if (
        "warehouse" in normalized_requirement
        or mentioned_warehouses
    ):
        if mentioned_warehouses:
            if (
                requirement_uses_alternatives(requirement)
                and not requirement_requires_all(requirement)
            ):
                return any(
                    profile_has_alias_group(
                        resume_profile,
                        group,
                    )
                    for group in mentioned_warehouses
                )

            return all(
                profile_has_alias_group(
                    resume_profile,
                    group,
                )
                for group in mentioned_warehouses
            )

        return any(
            profile_has_alias_group(
                resume_profile,
                group,
            )
            for group in WAREHOUSE_GROUPS
        )

    if (
        "ci/cd" in normalized_requirement
        or "cicd" in normalized_requirement
        or "continuous integration" in normalized_requirement
        or "continuous delivery" in normalized_requirement
        or "continuous deployment" in normalized_requirement
    ):
        has_ci_cd = profile_has_alias_group(
            resume_profile,
            "ci_cd",
        )

        if "git" in normalized_requirement:
            return has_ci_cd and profile_has_alias_group(
                resume_profile,
                "git",
            )

        return has_ci_cd

    if "git" in normalized_requirement:
        return profile_has_alias_group(
            resume_profile,
            "git",
        )

    if (
        "agile" in normalized_requirement
        or "scrum" in normalized_requirement
        or "sprint" in normalized_requirement
    ):
        return profile_has_alias_group(
            resume_profile,
            "agile",
        )

    if groups:
        if (
            requirement_uses_alternatives(requirement)
            and not requirement_requires_all(requirement)
        ):
            return any(
                profile_has_alias_group(
                    resume_profile,
                    group,
                )
                for group in groups
            )

        return all(
            profile_has_alias_group(
                resume_profile,
                group,
            )
            for group in groups
        )

    requirement_terms = significant_terms(
        requirement
    )

    if not requirement_terms:
        return False

    profile_terms = significant_terms(
        non_cert_profile_text
    )

    return requirement_terms.issubset(
        profile_terms
    )


def remove_demonstrated_missing_items(
    missing_items: list[str],
    resume_profile: ResumeProfile,
) -> list[str]:
    return [
        item
        for item in missing_items
        if not requirement_is_demonstrated(
            item,
            resume_profile,
        )
    ]


def validate_missing_qualifications(
    analysis: JobMatchAnalysis,
    resume_profile: ResumeProfile,
) -> JobMatchAnalysis:
    hard_missing = remove_demonstrated_missing_items(
        analysis.hard_requirements_missing,
        resume_profile,
    )
    preferred_missing = remove_demonstrated_missing_items(
        analysis.preferred_qualifications_missing,
        resume_profile,
    )

    if (
        hard_missing == analysis.hard_requirements_missing
        and preferred_missing
        == analysis.preferred_qualifications_missing
    ):
        return analysis

    return analysis.model_copy(
        update={
            "hard_requirements_missing": hard_missing,
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
    # Incomplete postings cannot be confidently approved
    # or rejected unless the fit is very strong or weak.
    if not description_complete:
        if overall_score >= 85:
            return "apply"

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
    # as a confirmed high-confidence match. Keep the
    # score itself uncapped so review jobs still sort
    # meaningfully instead of bunching at one value.
    if not description_complete:
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
