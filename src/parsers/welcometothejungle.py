import re
from typing import Any

from bs4 import BeautifulSoup


SALARY_PATTERN = re.compile(
    r"^Salary:\s*(?P<salary>.+)$",
    re.IGNORECASE,
)

REMOTE_PATTERN = re.compile(
    r"remote",
    re.IGNORECASE,
)

FOOTER_LINES = {
    "new job notification",
    "see all top matches",
    "receive these notifications:",
    "never",
    "weekly",
    "daily",
    "change email frequency",
    "manage email notifications",
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


def job_links(
    links: list[str] | None,
) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()

    for link in links or []:
        if "sendgrid.net/ls/click" not in link:
            continue

        if link in seen:
            continue

        seen.add(link)
        selected.append(link)

    return selected


def is_footer(value: str) -> bool:
    normalized = clean_text(value).casefold()

    return (
        normalized in FOOTER_LINES
        or normalized.startswith("welcome to the jungle is")
        or normalized.startswith("unit ")
    )


def is_header(value: str) -> bool:
    normalized = clean_text(value).casefold()

    return (
        normalized == "new job notification"
        or normalized.startswith("there are new jobs")
    )


def parse_title(lines: list[str], title_index: int) -> tuple[str, int]:
    title = lines[title_index]
    next_index = title_index + 1

    if (
        next_index < len(lines)
        and not SALARY_PATTERN.match(lines[next_index])
        and not REMOTE_PATTERN.search(lines[next_index])
        and len(lines[next_index]) <= 30
    ):
        title = f"{title} {lines[next_index]}"
        next_index += 1

    return title, next_index


def parse_welcometothejungle_email(
    text: str = "",
    html: str | None = None,
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    lines = normalize_lines(text=text, html=html)
    links_by_card = job_links(links)
    jobs: list[dict[str, Any]] = []

    for salary_index, salary_line in enumerate(lines):
        salary_match = SALARY_PATTERN.match(salary_line)

        if not salary_match:
            continue

        if salary_index + 1 >= len(lines):
            continue

        location_line = lines[salary_index + 1]

        if not REMOTE_PATTERN.search(location_line):
            continue

        title_index = salary_index - 1
        title_parts = [lines[title_index]]

        if (
            title_index - 1 >= 0
            and lines[title_index].startswith("(")
            and lines[title_index].endswith(")")
        ):
            title_index -= 1
            title_parts.insert(0, lines[title_index])

        description_index = title_index - 1
        company_index = title_index - 2

        if company_index < 0:
            continue

        company = lines[company_index]
        description = lines[description_index]

        if is_header(company) or is_footer(company):
            continue

        title = " ".join(title_parts)

        link_offset = 1 if len(links_by_card) > 3 else 0
        link_index = len(jobs) + link_offset

        if link_index >= len(links_by_card):
            apply_url = links_by_card[-1] if links_by_card else None
        else:
            apply_url = links_by_card[link_index]

        if not apply_url:
            index += 1
            continue

        jobs.append(
            {
                "source": "welcometothejungle",
                "source_job_id": None,
                "title": title,
                "company_name": company,
                "location": location_line,
                "salary_text": salary_match.group("salary"),
                "description": description,
                "apply_url": apply_url,
                "posted_age_text": None,
            }
        )

    return jobs
