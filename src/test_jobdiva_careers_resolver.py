from src.resolvers.jobdiva_careers_resolver import (
    extract_jobdiva_detail,
)


DETAIL_TEXT = """
Create Job Alert
Search Jobs
Medical Affairs Data/BI Engineer#26-03486
Remote, RI
Remote
Apply Now
Share on
Job Description

Role: Medical Affairs Data/BI Engineer

Location: Remote

Rate - 50 to 55 w2 flexible here

Duration: 2+ years

Responsibilities

Design, develop, and maintain executive-level dashboards and reporting
solutions using Microsoft Power BI.

Required Qualifications

5+ years of experience developing dashboards and reporting solutions
using Microsoft Power BI.
"""


def test_extract_jobdiva_detail() -> None:
    result = extract_jobdiva_detail(
        DETAIL_TEXT,
        expected_company="V-Soft Consulting Group, Inc.",
    )

    assert result.title == "Medical Affairs Data/BI Engineer"
    assert result.company == "V-Soft Consulting Group, Inc."
    assert result.location == "Remote, RI"
    assert result.salary == "50 to 55 w2 flexible here"
    assert result.employment_type == "2+ years"
    assert "Microsoft Power BI" in (result.description or "")
    assert result.description_word_count > 20


def main() -> None:
    test_extract_jobdiva_detail()
    print("JobDiva careers resolver tests passed.")


if __name__ == "__main__":
    main()
