"""Resolver for JobDiva-powered company career portals."""

from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import dataclass

from playwright.async_api import Frame, TimeoutError as PlaywrightTimeoutError

from src.browser.playwright_client import PlaywrightClient
from src.enrichment.job_description import (
    count_words,
    normalize_description_text,
)
from src.resolvers.job_matching import score_title


@dataclass(frozen=True)
class JobDivaResolutionResult:
    title: str | None
    company: str | None
    location: str | None
    salary: str | None
    employment_type: str | None
    description: str | None
    source_url: str
    resolved_url: str | None
    extraction_method: str | None
    description_word_count: int


class JobDivaResolverError(Exception):
    pass


def log(message: str) -> None:
    print(message)


def extract_rate_or_salary(text: str) -> str | None:
    normalized = normalize_description_text(text)

    patterns = (
        r"Rate\s*[\u2013:-]\s*([^\n]+)",
        r"\$[\d,.]+\s*(?:to|-|\u2013)\s*\$?[\d,.]+\s*(?:/hr|/hour|w2)?",
        r"\$[\d,.]+K?\s*(?:-|\u2013)\s*\$?[\d,.]+K?\s*/\s*(?:yr|year|hr|hour)",
    )

    for pattern in patterns:
        match = re.search(
            pattern,
            normalized,
            flags=re.IGNORECASE,
        )

        if match:
            return normalize_description_text(match.group(1) if match.groups() else match.group(0))

    return None


def extract_employment_type(text: str) -> str | None:
    normalized = normalize_description_text(text)

    duration_match = re.search(
        r"Duration\s*[\u2013:-]\s*([^\n]+)",
        normalized,
        flags=re.IGNORECASE,
    )

    if duration_match:
        return normalize_description_text(duration_match.group(1))

    for value in (
        "Contract to Hire",
        "Direct Placement",
        "Full Time/Contract",
        "Part Time",
        "Contract",
    ):
        if value.casefold() in normalized.casefold():
            return value

    return None


def extract_jobdiva_detail(
    text: str,
    expected_company: str,
) -> JobDivaResolutionResult:
    lines = [
        line
        for line in normalize_description_text(text).splitlines()
        if line.strip()
    ]

    try:
        description_index = next(
            index
            for index, line in enumerate(lines)
            if line.casefold() == "job description"
        )
    except StopIteration as error:
        raise JobDivaResolverError(
            "JobDiva detail page did not contain a Job Description section."
        ) from error

    title = None
    location = None

    for index, line in enumerate(lines[:description_index]):
        if "#" in line:
            title = normalize_description_text(line.split("#", maxsplit=1)[0])
            if index + 1 < description_index:
                location = lines[index + 1]
            break

    description = normalize_description_text(
        "\n".join(lines[description_index + 1 :])
    )

    return JobDivaResolutionResult(
        title=title,
        company=expected_company,
        location=location,
        salary=extract_rate_or_salary(description),
        employment_type=extract_employment_type(description),
        description=description or None,
        source_url="",
        resolved_url=None,
        extraction_method="jobdiva_dom",
        description_word_count=count_words(description),
    )


async def find_jobdiva_frame(
    client: PlaywrightClient,
    url: str,
) -> Frame:
    page = await client.new_page()
    await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=60_000,
    )

    await page.wait_for_timeout(6_000)

    for frame in page.frames:
        if "jobdiva.com/portal" in frame.url:
            return frame

    raise JobDivaResolverError(
        "No JobDiva portal iframe was found on the careers page."
    )


