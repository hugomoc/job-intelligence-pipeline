from src.job_title_filter import (
    FILTERED_OUT,
    POSSIBLE_MATCH,
    STRONG_MATCH,
    classify_job_title,
    normalize_job_title,
)


STRONG_TITLES = (
    "Senior Data Engineer",
    "Principal Data Engineer, Analytics",
    "Sr. Data Engineer - Snowflake",
    "Data Engineer III",
    "Senior Analytics Engineer",
    "Analytics Engineer, Growth",
    "Senior Analytics Engineer - Data Platform",
    "BI Data Engineer",
    "Senior BI Developer",
    "Business Intelligence Engineer",
    "Snowflake Data Engineer",
    "Looker Developer",
    "Senior Looker Developer",
    "Analytics Architect",
    "Data Warehouse Engineer",
    "ETL Engineer",
    "Senior Data & Analytics Engineer",
    "Data Engineer - Machine Learning Platform",
    "Senior Data Engineer - Azure",
    "Analytics Engineer - BigQuery",
    "Analytics Engineer - GCP",
    "Data Engineer - Kafka",
    "Analytics Engineer - Tableau",
)

POSSIBLE_TITLES = (
    "Senior Data Analyst",
    "Senior BI Analyst",
    "Data Analyst",
    "Data Architect",
    "Data Platform Engineer",
    "Database Engineer",
    "Reporting Developer",
    "Solutions Architect - Data",
    "Data Business Analyst",
)

FILTERED_TITLES = (
    "Remote Software Engineer III - ML-Driven Asset Analytics",
    "Senior Software Engineer",
    "Software Engineer - Data Analytics",
    "Backend Software Engineer - Data Platform",
    "Frontend Engineer",
    "Full Stack Engineer",
    "Machine Learning Engineer",
    "Senior Machine Learning Engineer",
    "ML Engineer - Analytics Platform",
    "AI Engineer",
    "Senior Data Scientist",
    "Applied Scientist",
    "DevOps Engineer",
    "Site Reliability Engineer",
    "Database Administrator",
    "Oracle DBA",
    "Product Manager - Data Platform",
    "Technical Product Manager - Analytics",
    "Financial Analyst",
    "Marketing Analyst",
    "Junior Data Engineer",
    "Data Engineering Manager",
    "Director of Data Engineering",
)


def test_normalization() -> None:
    assert normalize_job_title(" Sr. BI Engineer - Remote, US ") == (
        "senior business intelligence engineer"
    )
    assert normalize_job_title("Analytics Engineer, Growth") == (
        "analytics engineer growth"
    )


def test_strong_titles() -> None:
    for title in STRONG_TITLES:
        assert classify_job_title(title).category == STRONG_MATCH, title


def test_possible_titles() -> None:
    for title in POSSIBLE_TITLES:
        assert classify_job_title(title).category == POSSIBLE_MATCH, title


def test_filtered_titles() -> None:
    for title in FILTERED_TITLES:
        assert classify_job_title(title).category == FILTERED_OUT, title


def test_primary_role_precedence() -> None:
    assert classify_job_title(
        "Software Engineer - Data Analytics"
    ).category == FILTERED_OUT
    assert classify_job_title(
        "Data Engineer - Analytics Platform"
    ).category == STRONG_MATCH
    assert classify_job_title(
        "ML Engineer - Data Platform"
    ).category == FILTERED_OUT
    assert classify_job_title(
        "Data Engineer - ML Platform"
    ).category == STRONG_MATCH


def test_false_substring_matches() -> None:
    assert classify_job_title(
        "Retail Data Engineer"
    ).category == STRONG_MATCH
    assert classify_job_title(
        "Business Intelligence Engineer"
    ).matched_pattern == "business intelligence engineer"


def main() -> None:
    test_normalization()
    test_strong_titles()
    test_possible_titles()
    test_filtered_titles()
    test_primary_role_precedence()
    test_false_substring_matches()
    print("Job title filter tests passed.")


if __name__ == "__main__":
    main()
