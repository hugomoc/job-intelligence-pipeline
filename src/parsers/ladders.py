"""Parser for Ladders job-alert emails."""

import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup


SALARY_PATTERN = re.compile(
    r"""
    (?:
        \$[\d,.]+K?
        (?:\s*[-–]\s*\$[\d,.]+K?)?
        (?:\s*/\s*(?:hour|hr|year|yr|month|week|day))?
        |
        \$[\d,.]+K\+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

LOCATION_PATTERN = re.compile(
    r"""
    ^
    (?:
        remote
        |
        united\ states
        |
        [A-Za-z0-9 .'/&()\-]+,\s*[A-Z]{2}
    )
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)

IGNORED_LINES = {
    "apply",
    "apply now",
    "contact us",
    "get started",
    "privacy",
    "privacy policy",
    "terms",
    "terms & conditions",
    "unsubscribe",
    "upload resume",
    "view options",
}

IGNORED_LINK_TEXT = IGNORED_LINES | {
    "get 20% off",
    "ladders premium",
    "see your resume results now",
    "try ladders premium",
}


def clean_text(value: str | None) -> str:
    if not value:
        return ""

    return " ".join(value.replace("\xa0", " ").split()).strip()


def normalize_lines(text: str, html: str | None) -> list[str]:
    if html:
        soup = BeautifulSoup(html, "html.parser")
        raw_text = soup.get_text(separator="\n", strip=True)
    else:
        raw_text = text

    return [
        clean_text(line.lstrip("* "))
        for line in raw_text.splitlines()
        if clean_text(line.lstrip("* "))
    ]


def is_ladders_url(value: str) -> bool:
    parsed = urlparse(value)
    host = parsed.netloc.casefold()

    return (
        parsed.scheme in {"http", "https"}
        and (
            host.endswith("ladders.com")
            or host.endswith("theladders.com")
            or host.endswith("ladders.co")
        )
    )


def is_location(value: str) -> bool:
    return bool(LOCATION_PATTERN.fullmatch(clean_text(value)))


def is_salary(value: str) -> bool:
    return bool(SALARY_PATTERN.search(clean_text(value)))


def is_ignored_line(value: str) -> bool:
    normalized = clean_text(value).casefold()

    return (
        normalized in IGNORED_LINES
        or "resume review" in normalized
        or "premium" in normalized
        or "unsubscribe" in normalized
    )


def extract_job_links(
    html: str | None,
    links: list[str] | None,
) -> list[str]:
    job_links: list[str] = []
    seen: set[str] = set()

    if html:
        soup = BeautifulSoup(html, "html.parser")

        for anchor in soup.find_all("a", href=True):
            href = clean_text(anchor["href"])
            label = clean_text(
                anchor.get_text(" ", strip=True)
            ).casefold()

            if not is_ladders_url(href):
                continue

            if label in IGNORED_LINK_TEXT:
                continue

            if href in seen:
                continue

            seen.add(href)
            job_links.append(href)

    for link in links or []:
        if not is_ladders_url(link):
            continue

        if link in seen:
            continue

        seen.add(link)
        job_links.append(link)

    return job_links


def extract_salary(
    listing_lines: list[str],
    location_index: int,
) -> str | None:
    for candidate in listing_lines[
        location_index + 1 : location_index + 4
    ]:
        if is_salary(candidate):
            return clean_text(candidate)

    return None


def parse_listing_text(
    lines: list[str],
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []

    for location_index, line in enumerate(lines):
        if not is_location(line):
            continue

        title_index = location_index - 1
        company_index = location_index - 2

        if title_index < 0 or company_index < 0:
            continue

        title = clean_text(lines[title_index])
        company_name = clean_text(lines[company_index])

        if (
            not title
            or not company_name
            or is_ignored_line(title)
            or is_ignored_line(company_name)
            or is_location(company_name)
            or is_salary(title)
            or is_salary(company_name)
        ):
            continue

        jobs.append(
            {
                "source": "ladders",
                "source_job_id": None,
                "title": title,
                "company_name": company_name,
                "location": clean_text(line),
                "salary_text": extract_salary(
                    lines,
                    location_index,
                ),
                "description": None,
                "apply_url": None,
                "posted_age_text": None,
            }
        )

    return jobs


def parse_ladders_email(
    text: str = "",
    html: str | None = None,
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    lines = normalize_lines(text=text, html=html)
    jobs = parse_listing_text(lines)

    if not jobs:
        return []

    job_links = extract_job_links(
        html=html,
        links=links,
    )

    if len(job_links) < len(jobs):
        return []

    for job, job_url in zip(jobs, job_links):
        job["apply_url"] = job_url

    return jobs
