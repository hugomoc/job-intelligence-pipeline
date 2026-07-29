from email import message_from_bytes
from pathlib import Path

from src.connectors.yahoo_imap import extract_email_content, extract_links
from src.parsers.linkedin import parse_linkedin_email


SANITIZED_LINKEDIN_HTML = """
<html>
  <body>
    <a href="https://www.linkedin.com/feed/">LinkedIn</a>
    <a href="https://www.linkedin.com/comm/jobs/view/1001/?tracking=safe"></a>
    <a href="https://www.linkedin.com/comm/jobs/view/1001/?tracking=safe">
      AWS Data Engineer
      AgileGrid Solutions · United States (Remote)
    </a>
    <a href="https://www.linkedin.com/comm/jobs/view/1001/?tracking=safe">
      AWS Data Engineer
    </a>
    <a href="https://www.linkedin.com/comm/jobs/view/1002/?tracking=safe">
      Analytics Engineer
      American Public Education, Inc. (APEI) · United States (Remote)
      Actively recruiting
    </a>
    <a href="https://www.linkedin.com/comm/jobs/view/1003/?tracking=safe">
      Data Engineer
      Haystack · United States (Remote)
      Actively recruiting
    </a>
    <a href="https://www.linkedin.com/comm/jobs/view/1004/?tracking=safe">
      Data Engineer, Payments
      Sundayy · United States (Remote)
    </a>
    <a href="https://www.linkedin.com/comm/jobs/view/1005/?tracking=safe">
      Senior Data Engineer
      Haystack · Los Angeles, CA (Remote)
      Actively recruiting
    </a>
    <a href="https://www.linkedin.com/comm/jobs/view/1006/?tracking=safe">
      Senior Data Science Engineer I, Claims
      Turquoise · San Diego, CA (Remote)
      $170K-$190K / year
    </a>
    <a href="https://www.linkedin.com/premium/">Try Premium</a>
    <a href="https://www.linkedin.com/comm/jobs/alerts/">Manage alerts</a>
    <a href="https://www.linkedin.com/help/linkedin/answer/a1339724">Unsubscribe</a>
  </body>
</html>
"""


def test_six_jobs_and_duplicate_links_collapse() -> None:
    jobs = parse_linkedin_email(
        html=SANITIZED_LINKEDIN_HTML,
        text="",
        links=[],
    )

    assert len(jobs) == 6
    assert {job["source_job_id"] for job in jobs} == {
        "1001",
        "1002",
        "1003",
        "1004",
        "1005",
        "1006",
    }


def test_linkedin_fields_are_normalized() -> None:
    jobs = parse_linkedin_email(
        html=SANITIZED_LINKEDIN_HTML,
        text="",
        links=[],
    )

    first_job = jobs[0]
    salary_job = jobs[-1]

    assert first_job["title"] == "AWS Data Engineer"
    assert first_job["company_name"] == "AgileGrid Solutions"
    assert first_job["location"] == "United States (Remote)"
    assert first_job["posted_age_text"] is None
    assert first_job["apply_url"] == "https://www.linkedin.com/jobs/view/1001/"

    assert salary_job["salary_text"] == "$170K-$190K / year"

    for job in jobs:
        combined_text = " ".join(
            str(value)
            for value in job.values()
            if value is not None
        ).casefold()
        assert "actively recruiting" not in combined_text
        assert "premium" not in combined_text
        assert "unsubscribe" not in combined_text


def test_real_fixture_when_available() -> None:
    fixture_path = Path("tests/fixtures/linkedin_job_alert.eml")

    if not fixture_path.exists():
        return

    message = message_from_bytes(fixture_path.read_bytes())
    text, html = extract_email_content(message)
    jobs = parse_linkedin_email(
        text=text,
        html=html,
        links=extract_links(html, text),
    )

    assert len(jobs) == 6
    assert len({job["source_job_id"] for job in jobs}) == 6
    assert all(job["posted_age_text"] is None for job in jobs)


def main() -> None:
    test_six_jobs_and_duplicate_links_collapse()
    test_linkedin_fields_are_normalized()
    test_real_fixture_when_available()
    print("LinkedIn parser tests passed.")


if __name__ == "__main__":
    main()
