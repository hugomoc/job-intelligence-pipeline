"""Parser for Wellfound digest emails with repeated title/company blocks."""

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup


WELLFOUND_HOST_PATTERN = re.compile(
    r"(?:^|\.)wellfound\.com$",
    re.IGNORECASE,
)

SALARY_PATTERN = re.compile(
    r"\$[\d,]+\s*[–-]\s*\$?[\d,]+k?",
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


def is_wellfound_job_url(value: str) -> bool:
    parsed = urlparse(clean_text(value).strip("<>"))

    return (
        parsed.scheme in {"http", "https"}
        and bool(WELLFOUND_HOST_PATTERN.search(parsed.netloc))
        and parsed.path.startswith("/jobs")
        and "job_listing_slug" in parse_qs(parsed.query)
    )


def extract_job_links(
    text: str,
    html: str | None,
    links: list[str] | None,
) -> list[str]:
    job_links: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        href = clean_text(value).strip("<>")

        if not is_wellfound_job_url(href) or href in seen:
            return

        seen.add(href)
        job_links.append(href)

    if html:
        soup = BeautifulSoup(html, "html.parser")

        for anchor in soup.find_all("a", href=True):
            add(anchor["href"])

    for link in links or []:
        add(link)

    for match in re.findall(r"https://wellfound\.com/jobs\?job_listing_slug=[^\s>)]+", text):
        add(match)

    return job_links


def extract_source_job_id(url: str) -> str | None:
    slug = parse_qs(urlparse(url).query).get("job_listing_slug", [None])[0]

    if not slug:
        return None

    return slug.split("-", maxsplit=1)[0]


def parse_company(value: str) -> str:
    return clean_text(value.split("/", maxsplit=1)[0])


def extract_salary(details: str) -> str | None:
    match = SALARY_PATTERN.search(details)
    return clean_text(match.group(0)) if match else None


def extract_location(details: str) -> str | None:
    parts = [clean_text(part) for part in details.split("|")]

    for part in parts:
        normalized = part.casefold()

        if "remote" in normalized:
            return part

        if "in office" in normalized or "hybrid" in normalized:
            return part

    return None


def extract_employment_type(details: str) -> str | None:
    parts = [clean_text(part) for part in details.split("|")]

    for part in parts:
        if part.casefold() in {"full-time", "part-time", "contract", "internship"}:
            return part

    return None


def parse_wellfound_email(
    text: str = "",
    html: str | None = None,
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    lines = normalize_lines(text=text, html=html)
    job_links = extract_job_links(text=text, html=html, links=links)
    jobs: list[dict[str, Any]] = []
    link_index = 0

    for index in range(len(lines) - 2):
        title = lines[index]
        company_line = lines[index + 1]
        details = lines[index + 2]

        if "/" not in company_line or "employees" not in company_line.casefold():
            continue

        if "|" not in details:
            continue

        if link_index >= len(job_links):
            continue

        apply_url = job_links[link_index]
        link_index += 1
        employment_type = extract_employment_type(details)
        description = None

        if employment_type:
            description = f"Employment type: {employment_type}"

        jobs.append(
            {
                "source": "wellfound",
                "source_job_id": extract_source_job_id(apply_url),
                "title": title,
                "company_name": parse_company(company_line),
                "location": extract_location(details),
                "salary_text": extract_salary(details),
                "description": description,
                "apply_url": apply_url,
                "posted_age_text": None,
            }
        )

    return jobs
