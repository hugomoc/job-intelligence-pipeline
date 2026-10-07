import os

from src.connectors.yahoo_imap import read_messages
from src.parsers.glassdoor import parse_glassdoor_email


GLASSDOOR_FOLDER = "jobs/Glassdoor"
JOB_LINK = (
    "https://www.glassdoor.com/partner/jobListing.htm?"
    "jobListingId={job_id}"
)
SUPER_MATCH_LINK = (
    "https://www.glassdoor.com/recruiter/link/matches?"
    "matchId=abc&jobListingId={job_id}"
)


def test_parse_glassdoor_email_with_extra_job_link() -> None:
    text = """
Your job listings for
Data Engineer
San Diego, CA
Road Scholar
Senior Analyst, Direct Mail Acquisition
San Diego, CA
Acme Data
4.2 ★
Data Engineer
Remote
$100K - $130K a year
See more jobs
"""

    jobs = parse_glassdoor_email(
        text=text,
        links=[
            JOB_LINK.format(job_id="1001"),
            JOB_LINK.format(job_id="1002"),
            JOB_LINK.format(job_id="extra"),
        ],
    )

    assert len(jobs) == 2
    assert jobs[0]["title"] == "Senior Analyst, Direct Mail Acquisition"
    assert jobs[0]["source_job_id"] == "1001"
    assert jobs[1]["title"] == "Data Engineer"
    assert jobs[1]["source_job_id"] == "1002"


def test_parse_glassdoor_super_match_email() -> None:
    text = """
New roles to put on your radar
Hugo, your top job matches are in
You matched with
new roles
at Liquidity Services, California Coast Credit Union, Tech2i and 5 more.
Great fit for the Data Engineer role at Liquidity Services, Inc.
Liquidity Services
Data Engineer
United States
$136K – $153K/yr (Employer est.)
84%
Excellent
Job match
3.8
★
Company
rating
Review 9 new matches
"""

    jobs = parse_glassdoor_email(
        text=text,
        links=[
            "https://www.glassdoor.com/recruiter/link/matches",
            SUPER_MATCH_LINK.format(job_id="2001"),
        ],
    )

    assert len(jobs) == 1
    assert jobs[0]["company_name"] == "Liquidity Services"
    assert jobs[0]["title"] == "Data Engineer"
    assert jobs[0]["location"] == "United States"
    assert jobs[0]["salary_text"] == "$136K – $153K/yr (Employer est.)"
    assert jobs[0]["source_job_id"] == "2001"


def main() -> None:
    test_parse_glassdoor_email_with_extra_job_link()
    test_parse_glassdoor_super_match_email()

    if os.getenv("RUN_LIVE_EMAIL_TESTS") != "1":
        print("Glassdoor parser offline tests passed.")
        print("Skipping live Yahoo mailbox inspection.")
        return

    messages = read_messages(
        folder_name=GLASSDOOR_FOLDER,
        limit=1,
        unread_only=False,
    )

    if not messages:
        print("No Glassdoor emails found.")
        return

    message = messages[0]

    jobs = parse_glassdoor_email(
        text=message["text"],
        links=message["links"],
    )

    print(f"Email subject: {message['subject']}")
    print(f"Jobs extracted: {len(jobs)}")

    for number, job in enumerate(jobs, start=1):
        print(f"\nJob {number}")
        print(f"Title: {job['title']}")
        print(f"Company: {job['company_name']}")
        print(f"Location: {job['location']}")
        print(
            f"Salary: "
            f"{job['salary_text'] or 'Not provided'}"
        )
        print(
            f"Source job ID: "
            f"{job['source_job_id'] or 'Not provided'}"
        )
        print(f"URL: {job['apply_url'][:150]}...")


if __name__ == "__main__":
    main()
