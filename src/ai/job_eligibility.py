"""Lightweight AI screening before full resume/job scoring.

The full matcher is expensive and should only run on plausible opportunities.
This module asks the model a cheaper question first: should this job stay in
the user's queue, be excluded, or wait for a better description?
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Literal

from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from src.ai.resume_matcher import ResumeMatcherError
from src.ai.resume_profiler import ResumeProfile, get_gemini_client, get_model_name


ELIGIBILITY_PROMPT_VERSION = "v1"


class JobEligibilityAnalysis(BaseModel):
    decision: Literal[
        "eligible",
        "exclude",
        "needs_description",
    ] = Field(
        description=(
            "Whether the job should stay in the user's job-search queue. "
            "Use needs_description only when the title is plausibly relevant "
            "but there is not enough description to make a confident decision."
        )
    )
    confidence: Literal["high", "medium", "low"]
    reason: str
    matched_resume_signals: list[str] = Field(default_factory=list)
    missing_or_mismatched_signals: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class JobEligibilityDecision:
    canonical_job_key: str
    record_key: str
    resume_hash: str
    model_name: str
    prompt_version: str
    analysis: JobEligibilityAnalysis


def build_job_eligibility_prompt(
    resume_profile: ResumeProfile,
    job: dict[str, Any],
) -> str:
    """Build the title/description prompt used for pre-score screening."""
    description = str(job.get("description") or "").strip()
    description_available = "yes" if description else "no"

    return f"""
You are screening job-alert results for one candidate.

Decide whether this job should remain in the candidate's job-search queue
before a more expensive fit score is run.

Use the job title, company, location, source, and description when present.
If the description is missing, analyze the title only and use low confidence
unless the title is clearly outside the candidate's target lane.

Rules:

1. Return exclude for roles outside the candidate's resume and target lane.
2. Exclude software engineering, full-stack, front-end, back-end, network,
   SAP/ABAP, Oracle-specific, Java-specific, Power BI-specific, Palantir
   Foundry-specific, executive, VP, director, and forward-deployed roles unless
   the resume clearly supports them.
3. Do not keep a job just because it contains the word data.
4. Keep data engineering, analytics engineering, business intelligence,
   SQL, Python, ELT/ETL, dbt, Snowflake, data warehouse, reporting automation,
   and analytics platform roles when they align with the resume.
5. If the title is plausible but the description is missing or too thin, return
   needs_description instead of eligible.
6. Be conservative. The UI should save the candidate time.

CANDIDATE PROFILE:

{resume_profile.model_dump_json(indent=2)}

JOB:

Title: {job.get("title") or "Not provided"}
Company: {job.get("company_name") or "Not provided"}
Location: {job.get("location") or "Not provided"}
Salary: {job.get("salary_text") or "Not provided"}
Source: {job.get("source") or "Not provided"}
Description available: {description_available}

Description:

{description or "No description is available."}
""".strip()


def parse_job_eligibility_payload(
    payload: Any,
    provider_name: str,
) -> JobEligibilityAnalysis:
    if not isinstance(payload, dict):
        raise ResumeMatcherError(
            f"{provider_name} returned an unexpected eligibility response."
        )

    for field_name in (
        "matched_resume_signals",
        "missing_or_mismatched_signals",
    ):
        if payload.get(field_name) is None:
            payload[field_name] = []

    for field_name in ("decision", "confidence"):
        if isinstance(payload.get(field_name), str):
            payload[field_name] = payload[field_name].strip().casefold()

    try:
        return JobEligibilityAnalysis.model_validate(payload)
    except ValidationError as error:
        raise ResumeMatcherError(
            f"{provider_name} returned an invalid eligibility response: {error}"
        ) from error


def parse_gemini_job_eligibility_response(
    response: Any,
) -> JobEligibilityAnalysis:
    if isinstance(response.parsed, JobEligibilityAnalysis):
        return response.parsed

    if response.parsed is not None:
        payload = (
            response.parsed.model_dump()
            if isinstance(response.parsed, BaseModel)
            else response.parsed
        )
    elif response.text:
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as error:
            raise ResumeMatcherError(
                "Gemini returned invalid eligibility JSON."
            ) from error
    else:
        raise ResumeMatcherError("Gemini returned an empty eligibility response.")

    return parse_job_eligibility_payload(payload, "Gemini")


def get_openai_job_eligibility_schema() -> dict[str, Any]:
    schema = JobEligibilityAnalysis.model_json_schema()
    schema["additionalProperties"] = False
    schema["required"] = list(schema.get("properties", {}).keys())
    return schema


def get_openai_screening_model_name() -> str:
    return os.getenv("OPENAI_SCREENING_MODEL", os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))


def evaluate_job_eligibility(
    resume_hash: str,
    resume_profile: ResumeProfile,
    job: dict[str, Any],
    model_name: str | None = None,
) -> JobEligibilityDecision:
    """Screen one job with Gemini and return a cached decision payload."""
    selected_model = model_name or get_model_name()
    prompt = build_job_eligibility_prompt(resume_profile, job)

    try:
        response = get_gemini_client().models.generate_content(
            model=selected_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=JobEligibilityAnalysis,
                temperature=0.0,
            ),
        )
    except Exception as error:
        raise ResumeMatcherError(
            "Gemini was unable to screen the job. "
            f"{type(error).__name__}: {error}"
        ) from error

    analysis = parse_gemini_job_eligibility_response(response)

    return JobEligibilityDecision(
        canonical_job_key=str(job.get("canonical_job_key") or job.get("record_key") or ""),
        record_key=str(job.get("record_key") or ""),
        resume_hash=resume_hash,
        model_name=selected_model,
        prompt_version=ELIGIBILITY_PROMPT_VERSION,
        analysis=analysis,
    )


def evaluate_job_eligibility_openai(
    resume_hash: str,
    resume_profile: ResumeProfile,
    job: dict[str, Any],
    model_name: str | None = None,
) -> JobEligibilityDecision:
    """Screen one job with OpenAI when Gemini is unavailable or exhausted."""
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise ResumeMatcherError("OPENAI_API_KEY is missing from .env.")

    try:
        from openai import OpenAI
    except ImportError as error:
        raise ResumeMatcherError(
            "The OpenAI package is not installed. Run pip install -r requirements.txt."
        ) from error

    selected_model = model_name or get_openai_screening_model_name()
    prompt = build_job_eligibility_prompt(resume_profile, job)
    client = OpenAI(api_key=api_key)

    try:
        response = client.chat.completions.create(
            model=selected_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Return only valid JSON with keys decision, confidence, "
                        "reason, matched_resume_signals, and "
                        "missing_or_mismatched_signals."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "job_eligibility_analysis",
                    "schema": get_openai_job_eligibility_schema(),
                    "strict": True,
                },
            },
            temperature=0.0,
        )
    except Exception as error:
        raise ResumeMatcherError(
            "OpenAI was unable to screen the job. "
            f"{type(error).__name__}: {error}"
        ) from error

    content = response.choices[0].message.content

    if not content:
        raise ResumeMatcherError("OpenAI returned an empty eligibility response.")

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise ResumeMatcherError("OpenAI returned invalid eligibility JSON.") from error

    analysis = parse_job_eligibility_payload(payload, "OpenAI")

    return JobEligibilityDecision(
        canonical_job_key=str(job.get("canonical_job_key") or job.get("record_key") or ""),
        record_key=str(job.get("record_key") or ""),
        resume_hash=resume_hash,
        model_name=selected_model,
        prompt_version=ELIGIBILITY_PROMPT_VERSION,
        analysis=analysis,
    )
