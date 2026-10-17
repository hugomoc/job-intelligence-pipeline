"""Shared matcher constants and capability contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from src.description_quality import MINIMUM_FULL_DESCRIPTION_WORDS


MINIMUM_COMPLETE_DESCRIPTION_WORDS = MINIMUM_FULL_DESCRIPTION_WORDS


MATCHER_PROMPT_VERSION = "v3"


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
    "databricks": {
        "databricks",
        "delta lake",
        "lakehouse",
    },
    "spark": {
        "spark",
        "apache spark",
        "pyspark",
    },
    "looker": {"looker", "looker studio"},
    "tableau": {"tableau"},
    "power_bi": {
        "power bi",
        "power business intelligence",
        "microsoft power bi",
    },
    "domo": {"domo"},
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
    "databricks",
}


DATA_PROCESSING_GROUPS = {
    "databricks",
    "spark",
}


BI_PLATFORM_GROUPS = {
    "looker",
    "tableau",
    "power_bi",
    "domo",
}


INTERCHANGEABLE_TECH_GROUPS = (
    CLOUD_GROUPS,
    WAREHOUSE_GROUPS,
    DATA_PROCESSING_GROUPS,
    BI_PLATFORM_GROUPS,
)


SIGNAL_ALIAS_GROUPS = {
    "Databricks production experience": "databricks",
    "Domo as a primary BI platform": "domo",
}


@dataclass(frozen=True)
class CriticalCapabilitySignal:
    label: str
    job_patterns: tuple[str, ...]
    production_patterns: tuple[str, ...]
    project_patterns: tuple[str, ...] = ()
    skill_component: Literal[
        "skills",
        "experience",
        "seniority",
        "industry",
    ] = "skills"


@dataclass(frozen=True)
class CriticalRequirementGap:
    label: str
    skill_component: Literal[
        "skills",
        "experience",
        "seniority",
        "industry",
    ]
    severity: Literal[
        "mandatory",
        "specialization",
    ]


CRITICAL_CAPABILITY_SIGNALS: tuple[
    CriticalCapabilitySignal,
    ...
] = (
    CriticalCapabilitySignal(
        label="production RAG, agentic workflows, and AI tool integrations",
        job_patterns=(
            r"\brag\b",
            r"\bretrieval augmented generation\b",
            r"\bagentic\b",
            r"\bllms?\b",
            r"\blarge language model",
            r"\bai[\s-]+agents?\b",
            r"\bai[\s-]+tool[\s-]+integrations?\b",
            r"\bmodel[\s-]+context[\s-]+protocol\b",
        ),
        production_patterns=(
            r"\bproduction\b.{0,80}\b(rag|agentic|agents?|llm|mcp|ai systems?)\b",
            r"\b(rag|agentic|agents?|llm|mcp|ai systems?)\b.{0,80}\bproduction\b",
            r"\bdeployed\b.{0,80}\b(rag|agentic|agents?|llm|mcp|ai systems?)\b",
            r"\b(real users|user facing|customer facing)\b.{0,80}\b(rag|agentic|agents?|llm|mcp|ai)\b",
        ),
        project_patterns=(
            r"\b(rag|agentic|agents?|llm|mcp|ai)\b",
        ),
        skill_component="skills",
    ),
    CriticalCapabilitySignal(
        label="Databricks production experience",
        job_patterns=(r"\bdatabricks\b",),
        production_patterns=(r"\bdatabricks\b",),
        project_patterns=(r"\bdatabricks\b",),
        skill_component="skills",
    ),
    CriticalCapabilitySignal(
        label="Domo as a primary BI platform",
        job_patterns=(r"\bdomo\b",),
        production_patterns=(r"\bdomo\b",),
        project_patterns=(r"\bdomo\b",),
        skill_component="skills",
    ),
    CriticalCapabilitySignal(
        label="healthcare claims domain expertise",
        job_patterns=(
            r"\bhealthcare claims?\b",
            r"\bmedical claims?\b",
            r"\bclaims analytics?\b",
        ),
        production_patterns=(
            r"\bhealthcare claims?\b",
            r"\bmedical claims?\b",
            r"\bclaims analytics?\b",
        ),
        skill_component="industry",
    ),
    CriticalCapabilitySignal(
        label="production machine-learning model deployment",
        job_patterns=(
            r"\bmodel deployment\b",
            r"\bproduction ml\b",
            r"\bmlops\b",
            r"\bmachine learning\b.{0,80}\bproduction\b",
        ),
        production_patterns=(
            r"\bmodel deployment\b",
            r"\bproduction ml\b",
            r"\bmlops\b",
            r"\bdeployed\b.{0,80}\b(machine learning|ml models?)\b",
        ),
        project_patterns=(r"\b(machine learning|ml|model)\b",),
        skill_component="skills",
    ),
    CriticalCapabilitySignal(
        label="ledger or accounting data-domain expertise",
        job_patterns=(
            r"\bledger\b",
            r"\baccounting\b",
            r"\bfinancial close\b",
            r"\bgeneral ledger\b",
        ),
        production_patterns=(
            r"\bledger\b",
            r"\baccounting\b",
            r"\bfinancial close\b",
            r"\bgeneral ledger\b",
        ),
        skill_component="industry",
    ),
)


HARD_MANDATORY_CONTEXT_PATTERNS = (
    r"\bmust have\b",
    r"\brequired\b",
    r"\brequires?\b",
    r"\bmandatory\b",
)


MANDATORY_REQUIREMENT_CONTEXT_PATTERNS = (
    *HARD_MANDATORY_CONTEXT_PATTERNS,
    r"\brequirements?\b.{0,30}\b(include|including)\b",
    r"\bqualifications?\b.{0,30}\b(include|including)\b",
)


CRITICAL_REQUIREMENT_CONTEXT_PATTERNS = (
    *MANDATORY_REQUIREMENT_CONTEXT_PATTERNS,
    r"\brequirements?\b",
    r"\bqualifications?\b",
    r"\bproduction experience\b",
    r"\bexperienced building\b",
    r"\bdemonstrated experience\b",
    r"\bdeep expertise\b",
    r"\bextensive experience\b",
    r"\bcore to\b",
    r"\breal users?\b",
    r"\bdepend on\b",
    r"\bmanage(?:s|d)?\b",
    r"\boptimiz(?:e|es|ed|ing|ation)\b",
)


OPTIONAL_REQUIREMENT_CONTEXT_PATTERNS = (
    r"\bpreferred\b",
    r"\bnice to have\b",
    r"\bbonus\b",
    r"\bplus\b",
    r"\boptional\b",
    r"\bnot\s+(?:required|mandatory)\b",
)
