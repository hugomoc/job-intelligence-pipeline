from src.parsers.jobright import parse_jobright_email


SANITIZED_JOBRIGHT_TEXT = """
Hi there,

Publicis Groupe
Advertising · Public
Looker BI Developer
$73K/yr - $113K/yr
New York, NY
2 days ago ·
Be an early applicant
APPLY NOW

Acme Data
Technology · Private
Senior Data Engineer
Remote
1 hour ago ·
Be an early applicant
APPLY NOW
"""


SANITIZED_JOBOT_TEXT = """
[Jobot logo]
12 New Jobs for your Job Search

Recommended Apply -- Based on your resume
Artificial Intelligence Manager (Public Accounting) - Remote
[https://url9751.alerts.jobot.com/ls/click?upn=abc123]
Artificial Intelligence Manager / $$$ / Supporting Tax and Audit teams.
[🏡] REMOTE [📍] Tampa, FL [💵] $120,000 - $180,000
1-Click Apply

Also Consider -- Based on your job alert
Senior Data Engineer
[https://url9751.alerts.jobot.com/ls/click?upn=def456]
Build analytics products for financial operations.
[📍] San Diego, CA [💵] $130,000 - $160,000
1-Click Apply
"""


def test_jobright_ai_cards_extraction() -> None:
    jobs = parse_jobright_email(
        text=SANITIZED_JOBRIGHT_TEXT,
        html=None,
        links=[
            "https://jobright.ai/jobs/info/111111111111111111111111",
            "https://jobright.ai/jobs/info/222222222222222222222222",
        ],
    )

    assert len(jobs) == 2

    first = jobs[0]
    assert first["source"] == "jobright"
    assert first["source_job_id"] == "111111111111111111111111"
    assert first["company_name"] == "Publicis Groupe"
    assert first["title"] == "Looker BI Developer"
    assert first["salary_text"] == "$73K/yr - $113K/yr"
    assert first["location"] == "New York, NY"
    assert first["posted_age_text"] == "2 days ago ·"

    second = jobs[1]
    assert second["company_name"] == "Acme Data"
    assert second["title"] == "Senior Data Engineer"
    assert second["salary_text"] is None
    assert second["location"] == "Remote"


def test_jobot_style_alert_extraction() -> None:
    jobs = parse_jobright_email(
        text=SANITIZED_JOBOT_TEXT,
        html=None,
        links=[],
    )

    assert len(jobs) == 2

    first = jobs[0]
    assert first["source"] == "jobright"
    assert first["company_name"] == "Jobot"
    assert first["title"] == (
        "Artificial Intelligence Manager "
        "(Public Accounting) - Remote"
    )
    assert first["location"] == "Tampa, FL (Remote)"
    assert first["salary_text"] == "$120,000 - $180,000"
    assert "Supporting Tax" in first["description"]

    second = jobs[1]
    assert second["title"] == "Senior Data Engineer"
    assert second["location"] == "San Diego, CA"
    assert second["apply_url"].endswith("def456")


def main() -> None:
    test_jobright_ai_cards_extraction()
    test_jobot_style_alert_extraction()
    print("Jobright parser tests passed.")


if __name__ == "__main__":
    main()
