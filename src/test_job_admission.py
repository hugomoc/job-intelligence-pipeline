from src.job_admission import evaluate_job_admission


RESUME_PROFILE = {
    "target_roles": [
        "Senior Data Engineer",
        "Analytics Engineer",
    ],
    "current_or_recent_titles": [
        "Senior Data Engineer",
    ],
    "seniority": "senior",
    "years_of_relevant_experience": 12,
    "production_skills": [
        "Python",
        "SQL",
        "AWS",
        "Snowflake",
        "dbt",
        "Airflow",
    ],
    "data_engineering_capabilities": [
        "data pipelines",
        "ELT",
        "data warehouse",
        "orchestration",
    ],
    "bi_analytics_skills": [
        "Looker",
        "analytics modeling",
        "reporting automation",
    ],
    "programming_languages": [
        "Python",
        "SQL",
    ],
    "databases_warehouses": [
        "Snowflake",
    ],
    "cloud_platforms": [
        "AWS",
    ],
}


def decision(title: str, description: str = ""):
    return evaluate_job_admission(
        job={
            "title": title,
            "company_name": "Example",
            "location": "Remote",
            "description": description,
        },
        resume_profile=RESUME_PROFILE,
    )


def test_includes_supported_data_engineering_title() -> None:
    result = decision(
        "Senior Data Engineer with Snowflake, Python, SQL, AWS"
    )

    assert result.admission_decision == "include"
    assert result.role_family_match == "strong"
    assert result.required_skill_match == "strong"


def test_includes_supported_analytics_engineering_title() -> None:
    result = decision(
        "Senior Analytics Engineer with dbt, Snowflake, SQL"
    )

    assert result.admission_decision == "include"
    assert result.role_family_match == "strong"
    assert result.required_skill_match == "strong"


def test_excludes_forward_deployed_databricks_gap() -> None:
    result = decision(
        "Forward Deployed Data Engineer IV, Databricks"
    )

    assert result.admission_decision == "exclude"
    assert "Databricks" in result.critical_skill_gaps
    assert "Forward Deployed" in result.critical_skill_gaps
    assert result.role_family_match == "strong"
    assert result.specialization_match == "weak"


def test_excludes_sap_gap() -> None:
    result = decision("SAP Data Engineer")

    assert result.admission_decision == "exclude"
    assert "SAP" in result.critical_skill_gaps


def test_excludes_ml_gap() -> None:
    result = decision("ML Data Engineer")

    assert result.admission_decision == "exclude"
    assert "Machine Learning" in result.critical_skill_gaps


def test_excludes_management_gap() -> None:
    result = decision("Data Engineering Manager")

    assert result.admission_decision == "exclude"
    assert (
        "management" in result.admission_reason.casefold()
        or "management experience" in result.critical_skill_gaps
    )


def test_excludes_title_family_only() -> None:
    result = decision("Senior Data Engineer")

    assert result.admission_decision == "exclude"
    assert result.admission_reason == (
        "The title family alone is not enough evidence to show this job."
    )


def test_excludes_no_description_tableau_gap() -> None:
    result = decision("SQL/Tableau Developer (Contingent Upon Contract Award)")

    assert result.admission_decision == "exclude"
    assert "Tableau" in result.critical_skill_gaps


def test_excludes_possible_match_with_one_broad_title_signal() -> None:
    result = decision("SQL Developer")

    assert result.admission_decision == "exclude"
    assert result.admission_reason == (
        "A possible title match without a description needs at "
        "least two concrete resume-supported title signals."
    )


def main() -> None:
    test_includes_supported_data_engineering_title()
    test_includes_supported_analytics_engineering_title()
    test_excludes_forward_deployed_databricks_gap()
    test_excludes_sap_gap()
    test_excludes_ml_gap()
    test_excludes_management_gap()
    test_excludes_title_family_only()
    test_excludes_no_description_tableau_gap()
    test_excludes_possible_match_with_one_broad_title_signal()
    print("Job admission tests passed.")


if __name__ == "__main__":
    main()
