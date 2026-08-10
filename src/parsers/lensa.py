import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup


LENSA_CLICK_HOST = "sg3email.lensa.com"

LINK_PATTERN = re.compile(
    r"https://sg3email\.lensa\.com/ls/click\?[^\s)\]]+",
    re.IGNORECASE,
)

SALARY_PATTERN = re.compile(
    r"""
    \$[\d,.]+K?
    (?:\s*[-–]\s*\$[\d,.]+K?)?
    \s*/\s*
    (?:yr|year|hr|hour|month|week|day)
    \.?
    (?:\s*\(est\.\))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

IGNORED_LINES = {
    "---|---",
    "apply",
    "apply now",
    "change preferences",
    "manage alerts",
    "unsubscribe",
    "view job",
}


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
        clean_text(line.lstrip("* "))
        for line in raw_text.splitlines()
        if clean_text(line.lstrip("* "))
    ]


def is_lensa_click_url(value: str) -> bool:
    parsed = urlparse(value)

    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.casefold() == LENSA_CLICK_HOST
        and parsed.path.startswith("/ls/click")
    )


def extract_job_links(
    text: str,
    html: str | None,
    links: list[str] | None,
) -> list[str]:
    job_links: list[str] = []
    seen: set[str] = set()

    if html:
        soup = BeautifulSoup(html, "html.parser")

        for anchor in soup.find_all("a", href=True):
            href = clean_text(anchor["href"])

            if not is_lensa_click_url(href):
                continue

            if href in seen:
                continue

            seen.add(href)
            job_links.append(href)

    for link in links or []:
        if not is_lensa_click_url(link):
            continue

        if link in seen:
            continue

        seen.add(link)
        job_links.append(link)

    for value in LINK_PATTERN.findall(text):
        if value in seen:
            continue

        seen.add(value)
        job_links.append(value)

    return job_links


def extract_company_before_separator(lines: list[str], index: int) -> str | None:
    for candidate in reversed(lines[max(0, index - 3) : index]):
        if "|" in candidate:
            company = clean_text(candidate.rsplit("|", maxsplit=1)[-1])
        else:
            company = clean_text(candidate)

        if not company or company.casefold() in IGNORED_LINES:
            continue

        if company.endswith((".webp)", ".png)", ".jpg)")):
            continue

        if company.startswith("["):
            continue

        return company

    return None


def extract_salary(value: str | None) -> str | None:
    match = SALARY_PATTERN.search(clean_text(value))

    if not match:
        return None

    return clean_text(match.group(0))


def extract_location(details: str | None) -> str | None:
    details_text = clean_text(details)

    if "remote" in details_text.casefold():
        return "Remote"

    return None


def find_following_link(
    lines: list[str],
    start_index: int,
    job_links: list[str],
    link_index: int,
) -> tuple[str | None, int]:
    if link_index < len(job_links):
        return job_links[link_index], link_index + 1

    for line in lines[start_index : start_index + 6]:
        match = LINK_PATTERN.search(line)

        if match:
            return match.group(0), link_index

    return None, link_index


def parse_lensa_email(
    text: str = "",
    html: str | None = None,
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    lines = normalize_lines(text=text, html=html)
    job_links = extract_job_links(
        text=text,
        html=html,
        links=links,
    )
    jobs: list[dict[str, Any]] = []
    link_index = 0

    for index, line in enumerate(lines):
        if clean_text(line) != "---|---":
            continue

        company_name = extract_company_before_separator(lines, index)

        if not company_name:
            continue

        title_index = index + 1

        if title_index >= len(lines):
            continue

        title = clean_text(lines[title_index])

        if not title or title.casefold() in IGNORED_LINES:
            continue

        salary_text = None
        details = None

        if title_index + 1 < len(lines):
            salary_text = extract_salary(lines[title_index + 1])

        if title_index + 2 < len(lines):
            details = lines[title_index + 2]

        apply_url, link_index = find_following_link(
            lines=lines,
            start_index=title_index + 1,
            job_links=job_links,
            link_index=link_index,
        )

        if not apply_url:
            continue

        jobs.append(
            {
                "source": "lensa",
                "source_job_id": None,
                "title": title,
                "company_name": company_name,
                "location": extract_location(details),
                "salary_text": salary_text,
                "description": None,
                "apply_url": apply_url,
                "posted_age_text": None,
            }
        )

    return jobs
