from email import message_from_bytes
from pathlib import Path

from src.connectors.yahoo_imap import extract_email_content, extract_links
from src.database import create_job_fingerprint
from src.parsers.ziprecruiter import parse_ziprecruiter_email


SANITIZED_ZIPRECRUITER_HTML = """
<html>
  <body>
    <a href="https://www.ziprecruiter.com/jobs">Logo</a>
    <p>I recently found a Inventory Analyst job at Volt for you!</p>
    <a href="https://www.ziprecruiter.com/km/title-redirect?safe=1">
      Inventory Analyst
    </a>
    <ul>
      <li>Volt</li>
      <li>San Diego, CA</li>
      <li>$39 / hr</li>
      <li>Full-Time</li>
      <li>Medical, Vision, Dental, Life, Retirement</li>
      <li>New</li>
    </ul>
    <a href="https://www.ziprecruiter.com/km/apply-redirect?safe=2">
      1-Click Apply
    </a>
    <a href="https://www.ziprecruiter.com/km/title-redirect?safe=1">
      View Details
    </a>
    <a href="https://www.ziprecruiter.com/km/more-jobs?safe=3">
      View More Jobs
    </a>
    <a href="https://www.ziprecruiter.com/privacy">Privacy Policy</a>
    <a href="https://www.ziprecruiter.com/unsubscribe">Unsubscribe</a>
  </body>
</html>
"""


def test_single_recommendation_extraction() -> None:
    jobs = parse_ziprecruiter_email(
        html=SANITIZED_ZIPRECRUITER_HTML,
        text="",
        links=[],
    )

    assert len(jobs) == 1

    job = jobs[0]
    assert job["title"] == "Inventory Analyst"
    assert job["company_name"] == "Volt"
    assert job["location"] == "San Diego, CA"
    assert job["salary_text"] == "$39 / hr"
    assert job["posted_age_text"] == "New"
    assert job["source_job_id"] is None
    assert job["description"] is None
    assert job["apply_url"].endswith("/km/apply-redirect?safe=2")


def test_fingerprint_fallback_is_stable() -> None:
    job = parse_ziprecruiter_email(
        html=SANITIZED_ZIPRECRUITER_HTML,
        text="",
        links=[],
    )[0]

    assert create_job_fingerprint(job) == create_job_fingerprint(job)


def test_malformed_candidate_fails_safely() -> None:
    jobs = parse_ziprecruiter_email(
        html="""
        <a href="https://www.ziprecruiter.com/km/title-redirect?safe=1">
          Inventory Analyst
        </a>
        <a href="https://www.ziprecruiter.com/km/apply-redirect?safe=2">
          1-Click Apply
        </a>
        """,
        text="",
        links=[],
    )

    assert jobs == []


def test_footer_links_are_excluded() -> None:
    job = parse_ziprecruiter_email(
        html=SANITIZED_ZIPRECRUITER_HTML,
        text="",
        links=[],
    )[0]

    assert "privacy" not in job["apply_url"]
    assert "unsubscribe" not in job["apply_url"]
    assert "more-jobs" not in job["apply_url"]


def test_real_fixture_when_available() -> None:
    fixture_path = Path("tests/fixtures/ziprecruiter_job_alert.eml")

    if not fixture_path.exists():
        return

    message = message_from_bytes(fixture_path.read_bytes())
    text, html = extract_email_content(message)
    jobs = parse_ziprecruiter_email(
        text=text,
        html=html,
        links=extract_links(html, text),
    )

    assert len(jobs) == 1

    job = jobs[0]
    assert job["title"] == "Inventory Analyst"
    assert job["company_name"] == "Volt"
    assert job["location"] == "San Diego, CA"
    assert job["salary_text"] == "$39 / hr"
    assert job["posted_age_text"] == "New"
    assert job["source_job_id"] is None
    assert "?" in job["apply_url"]


def main() -> None:
    test_single_recommendation_extraction()
    test_fingerprint_fallback_is_stable()
    test_malformed_candidate_fails_safely()
    test_footer_links_are_excluded()
    test_real_fixture_when_available()
    print("ZipRecruiter parser tests passed.")


if __name__ == "__main__":
    main()
