from src.parsers.builtin import parse_builtin_email


SAMPLE_TEXT = """
Find top Jobs at Built In
Job Preferences
Data Engineer,
San Diego, CA, USA, Hybrid, In Office, Remote, Mid Level, Senior Level
Coinbase
Software Engineer, Data Platform Team
Remote
USA
$152,405-$179,300
Coinbase
Staff Analytics Engineer, Compliance Data
Remote
USA
$207,485-$244,100
Get More Recommendations
"""


def test_builtin_jobs_are_extracted() -> None:
    jobs = parse_builtin_email(
        text=SAMPLE_TEXT,
        html=None,
        links=[
            "https://builtin.com/job/software-engineer-data-platform-team/10571814",
            "https://builtin.com/job/staff-analytics-engineer-compliance-data/10570366",
        ],
    )

    assert len(jobs) == 2
    assert jobs[0]["source"] == "builtin"
    assert jobs[0]["company_name"] == "Coinbase"
    assert jobs[0]["title"] == "Software Engineer, Data Platform Team"
    assert jobs[0]["location"] == "Remote"
    assert jobs[0]["salary_text"] == "$152,405-$179,300"
    assert jobs[1]["title"] == "Staff Analytics Engineer, Compliance Data"


def main() -> None:
    test_builtin_jobs_are_extracted()
    print("Built In parser tests passed.")


if __name__ == "__main__":
    main()
