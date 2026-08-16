"""Parser for ZipRecruiter job-alert emails."""

import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup


SALARY_PATTERN = re.compile(
    r"""
    \$[\d,.]+
    (?:\s*-\s*\$[\d,.]+)?
    (?:\s*/\s*(?:hour|hr|year|yr|month|week|day))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

LOCATION_PATTERN = re.compile(
    r"^[A-Za-z0-9 .'/&()\-]+,\s*[A-Z]{2}$"
)

IGNORED_TITLE_TEXT = {
    "1-click apply",
    "view details",
    "view more jobs",
}


def clean_text(value: str | None) -> str:
    if not value:
        return ""

    return " ".join(value.replace("\xa0", " ").split()).strip()


def is_ziprecruiter_job_url(value: str) -> bool:
    parsed = urlparse(value)

    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.endswith("ziprecruiter.com")
        and parsed.path.startswith("/km/")
    )


def extract_lines(text: str, html: str | None) -> list[str]:
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


def extract_action_urls(
    html: str | None,
    links: list[str] | None,
) -> tuple[str | None, str | None, str | None]:
    title_url = None
    details_url = None
    apply_url = None

    if html:
        soup = BeautifulSoup(html, "html.parser")

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()

            if not is_ziprecruiter_job_url(href):
                continue

            label = clean_text(anchor.get_text(" ", strip=True)).casefold()

            if label == "1-click apply":
                apply_url = href
            elif label == "view details":
                details_url = href
            elif label and label not in IGNORED_TITLE_TEXT:
                title_url = href

    for link in links or []:
        if not is_ziprecruiter_job_url(link):
            continue

        if not title_url:
            title_url = link

    return title_url, details_url, apply_url


def extract_title(lines: list[str]) -> str | None:
    for line in lines:
        normalized = line.casefold()

        if normalized in IGNORED_TITLE_TEXT:
            continue

        if " at " in line and " for you" in line:
            match = re.search(
                r"found an? (?P<title>.+?) job at ",
                line,
                re.IGNORECASE,
            )

            if match:
                return clean_text(match.group("title"))

        if normalized.startswith(("hi ", "i recently found")):
            continue

        if is_ziprecruiter_job_url(line):
            continue

        next_index = lines.index(line) + 1

        if next_index < len(lines) and is_ziprecruiter_job_url(lines[next_index]):
            return line

    return None


def extract_company(lines: list[str], title: str) -> str | None:
    intro_pattern = re.compile(
        rf"found an? {re.escape(title)} job at (?P<company>.+?) for you",
        re.IGNORECASE,
    )

    for line in lines:
        match = intro_pattern.search(line)

        if match:
            return clean_text(match.group("company"))

    for index, line in enumerate(lines):
        if line == title:
            for candidate in lines[index + 1 : index + 5]:
                if is_ziprecruiter_job_url(candidate):
                    continue

                if LOCATION_PATTERN.fullmatch(candidate):
                    continue

                if SALARY_PATTERN.search(candidate):
                    continue

                if candidate.casefold() not in IGNORED_TITLE_TEXT:
                    return candidate

    return None


def extract_location(lines: list[str]) -> str | None:
    for line in lines:
        if LOCATION_PATTERN.fullmatch(line):
            return line

    return None


def extract_salary(lines: list[str]) -> str | None:
    for line in lines:
        match = SALARY_PATTERN.fullmatch(line)

        if match:
            return clean_text(match.group(0))

    return None


def extract_posted_age(lines: list[str]) -> str | None:
    for line in lines:
        if line == "New":
            return "New"

    return None


def parse_ziprecruiter_email(
    text: str = "",
    html: str | None = None,
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    lines = extract_lines(text=text, html=html)
    title_url, details_url, apply_url = extract_action_urls(
        html=html,
        links=links,
    )
    selected_url = apply_url or title_url or details_url

    if not selected_url:
        return []

    title = extract_title(lines)

    if not title:
        return []

    company = extract_company(lines, title)
    location = extract_location(lines)

    if not company or not location:
        return []

    return [
        {
            "source": "ziprecruiter",
            "source_job_id": None,
            "title": title,
            "company_name": company,
            "location": location,
            "salary_text": extract_salary(lines),
            "description": None,
            "apply_url": selected_url,
            "posted_age_text": extract_posted_age(lines),
        }
    ]
