"""Extractor for JobLeads job-detail pages reached from resolvers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Page

from src.enrichment.job_description import (
    count_words,
    html_fragment_to_text,
    normalize_description_text,
    walk_json,
)


@dataclass(frozen=True)
class JobLeadsJob:
    title: str | None
    company: str | None
    location: str | None
    salary: str | None
    employment_type: str | None
    description: str | None
    extraction_method: str | None


def is_job_posting_type(value: Any) -> bool:
    if isinstance(value, str):
        return value.casefold() == "jobposting"

    if isinstance(value, list):
        return any(is_job_posting_type(item) for item in value)

    return False


def value_to_text(value: Any) -> str | None:
    if value is None:
        return None

    text = html_fragment_to_text(value)

    return text or None


def organization_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value_to_text(value.get("name"))

    return value_to_text(value)


def location_text(value: Any) -> str | None:
    if isinstance(value, list):
        pieces = [
            location_text(item)
            for item in value
        ]

        return ", ".join(piece for piece in pieces if piece) or None

    if not isinstance(value, dict):
        return value_to_text(value)

    address = value.get("address")

    if isinstance(address, dict):
        parts = [
            address.get("addressLocality"),
            address.get("addressRegion"),
            address.get("addressCountry"),
        ]

        return ", ".join(
            str(part)
            for part in parts
            if part
        ) or None

    return value_to_text(value.get("name") or address)


def salary_text(value: Any) -> str | None:
    if not isinstance(value, dict):
        return value_to_text(value)

    value_node = value.get("value")

    if isinstance(value_node, dict):
        min_value = value_node.get("minValue")
        max_value = value_node.get("maxValue")
        unit = value_node.get("unitText")
        currency = value.get("currency")

        if min_value and max_value:
            salary = f"{min_value} - {max_value}"
        else:
            salary = str(min_value or max_value or "")

        if currency:
            salary = f"{currency} {salary}"

        if unit:
            salary = f"{salary} / {unit}"

        return salary.strip() or None

    return value_to_text(value)


def clean_description(value: str | None) -> str | None:
    description = normalize_description_text(value or "")
    description = re.sub(
        r"^job description\s*",
        "",
        description,
        flags=re.IGNORECASE,
    ).strip()

    return description or None


async def extract_json_ld_job(page: Page) -> JobLeadsJob | None:
    scripts = await page.locator(
        "script[type='application/ld+json']"
    ).all_text_contents()

    for raw_json in scripts:
        if not raw_json.strip():
            continue

        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            continue

        for node in walk_json(payload):
            if not is_job_posting_type(node.get("@type")):
                continue

            description = clean_description(
                value_to_text(node.get("description"))
            )

            if not description:
                continue

            return JobLeadsJob(
                title=value_to_text(node.get("title")),
                company=organization_name(node.get("hiringOrganization")),
                location=location_text(node.get("jobLocation")),
                salary=salary_text(node.get("baseSalary")),
                employment_type=value_to_text(
                    node.get("employmentType")
                ),
                description=description,
                extraction_method="json_ld",
            )

    return None


async def nearby_text(page: Page, label: str) -> str | None:
    locator = page.get_by_text(label, exact=True)

    if await locator.count() == 0:
        return None

    try:
        text = await locator.first.evaluate(
            """
            element => {
                const parent = element.parentElement;
                if (!parent) return '';
                return parent.innerText || '';
            }
            """
        )
    except Exception:
        return None

    lines = [
        normalize_description_text(line)
        for line in str(text).splitlines()
        if normalize_description_text(line)
    ]

    for index, line in enumerate(lines):
        if line.casefold() == label.casefold() and index + 1 < len(lines):
            return lines[index + 1]

    return None


async def extract_dom_description(page: Page) -> str | None:
    candidates: list[str] = []

    for selector in (
        "[itemprop='description']",
        "[data-testid='job-description']",
        ".job-description",
        ".jobDescription",
        "article",
        "main",
    ):
        locator = page.locator(selector)

        for index in range(min(await locator.count(), 5)):
            try:
                text = await locator.nth(index).inner_text(timeout=2_000)
            except Exception:
                continue

            text = clean_description(text)

            if text and count_words(text) >= 40:
                candidates.append(text)

    if not candidates:
        return None

    return max(candidates, key=count_words)


async def extract_jobleads_job(page: Page) -> JobLeadsJob:
    json_ld_job = await extract_json_ld_job(page)

    if json_ld_job:
        return json_ld_job

    description = await extract_dom_description(page)

    title = None

    if await page.locator("h1").count():
        title = normalize_description_text(
            await page.locator("h1").first.inner_text()
        )

    return JobLeadsJob(
        title=title,
        company=await nearby_text(page, "Company"),
        location=await nearby_text(page, "Location"),
        salary=await nearby_text(page, "Salary"),
        employment_type=await nearby_text(page, "Employment type"),
        description=description,
        extraction_method="dom" if description else None,
    )
