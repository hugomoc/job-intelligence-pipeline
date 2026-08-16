from src.parsers.joblookup import parse_joblookup_email


SAMPLE_TEXT = """
Hello,
welcome to JobLookup!
Check out these jobs below to get started:
Job title: Principal Data & Analytics Engineer
Location: New River, AZ
https://joblookup.com/us/dispatch/job/jbe/principal-data-analytics-engineer-job-in-chandler-az?uid=11346470&t=1786647604
------------------------------
Find more Analytics Engineer jobs here
"""


def test_joblookup_single_job_is_extracted() -> None:
    jobs = parse_joblookup_email(text=SAMPLE_TEXT)

    assert len(jobs) == 1
    assert jobs[0]["source"] == "joblookup"
    assert jobs[0]["source_job_id"] == "11346470"
    assert jobs[0]["title"] == "Principal Data & Analytics Engineer"
    assert jobs[0]["company_name"] == "Unknown company"
    assert jobs[0]["location"] == "New River, AZ"
    assert jobs[0]["apply_url"].startswith("https://joblookup.com/us/dispatch/job/")


def main() -> None:
    test_joblookup_single_job_is_extracted()
    print("Joblookup parser tests passed.")


if __name__ == "__main__":
    main()
