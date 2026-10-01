from src.parsers.ladders import parse_ladders_email


SANITIZED_LADDERS_JOB_HTML = """
<html>
  <body>
    <p>Recommended jobs for you</p>
    <p>Acme Analytics</p>
    <a href="https://www.theladders.com/job/data-engineer-example">
      Data Engineer
    </a>
    <p>San Diego, CA</p>
    <p>$130K - $155K</p>
    <a href="https://www.theladders.com/privacy">Privacy</a>
    <a href="https://www.theladders.com/unsubscribe">Unsubscribe</a>
  </body>
</html>
"""


SANITIZED_LADDERS_MARKETING_TEXT = """
Hugo,
Your resume review is complete.
See Your Resume Results Now
Try Ladders Premium
Privacy
Terms & Conditions
Unsubscribe
"""


def test_single_job_card_extraction() -> None:
    jobs = parse_ladders_email(
        html=SANITIZED_LADDERS_JOB_HTML,
        text="",
        links=[],
    )

    assert len(jobs) == 1

    job = jobs[0]
    assert job["source"] == "ladders"
    assert job["title"] == "Data Engineer"
    assert job["company_name"] == "Acme Analytics"
    assert job["location"] == "San Diego, CA"
    assert job["salary_text"] == "$130K - $155K"
    assert job["description"] is None
    assert job["apply_url"].endswith(
        "/job/data-engineer-example"
    )


def test_marketing_email_is_ignored() -> None:
    jobs = parse_ladders_email(
        text=SANITIZED_LADDERS_MARKETING_TEXT,
        html=None,
        links=[
            "https://t.ladders.co/f/a/example",
        ],
    )

    assert jobs == []


def test_missing_link_fails_safely() -> None:
    jobs = parse_ladders_email(
        text="""
        Acme Analytics
        Data Engineer
        San Diego, CA
        $130K - $155K
        """,
        html=None,
        links=[],
    )

    assert jobs == []


def test_punctuation_title_is_ignored() -> None:
    jobs = parse_ladders_email(
        text="""
        Apple
        |
        Cupertino, CA
        $115K - $135K*
        """,
        html=None,
        links=[
            "https://t.ladders.co/f/a/example",
        ],
    )

    assert jobs == []


def main() -> None:
    test_single_job_card_extraction()
    test_marketing_email_is_ignored()
    test_missing_link_fails_safely()
    test_punctuation_title_is_ignored()
    print("Ladders parser tests passed.")


if __name__ == "__main__":
    main()
