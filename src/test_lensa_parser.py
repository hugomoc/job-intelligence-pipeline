from src.parsers.lensa import parse_lensa_email


SANITIZED_LENSA_TEXT = """
Just in: new jobs from 52+ job boards

logo.webp) | LTM
---|---
nCino Developer
$78K-$95K / yr. (est.)
Full-Time• Remote• Outstanding Work-Life Balance
](https://sg3email.lensa.com/ls/click?upn=abc123)

logo.webp) |
Eton Technologies
---|---
Boomi Developer
$120K-$150K / yr. (est.)
Full-Time• Remote• Contract
](https://sg3email.lensa.com/ls/click?upn=def456)

Unsubscribe
"""


def test_multiple_lensa_cards_extraction() -> None:
    jobs = parse_lensa_email(
        text=SANITIZED_LENSA_TEXT,
        html=None,
        links=[],
    )

    assert len(jobs) == 2

    first = jobs[0]
    assert first["source"] == "lensa"
    assert first["company_name"] == "LTM"
    assert first["title"] == "nCino Developer"
    assert first["salary_text"] == "$78K-$95K / yr. (est.)"
    assert first["location"] == "Remote"
    assert first["description"] is None
    assert first["apply_url"].endswith("abc123")

    second = jobs[1]
    assert second["company_name"] == "Eton Technologies"
    assert second["title"] == "Boomi Developer"
    assert second["apply_url"].endswith("def456")


def test_marketing_email_is_ignored() -> None:
    jobs = parse_lensa_email(
        text="""
        Change preferences
        Manage alerts
        Unsubscribe
        """,
        html=None,
        links=[],
    )

    assert jobs == []


def test_wrapped_text_link_does_not_swallow_next_card() -> None:
    jobs = parse_lensa_email(
        text="""
        logo.webp) | RIT Solutions, Inc.
        ---|---
        Snowflake DBT Developer
        $101K-$123K / yr. (est.)
        Full-Time• Remote
        ](https://sg3email.lensa.com/ls/click?upn=first
        [ | | ![LE](https://sg3email.lensa.com/ls/click?upn=image
        logo.webp) | Next Company
        """,
        html=None,
        links=[
            "https://sg3email.lensa.com/ls/click?upn=complete-first-link",
        ],
    )

    assert len(jobs) == 1
    assert jobs[0]["apply_url"].endswith("complete-first-link")
    assert "[ |" not in jobs[0]["apply_url"]
    assert "logo.webp" not in jobs[0]["apply_url"]


def test_nearby_card_link_wins_over_unrelated_link_list() -> None:
    jobs = parse_lensa_email(
        text="""
        logo.webp) | Defense Unicorns
        ---|---
        Data Engineer
        $149K / yr.
        Full-Time• Remote
        ](https://sg3email.lensa.com/ls/click?upn=defense-unicorns-link)

        logo.webp) | Other Company
        ---|---
        Remote Databricks Data AI Engineer
        $120K-$140K / yr. (est.)
        Full-Time• Remote
        ](https://sg3email.lensa.com/ls/click?upn=other-company-link)
        """,
        html=None,
        links=[
            "https://sg3email.lensa.com/ls/click?upn=other-company-link",
            "https://sg3email.lensa.com/ls/click?upn=defense-unicorns-link",
        ],
    )

    assert len(jobs) == 2
    assert jobs[0]["company_name"] == "Defense Unicorns"
    assert jobs[0]["title"] == "Data Engineer"
    assert jobs[0]["apply_url"].endswith("defense-unicorns-link")
    assert jobs[1]["company_name"] == "Other Company"
    assert jobs[1]["apply_url"].endswith("other-company-link")


def test_email_greeting_separator_is_not_a_job_card() -> None:
    jobs = parse_lensa_email(
        text="""
        August 15, 2026
        ---|---
        Hi there,
        Here are jobs we thought you might like.
        ](https://sg3email.lensa.com/ls/click?upn=bad-header-link)

        logo.webp) | Paradigm Corp.
        ---|---
        Remote Data Engineer III
        $75K-$91K / yr. (est.)
        Full-Time• Remote
        ](https://sg3email.lensa.com/ls/click?upn=real-job-link)
        """,
        html=None,
        links=[],
    )

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Remote Data Engineer III"
    assert jobs[0]["company_name"] == "Paradigm Corp."
    assert jobs[0]["apply_url"].endswith("real-job-link")


def test_malformed_markdown_link_card_is_ignored() -> None:
    jobs = parse_lensa_email(
        text="""
        ›
        ---|---
        [Senior BI Analyst - Remote Dashboards &
        Here is some wrapped link text.
        ](https://sg3email.lensa.com/ls/click?upn=bad-link-card)

        logo.webp) | Clean Company
        ---|---
        Senior Data Engineer
        $120K-$140K / yr. (est.)
        Full-Time• Remote
        ](https://sg3email.lensa.com/ls/click?upn=real-clean-card)
        """,
        html=None,
        links=[],
    )

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Senior Data Engineer"
    assert jobs[0]["company_name"] == "Clean Company"
    assert jobs[0]["apply_url"].endswith("real-clean-card")


def main() -> None:
    test_multiple_lensa_cards_extraction()
    test_marketing_email_is_ignored()
    test_wrapped_text_link_does_not_swallow_next_card()
    test_nearby_card_link_wins_over_unrelated_link_list()
    test_email_greeting_separator_is_not_a_job_card()
    test_malformed_markdown_link_card_is_ignored()
    print("Lensa parser tests passed.")


if __name__ == "__main__":
    main()
