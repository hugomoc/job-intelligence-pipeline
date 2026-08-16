"""Parser for beBee numbered job-digest emails."""

import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup


BEBEE_JOB_HOST_PATTERN = re.compile(
    r"(?:^|\.)bebee\.com$",
    re.IGNORECASE,
)

JOB_LINE_PATTERN = re.compile(
    r"^\d+\.\s+(?P<title>.+?)\s+[—-]\s+(?P<company>.+?)\s+·\s+(?P<details>.+)$"
)

SALARY_PATTERN = re.compile(
    r"\$[\d,]+(?:\s*[-–]\s*\$[\d,]+)?\s*/\s*(?:year|yr|hour|hr|month|week|day)",
    re.IGNORECASE,
)


def clean_text(value: str | None) -> str:
    if not value:
        return ""

    return " ".join(value.replace("\xa0", " ").split()).strip()


def normalize_lines(text: str, html: str | None) -> list[str]:
    if text:
        raw_text = text
    elif html:
        soup = BeautifulSoup(html, "html.parser")
        raw_text = soup.get_text(separator="\n", strip=True)
    else:
        raw_text = ""

    return [
        clean_text(line)
        for line in raw_text.splitlines()
        if clean_text(line)
    ]


def is_bebee_job_url(value: str) -> bool:
    parsed = urlparse(clean_text(value).strip("<>"))

    return (
        parsed.scheme in {"http", "https"}
        and bool(BEBEE_JOB_HOST_PATTERN.search(parsed.netloc))
        and parsed.path.startswith("/us/jobs/")
        and "?q=" not in value
    )


def extract_salary(value: str) -> str | None:
    match = SALARY_PATTERN.search(value)
    return clean_text(match.group(0)) if match else None


def extract_location(details: str) -> str | None:
    parts = [clean_text(part) for part in details.split("·")]
    non_salary_parts = [
        part for part in parts if part and not SALARY_PATTERN.search(part)
    ]

    return non_salary_parts[0] if non_salary_parts else None


def extract_source_job_id(url: str) -> str | None:
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    return slug or None


def find_next_job_url(lines: list[str], start_index: int) -> str | None:
    for line in lines[start_index + 1 : start_index + 5]:
        candidate = clean_text(line).strip("<>")

        if is_bebee_job_url(candidate):
            return candidate

    return None


def parse_bebee_email(
    text: str = "",
    html: str | None = None,
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    lines = normalize_lines(text=text, html=html)
    jobs: list[dict[str, Any]] = []

    for index, line in enumerate(lines):
        match = JOB_LINE_PATTERN.match(line)

        if not match:
            continue

        apply_url = find_next_job_url(lines, index)

        if not apply_url:
            continue

        details = clean_text(match.group("details"))

        jobs.append(
            {
                "source": "bebee",
                "source_job_id": extract_source_job_id(apply_url),
                "title": clean_text(match.group("title")),
                "company_name": clean_text(match.group("company")),
                "location": extract_location(details),
                "salary_text": extract_salary(details),
                "description": None,
                "apply_url": apply_url,
                "posted_age_text": None,
            }
        )

    return jobs
