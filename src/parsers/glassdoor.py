import re
import unicodedata
from typing import Any
from urllib.parse import parse_qs, urlparse


RATING_PATTERN = re.compile(
    r"^\d(?:\.\d)?\s*★$"
)

SALARY_PATTERN = re.compile(
    r"""
    ^
    (?:(?:up\ to|from)\s+)?
    \$[\d,.]+K?
    (?:\s*-\s*\$[\d,.]+K?)?
    (?:\s+(?:an?\s+|per\s+)?(?:hour|day|week|month|year))?
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

MATCH_PERCENT_PATTERN = re.compile(
    r"^\d{1,3}%$"
)

IGNORED_COMPANY_LINES = {
    "easy apply",
    "just posted",
    "best place to work",
    "employer est.",
}


def clean_line(value: str) -> str:
    # Remove invisible formatting characters included in some
    # Glassdoor emails.
    without_formatting = "".join(
        character
        for character in value
        if unicodedata.category(character) != "Cf"
    )

    return " ".join(
        without_formatting.replace("\xa0", " ").split()
    ).strip()


def normalize_lines(text: str) -> list[str]:
    lines: list[str] = []

    for raw_line in text.splitlines():
        line = clean_line(raw_line)

        if line:
            lines.append(line)

    return lines


def is_rating(value: str) -> bool:
    return bool(RATING_PATTERN.fullmatch(value))


def is_salary(value: str) -> bool:
    normalized = clean_line(value).replace("–", "-")

    if bool(SALARY_PATTERN.fullmatch(normalized)):
        return True

    return (
        "$" in normalized
        and bool(re.search(r"\d", normalized))
        and bool(
            re.search(
                r"\b(?:yr|year|hour|month|week|day)\b",
                normalized,
                re.IGNORECASE,
            )
        )
    )


def is_location(value: str) -> bool:
    return bool(LOCATION_PATTERN.fullmatch(value))


def is_glassdoor_job_url(url: str) -> bool:
    parsed = urlparse(url)
    query_parameters = parse_qs(parsed.query)

    return (
        "glassdoor.com" in parsed.netloc.lower()
        and "jobListingId" in query_parameters
    )


def extract_source_job_id(url: str) -> str | None:
    query_parameters = parse_qs(
        urlparse(url).query
    )

    job_ids = query_parameters.get("jobListingId")

    if not job_ids:
        return None

    return job_ids[0]


def extract_job_links(links: list[str]) -> list[str]:
    job_links: list[str] = []
    seen: set[str] = set()

    for link in links:
        if not is_glassdoor_job_url(link):
            continue

        if link in seen:
            continue

        seen.add(link)
        job_links.append(link)

    return job_links


def find_listing_range(
    lines: list[str],
) -> tuple[int, int]:
    start_index = None
    end_index = None

    for index, line in enumerate(lines):
        if line.lower().startswith("your job listings for"):
            # The next two lines contain the saved-search title
            # and saved-search location, not an actual job.
            start_index = index + 3
            break

    if start_index is None:
        raise ValueError(
            "Glassdoor listing section was not found."
        )

    for index in range(start_index, len(lines)):
        if lines[index].lower() == "see more jobs":
            end_index = index
            break

    if end_index is None:
        raise ValueError(
            "Glassdoor listing section ending was not found."
        )

    return start_index, end_index


def parse_listing_text(
    listing_lines: list[str],
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []

    for location_index, line in enumerate(listing_lines):
        if not is_location(line):
            continue

        title_index = location_index - 1

        if title_index < 0:
            continue

        title = listing_lines[title_index]

        company_index = title_index - 1

        if (
            company_index >= 0
            and is_rating(listing_lines[company_index])
        ):
            company_index -= 1

        if company_index < 0:
            continue

        company_name = listing_lines[company_index]

        if company_name.casefold() in IGNORED_COMPANY_LINES:
            continue

        salary_text = None
        salary_index = location_index + 1

        if (
            salary_index < len(listing_lines)
            and is_salary(listing_lines[salary_index])
        ):
            salary_text = listing_lines[salary_index]

        jobs.append(
            {
                "source": "glassdoor",
                "source_job_id": None,
                "title": title,
                "company_name": company_name,
                "location": line,
                "salary_text": salary_text,
                "description": None,
                "apply_url": None,
            }
        )

    return jobs


def parse_super_match_text(
    lines: list[str],
) -> list[dict[str, Any]]:
    """
    Parses Glassdoor's "New roles to put on your radar"
    template, which exposes one featured job plus a
    "Review N new matches" CTA.
    """
    percent_index = None

    for index, line in enumerate(lines):
        if MATCH_PERCENT_PATTERN.fullmatch(line):
            percent_index = index
            break

    if percent_index is None:
        return []

    detail_lines = lines[:percent_index]

    if not detail_lines:
        return []

    salary_text = None

    if is_salary(detail_lines[-1]):
        salary_text = detail_lines[-1]
        detail_lines = detail_lines[:-1]

    if len(detail_lines) < 3:
        return []

    location = detail_lines[-1]

    if not is_location(location):
        return []

    title = detail_lines[-2]
    company_name = detail_lines[-3]
    description = None

    if len(detail_lines) >= 4:
        description = detail_lines[-4]

    return [
        {
            "source": "glassdoor",
            "source_job_id": None,
            "title": title,
            "company_name": company_name,
            "location": location,
            "salary_text": salary_text,
            "description": description,
            "apply_url": None,
        }
    ]


def parse_glassdoor_email(
    text: str,
    links: list[str],
) -> list[dict[str, Any]]:
    lines = normalize_lines(text)
    job_links = extract_job_links(links)

    try:
        start_index, end_index = find_listing_range(lines)
        listing_lines = lines[start_index:end_index]
        jobs = parse_listing_text(listing_lines)

    except ValueError:
        jobs = parse_super_match_text(lines)

        if not jobs:
            raise

    if len(job_links) < len(jobs):
        raise ValueError(
            "Glassdoor parser count mismatch: "
            f"{len(jobs)} jobs found in the text, but "
            f"{len(job_links)} job links were found."
        )

    for job, job_url in zip(
        jobs,
        job_links,
    ):
        job["apply_url"] = job_url
        job["source_job_id"] = extract_source_job_id(
            job_url
        )

    return jobs
