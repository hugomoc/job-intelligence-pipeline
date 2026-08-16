"""Resolver for Lensa links that open multi-job pages.

The resolver finds the intended Lensa card, clicks its Read more action, follows
the JobLeads page, and extracts the richer job description when possible.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from src.browser.playwright_client import PlaywrightClient
from src.resolvers.job_matching import (
    CandidateMatch,
    score_candidate,
    select_best_candidate,
)
from src.resolvers.jobleads_resolver import (
    JobLeadsJob,
    extract_jobleads_job,
)


@dataclass(frozen=True)
class LensaResolutionResult:
    title: str | None
    company: str | None
    location: str | None
    salary: str | None
    employment_type: str | None
    description: str | None
    source: str
    source_url: str
    resolved_url: str | None
    extraction_method: str | None
    selected_reason: str | None
    candidate_count: int


@dataclass
class LensaCardCandidate:
    index: int
    locator: Locator
    text: str
    title: str
    company: str
    match: CandidateMatch


class LensaResolverError(Exception):
    pass


def log(message: str) -> None:
    print(message)


async def accept_cookie_modal(page: Page) -> None:
    for pattern in (
        re.compile("accept all", re.IGNORECASE),
        re.compile("accept", re.IGNORECASE),
        re.compile("agree", re.IGNORECASE),
    ):
        try:
            button = page.get_by_role("button", name=pattern)

            if await button.count():
                await button.first.click(timeout=2_000)
                return
        except Exception:
            continue


def line_candidates(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]


def best_line_for_title(
    expected_title: str,
    lines: list[str],
) -> str:
    scored = [
        (
            score_candidate(
                expected_title=expected_title,
                expected_company="placeholder",
                candidate_title=line,
                candidate_company="placeholder",
            ).title_score,
            line,
        )
        for line in lines
        if len(line) >= 4
    ]

    if not scored:
        return ""

    return max(scored, key=lambda item: item[0])[1]


def best_line_for_company(
    expected_company: str,
    lines: list[str],
) -> str:
    scored = [
        (
            score_candidate(
                expected_title="placeholder",
                expected_company=expected_company,
                candidate_title="placeholder",
                candidate_company=line,
            ).company_score,
            line,
        )
        for line in lines
        if len(line) >= 2
    ]

    if not scored:
        return ""

    return max(scored, key=lambda item: item[0])[1]


async def candidate_locators(page: Page) -> list[Locator]:
    locators: list[Locator] = []

    for selector in (
        "article",
        "li",
        "section",
        "[role='listitem']",
        "div",
    ):
        locator = page.locator(selector).filter(
            has_text=re.compile("read more", re.IGNORECASE)
        )

        for index in range(min(await locator.count(), 200)):
            locators.append(locator.nth(index))

        if locators:
            break

    return locators


async def collect_candidates(
    page: Page,
    expected_title: str,
    expected_company: str,
) -> list[LensaCardCandidate]:
    candidates: list[LensaCardCandidate] = []

    for index, locator in enumerate(await candidate_locators(page)):
        try:
            text = await locator.inner_text(timeout=2_000)
        except Exception:
            continue

        lines = line_candidates(text)

        if not lines:
            continue

        title = best_line_for_title(
            expected_title=expected_title,
            lines=lines,
        )
        company = best_line_for_company(
            expected_company=expected_company,
            lines=lines,
        )

        match = score_candidate(
            expected_title=expected_title,
            expected_company=expected_company,
            candidate_title=title,
            candidate_company=company,
        )

        candidates.append(
            LensaCardCandidate(
                index=index,
                locator=locator,
                text=text,
                title=title,
                company=company,
                match=match,
            )
        )

    return candidates


async def click_read_more(
    page: Page,
    candidate: LensaCardCandidate,
) -> Page:
    read_more = candidate.locator.get_by_text(
        re.compile("read more", re.IGNORECASE)
    ).first

    if await read_more.count() == 0:
        raise LensaResolverError("Read more link was not found in selected card.")

    original_url = page.url

    try:
        async with page.context.expect_page(timeout=5_000) as popup_info:
            await read_more.click()

        destination_page = await popup_info.value
        await destination_page.wait_for_load_state("domcontentloaded")
        log("Destination opened in new tab")

        return destination_page

    except PlaywrightTimeoutError:
        try:
            async with page.expect_navigation(
                wait_until="domcontentloaded",
                timeout=10_000,
            ):
                await read_more.click()
        except PlaywrightTimeoutError:
            if page.url == original_url:
                raise LensaResolverError(
                    "Read more click did not open a destination page."
                )

        log("Destination opened in current tab")
        return page


async def resolve_lensa_job_with_client(
    client: PlaywrightClient,
    url: str,
    title: str,
    company: str,
) -> LensaResolutionResult | None:
    log(f"Resolving Lensa job: {title} / {company}")
    log("Opening Lensa URL")

    page = await client.new_page()
    await page.goto(url, wait_until="domcontentloaded")
    await accept_cookie_modal(page)

    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        pass

    candidates = await collect_candidates(
        page=page,
        expected_title=title,
        expected_company=company,
    )
    log(f"Found {len(candidates)} Lensa job card candidates")

    for candidate in sorted(
        candidates,
        key=lambda item: item.match.score,
        reverse=True,
    )[:10]:
        log(
            "Candidate score "
            f"{candidate.match.score}: "
            f"{candidate.title} / {candidate.company} "
            f"({candidate.match.reason})"
        )

    best_match = select_best_candidate(
        [candidate.match for candidate in candidates]
    )

    if best_match is None:
        log("No reliable Lensa match was selected.")
        return None

    selected = next(
        candidate
        for candidate in candidates
        if candidate.match == best_match
    )
    log(
        "Selected: "
        f"{selected.title} / {selected.company} "
        f"({selected.match.reason})"
    )
    log("Clicking Read more")

    destination_page = await click_read_more(
        page=page,
        candidate=selected,
    )

    try:
        await destination_page.wait_for_load_state(
            "networkidle",
            timeout=10_000,
        )
    except PlaywrightTimeoutError:
        pass

    resolved_url = destination_page.url
    log(f"Resolved destination: {resolved_url}")

    job: JobLeadsJob = await extract_jobleads_job(destination_page)

    if not job.description:
        log("JobLeads page loaded, but no description was extracted.")
    else:
        log(
            "Extracted description: "
            f"{len(job.description)} characters"
        )

    return LensaResolutionResult(
        title=job.title,
        company=job.company,
        location=job.location,
        salary=job.salary,
        employment_type=job.employment_type,
        description=job.description,
        source="lensa",
        source_url=url,
        resolved_url=resolved_url,
        extraction_method=job.extraction_method,
        selected_reason=selected.match.reason,
        candidate_count=len(candidates),
    )


async def resolve_lensa_job(
    url: str,
    title: str,
    company: str,
    headless: bool | None = None,
) -> LensaResolutionResult | None:
    delay_seconds = float(
        os.getenv("JOB_RESOLVER_DELAY_SECONDS", "1")
    )

    async with PlaywrightClient(headless=headless) as client:
        result = await resolve_lensa_job_with_client(
            client=client,
            url=url,
            title=title,
            company=company,
        )
        await asyncio.sleep(delay_seconds)

        return result
