import re


EXCLUDED_TITLE_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"^hi\s+there,?$",
        "title is an email greeting",
    ),
    (
        r"^\[",
        "title is malformed link text",
    ),
    (
        r"(^|\s)›($|\s)",
        "title is malformed link text",
    ),
    (
        r"(^|[^a-z0-9])oracle([^a-z0-9]|$)",
        "title focuses on Oracle",
    ),
    (
        r"(^|[^a-z0-9])java([^a-z0-9]|$)",
        "title focuses on Java",
    ),
    (
        r"(^|[^a-z0-9])power\s*bi([^a-z0-9]|$)",
        "title focuses on Power BI",
    ),
    (
        r"(^|[^a-z0-9])unqork([^a-z0-9]|$)",
        "title focuses on Unqork",
    ),
    (
        r"(^|[^a-z0-9])aep([^a-z0-9]|$)",
        "title focuses on AEP",
    ),
    (
        r"(^|[^a-z0-9])rtcdp([^a-z0-9]|$)",
        "title focuses on RTCDP",
    ),
    (
        r"adobe\s+experience\s+platform",
        "title focuses on Adobe Experience Platform",
    ),
)

EXCLUDED_TITLE_SQL_REGEX = "|".join(
    f"({pattern})"
    for pattern, _reason in EXCLUDED_TITLE_PATTERNS
)


def excluded_job_title_reason(title: str | None) -> str | None:
    normalized = (title or "").casefold()

    for pattern, reason in EXCLUDED_TITLE_PATTERNS:
        if re.search(pattern, normalized):
            return reason

    return None


def is_excluded_job_title(title: str | None) -> bool:
    return excluded_job_title_reason(title) is not None
