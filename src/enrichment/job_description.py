"""Public job-description extraction helpers.

This module extracts useful job-description text from public HTML, structured
data, and common job-board pages. It deliberately validates length and content
quality so login pages, navigation, and marketing copy do not overwrite better
descriptions.
"""

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


LINKEDIN_POSTING_DESCRIPTION_SELECTORS = (
    ".show-more-less-html__markup",
    ".description__text",
    ".jobs-description__content",
    "#job-details",
    "[data-test-id='job-details-description']",
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
    resolved_title: str | None = None
    resolved_company: str | None = None
    resolved_location: str | None = None
    resolved_job_id: str | None = None
    resolved_title_source: str | None = None
    resolved_company_source: str | None = None
    resolved_location_source: str | None = None
    resolved_job_id_source: str | None = None


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


def organization_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return html_fragment_to_text(value.get("name")) or None

    return html_fragment_to_text(value) or None


def location_text(value: Any) -> str | None:
    if isinstance(value, list):
        pieces = [
            location_text(item)
            for item in value
        ]

        return ", ".join(piece for piece in pieces if piece) or None

    if not isinstance(value, dict):
        return html_fragment_to_text(value) or None

    address = value.get("address")

    if isinstance(address, dict):
        pieces = [
            address.get("addressLocality"),
            address.get("addressRegion"),
            address.get("addressCountry"),
        ]

        return ", ".join(str(piece) for piece in pieces if piece) or None

    return html_fragment_to_text(value.get("name") or address) or None


def extract_json_ld_job_metadata(
    soup: BeautifulSoup,
) -> dict[str, str | None]:
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
            raw_json = script.get_text(strip=True)

        if not raw_json:
            continue

        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            continue

        for node in walk_json(payload):
            if not is_job_posting_type(node.get("@type")):
                continue

            identifier = node.get("identifier")
            metadata = {
                "title": html_fragment_to_text(node.get("title")) or None,
                "company": organization_name(node.get("hiringOrganization")) or None,
                "location": location_text(node.get("jobLocation")) or None,
                "job_id": str(identifier.get("value")) if isinstance(identifier, dict) and identifier.get("value") else None,
            }
            requirements = node.get("applicantLocationRequirements")
            if isinstance(requirements, dict):
                requirements = [requirements]
            if not isinstance(requirements, list):
                requirements = []
            countries = [location_text(item) for item in requirements]
            countries = [country for country in countries if country]
            remote = str(node.get("jobLocationType") or "").upper() == "TELECOMMUTE"
            extra_location = ("Remote" if remote else "")
            if countries:
                extra_location += (" - " if extra_location else "") + ", ".join(countries)
            if extra_location:
                metadata["location"] = "; ".join(filter(None, [metadata["location"], extra_location]))
            for key in ("title", "company", "location", "job_id"):
                metadata[key + "_source"] = "jobposting_json_ld" if metadata[key] else None
            return metadata

    return {}


def linkedin_posting_id(url: str | None) -> str | None:
    """Require a numeric LinkedIn individual-posting URL, not a search page."""
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").casefold()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    match = re.fullmatch(r"/jobs/view/(\\d+)/?", parsed.path)
    return match.group(1) if match else None


def extract_linkedin_posting_metadata(
    soup: BeautifulSoup, url: str
) -> dict[str, str | None]:
    """Read only job-owned visible top-card fields on a specific posting URL.

    No title/company is inferred from the original job record, OG title,
    search snippets, job recommendations, or a generic LinkedIn results page.
    """
    posting_id = linkedin_posting_id(url)
    if posting_id is None:
        return {}

    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        canonical_id = linkedin_posting_id(str(canonical.get("href")))
        if canonical_id != posting_id:
            return {}

    title_element = soup.select_one(
        "h1.top-card-layout__title, "
        "h1.topcard__title, "
        "h1.job-details-jobs-unified-top-card__job-title"
    )
    company_element = soup.select_one(
        "a.topcard__org-name-link, "
        "a.top-card-layout__company-url, "
        ".job-details-jobs-unified-top-card__company-name a"
    )
    if title_element is None or company_element is None:
        return {}

    title = normalize_description_text(title_element.get_text(" ", strip=True))
    company = normalize_description_text(company_element.get_text(" ", strip=True))
    if not title or not company:
        return {}

    return {
        "title": title,
        "company": company,
        "title_source": "linkedin_posting_top_card",
        "company_source": "linkedin_posting_top_card",
    }


def extract_ats_metadata(soup: BeautifulSoup, url: str) -> dict:
    """Read posting-owned metadata only; never receives expected job fields."""
    metadata = extract_json_ld_job_metadata(soup)
    visible = extract_linkedin_posting_metadata(soup, url)
    if visible:
        # If structured metadata contradicts the visible posting top card,
        # never combine the fields into an apparently trustworthy identity.
        if (
            metadata.get("title")
            and metadata["title"].casefold() != visible["title"].casefold()
        ) or (
            metadata.get("company")
            and metadata["company"].casefold() != visible["company"].casefold()
        ):
            visible = {}
        for field in ("title", "company"):
            if visible and not metadata.get(field):
                metadata[field] = visible[field]
                metadata[field + "_source"] = visible[field + "_source"]
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    greenhouse = host in {"job-boards.greenhouse.io", "boards.greenhouse.io"}
    posting_match = re.fullmatch(r"/([^/]+)/jobs/(\d+)/?", parsed.path)
    if greenhouse and posting_match:
        for script in soup.find_all("script"):
            text = script.get_text()
            marker = "window.__remixContext = "
            if marker not in text:
                continue
            try:
                payload = json.JSONDecoder().raw_decode(text.split(marker, 1)[1])[0]
                loaders = payload["state"]["loaderData"]
                post = loaders.get("routes/$url_token_.jobs_.$job_post_id", {}).get("jobPost", {})
                # Bind embedded metadata to this specific public posting URL.
                public_url = post.get("public_url", "")
                if urlparse(public_url).path.rstrip("/") != parsed.path.rstrip("/"):
                    continue
                values = {"title": post.get("title"), "company": post.get("company_name"),
                          "location": post.get("job_post_location"), "job_id": posting_match.group(2)}
                for key, value in values.items():
                    if isinstance(value, dict):
                        value = value.get("name")
                    if isinstance(value, str) and value.strip() and not metadata.get(key):
                        metadata[key] = value.strip()
                        metadata[key + "_source"] = "greenhouse_posting_url" if key == "job_id" else "greenhouse_remix_jobPost"
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
    if (host.endswith(".myworkdayjobs.com") or host.endswith(".myworkdaysite.com")) and "/job/" in parsed.path:
        # An explicit employer statement in the posting is independent evidence.
        # Do not infer an employer name from a tenant slug or expected company.
        description = extract_json_ld_description(soup)
        match = re.search(r"(?:^|[.\n])\s*([A-Z][A-Za-z0-9&' -]{1,70}?) is an Equal Opportunity Employer\b", description)
        if match and not metadata.get("company"):
            metadata["company"] = match.group(1).strip()
            metadata["company_source"] = "workday_jobposting_employer_statement"
    return metadata


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
    selectors: tuple[str, ...] = DESCRIPTION_SELECTORS,
) -> str:
    candidates: list[str] = []

    for selector in selectors:
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


