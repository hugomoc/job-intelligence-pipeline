"""Parser for Indeed job-alert emails."""

import re
from typing import Any
from urllib.parse import parse_qs, urlparse


SALARY_PATTERN = re.compile(
    r"""
    ^
    (?:(?:up\ to|from)\s+)?
    \$[\d,]+
    (?:\.\d{2})?
    (?:\s*-\s*\$[\d,]+(?:\.\d{2})?)?
    \s+
    (?:an?\s+)?
    (?:hour|day|week|month|year)
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Indeed frequently reports a location as a bare country or an unabbreviated
# state instead of "City, ST". These are listed explicitly rather than matched
# with a generic word pattern so that title fragments such as
# "Senior Data Engineer - Analytics" are still rejected.
NAMED_REGIONS: tuple[str, ...] = (
    "United States",
    "Alabama",
    "Alaska",
    "Arizona",
    "Arkansas",
    "California",
    "Colorado",
    "Connecticut",
    "Delaware",
    "Florida",
    "Georgia",
    "Hawaii",
    "Idaho",
    "Illinois",
    "Indiana",
    "Iowa",
    "Kansas",
    "Kentucky",
    "Louisiana",
    "Maine",
    "Maryland",
    "Massachusetts",
    "Michigan",
    "Minnesota",
    "Mississippi",
    "Missouri",
    "Montana",
    "Nebraska",
    "Nevada",
    "New Hampshire",
    "New Jersey",
    "New Mexico",
    "New York",
    "North Carolina",
    "North Dakota",
    "Ohio",
    "Oklahoma",
    "Oregon",
    "Pennsylvania",
    "Rhode Island",
    "South Carolina",
    "South Dakota",
    "Tennessee",
    "Texas",
    "Utah",
    "Vermont",
    "Virginia",
    "Washington",
    "West Virginia",
    "Wisconsin",
    "Wyoming",
    "District of Columbia",
    "Puerto Rico",
)

# Words are joined with \s+ because LOCATION_PATTERN is compiled with
# re.VERBOSE, which would otherwise discard literal spaces.
NAMED_REGION_REGEX = "|".join(
    r"\s+".join(re.escape(word) for word in region.split())
    for region in NAMED_REGIONS
)

LOCATION_PATTERN = re.compile(
    r"""
    ^
    (?:
        remote
        |
        hybrid
        |
        on-site
        |
        onsite
        |
        [A-Za-z .'-]+,\s*[A-Z]{2}
        (?:\s+\d{5})?
        |
    """
    + NAMED_REGION_REGEX
    + r"""
    )
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)


IGNORED_LINES = {
    "easily apply",
    "apply now",
    "new",
    "urgently hiring",
    "responsive employer",
}


def is_indeed_job_url(value: str) -> bool:
    value_lower = value.lower()

    return (
        value_lower.startswith("http")
        and "indeed.com" in value_lower
        and any(
            path in value_lower
            for path in (
                "/pagead/clk/",
                "/viewjob",
                "/rc/clk",
            )
        )
    )


def normalize_lines(text: str) -> list[str]:
    lines: list[str] = []

    for raw_line in text.splitlines():
        line = " ".join(raw_line.split()).strip()

        if line:
            lines.append(line)

    return lines


def split_company_location(value: str) -> tuple[str, str] | None:
    value = value.strip()

    # Prevent salary ranges from being interpreted as
    # "company - location".
    if SALARY_PATTERN.fullmatch(value):
        return None

    if " - " not in value:
        return None

    company, location = value.rsplit(" - ", 1)

    company = company.strip()
    location = location.strip()

    if not company or not location:
        return None

    if company.lower().startswith(("http://", "https://")):
        return None

    if "$" in company or "$" in location:
        return None

    # Prevent titles such as "Senior Data Engineer - Analytics"
    # from being interpreted as a company and location.
    if not LOCATION_PATTERN.fullmatch(location):
        return None

    return company, location


def extract_source_job_id(url: str) -> str | None:
    query = parse_qs(urlparse(url).query)

    for parameter in ("jk", "jrtk"):
        values = query.get(parameter)

        if values:
            return values[0]

    return None


def find_company_line(
    lines: list[str],
) -> tuple[int, str, str] | None:
    for index in range(len(lines) - 1, -1, -1):
        result = split_company_location(lines[index])

        if result:
            company, location = result
            return index, company, location

    return None


def find_title(
    lines: list[str],
    company_index: int,
) -> str | None:
    for index in range(company_index - 1, -1, -1):
        candidate = lines[index]

        if candidate.lower() in IGNORED_LINES:
            continue

        if candidate.startswith(("http://", "https://")):
            continue

        return candidate

    return None


def parse_job_block(block_lines: list[str], job_url: str) -> dict[str, Any] | None:
    company_result = find_company_line(block_lines)

    if not company_result:
        return None

    company_index, company, location = company_result

    title = find_title(
        block_lines,
        company_index,
    )

    if not title:
        return None

    detail_lines = block_lines[company_index + 1 :]

    salary = None
    description_lines: list[str] = []

    for line in detail_lines:
        normalized = line.lower()

        if normalized in IGNORED_LINES:
            continue

        if SALARY_PATTERN.fullmatch(line):
            salary = line
            continue

        if not line.startswith(("http://", "https://")):
            description_lines.append(line)

    return {
        "source": "indeed",
        "source_job_id": extract_source_job_id(job_url),
        "title": title,
        "company_name": company,
        "location": location,
        "salary_text": salary,
        "description": " ".join(description_lines),
        "apply_url": job_url,
    }


def parse_indeed_email(text: str) -> list[dict[str, Any]]:
    lines = normalize_lines(text)

    jobs: list[dict[str, Any]] = []
    block_start = 0
    seen_urls: set[str] = set()

    for index, line in enumerate(lines):
        if not is_indeed_job_url(line):
            continue

        job_url = line

        if job_url in seen_urls:
            block_start = index + 1
            continue

        block_lines = lines[block_start:index]

        job = parse_job_block(
            block_lines=block_lines,
            job_url=job_url,
        )

        if job:
            jobs.append(job)
            seen_urls.add(job_url)

        block_start = index + 1

    return jobs
