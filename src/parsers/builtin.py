import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup


BUILTIN_JOB_HOST_PATTERN = re.compile(
    r"(?:^|\.)builtin\.com$",
    re.IGNORECASE,
)

BUILTIN_TRACKING_HOST_PATTERN = re.compile(
    r"\.awstrack\.me$",
    re.IGNORECASE,
)

SALARY_PATTERN = re.compile(
    r"^\$[\d,]+(?:\s*[-–]\s*\$[\d,]+)?$",
    re.IGNORECASE,
)

LOCATION_MARKERS = {
    "remote",
    "hybrid",
    "in office",
}

FOOTER_LINES = {
    "get more recommendations",
    "share your feedback",
    "update email frequency",
    "unsubscribe",
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
        clean_text(line)
        for line in raw_text.splitlines()
        if clean_text(line)
    ]


def is_builtin_job_url(value: str) -> bool:
    parsed = urlparse(value)

    if parsed.scheme not in {"http", "https"}:
        return False

    if BUILTIN_JOB_HOST_PATTERN.search(parsed.netloc):
        return parsed.path.startswith("/job/")

    if BUILTIN_TRACKING_HOST_PATTERN.search(parsed.netloc):
        return "%2Fjob%2F" in value or "/job/" in value

    return False


def extract_job_links(
    html: str | None,
    links: list[str] | None,
) -> list[str]:
    job_links: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        href = clean_text(value)

        if not is_builtin_job_url(href):
            return

        if href in seen:
            return

        seen.add(href)
        job_links.append(href)

    if html:
        soup = BeautifulSoup(html, "html.parser")

        for anchor in soup.find_all("a", href=True):
            add(anchor["href"])

    for link in links or []:
        add(link)

    return job_links


def is_salary(value: str) -> bool:
    return bool(SALARY_PATTERN.fullmatch(clean_text(value)))


def is_location_marker(value: str) -> bool:
    normalized = clean_text(value).casefold()

    return normalized in LOCATION_MARKERS or (
        "remote" in normalized
        and len(normalized.split()) <= 4
    )


def is_footer(value: str) -> bool:
    normalized = clean_text(value).casefold()

    return normalized in FOOTER_LINES or normalized.startswith("© built in")


def find_job_starts(lines: list[str]) -> list[int]:
    starts: list[int] = []

    for index in range(len(lines) - 4):
        company = lines[index]
        title = lines[index + 1]
        location_marker = lines[index + 2]
        location = lines[index + 3]

        if is_footer(company):
            break

        if not is_location_marker(location_marker):
            continue

        if is_salary(company) or is_salary(title):
            continue

        if location.casefold() in {"|", "unsubscribe"}:
            continue

        starts.append(index)

    return starts


def parse_builtin_email(
    text: str = "",
    html: str | None = None,
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    lines = normalize_lines(text=text, html=html)
    job_links = extract_job_links(html=html, links=links)
    jobs: list[dict[str, Any]] = []

    for job_index, start in enumerate(find_job_starts(lines)):
        company_name = lines[start]
        title = lines[start + 1]
        work_mode = lines[start + 2]
        location = lines[start + 3]
        salary_text = None

        if start + 4 < len(lines) and is_salary(lines[start + 4]):
            salary_text = lines[start + 4]

        if job_index >= len(job_links):
            continue

        jobs.append(
            {
                "source": "builtin",
                "source_job_id": None,
                "title": title,
                "company_name": company_name,
                "location": (
                    "Remote"
                    if "remote" in work_mode.casefold()
                    else location
                ),
                "salary_text": salary_text,
                "description": None,
                "apply_url": job_links[job_index],
                "posted_age_text": None,
            }
        )

    return jobs
