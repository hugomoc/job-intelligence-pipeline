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


def main() -> None:
    test_multiple_lensa_cards_extraction()
    test_marketing_email_is_ignored()
    test_wrapped_text_link_does_not_swallow_next_card()
    print("Lensa parser tests passed.")


if __name__ == "__main__":
    main()
