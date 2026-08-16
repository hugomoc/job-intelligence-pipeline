"""Parser for LinkedIn job-alert emails.

LinkedIn emails can expose both text links and HTML cards. This parser extracts
stable LinkedIn job IDs when possible so duplicate alerts collapse cleanly.
"""

import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup


JOB_ID_PATTERN = re.compile(
    r"/(?:comm/)?jobs/view/(\d+)/"
)

SALARY_PATTERN = re.compile(
    r"""
    \$[\d,.]+K?
    (?:\s*-\s*\$[\d,.]+K?)?
    (?:\s*/\s*(?:hour|hr|year|yr|month|week|day))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

IGNORED_CARD_LINES = {
    "actively recruiting",
    "this company is actively hiring",
}


def clean_text(value: str | None) -> str:
    if not value:
        return ""

    return " ".join(value.replace("\xa0", " ").split()).strip()


def extract_source_job_id(url: str) -> str | None:
    match = JOB_ID_PATTERN.search(urlparse(url).path)

    if not match:
        return None

    return match.group(1)


def normalize_linkedin_job_url(source_job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{source_job_id}/"


def extract_anchor_lines(anchor: Any) -> list[str]:
    lines: list[str] = []

    for raw_line in anchor.get_text(
        separator="\n",
        strip=True,
    ).splitlines():
        line = clean_text(raw_line)

        if line:
            lines.append(line)

    return lines


def choose_complete_cards(html: str) -> dict[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    cards: dict[str, list[str]] = {}

    for anchor in soup.find_all("a", href=True):
        source_job_id = extract_source_job_id(anchor["href"])

        if not source_job_id:
            continue

        lines = extract_anchor_lines(anchor)

        if not lines:
            continue

        current = cards.get(source_job_id, [])

        if len(lines) > len(current):
            cards[source_job_id] = lines

    return cards


def split_company_location(value: str) -> tuple[str, str] | None:
    if " · " not in value:
        return None

    company, location = value.split(" · ", maxsplit=1)
    company = clean_text(company)
    location = clean_text(location)

    if not company or not location:
        return None

    return company, location


def parse_card_lines(
    source_job_id: str,
    lines: list[str],
) -> dict[str, Any] | None:
    if len(lines) < 2:
        return None

    title = lines[0]
    detail_lines = [
        line
        for line in lines[1:]
        if line.casefold() not in IGNORED_CARD_LINES
    ]

    company = None
    location = None
    salary = None

    for index, line in enumerate(detail_lines):
        split_result = split_company_location(line)

        if split_result:
            company, location = split_result
            detail_lines = detail_lines[index + 1 :]
            break

    if company is None and len(detail_lines) >= 2:
        company = detail_lines[0]
        location = detail_lines[1]
        detail_lines = detail_lines[2:]

    for line in detail_lines:
        salary_match = SALARY_PATTERN.search(line)

        if salary_match:
            salary = clean_text(salary_match.group(0))
            break

    if not title or not company or not location:
        return None

    return {
        "source": "linkedin",
        "source_job_id": source_job_id,
        "title": title,
        "company_name": company,
        "location": location,
        "salary_text": salary,
        "description": None,
        "apply_url": normalize_linkedin_job_url(source_job_id),
        "posted_age_text": None,
    }


def parse_text_fallback(text: str) -> list[dict[str, Any]]:
    lines = [
        clean_text(line)
        for line in text.splitlines()
        if clean_text(line)
    ]

    jobs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for index, line in enumerate(lines):
        source_job_id = extract_source_job_id(line)

        if not source_job_id or source_job_id in seen_ids:
            continue

        candidate_lines = lines[max(0, index - 4) : index]
        job = parse_card_lines(
            source_job_id=source_job_id,
            lines=candidate_lines,
        )

        if job:
            jobs.append(job)
            seen_ids.add(source_job_id)

    return jobs


def parse_linkedin_email(
    text: str = "",
    html: str | None = None,
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    if html:
        for source_job_id, lines in choose_complete_cards(html).items():
            if source_job_id in seen_ids:
                continue

            job = parse_card_lines(
                source_job_id=source_job_id,
                lines=lines,
            )

            if job:
                jobs.append(job)
                seen_ids.add(source_job_id)

    if not jobs and text:
        jobs = parse_text_fallback(text)
        seen_ids = {
            str(job["source_job_id"])
            for job in jobs
            if job.get("source_job_id")
        }

    for link in links or []:
        source_job_id = extract_source_job_id(link)

        if not source_job_id or source_job_id in seen_ids:
            continue

        seen_ids.add(source_job_id)

    return jobs