async def click_matching_details(
    frame: Frame,
    expected_title: str,
) -> None:
    search_input = frame.locator(
        "input[placeholder='Search job title or keyword...']"
    ).first

    try:
        await search_input.wait_for(
            state="visible",
            timeout=30_000,
        )
    except PlaywrightTimeoutError as error:
        page_text = ""

        try:
            page_text = await frame.locator("body").inner_text(timeout=5_000)
        except Exception:
            pass

        raise JobDivaResolverError(
            "JobDiva search input was not found. "
            f"Page preview: {page_text[:1_000]}"
        ) from error

    if await search_input.count() == 0:
        raise JobDivaResolverError(
            "JobDiva search input was not found."
        )

    await search_input.fill(expected_title)
    await frame.get_by_role(
        "button",
        name="Search Jobs",
    ).last.click()
    await frame.page.wait_for_timeout(6_000)

    page_text = await frame.locator("body").inner_text(timeout=10_000)

    if expected_title.casefold() not in page_text.casefold():
        raise JobDivaResolverError(
            "The expected job title was not found in JobDiva search results."
        )

    detail_buttons = frame.get_by_role(
        "button",
        name="Details",
    )

    for index in range(await detail_buttons.count()):
        button = detail_buttons.nth(index)

        try:
            row_text = await button.evaluate(
                """
                (element, expectedTitle) => {
                    let current = element.parentElement;
                    for (let i = 0; current && i < 8; i += 1) {
                        const text = current.innerText || '';
                        if (
                            text.includes('Details')
                            && text.toLowerCase().includes(expectedTitle.toLowerCase())
                        ) return text;
                        current = current.parentElement;
                    }
                    return element.innerText || '';
                }
                """,
                expected_title,
            )
        except Exception:
            row_text = ""

        if score_title(expected_title, row_text) < 75:
            continue

        await button.click()
        return

    raise JobDivaResolverError(
        "No Details button matched the expected job title."
    )


async def resolve_jobdiva_careers_job_with_client(
    client: PlaywrightClient,
    careers_url: str,
    title: str,
    company: str,
) -> JobDivaResolutionResult:
    log(f"Opening company careers page: {careers_url}")
    frame = await find_jobdiva_frame(
        client=client,
        url=careers_url,
    )

    log(f"Searching JobDiva portal for: {title}")
    await click_matching_details(
        frame=frame,
        expected_title=title,
    )

    try:
        await frame.page.wait_for_load_state(
            "networkidle",
            timeout=10_000,
        )
    except PlaywrightTimeoutError:
        pass

    await frame.page.wait_for_timeout(4_000)

    detail_text = await frame.locator("body").inner_text(timeout=10_000)
    result = extract_jobdiva_detail(
        text=detail_text,
        expected_company=company,
    )

    if not result.description:
        raise JobDivaResolverError(
            "JobDiva detail page did not yield a description."
        )

    return JobDivaResolutionResult(
        title=result.title,
        company=result.company,
        location=result.location,
        salary=result.salary,
        employment_type=result.employment_type,
        description=result.description,
        source_url=careers_url,
        resolved_url=frame.url,
        extraction_method=result.extraction_method,
        description_word_count=result.description_word_count,
    )


async def resolve_jobdiva_careers_job(
    careers_url: str,
    title: str,
    company: str,
    headed: bool = False,
) -> JobDivaResolutionResult:
    client = PlaywrightClient(headless=not headed)
    await client.start()

    try:
        return await resolve_jobdiva_careers_job_with_client(
            client=client,
            careers_url=careers_url,
            title=title,
            company=company,
        )
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve one public JobDiva careers posting."
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(
        resolve_jobdiva_careers_job(
            careers_url=args.url,
            title=args.title,
            company=args.company,
            headed=args.headed,
        )
    )

    print("\nResult:")
    print(f"Title: {result.title}")
    print(f"Company: {result.company}")
    print(f"Location: {result.location}")
    print(f"Salary: {result.salary}")
    print(f"Employment type: {result.employment_type}")
    print(f"Extraction method: {result.extraction_method}")
    print(f"Description length: {result.description_word_count}")
    print(f"Resolved URL: {result.resolved_url}")

    if args.verbose and result.description:
        print("\nDescription:")
        print(result.description)


if __name__ == "__main__":
    main()
