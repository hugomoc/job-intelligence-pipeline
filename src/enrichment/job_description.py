from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup


MINIMUM_USEFUL_DESCRIPTION_WORDS = 40


NON_DESCRIPTION_PHRASES = (
    "exact matches for your query ends here",
    "time to start afresh",
    "search jobs applyassist careerpilot",
    "companies insights jobseekers workstyle game",
)


JOB_DESCRIPTION_SIGNAL_PHRASES = (
    "responsibilities",
    "qualifications",
    "requirements",
    "required qualifications",
    "preferred qualifications",
    "what you'll do",
    "what you will do",
    "about the role",
    "job description",
    "experience with",
    "we are looking for",
    "you will",
    "duties",
)


BLOCK_PAGE_PHRASES = (
    "access denied",
    "verify you are human",
    "unusual traffic",
    "captcha",
    "enable javascript and cookies",
    "checking your browser",
    "temporarily blocked",
    "request unsuccessful",
    "automated access",
)


DESCRIPTION_SELECTORS = (
    "#jobDescriptionText",
    "[data-testid='jobDescriptionText']",
    "[data-test='jobDescriptionContent']",
    "[data-testid='job-description']",
    "[itemprop='description']",
    ".job-description",
    ".jobDescription",
    ".job-description-content",
    "article",
    "main",
)


@dataclass(frozen=True)
class JobDescriptionResult:
    requested_url: str
    final_url: str | None

    status: str
    http_status: int | None
    extraction_method: str | None

    description: str
    word_count: int

    error_message: str | None


def create_http_client(
    timeout_seconds: float = 30.0,
) -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(
            timeout_seconds
        ),
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": (
                "en-US,en;q=0.9"
            ),
        },
    )


def normalize_description_text(
    value: str,
) -> str:
    if not value:
        return ""

    normalized = html.unescape(value)
    normalized = normalized.replace(
        "\u00a0",
        " ",
    )
    normalized = normalized.replace(
        "\u200b",
        "",
    )
    normalized = normalized.replace(
        "\ufeff",
        "",
    )

    lines: list[str] = []

    for line in normalized.splitlines():
        cleaned_line = re.sub(
            r"[ \t]+",
            " ",
            line,
        ).strip()

        if cleaned_line:
            lines.append(cleaned_line)

    return "\n".join(lines).strip()


def html_fragment_to_text(
    value: Any,
) -> str:
    if value is None:
        return ""

    if isinstance(value, list):
        pieces = [
            html_fragment_to_text(item)
            for item in value
        ]

        return normalize_description_text(
            "\n".join(
                piece
                for piece in pieces
                if piece
            )
        )

    if isinstance(value, dict):
        pieces = [
            html_fragment_to_text(item)
            for item in value.values()
        ]

        return normalize_description_text(
            "\n".join(
                piece
                for piece in pieces
                if piece
            )
        )

    soup = BeautifulSoup(
        str(value),
        "html.parser",
    )

    for element in soup(
        [
            "script",
            "style",
            "noscript",
            "svg",
        ]
    ):
        element.decompose()

    return normalize_description_text(
        soup.get_text(
            separator="\n",
        )
    )


def count_words(
    value: str | None,
) -> int:
    if not value:
        return 0

    return len(
        value.split()
    )


def appears_to_be_non_description_text(
    value: str,
) -> bool:
    normalized = " ".join(
        value.casefold().split()
    )

    if not normalized:
        return True

    if any(
        phrase in normalized
        for phrase in NON_DESCRIPTION_PHRASES
    ):
        return True

    if any(
        phrase in normalized
        for phrase in JOB_DESCRIPTION_SIGNAL_PHRASES
    ):
        return False

    return False


def is_valid_http_url(
    url: str,
) -> bool:
    if re.search(r"[\x00-\x1f\x7f]", url):
        return False

    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    return (
        parsed.scheme in {
            "http",
            "https",
        }
        and bool(parsed.netloc)
    )


def is_job_posting_type(
    value: Any,
) -> bool:
    if isinstance(value, str):
        return value.casefold() == "jobposting"

    if isinstance(value, list):
        return any(
            is_job_posting_type(item)
            for item in value
        )

    return False


def walk_json(
    value: Any,
):
    if isinstance(value, dict):
        yield value

        for child in value.values():
            yield from walk_json(child)

    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def build_structured_description(
    posting: dict[str, Any],
) -> str:
    description = html_fragment_to_text(
        posting.get("description")
    )

    extra_fields = (
        "responsibilities",
        "qualifications",
        "skills",
        "experienceRequirements",
        "educationRequirements",
    )

    pieces: list[str] = []

    if description:
        pieces.append(description)

    for field_name in extra_fields:
        field_text = html_fragment_to_text(
            posting.get(field_name)
        )

        if not field_text:
            continue

        normalized_field = (
            field_text.casefold()
        )

        already_present = any(
            normalized_field
            in existing.casefold()
            for existing in pieces
        )

        if not already_present:
            pieces.append(field_text)

    return normalize_description_text(
        "\n\n".join(pieces)
    )


