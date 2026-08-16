"""Parser for Jobot single-job alert emails."""

import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup


JOBOT_URL_PATTERN = re.compile(
    r"https?://[^\s<>\"]*jobot\.com/[^\s<>\"]+",
    re.IGNORECASE,
)

SALARY_PATTERN = re.compile(
    r"\$[\d,.]+K?(?:\s*[-–]\s*\$?[\d,.]+K?)?",
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

    return [clean_text(line) for line in raw_text.splitlines() if clean_text(line)]


def is_jobot_url(value: str) -> bool:
    parsed = urlparse(value)

    return parsed.scheme in {"http", "https"} and "jobot.com" in parsed.netloc


def extract_jobot_links(
    text: str,
    html: str | None,
    links: list[str] | None,
) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        url = clean_text(value)

        if not is_jobot_url(url):
            return

        if url in seen:
            return

        seen.add(url)
        selected.append(url)

    if html:
        soup = BeautifulSoup(html, "html.parser")

        for anchor in soup.find_all("a", href=True):
            add(anchor["href"])

    for link in links or []:
        add(link)

    for match in JOBOT_URL_PATTERN.findall(text):
        add(match)

    return selected


def parse_jobot_email(
    text: str = "",
    html: str | None = None,
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    lines = normalize_lines(text=text, html=html)
    links_by_card = extract_jobot_links(text=text, html=html, links=links)

    if not links_by_card:
        return []

    subject_like_lines = [
        line
        for line in lines[:20]
        if " at " in line.casefold()
    ]

    for line in subject_like_lines:
        title, company = re.split(r"\s+at\s+", line, maxsplit=1, flags=re.IGNORECASE)

        return [
            {
                "source": "jobot",
                "source_job_id": None,
                "title": clean_text(title),
                "company_name": clean_text(company),
                "location": next(
                    (
                        value
                        for value in lines
                        if "remote" in value.casefold()
                    ),
                    None,
                ),
                "salary_text": next(
                    (
                        SALARY_PATTERN.search(value).group(0)
                        for value in lines
                        if SALARY_PATTERN.search(value)
                    ),
                    None,
                ),
                "description": None,
                "apply_url": links_by_card[0],
                "posted_age_text": None,
            }
        ]

    return []