def extract_job_description_from_html(
    requested_url: str,
    final_url: str | None,
    http_status: int | None,
    html_text: str,
    extraction_method_prefix: str | None = None,
) -> JobDescriptionResult:
    """Turn already-fetched HTML into a normalized description result."""
    soup = BeautifulSoup(
        html_text,
        "html.parser",
    )
    metadata = extract_ats_metadata(soup, final_url or requested_url)

    if appears_to_be_block_page(soup):
        return JobDescriptionResult(
            requested_url=requested_url,
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
            resolved_title=metadata.get("title"),
            resolved_company=metadata.get("company"),
            resolved_location=metadata.get("location"),
            resolved_job_id=metadata.get("job_id"),
            resolved_title_source=metadata.get("title_source"),
            resolved_company_source=metadata.get("company_source"),
            resolved_location_source=metadata.get("location_source"),
            resolved_job_id_source=metadata.get("job_id_source"),
        )

    description = (
        extract_json_ld_description(soup)
    )

    extraction_method = "json_ld"

    if (
        count_words(description)
        < MINIMUM_USEFUL_DESCRIPTION_WORDS
    ):
        # When LinkedIn lacks JobPosting JSON-LD and identity comes from
        # a visible top card, its article/main may include unrelated jobs.
        # Only accept an explicitly posting-owned description container.
        selectors = (
            LINKEDIN_POSTING_DESCRIPTION_SELECTORS
            if metadata.get("title_source") == "linkedin_posting_top_card"
            or metadata.get("company_source") == "linkedin_posting_top_card"
            else DESCRIPTION_SELECTORS
        )
        selector_description = (
            extract_selector_description(soup, selectors)
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

    if extraction_method_prefix:
        extraction_method = (
            f"{extraction_method_prefix}_{extraction_method}"
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
            requested_url=requested_url,
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
            resolved_title=metadata.get("title"),
            resolved_company=metadata.get("company"),
            resolved_location=metadata.get("location"),
            resolved_job_id=metadata.get("job_id"),
            resolved_title_source=metadata.get("title_source"),
            resolved_company_source=metadata.get("company_source"),
            resolved_location_source=metadata.get("location_source"),
            resolved_job_id_source=metadata.get("job_id_source"),
        )

    return JobDescriptionResult(
        requested_url=requested_url,
        final_url=final_url,
        status="enriched",
        http_status=http_status,
        extraction_method=(
            extraction_method
        ),
        description=description,
        word_count=word_count,
        error_message=None,
        resolved_title=metadata.get("title"),
        resolved_company=metadata.get("company"),
        resolved_location=metadata.get("location"),
        resolved_job_id=metadata.get("job_id"),
        resolved_title_source=metadata.get("title_source"),
        resolved_company_source=metadata.get("company_source"),
        resolved_location_source=metadata.get("location_source"),
        resolved_job_id_source=metadata.get("job_id_source"),
    )


def is_unsafe_linkedin_posting_redirect(
    requested_url: str | None,
    final_url: str | None,
) -> bool:
    """Reject LinkedIn expired/search redirects and different posting IDs.

    The search page can contain a very long `main` element that looks like a
    JD by word count alone. Its text is not evidence for the requested job.
    """
    requested = urlparse(str(requested_url or ""))
    final = urlparse(str(final_url or ""))
    final_host = (final.hostname or "").casefold()
    if final_host != "linkedin.com" and not final_host.endswith(".linkedin.com"):
        return False

    path = final.path.rstrip("/").casefold()
    if "expired_jd_redirect" in final.query.casefold():
        return True
    if (
        path in {"/jobs", "/jobs/search"}
        or re.fullmatch(r"/jobs/[^/]+-jobs", path)
        or path.startswith(("/jobs/search/", "/jobs/collections/",
                             "/jobs/job-alerts"))
    ):
        return True

    # A redirect to a different LinkedIn job ID cannot establish that the
    # fetched description still represents the original exact posting.
    original_id = re.fullmatch(r"/jobs/view/(\d+)/?", requested.path)
    resolved_id = re.fullmatch(r"/jobs/view/(\d+)/?", final.path)
    if original_id and resolved_id and original_id.group(1) != resolved_id.group(1):
        return True
    return False


def fetch_job_description(
    url: str,
    client: httpx.Client,
    *,
    timeout_seconds: float | None = None,
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
        # Candidate requests use a short timeout; source/verified URLs keep their
        # existing default unless their caller explicitly opts in.
        response = (client.get(url) if timeout_seconds is None
                    else client.get(url, timeout=timeout_seconds))

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

    if is_unsafe_linkedin_posting_redirect(url, final_url):
        return JobDescriptionResult(
            requested_url=url,
            final_url=final_url,
            status="no_description",
            http_status=http_status,
            extraction_method=None,
            description="",
            word_count=0,
            error_message=(
                "Original LinkedIn posting redirected to a generic, expired, "
                "or different job page; do not reuse that page as its JD."
            ),
        )

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

    return extract_job_description_from_html(
        requested_url=url,
        final_url=final_url,
        http_status=http_status,
        html_text=response.text,
    )
