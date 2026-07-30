import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup


APPLY_PATH_PATTERN = re.compile(
    r"/apply-with-ai/(?P<job_id>[0-9a-f-]{36})",
    re.IGNORECASE,
)

APPLY_URL_PATTERN = re.compile(
    r"https://www\.remotehunter\.com/apply-with-ai/[^\s)]+",
    re.IGNORECASE,
)

SALARY_PATTERN = re.compile(
    r"""
    ^
    (?:
        \$[\d,.]+k?
        (?:\s*[-–]\s*\$[\d,.]+k?)?
        (?:\s*/\s*(?:hour|hr|year|yr|month|week|day))?
        |
        \$/yr
    )
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
        remote,\s*[A-Z]{2}
        |
        united\ states
        |
        [A-Z]{2},\s*US
        |
        [A-Za-z .'-]+,\s*[A-Z]{2},\s*US
        |
        US
        |
        London,\s*United\ Kingdom
        |
        Spain
    )
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)

SECTION_MARKERS = {
    "best match",
    "your next roles to explore",
    "your new job matches are below",
    "roles you can target right now",
}

IGNORED_DESCRIPTION_LINES = {
    "attach resume/cv",
    "description",
    "email...",
    "full name",
    "on-site",
    "remote",
    "resume/cv",
    "submit your application",
    "✱",
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


def is_remotehunter_apply_url(value: str) -> bool:
    parsed = urlparse(value)

    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.casefold().endswith(
            "remotehunter.com"
        )
        and bool(APPLY_PATH_PATTERN.search(parsed.path))
    )


def extract_source_job_id(url: str) -> str | None:
    match = APPLY_PATH_PATTERN.search(
        urlparse(url).path
    )

    if not match:
        return None

    return match.group("job_id")


def extract_apply_links(
    text: str,
    html: str | None,
    links: list[str] | None,
) -> list[str]:
    apply_links: list[str] = []
    seen: set[str] = set()

    for value in APPLY_URL_PATTERN.findall(text):
        if value not in seen:
            seen.add(value)
            apply_links.append(value)

    if html:
        soup = BeautifulSoup(html, "html.parser")

        for anchor in soup.find_all("a", href=True):
            href = clean_text(anchor["href"])

            if not is_remotehunter_apply_url(href):
                continue

            if href in seen:
                continue

            seen.add(href)
            apply_links.append(href)

    for link in links or []:
        if not is_remotehunter_apply_url(link):
            continue

        if link in seen:
            continue

        seen.add(link)
        apply_links.append(link)

    return apply_links


def marker_line_to_url(line: str) -> str | None:
    match = APPLY_URL_PATTERN.search(line)

    if not match:
        return None

    return match.group(0)


def marker_line_to_location(line: str) -> str | None:
    prefix = line.split(
        "Optimize For This Role",
        maxsplit=1,
    )[0]
    prefix = clean_text(prefix.replace("→", ""))

    if not prefix:
        return None

    return prefix


def is_salary(value: str) -> bool:
    return bool(SALARY_PATTERN.fullmatch(clean_text(value)))


def is_location(value: str) -> bool:
    return bool(LOCATION_PATTERN.fullmatch(clean_text(value)))


def prepare_segment(lines: list[str]) -> list[str]:
    normalized = [clean_text(line) for line in lines if clean_text(line)]

    for index in range(len(normalized) - 1, -1, -1):
        if normalized[index].casefold() in SECTION_MARKERS:
            normalized = normalized[index + 1 :]
            break

    while normalized and normalized[0].casefold() in SECTION_MARKERS:
        normalized = normalized[1:]

    return normalized


def extract_location(
    segment: list[str],
    marker_line: str,
) -> str | None:
    marker_location = marker_line_to_location(marker_line)

    if marker_location and marker_location.casefold() != "us":
        return marker_location

    if marker_location:
        for line in reversed(segment):
            if clean_text(line).casefold() in {
                "united states",
                "remote, us",
            }:
                return line

        return marker_location

    for line in reversed(segment):
        if is_salary(line):
            continue

        if line.casefold() in {"on-site", "remote"}:
            continue

        if is_location(line):
            return line

    return marker_location


def extract_salary(
    segment: list[str],
) -> str | None:
    for line in reversed(segment):
        if is_salary(line):
            return line

    return None


def extract_description(
    segment: list[str],
    title: str,
    company_name: str,
) -> str | None:
    description_lines: list[str] = []
    seen: set[str] = set()

    for line in segment[2:]:
        normalized = line.casefold()

        if (
            line == title
            or line == company_name
            or normalized in IGNORED_DESCRIPTION_LINES
            or is_salary(line)
            or is_location(line)
            or normalized.startswith("product ")
        ):
            continue

        if len(line) < 25:
            continue

        if normalized in seen:
            continue

        seen.add(normalized)
        description_lines.append(line)

    if not description_lines:
        return None

    return "\n\n".join(description_lines)


def parse_job_segment(
    segment_lines: list[str],
    marker_line: str,
    fallback_url: str | None,
) -> dict[str, Any] | None:
    segment = prepare_segment(segment_lines)

    if len(segment) < 2:
        return None

    apply_url = marker_line_to_url(marker_line) or fallback_url

    if not apply_url:
        return None

    company_name = segment[0]
    title = segment[1]
    location = extract_location(
        segment=segment,
        marker_line=marker_line,
    )

    if not company_name or not title or not location:
        return None

    return {
        "source": "remotehunter",
        "source_job_id": extract_source_job_id(apply_url),
        "title": title,
        "company_name": company_name,
        "location": location,
        "salary_text": extract_salary(segment),
        "description": extract_description(
            segment=segment,
            title=title,
            company_name=company_name,
        ),
        "apply_url": apply_url,
        "posted_age_text": None,
    }


def parse_remotehunter_email(
    text: str = "",
    html: str | None = None,
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    lines = normalize_lines(text=text, html=html)
    apply_links = extract_apply_links(
        text=text,
        html=html,
        links=links,
    )
    link_index = 0
    jobs: list[dict[str, Any]] = []
    segment_start = 0

    for index, line in enumerate(lines):
        if "Optimize For This Role" not in line:
            continue

        fallback_url = None

        if link_index < len(apply_links):
            fallback_url = apply_links[link_index]
            link_index += 1

        job = parse_job_segment(
            segment_lines=lines[segment_start:index],
            marker_line=line,
            fallback_url=fallback_url,
        )

        if job:
            jobs.append(job)

        segment_start = index + 1

    return jobs