def extract_json_ld_description(
    soup: BeautifulSoup,
) -> str:
    candidates: list[str] = []

    scripts = soup.find_all(
        "script",
        attrs={
            "type": (
                "application/ld+json"
            )
        },
    )

    for script in scripts:
        raw_json = script.string

        if not raw_json:
            raw_json = script.get_text(
                strip=True
            )

        if not raw_json:
            continue

        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            continue

        for node in walk_json(payload):
            if not is_job_posting_type(
                node.get("@type")
            ):
                continue

            description = (
                build_structured_description(
                    node
                )
            )

            if description:
                candidates.append(
                    description
                )

    if not candidates:
        return ""

    return max(
        candidates,
        key=count_words,
    )


def extract_selector_description(
    soup: BeautifulSoup,
) -> str:
    candidates: list[str] = []

    for selector in DESCRIPTION_SELECTORS:
        try:
            elements = soup.select(selector)
        except Exception:
            continue

        for element in elements:
            element_copy = BeautifulSoup(
                str(element),
                "html.parser",
            )

            for unwanted in element_copy(
                [
                    "script",
                    "style",
                    "noscript",
                    "nav",
                    "header",
                    "footer",
                    "form",
                    "button",
                    "svg",
                ]
            ):
                unwanted.decompose()

            text = normalize_description_text(
                element_copy.get_text(
                    separator="\n",
                )
            )

            if count_words(text) >= 20:
                candidates.append(text)

    if not candidates:
        return ""

    return max(
        candidates,
        key=count_words,
    )


def appears_to_be_block_page(
    soup: BeautifulSoup,
) -> bool:
    title = ""

    if soup.title and soup.title.string:
        title = soup.title.string

    page_preview = (
        title
        + "\n"
        + soup.get_text(
            separator=" ",
            strip=True,
        )[:4_000]
    ).casefold()

    return any(
        phrase in page_preview
        for phrase in BLOCK_PAGE_PHRASES
    )


def fetch_job_description(
    url: str,
    client: httpx.Client,
) -> JobDescriptionResult:
    if not is_valid_http_url(url):
        return JobDescriptionResult(
            requested_url=url,
            final_url=None,
            status="invalid_url",
            http_status=None,
            extraction_method=None,
            description="",
            word_count=0,
            error_message=(
                "The apply URL is not a valid "
                "HTTP or HTTPS URL."
            ),
        )

    try:
        response = client.get(url)

    except httpx.TimeoutException:
        return JobDescriptionResult(
            requested_url=url,
            final_url=None,
            status="fetch_error",
            http_status=None,
            extraction_method=None,
            description="",
            word_count=0,
            error_message=(
                "The request timed out."
            ),
        )

    except httpx.RequestError as error:
        return JobDescriptionResult(
            requested_url=url,
            final_url=None,
            status="fetch_error",
            http_status=None,
            extraction_method=None,
            description="",
            word_count=0,
            error_message=(
                f"{type(error).__name__}: "
                f"{error}"
            ),
        )

    final_url = str(response.url)
    http_status = response.status_code

    if http_status in {
        401,
        403,
        429,
    }:
        return JobDescriptionResult(
            requested_url=url,
            final_url=final_url,
            status="blocked",
            http_status=http_status,
            extraction_method=None,
            description="",
            word_count=0,
            error_message=(
                "The job site rejected or "
                "rate-limited the request."
            ),
        )

    if http_status >= 400:
        return JobDescriptionResult(
            requested_url=url,
            final_url=final_url,
            status="fetch_error",
            http_status=http_status,
            extraction_method=None,
            description="",
            word_count=0,
            error_message=(
                f"HTTP status {http_status}."
            ),
        )

    content_type = response.headers.get(
        "content-type",
        "",
    ).casefold()

    if (
        "text/html" not in content_type
        and "application/xhtml+xml"
        not in content_type
    ):
        return JobDescriptionResult(
            requested_url=url,
            final_url=final_url,
            status="no_description",
            http_status=http_status,
            extraction_method=None,
            description="",
            word_count=0,
            error_message=(
                "The response was not an "
                "HTML document."
            ),
        )

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    if appears_to_be_block_page(soup):
        return JobDescriptionResult(
            requested_url=url,
            final_url=final_url,
            status="blocked",
            http_status=http_status,
            extraction_method=None,
            description="",
            word_count=0,
            error_message=(
                "The response appears to be "
                "an access-control or CAPTCHA page."
            ),
        )

    description = (
        extract_json_ld_description(soup)
    )

    extraction_method = "json_ld"

    if (
        count_words(description)
        < MINIMUM_USEFUL_DESCRIPTION_WORDS
    ):
        selector_description = (
            extract_selector_description(soup)
        )

        if (
            count_words(selector_description)
            > count_words(description)
        ):
            description = (
                selector_description
            )
            extraction_method = (
                "html_selector"
            )

    word_count = count_words(description)

    if (
        word_count
        < MINIMUM_USEFUL_DESCRIPTION_WORDS
        or appears_to_be_non_description_text(
            description
        )
    ):
        return JobDescriptionResult(
            requested_url=url,
            final_url=final_url,
            status="no_description",
            http_status=http_status,
            extraction_method=(
                extraction_method
                if description
                else None
            ),
            description=description,
            word_count=word_count,
            error_message=(
                "No sufficiently complete job "
                "description was found."
            ),
        )

    return JobDescriptionResult(
        requested_url=url,
        final_url=final_url,
        status="enriched",
        http_status=http_status,
        extraction_method=(
            extraction_method
        ),
        description=description,
        word_count=word_count,
        error_message=None,
    )
