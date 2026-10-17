"""Text and resume evidence helpers used by matcher policy."""

from __future__ import annotations

import json
import re

from src.ai.resume_profiler import ResumeProfile
from src.ai.matcher_contract import (
    CLOUD_GROUPS,
    INTERCHANGEABLE_TECH_GROUPS,
    SIGNIFICANT_TERM_PATTERN,
    TECH_ALIASES,
    WAREHOUSE_GROUPS,
)


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

    return bool(re.search(
        r"(?<![a-z0-9+#.])" + re.escape(normalized_phrase) + r"(?![a-z0-9+#]|\.[a-z0-9])",
        normalized_text,
    ))


def profile_to_text(
    resume_profile: ResumeProfile,
    include_certifications: bool = True,
) -> str:
    profile_data = resume_profile.model_dump()
    profile_data.pop("personal_eligibility", None)

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


def profile_fields_to_text(
    resume_profile: ResumeProfile,
    field_names: tuple[str, ...],
) -> str:
    profile_data = resume_profile.model_dump()

    return json.dumps(
        {
            field_name: profile_data.get(field_name)
            for field_name in field_names
        },
        ensure_ascii=False,
    )


def regex_present(
    text: str,
    patterns: tuple[str, ...],
) -> bool:
    normalized = normalize_match_text(text)

    return any(
        re.search(pattern, normalized)
        for pattern in patterns
    )


def regex_count(
    text: str,
    patterns: tuple[str, ...],
) -> int:
    normalized = normalize_match_text(text)
    count = 0

    for pattern in patterns:
        count += len(
            re.findall(
                pattern,
                normalized,
            )
        )

    return count


def interchangeable_family_for_group(
    group_name: str,
) -> set[str]:
    for family in INTERCHANGEABLE_TECH_GROUPS:
        if group_name in family:
            return family

    return {group_name}


def production_resume_text(
    resume_profile: ResumeProfile,
) -> str:
    return profile_fields_to_text(
        resume_profile,
        (
            "professional_summary",
            "target_roles",
            "current_or_recent_titles",
            "production_skills",
            "data_engineering_capabilities",
            "bi_analytics_skills",
            "programming_languages",
            "databases_warehouses",
            "cloud_platforms",
            "tools_platforms",
            "ai_ml_experience",
            "industries",
            "quantified_achievements",
            "leadership_evidence",
            "strengths_for_job_matching",
        ),
    )


def project_resume_text(
    resume_profile: ResumeProfile,
) -> str:
    return profile_fields_to_text(
        resume_profile,
        (
            "project_skills",
        ),
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


def requirement_allows_open_equivalent(
    requirement: str,
) -> bool:
    normalized = normalize_match_text(requirement)

    return any(
        marker in f" {normalized} "
        for marker in (
            " or similar ",
            " or equivalent ",
            " comparable ",
            " such as ",
            " e g ",
            " eg ",
            " for example ",
            " equivalent platform ",
            " equivalent tool ",
            " equivalent technology ",
            " similar platform ",
            " similar tool ",
            " similar technology ",
        )
    )


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


def acceptable_alternative_groups(
    groups: set[str],
    requirement: str,
) -> set[str]:
    if not (
        requirement_uses_alternatives(requirement)
        and not requirement_requires_all(requirement)
    ):
        return groups

    if not requirement_allows_open_equivalent(
        requirement
    ):
        return groups

    acceptable: set[str] = set(groups)

    for group in groups:
        acceptable.update(
            interchangeable_family_for_group(group)
        )

    return acceptable


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
    # Total relevant years prove a general data/analytics minimum, not tenure
    # with a specific technology or specialist industry.
    minimum_years = re.search(r"\b(\d+)\s*\+?\s*years?\s+(?:of\s+)?(?:relevant\s+)?(?:professional\s+)?(?:experience\s+(?:in|with)\s+)?(?:data engineering|analytics engineering|business intelligence)\b", normalized_requirement)
    if minimum_years:
        remaining = normalized_requirement[minimum_years.end():].strip()
        if remaining in {"", "experience", "required", "is required", "experience required"}:
            return int(minimum_years.group(1)) <= resume_profile.years_of_relevant_experience
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
                acceptable_groups = acceptable_alternative_groups(
                    groups,
                    requirement,
                )
                return any(
                    profile_has_alias_group(
                        resume_profile,
                        group,
                        include_certifications=False,
                    )
                    for group in acceptable_groups
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
                acceptable_groups = acceptable_alternative_groups(
                    groups,
                    requirement,
                )
                return any(
                    profile_has_alias_group(
                        resume_profile,
                        group,
                    )
                    for group in acceptable_groups
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
            acceptable_groups = acceptable_alternative_groups(
                groups,
                requirement,
            )
            return any(
                profile_has_alias_group(
                    resume_profile,
                    group,
                )
                for group in acceptable_groups
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
