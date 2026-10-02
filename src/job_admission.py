"""Resume-aware admission gate for job-alert UI and scoring.

Title classification answers a narrow question: is the job title worth
evaluating? This module answers the stronger product question: should the job
enter the user's review queue for this resume?
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from src.job_title_filter import FILTERED_OUT, classify_job_title


AdmissionDecision = Literal["include", "exclude"]


@dataclass(frozen=True)
class SpecializationSignal:
    label: str
    patterns: tuple[str, ...]
    resume_patterns: tuple[str, ...]
    critical: bool = True


@dataclass(frozen=True)
class JobAdmissionEvaluation:
    role_family_match: Literal["strong", "possible", "weak", "none"]
    specialization_match: Literal["strong", "partial", "weak", "none"]
    required_skill_match: Literal["strong", "partial", "weak", "unknown"]
    responsibility_match: Literal["strong", "partial", "weak", "unknown"]
    seniority_match: Literal["strong", "partial", "weak", "unknown"]
    critical_skill_gaps: list[str] = field(default_factory=list)
    matched_specializations: list[str] = field(default_factory=list)
    matched_resume_signals: list[str] = field(default_factory=list)
    admission_decision: AdmissionDecision = "exclude"
    admission_reason: str = ""


CORE_SKILL_SIGNALS: tuple[SpecializationSignal, ...] = (
    SpecializationSignal("Snowflake", (r"\bsnowflake\b",), (r"\bsnowflake\b",), False),
    SpecializationSignal("Python", (r"\bpython\b",), (r"\bpython\b",), False),
    SpecializationSignal("SQL", (r"\bsql\b",), (r"\bsql\b",), False),
    SpecializationSignal("AWS", (r"\baws\b|\bamazon web services\b",), (r"\baws\b|\bamazon web services\b",), False),
    SpecializationSignal("dbt", (r"\bdbt\b",), (r"\bdbt\b",), False),
    SpecializationSignal("Looker", (r"\blooker\b",), (r"\blooker\b",), False),
)

CRITICAL_SPECIALIZATION_SIGNALS: tuple[SpecializationSignal, ...] = (
    SpecializationSignal("Databricks", (r"\bdatabricks\b",), (r"\bdatabricks\b",)),
    SpecializationSignal("SAP", (r"\bsap\b|\babap\b",), (r"\bsap\b|\babap\b",)),
    SpecializationSignal("Power BI", (r"\bpower\s+(bi|business intelligence)\b",), (r"\bpower\s+(bi|business intelligence)\b",)),
    SpecializationSignal("Tableau", (r"\btableau\b",), (r"\btableau\b",)),
    SpecializationSignal("PowerApps", (r"\bpower\s*apps?\b|\bpowerapps\b",), (r"\bpower\s*apps?\b|\bpowerapps\b",)),
    SpecializationSignal("Palantir Foundry", (r"\bpalantir\b|\bfoundry\b",), (r"\bpalantir\b|\bfoundry\b",)),
    SpecializationSignal("Adobe Experience Platform", (r"\badobe experience platform\b|\baep\b|\brtcdp\b",), (r"\badobe experience platform\b|\baep\b|\brtcdp\b",)),
    SpecializationSignal("Forward Deployed", (r"\bforward deployed\b",), (r"\bforward deployed\b|\bcustomer implementation\b|\bclient implementation\b|\bsolutions consulting\b|\bprofessional services\b",)),
    SpecializationSignal("Machine Learning", (r"\b(machine learning|ml|ai)\b",), (r"\b(machine learning|ml|ai|artificial intelligence)\b",)),
)

RESPONSIBILITY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("data pipelines", r"\b(data|etl|elt)\s+pipeline"),
    ("analytics modeling", r"\b(analytics|semantic|dimensional)\s+model"),
    ("data warehouse", r"\bdata warehouse|\bwarehouse\b"),
    ("reporting automation", r"\breporting|\bdashboard|\bautomation"),
    ("orchestration", r"\borchestrat|\bairflow|\bdag\b"),
)

PRODUCTION_RESUME_FIELDS = (
    "target_roles",
    "current_or_recent_titles",
    "seniority",
    "years_of_relevant_experience",
    "production_skills",
    "data_engineering_capabilities",
    "bi_analytics_skills",
    "cloud_platforms",
    "tools_platforms",
    "programming_languages",
    "databases_warehouses",
    "certifications",
)

PROJECT_RESUME_FIELDS = (
    "project_skills",
    "projects",
    "portfolio_projects",
)


def normalize_text(value: Any) -> str:
    if value is None:
        return ""

    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = text.casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9+#.]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def pattern_present(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def signal_present_in_job(signal: SpecializationSignal, title_text: str) -> bool:
    return pattern_present(title_text, signal.patterns)


def signal_present_in_resume(signal: SpecializationSignal, resume_text: str) -> bool:
    return pattern_present(resume_text, signal.resume_patterns)


def resume_profile_to_text(profile: Any) -> str:
    if profile is None:
        return ""

    if hasattr(profile, "model_dump"):
        return normalize_text(profile.model_dump())

    return normalize_text(profile)


def resume_profile_fields_to_text(
    profile: Any,
    field_names: tuple[str, ...],
) -> str:
    if profile is None:
        return ""

    if hasattr(profile, "model_dump"):
        profile = profile.model_dump()

    if not isinstance(profile, dict):
        return normalize_text(profile)

    selected_values = [
        profile.get(field_name)
        for field_name in field_names
        if profile.get(field_name) is not None
    ]

    return normalize_text(selected_values)


def role_family_strength(title: str | None) -> Literal["strong", "possible", "weak", "none"]:
    classification = classify_job_title(title)

    if classification.category == "STRONG_MATCH":
        return "strong"

    if classification.category == "POSSIBLE_MATCH":
        return "possible"

    if classification.category == FILTERED_OUT:
        return "weak"

    return "none"


def strength_from_count(count: int, strong_at: int = 2) -> Literal["strong", "partial", "weak", "unknown"]:
    if count >= strong_at:
        return "strong"

    if count == 1:
        return "partial"

    return "weak"


def evaluate_job_admission(
    job: dict[str, Any],
    resume_profile: Any,
) -> JobAdmissionEvaluation:
    title = str(job.get("title") or "")
    description = str(job.get("description") or "")
    title_text = normalize_text(title)
    description_text = normalize_text(description)
    job_text = normalize_text(
        " ".join(
            value
            for value in [
                title,
                job.get("company_name") or "",
                job.get("location") or "",
                description,
            ]
            if value
        )
    )
    resume_text = resume_profile_to_text(resume_profile)
    production_resume_text = resume_profile_fields_to_text(
        resume_profile,
        PRODUCTION_RESUME_FIELDS,
    )
    project_resume_text = resume_profile_fields_to_text(
        resume_profile,
        PROJECT_RESUME_FIELDS,
    )
    classification = classify_job_title(title)
    family_match = role_family_strength(title)

    title_specializations = [
        signal
        for signal in CRITICAL_SPECIALIZATION_SIGNALS
        if signal_present_in_job(signal, title_text)
    ]
    matched_specializations = [
        signal.label
        for signal in title_specializations
        if signal_present_in_resume(signal, production_resume_text)
    ]
    critical_gaps = [
        signal.label
        for signal in title_specializations
        if signal.critical
        and not signal_present_in_resume(signal, production_resume_text)
    ]

    title_core_matches = [
        signal.label
        for signal in CORE_SKILL_SIGNALS
        if signal_present_in_job(signal, title_text)
        and signal_present_in_resume(signal, production_resume_text)
    ]
    job_core_matches = [
        signal.label
        for signal in CORE_SKILL_SIGNALS
        if signal_present_in_job(signal, job_text)
        and signal_present_in_resume(signal, production_resume_text)
    ]
    project_only_matches = [
        f"project-only {signal.label}"
        for signal in CORE_SKILL_SIGNALS
        if signal_present_in_job(signal, job_text)
        and not signal_present_in_resume(signal, production_resume_text)
        and signal_present_in_resume(signal, project_resume_text)
    ]
    responsibility_matches = [
        label
        for label, pattern in RESPONSIBILITY_PATTERNS
        if re.search(pattern, job_text)
        and re.search(pattern, resume_text)
    ]

    specialization_match: Literal["strong", "partial", "weak", "none"]
    if title_specializations and critical_gaps:
        specialization_match = "weak"
    elif matched_specializations:
        specialization_match = "strong"
    elif title_specializations:
        specialization_match = "weak"
    else:
        specialization_match = "none"

    required_skill_match = strength_from_count(
        len(set(job_core_matches)),
        strong_at=2,
    )
    responsibility_match = (
        strength_from_count(len(set(responsibility_matches)), strong_at=2)
        if description_text
        else "unknown"
    )

    seniority_match: Literal["strong", "partial", "weak", "unknown"] = "unknown"
    if re.search(r"\b(manager|director|head of|vp|vice president)\b", title_text):
        if re.search(r"\b(manager|managed|people leadership|direct reports)\b", resume_text):
            seniority_match = "partial"
        else:
            seniority_match = "weak"
            critical_gaps.append("management experience")
    elif re.search(r"\b(senior|sr|lead|staff|principal|iv|iii)\b", title.casefold()):
        seniority_match = "strong" if re.search(r"\b(senior|lead|principal|architect|12|10|15)\b", resume_text) else "partial"

    if classification.category == FILTERED_OUT:
        return JobAdmissionEvaluation(
            role_family_match=family_match,
            specialization_match=specialization_match,
            required_skill_match=required_skill_match,
            responsibility_match=responsibility_match,
            seniority_match=seniority_match,
            critical_skill_gaps=list(dict.fromkeys(critical_gaps)),
            matched_specializations=matched_specializations,
            matched_resume_signals=sorted(set(title_core_matches + job_core_matches + project_only_matches + responsibility_matches)),
            admission_decision="exclude",
            admission_reason=classification.reason,
        )

    if critical_gaps:
        return JobAdmissionEvaluation(
            role_family_match=family_match,
            specialization_match=specialization_match,
            required_skill_match=required_skill_match,
            responsibility_match=responsibility_match,
            seniority_match=seniority_match,
            critical_skill_gaps=list(dict.fromkeys(critical_gaps)),
            matched_specializations=matched_specializations,
            matched_resume_signals=sorted(set(title_core_matches + job_core_matches + project_only_matches + responsibility_matches)),
            admission_decision="exclude",
            admission_reason=(
                "The base role family may be relevant, but the title contains "
                "central specialization requirements not supported by the resume."
            ),
        )

    if not description_text and family_match == "possible":
        title_evidence_count = len(set(title_core_matches + matched_specializations))

        if title_evidence_count < 2:
            return JobAdmissionEvaluation(
                role_family_match=family_match,
                specialization_match=specialization_match,
                required_skill_match=required_skill_match,
                responsibility_match=responsibility_match,
                seniority_match=seniority_match,
                critical_skill_gaps=[],
                matched_specializations=matched_specializations,
                matched_resume_signals=sorted(
                    set(title_core_matches + job_core_matches + project_only_matches + responsibility_matches)
                ),
                admission_decision="exclude",
                admission_reason=(
                    "A possible title match without a description needs at "
                    "least two concrete resume-supported title signals."
                ),
            )

    evidence_count = 0

    if family_match in {"strong", "possible"}:
        evidence_count += 1

    if matched_specializations:
        evidence_count += 1

    if title_core_matches or len(set(job_core_matches)) >= 2:
        evidence_count += 1

    if responsibility_matches:
        evidence_count += 1

    if seniority_match in {"strong", "partial"}:
        evidence_count += 1

    has_concrete_fit_signal = bool(
        matched_specializations
        or title_core_matches
        or len(set(job_core_matches)) >= 2
        or responsibility_matches
    )

    if evidence_count >= 2 and has_concrete_fit_signal:
        return JobAdmissionEvaluation(
            role_family_match=family_match,
            specialization_match=specialization_match,
            required_skill_match=required_skill_match,
            responsibility_match=responsibility_match,
            seniority_match=seniority_match,
            critical_skill_gaps=[],
            matched_specializations=matched_specializations,
            matched_resume_signals=sorted(set(title_core_matches + job_core_matches + project_only_matches + responsibility_matches)),
            admission_decision="include",
            admission_reason=(
                "The job has multiple fit signals beyond title family."
            ),
        )

    return JobAdmissionEvaluation(
        role_family_match=family_match,
        specialization_match=specialization_match,
        required_skill_match=required_skill_match,
        responsibility_match=responsibility_match,
        seniority_match=seniority_match,
        critical_skill_gaps=[],
        matched_specializations=matched_specializations,
        matched_resume_signals=sorted(set(title_core_matches + job_core_matches + project_only_matches + responsibility_matches)),
        admission_decision="exclude",
        admission_reason=(
            "The title family alone is not enough evidence to show this job."
        ),
    )
