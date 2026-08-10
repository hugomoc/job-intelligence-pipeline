import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup


URL_PATTERN = re.compile(r"https?://[^\s\])]+", re.IGNORECASE)

SALARY_PATTERN = re.compile(
    r"""
    ^
    \$[\d,.]+K?
    (?:\s*/\s*(?:yr|year|hr|hour))?
    (?:\s*[-–]\s*\$[\d,.]+K?
        (?:\s*/\s*(?:yr|year|hr|hour))?
    )?
    $
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

JOBRIGHT_MARKERS = {
    "APPLY NOW",
    "Be an early applicant",
}

JOBOT_MARKER_TEXT = (
    "Based on your resume",
    "Based on your job alert",
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
        clean_text(line.lstrip("* "))
        for line in raw_text.splitlines()
        if clean_text(line.lstrip("* "))
    ]


def extract_urls(
    text: str,
    html: str | None,
    links: list[str] | None,
) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    for value in URL_PATTERN.findall(text):
        normalized = clean_text(value)

        if normalized in seen:
            continue

        seen.add(normalized)
        urls.append(normalized)

    if html:
        soup = BeautifulSoup(html, "html.parser")

        for anchor in soup.find_all("a", href=True):
            href = clean_text(anchor["href"])

            if not href or href in seen:
                continue

            seen.add(href)
            urls.append(href)

    for link in links or []:
        if not link or link in seen:
            continue

        seen.add(link)
        urls.append(link)

    return urls


def is_jobright_job_url(value: str) -> bool:
    parsed = urlparse(value)

    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.casefold().endswith("jobright.ai")
        and parsed.path.startswith("/jobs/info/")
    )


def is_jobot_click_url(value: str) -> bool:
    parsed = urlparse(value)

    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.casefold().endswith("alerts.jobot.com")
        and parsed.path.startswith("/ls/click")
    )


def extract_source_job_id(url: str) -> str | None:
    parsed = urlparse(url)

    if not is_jobright_job_url(url):
        return None

    return parsed.path.rstrip("/").rsplit("/", maxsplit=1)[-1]


def is_salary(value: str | None) -> bool:
    return bool(SALARY_PATTERN.fullmatch(clean_text(value)))


def is_location(value: str | None) -> bool:
    return bool(LOCATION_PATTERN.fullmatch(clean_text(value)))


def is_posted_age(value: str | None) -> bool:
    normalized = clean_text(value).casefold()

    return "ago" in normalized and normalized.endswith("·")


def trim_jobright_segment(segment: list[str]) -> list[str]:
    ignored_prefixes = (
        "hi ",
        "here are",
        "you are invited",
        "why jobright",
    )
    trimmed = [
        line
        for line in segment
        if not any(
            line.casefold().startswith(prefix)
            for prefix in ignored_prefixes
        )
        and "early applicant" not in line.casefold()
        and line not in JOBRIGHT_MARKERS
    ]

    while trimmed and (
        URL_PATTERN.search(trimmed[0])
        or trimmed[0].casefold() in {"jobs", "recommend"}
    ):
        trimmed = trimmed[1:]

    return trimmed


def parse_jobright_ai_segment(
    segment: list[str],
    apply_url: str,
) -> dict[str, Any] | None:
    lines = trim_jobright_segment(segment)

    if len(lines) < 3:
        return None

    if "early applicant" in lines[0].casefold():
        return None

    company_name = lines[0]
    title_index = 2 if "·" in lines[1] else 1

    if title_index >= len(lines):
        return None

    title = lines[title_index]
    salary_text = None
    location = None
    posted_age_text = None

    for candidate in lines[title_index + 1 : title_index + 6]:
        if salary_text is None and is_salary(candidate):
            salary_text = candidate
            continue

        if location is None and is_location(candidate):
            location = candidate
            continue

        if posted_age_text is None and is_posted_age(candidate):
            posted_age_text = candidate

    if not company_name or not title or not location:
        return None

    return {
        "source": "jobright",
        "source_job_id": extract_source_job_id(apply_url),
        "title": title,
        "company_name": company_name,
        "location": location,
        "salary_text": salary_text,
        "description": None,
        "apply_url": apply_url,
        "posted_age_text": posted_age_text,
    }


def parse_jobright_ai_email(
    lines: list[str],
    urls: list[str],
) -> list[dict[str, Any]]:
    job_urls = [
        url
        for url in urls
        if is_jobright_job_url(url)
    ]
    jobs: list[dict[str, Any]] = []
    segment_start = 0

    for index, line in enumerate(lines):
        if line != "APPLY NOW":
            continue

        if len(jobs) >= len(job_urls):
            break

        job = parse_jobright_ai_segment(
            segment=lines[segment_start:index],
            apply_url=job_urls[len(jobs)],
        )

        if job:
            jobs.append(job)

        segment_start = index + 1

    return jobs


def extract_jobot_metadata(
    line: str,
) -> tuple[str | None, str | None]:
    location = None
    salary_text = None

    location_match = re.search(
        r"\[📍\]\s*(?P<location>.*?)(?:\s*\[💵\]|$)",
        line,
    )

    if location_match:
        location = clean_text(location_match.group("location"))

    salary_match = re.search(r"\[💵\]\s*(?P<salary>.+)$", line)

    if salary_match:
        salary_text = clean_text(salary_match.group("salary"))

    if line.startswith("[🏡] REMOTE") and location:
        location = f"{location} (Remote)"

    return location, salary_text


def parse_jobot_segment(
    segment: list[str],
) -> dict[str, Any] | None:
    lines = [
        line
        for line in segment
        if clean_text(line)
    ]

    marker_index = next(
        (
            index
            for index, line in enumerate(lines)
            if any(marker in line for marker in JOBOT_MARKER_TEXT)
        ),
        None,
    )

    if marker_index is None or marker_index + 2 >= len(lines):
        return None

    title = lines[marker_index + 1]
    apply_url = None
    metadata_index = None

    for index in range(marker_index + 2, len(lines)):
        if not apply_url:
            url_match = URL_PATTERN.search(lines[index])

            if url_match and is_jobot_click_url(url_match.group(0)):
                apply_url = url_match.group(0)
                continue

        if "[📍]" in lines[index]:
            metadata_index = index
            break

    if not apply_url or metadata_index is None:
        return None

    location, salary_text = extract_jobot_metadata(lines[metadata_index])

    if not location:
        return None

    description_lines = [
        line
        for line in lines[marker_index + 2 : metadata_index]
        if not URL_PATTERN.search(line)
    ]

    description = "\n\n".join(description_lines) or None

    return {
        "source": "jobright",
        "source_job_id": None,
        "title": title,
        "company_name": "Jobot",
        "location": location,
        "salary_text": salary_text,
        "description": description,
        "apply_url": apply_url,
        "posted_age_text": None,
    }


def parse_jobot_alert_email(lines: list[str]) -> list[dict[str, Any]]:
    marker_indexes = [
        index
        for index, line in enumerate(lines)
        if any(marker in line for marker in JOBOT_MARKER_TEXT)
    ]
    jobs: list[dict[str, Any]] = []

    for marker_position, marker_index in enumerate(marker_indexes):
        next_marker = (
            marker_indexes[marker_position + 1]
            if marker_position + 1 < len(marker_indexes)
            else len(lines)
        )
        job = parse_jobot_segment(lines[marker_index:next_marker])

        if job:
            jobs.append(job)

    return jobs


def parse_jobright_email(
    text: str = "",
    html: str | None = None,
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    lines = normalize_lines(text=text, html=html)
    urls = extract_urls(text=text, html=html, links=links)

    jobs = parse_jobright_ai_email(lines=lines, urls=urls)

    if jobs:
        return jobs

    return parse_jobot_alert_email(lines=lines)
