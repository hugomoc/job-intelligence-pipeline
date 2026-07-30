from src.parsers.remotehunter import parse_remotehunter_email


SANITIZED_REMOTEHUNTER_TEXT = """
RemoteHunter - Improve Your Score

Your Next Roles To Explore

Best Match

Innovaccer Analytics

Forward Deployed Engineer (AI/Agents)

Forward Deployed Engineer (AI/Agents)
Remote
Engineering And Analytics
United States

Description

About the Role

We are hiring an AI Engineer to build agentic systems for enterprise customers...

On-site
$/yr
US Optimize For This Role → ( https://www.remotehunter.com/apply-with-ai/f609bbe2-fb62-47ad-b23a-b42668aeb396?utm_source=email )

CrowdStrike

Sr. Backend Engineer - Charlotte AI (Remote, EST)

As a global leader in cybersecurity, CrowdStrike protects modern organizations...

As a global leader in cybersecurity, CrowdStrike protects modern organizations...

On-site
$215000/yr
US Optimize For This Role → ( https://www.remotehunter.com/apply-with-ai/fb3a2135-f70f-40c6-91f3-b4a2f7885b6d?utm_source=email )
"""


def test_multiple_job_cards_extraction() -> None:
    jobs = parse_remotehunter_email(
        text=SANITIZED_REMOTEHUNTER_TEXT,
        html=None,
        links=[],
    )

    assert len(jobs) == 2

    first = jobs[0]
    assert first["source"] == "remotehunter"
    assert first["source_job_id"] == (
        "f609bbe2-fb62-47ad-b23a-b42668aeb396"
    )
    assert first["company_name"] == "Innovaccer Analytics"
    assert first["title"] == (
        "Forward Deployed Engineer (AI/Agents)"
    )
    assert first["location"] == "United States"
    assert first["salary_text"] == "$/yr"
    assert "agentic systems" in first["description"]
    assert first["apply_url"].startswith(
        "https://www.remotehunter.com/apply-with-ai/"
    )

    second = jobs[1]
    assert second["company_name"] == "CrowdStrike"
    assert second["title"] == (
        "Sr. Backend Engineer - Charlotte AI (Remote, EST)"
    )
    assert second["location"] == "US"
    assert second["salary_text"] == "$215000/yr"
    assert second["description"] == (
        "As a global leader in cybersecurity, CrowdStrike "
        "protects modern organizations..."
    )


def test_marketing_email_is_ignored() -> None:
    jobs = parse_remotehunter_email(
        text="""
        RemoteHunter - Improve Your Score
        Search roles you actually want, then tighten your resume.
        Search Roles Now
        Unsubscribe
        """,
        html=None,
        links=[],
    )

    assert jobs == []


def main() -> None:
    test_multiple_job_cards_extraction()
    test_marketing_email_is_ignored()
    print("RemoteHunter parser tests passed.")


if __name__ == "__main__":
    main()
