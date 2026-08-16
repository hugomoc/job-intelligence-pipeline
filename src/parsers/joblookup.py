import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup


JOBLOOKUP_HOST_PATTERN = re.compile(
    r"(?:^|\.)joblookup\.com$",
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


def is_joblookup_job_url(value: str) -> bool:
    parsed = urlparse(clean_text(value).strip("<>"))

    return (
        parsed.scheme in {"http", "https"}
        and bool(JOBLOOKUP_HOST_PATTERN.search(parsed.netloc))
        and "/dispatch/job/" in parsed.path
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

        if not is_joblookup_job_url(href) or href in seen:
            return

        seen.add(href)
        job_links.append(href)

    if html:
        soup = BeautifulSoup(html, "html.parser")

        for anchor in soup.find_all("a", href=True):
            add(anchor["href"])

    for link in links or []:
        add(link)

    for match in re.findall(r"https://joblookup\.com/[^\s>]+", text):
        add(match)

    return job_links


def extract_source_job_id(url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    if query.get("uid"):
        return query["uid"][0]

    slug = parsed.path.rstrip("/").split("/")[-1]
    return slug or None


def parse_joblookup_email(
    text: str = "",
    html: str | None = None,
    links: list[str] | None = None,
) -> list[dict[str, Any]]:
    lines = normalize_lines(text=text, html=html)
    job_links = extract_job_links(text=text, html=html, links=links)

    title = None
    location = None

    for index, line in enumerate(lines):
        if line.casefold().startswith("job title:"):
            title = clean_text(line.split(":", maxsplit=1)[1])
        elif line.casefold().startswith("location:"):
            location = clean_text(line.split(":", maxsplit=1)[1])

        if title and location:
            break

    if not title or not job_links:
        return []

    return [
        {
            "source": "joblookup",
            "source_job_id": extract_source_job_id(job_links[0]),
            "title": title,
            "company_name": "Unknown company",
            "location": location,
            "salary_text": None,
            "description": None,
            "apply_url": job_links[0],
            "posted_age_text": None,
        }
    ]
