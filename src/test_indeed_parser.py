"""Checks for the Indeed alert parser.

The location checks run offline. The mailbox inspection at the end is skipped
when Yahoo credentials are unavailable.
"""

import os

from src.connectors.yahoo_imap import read_messages
from src.parsers.indeed import LOCATION_PATTERN, parse_indeed_email


# Real location strings observed in Indeed alert emails.
VALID_LOCATIONS = (
    "Remote",
    "Hybrid",
    "On-site",
    "Onsite",
    "San Diego, CA",
    "San Diego, CA 92101",
    "Washington, DC",
    "United States",
    "California",
    "New Hampshire",
    "Michigan",
    "Georgia",
    "District of Columbia",
    "Puerto Rico",
)

# Real title fragments that follow " - " and must never be read as locations,
# otherwise the parser splits a title into a bogus company and location.
INVALID_LOCATIONS = (
    "AI Trainer",
    "II",
    "IV",
    "Application Developer",
    "Databricks",
    "Federal Client",
    "AWS Data Platform (Redshift, Glue, and Airflow)",
    "Biotech & Life Sciences",
    "P&C Insurance",
    "Analytics",
    "Senior Data Analyst",
)

SAMPLE_EMAIL = """
Senior Data Engineer
innoVet Health, LLC - United States
From $130,000 a year
Easily apply
Gather and translate business requirements.
https://www.indeed.com/rc/clk?jk=aaaaaaaaaaaaaaaa
Analytics Engineer - II
Volto - Remote
$100 - $110 an hour
Easily apply
6+ years of professional experience in data analytics.
https://www.indeed.com/rc/clk?jk=bbbbbbbbbbbbbbbb
Senior Quantitative Analyst - AI Trainer
DataAnnotation - New Hampshire
$50 - $100 an hour
https://www.indeed.com/rc/clk?jk=cccccccccccccccc
"""


def check_location_pattern() -> None:
    for location in VALID_LOCATIONS:
        assert LOCATION_PATTERN.fullmatch(location), (
            f"Location should be accepted: {location}"
        )

    for value in INVALID_LOCATIONS:
        assert not LOCATION_PATTERN.fullmatch(value), (
            f"Title fragment should be rejected: {value}"
        )

    print(
        f"Location pattern: {len(VALID_LOCATIONS)} accepted, "
        f"{len(INVALID_LOCATIONS)} rejected."
    )


def check_sample_email() -> None:
    jobs = parse_indeed_email(SAMPLE_EMAIL)

    parsed = [
        (job["title"], job["company_name"], job["location"])
        for job in jobs
    ]

    expected = [
        (
            "Senior Data Engineer",
            "innoVet Health, LLC",
            "United States",
        ),
        (
            "Analytics Engineer - II",
            "Volto",
            "Remote",
        ),
        (
            "Senior Quantitative Analyst - AI Trainer",
            "DataAnnotation",
            "New Hampshire",
        ),
    ]

    assert parsed == expected, parsed

    print(f"Sample email: {len(jobs)} jobs parsed as expected.")


def inspect_mailbox() -> None:
    try:
        messages = read_messages(
            folder_name="jobs/Indeed",
            limit=1,
            unread_only=False,
        )
    except ValueError as error:
        print(f"Skipping mailbox inspection: {error}")
        return

    if not messages:
        print("No Indeed messages found.")
        return

    message = messages[0]
    jobs = parse_indeed_email(message["text"])

    print(f"\nEmail subject: {message['subject']}")
    print(f"Jobs extracted: {len(jobs)}")

    for number, job in enumerate(jobs, start=1):
        print(f"\nJob {number}")
        print(f"Title: {job['title']}")
        print(f"Company: {job['company_name']}")
        print(f"Location: {job['location']}")
        print(f"Salary: {job['salary_text'] or 'Not provided'}")
        print(f"Description: {job['description'][:300]}")
        print(f"URL: {job['apply_url'][:150]}...")


def main() -> None:
    check_location_pattern()
    check_sample_email()

    if os.getenv("RUN_LIVE_EMAIL_TESTS") == "1":
        inspect_mailbox()
        return

    print("Skipping live Yahoo mailbox inspection.")


if __name__ == "__main__":
    main()
