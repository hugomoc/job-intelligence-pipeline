"""Capability detection using the existing shared requirement policy."""

from __future__ import annotations

import re
from typing import Any

from src.ai.resume_profiler import ResumeProfile
from src.ai.requirement_policy import (
    explicit_production_ai_requirements,
    validate_hard_gap_clause,
)
from src.ai.matcher_contract import (
    CRITICAL_CAPABILITY_SIGNALS,
    CRITICAL_REQUIREMENT_CONTEXT_PATTERNS,
    HARD_MANDATORY_CONTEXT_PATTERNS,
    MANDATORY_REQUIREMENT_CONTEXT_PATTERNS,
    OPTIONAL_REQUIREMENT_CONTEXT_PATTERNS,
    SIGNAL_ALIAS_GROUPS,
    CriticalCapabilitySignal,
    CriticalRequirementGap,
)
from src.ai.matcher_evidence import (
    interchangeable_family_for_group,
    mentioned_alias_groups,
    normalize_match_text,
    production_resume_text,
    profile_has_alias_group,
    project_resume_text,
    regex_present,
    requirement_allows_open_equivalent,
    requirement_requires_all,
    requirement_uses_alternatives,
)


def requirement_has_satisfied_alternative(
    requirement: str,
    signal: CriticalCapabilitySignal,
    resume_profile: ResumeProfile,
) -> bool:
    signal_group = SIGNAL_ALIAS_GROUPS.get(
        signal.label
    )

    if not signal_group:
        return False

    if (
        not requirement_uses_alternatives(
            requirement
        )
        or requirement_requires_all(requirement)
    ):
        return False

    mentioned_groups = mentioned_alias_groups(
        requirement
    )
    interchangeable_groups = (
        interchangeable_family_for_group(signal_group)
    )

    if (
        signal_group not in mentioned_groups
        or not (
            mentioned_groups
            & interchangeable_groups
        )
    ):
        return False

    if requirement_allows_open_equivalent(requirement):
        acceptable_groups = interchangeable_groups
    else:
        acceptable_groups = mentioned_groups

    return any(
        profile_has_alias_group(
            resume_profile,
            group_name,
            include_certifications=False,
        )
        for group_name in acceptable_groups
    )


def capability_mentions_with_context(
    job_text: str,
    signal: CriticalCapabilitySignal,
    resume_profile: ResumeProfile | None = None,
) -> int:
    normalized = normalize_match_text(job_text)
    mention_count = 0

    for pattern in signal.job_patterns:
        for match in re.finditer(pattern, normalized):
            start = max(match.start() - 90, 0)
            end = min(match.end() + 90, len(normalized))
            window = normalized[start:end]
            has_critical_context = regex_present(
                window,
                CRITICAL_REQUIREMENT_CONTEXT_PATTERNS,
            )
            mandatory_window = re.sub(r"\bnot\s+(?:required|mandatory)\b", "", window)
            has_mandatory_context = regex_present(
                mandatory_window,
                MANDATORY_REQUIREMENT_CONTEXT_PATTERNS,
            )
            has_hard_mandatory_context = regex_present(
                mandatory_window,
                HARD_MANDATORY_CONTEXT_PATTERNS,
            )

            if (
                regex_present(
                    window,
                    OPTIONAL_REQUIREMENT_CONTEXT_PATTERNS,
                )
                and not has_hard_mandatory_context
            ):
                continue

            if (
                resume_profile is not None
                and requirement_has_satisfied_alternative(
                    window,
                    signal,
                    resume_profile,
                )
            ):
                continue

            if has_critical_context:
                mention_count += 2
            else:
                mention_count += 1

    return mention_count


def capability_mandatory_mentions(
    job_text: str,
    signal: CriticalCapabilitySignal,
    resume_profile: ResumeProfile,
) -> int:
    # Mandatory provenance comes from the shared complete-clause policy,
    # including essential/need-to-have wording outside headings.
    if signal.label.startswith("production RAG,"):
        # Do not treat nearby headings, support agents, or general AI tools as
        # mandatory production RAG experience.
        return len(explicit_production_ai_requirements(job_text))
    decision = validate_hard_gap_clause(signal.label, job_text, resume_profile)
    return sum(not row.get("satisfied", False) for row in decision["clauses"]) if decision["keep_hard_gap"] else 0


def capability_is_central_to_job(
    job: dict[str, Any],
    description: str,
    signal: CriticalCapabilitySignal,
    resume_profile: ResumeProfile,
) -> bool:
    if signal.label.startswith("production RAG,"):
        return bool(explicit_production_ai_requirements(description))

    title = str(job.get("title") or "")
    title_has_signal = regex_present(
        title,
        signal.job_patterns,
    )
    description_mentions = capability_mentions_with_context(
        description,
        signal,
        resume_profile,
    )

    if (
        title_has_signal
        and not requirement_has_satisfied_alternative(
            title,
            signal,
            resume_profile,
        )
    ):
        return True

    # A verified unsatisfied mandatory clause is central even if it appears
    # only once; otherwise an optional cloud-variant preference can mask it.
    if description_mentions and capability_mandatory_mentions(
        description, signal, resume_profile
    ):
        return True

    if description_mentions >= 3:
        return True

    if (
        description_mentions >= 2
        and regex_present(
            description,
            CRITICAL_REQUIREMENT_CONTEXT_PATTERNS,
        )
    ):
        return True

    return False


def detect_critical_requirement_gaps(
    resume_profile: ResumeProfile,
    job: dict[str, Any],
    description: str,
) -> tuple[list[CriticalRequirementGap], list[str]]:
    """Find central job requirements absent from production resume evidence."""
    production_text = production_resume_text(
        resume_profile
    )
    project_text = project_resume_text(
        resume_profile
    )
    gaps: list[CriticalRequirementGap] = []
    risks: list[str] = []
    seen_gaps: set[tuple[str, str]] = set()

    for signal in CRITICAL_CAPABILITY_SIGNALS:
        if not capability_is_central_to_job(
            job=job,
            description=description,
            signal=signal,
            resume_profile=resume_profile,
        ):
            continue

        if regex_present(
            production_text,
            signal.production_patterns,
        ):
            continue

        severity: Literal[
            "mandatory",
            "specialization",
        ] = (
            "mandatory"
            if capability_mandatory_mentions(
                description,
                signal,
                resume_profile,
            )
            else "specialization"
        )
        gap_key = (
            signal.label,
            severity,
        )

        if gap_key not in seen_gaps:
            gaps.append(
                CriticalRequirementGap(
                    label=signal.label,
                    skill_component=signal.skill_component,
                    severity=severity,
                )
            )
            seen_gaps.add(gap_key)

        if signal.project_patterns and regex_present(
            project_text,
            signal.project_patterns,
        ):
            risks.append(
                "Project-only or AI-assisted evidence exists for "
                f"{signal.label}, but the job asks for production ownership."
            )

    return (
        gaps,
        list(dict.fromkeys(risks)),
    )
