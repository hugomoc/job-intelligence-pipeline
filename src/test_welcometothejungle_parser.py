from src.parsers.welcometothejungle import parse_welcometothejungle_email


SAMPLE_TEXT = """
New Job Notification
There are new jobs matching your search preferences, Hugo!
Instrumentl
AI-powered SaaS platform for funding grants
Senior Data Engineer
Salary: $170-190K
Remote (within the US)
Circle
Building the Internet Financial Platform
Senior Staff Data Engineer
Salary: $225-290K
Remote (within the US)
Doximity
Professional medical network for physicians
Data Engineering Manager
Salary: $172-254K
Remote (within the US)
See all top matches
"""


def test_welcome_to_the_jungle_jobs_are_extracted() -> None:
    jobs = parse_welcometothejungle_email(
        text=SAMPLE_TEXT,
        html=None,
        links=[
            "https://u9255466.ct.sendgrid.net/ls/click?upn=header",
            "https://u9255466.ct.sendgrid.net/ls/click?upn=job1",
            "https://u9255466.ct.sendgrid.net/ls/click?upn=job2",
            "https://u9255466.ct.sendgrid.net/ls/click?upn=job3",
        ],
    )

    assert len(jobs) == 3
    assert jobs[0]["source"] == "welcometothejungle"
    assert jobs[0]["company_name"] == "Instrumentl"
    assert jobs[0]["title"] == "Senior Data Engineer"
    assert jobs[0]["salary_text"] == "$170-190K"
    assert jobs[0]["location"] == "Remote (within the US)"
    assert jobs[0]["description"] == "AI-powered SaaS platform for funding grants"
    assert jobs[0]["apply_url"].endswith("job1")


def test_multiline_title_is_joined() -> None:
    jobs = parse_welcometothejungle_email(
        text="""
        New Job Notification
        There are new jobs matching your search preferences, Hugo!
        Instacart
        Online grocery marketplace and grocery tech platform
        Senior Risk & Compliance Engineer
        (Data)
        Salary: $156-230K
        Remote (within the US)
        See all top matches
        """,
        html=None,
        links=[
            "https://u9255466.ct.sendgrid.net/ls/click?upn=header",
            "https://u9255466.ct.sendgrid.net/ls/click?upn=job1",
        ],
    )

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Senior Risk & Compliance Engineer (Data)"


def main() -> None:
    test_welcome_to_the_jungle_jobs_are_extracted()
    test_multiline_title_is_joined()
    print("Welcome to the Jungle parser tests passed.")


if __name__ == "__main__":
    main()
