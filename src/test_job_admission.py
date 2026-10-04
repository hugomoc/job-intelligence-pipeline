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


def test_excludes_databricks_gap() -> None:
    result = decision("Data Engineer, Databricks (Senior)")

    assert result.admission_decision == "exclude"
    assert "Databricks" in result.critical_skill_gaps


def test_includes_production_databricks_experience() -> None:
    profile = {
        **RESUME_PROFILE,
        "production_skills": [
            *RESUME_PROFILE["production_skills"],
            "Databricks",
        ],
    }
    result = evaluate_job_admission(
        job={
            "title": "Data Engineer, Databricks (Senior)",
            "company_name": "Example",
            "location": "Remote",
            "description": "",
        },
        resume_profile=profile,
    )

    assert result.admission_decision == "include"
    assert "Databricks" not in result.critical_skill_gaps
    assert result.specialization_match == "strong"


def test_project_databricks_does_not_satisfy_production_gap() -> None:
    profile = {
        **RESUME_PROFILE,
        "project_skills": [
            "Databricks",
        ],
    }
    result = evaluate_job_admission(
        job={
            "title": "Data Engineer, Databricks (Senior)",
            "company_name": "Example",
            "location": "Remote",
            "description": "",
        },
        resume_profile=profile,
    )

    assert result.admission_decision == "exclude"
    assert "Databricks" in result.critical_skill_gaps


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


def test_senior_role_has_strong_seniority_match() -> None:
    result = decision(
        "Senior Data Engineer with Snowflake SQL Python"
    )

    assert result.job_seniority_level == "senior"
    assert result.seniority_match == "strong"


def test_staff_role_with_cross_team_scope_is_not_rejected_by_title() -> None:
    profile = {
        **RESUME_PROFILE,
        "leadership_evidence": [
            "Set technical direction for cross-team data architecture and standards.",
        ],
    }
    result = evaluate_job_admission(
        job={
            "title": "Staff Data Engineer",
            "company_name": "Example",
            "location": "Remote",
            "description": (
                "Lead architecture across teams and establish standards "
                "for foundational data platforms."
            ),
        },
        resume_profile=profile,
    )

    assert result.job_seniority_level == "staff"
    assert result.seniority_match == "strong"
    assert not result.critical_skill_gaps


def test_staff_role_without_scope_is_not_strong_seniority() -> None:
    result = decision(
        "Staff Data Engineer",
        description=(
            "Own architecture across teams and set technical strategy "
            "for data platforms."
        ),
    )

    assert result.job_seniority_level == "staff"
    assert result.seniority_match == "weak"


def test_senior_staff_role_without_org_scope_is_critical_gap() -> None:
    result = decision(
        "Senior Staff Data Engineer",
        description=(
            "Set multi-year technical strategy, align organizations, "
            "and influence directors on foundational data architecture."
        ),
    )

    assert result.job_seniority_level == "senior_staff"
    assert result.seniority_match == "weak"
    assert any(
        "Senior Staff" in gap
        for gap in result.critical_skill_gaps
    )


def test_senior_staff_years_and_generic_senior_experience_has_scope_gap() -> None:
    profile = {
        **RESUME_PROFILE,
        "years_of_relevant_experience": 20,
        "leadership_evidence": [
            "Senior engineer with strong SQL, Python, Snowflake, and Airflow experience.",
        ],
    }
    result = evaluate_job_admission(
        job={
            "title": "Senior Staff Data Engineer",
            "company_name": "Example",
            "location": "Remote",
            "description": (
                "Own multi-year technical strategy and influence "
                "directors on cross-organization data architecture."
            ),
        },
        resume_profile=profile,
    )

    assert result.seniority_match == "weak"
    assert any(
        "Senior Staff" in gap
        for gap in result.critical_skill_gaps
    )


def test_senior_staff_standards_only_has_scope_gap() -> None:
    profile = {
        **RESUME_PROFILE,
        "leadership_evidence": [
            "Created standards for data modeling and code quality.",
        ],
    }
    result = evaluate_job_admission(
        job={
            "title": "Senior Staff Data Engineer",
            "company_name": "Example",
            "location": "Remote",
            "description": (
                "Set technical direction for senior engineers and "
                "drive adoption across organizations."
            ),
        },
        resume_profile=profile,
    )

    assert result.seniority_match == "weak"
    assert any(
        "Senior Staff" in gap
        for gap in result.critical_skill_gaps
    )


def test_senior_staff_cross_functional_only_has_scope_gap() -> None:
    profile = {
        **RESUME_PROFILE,
        "leadership_evidence": [
            "Collaborated cross-functionally with stakeholders.",
        ],
    }
    result = evaluate_job_admission(
        job={
            "title": "Senior Staff Data Engineer",
            "company_name": "Example",
            "location": "Remote",
            "description": (
                "Own organization-wide architecture and influence "
                "director-level technical decisions."
            ),
        },
        resume_profile=profile,
    )

    assert result.seniority_match == "weak"
    assert any(
        "Senior Staff" in gap
        for gap in result.critical_skill_gaps
    )


def test_senior_staff_reusable_framework_only_has_scope_gap() -> None:
    profile = {
        **RESUME_PROFILE,
        "leadership_evidence": [
            "Built a reusable framework for pipeline development.",
        ],
    }
    result = evaluate_job_admission(
        job={
            "title": "Senior Staff Data Engineer",
            "company_name": "Example",
            "location": "Remote",
            "description": (
                "Own multi-year technical strategy and broad data "
                "platform adoption across teams."
            ),
        },
        resume_profile=profile,
    )

    assert result.seniority_match == "weak"
    assert any(
        "Senior Staff" in gap
        for gap in result.critical_skill_gaps
    )


def test_senior_staff_role_with_org_scope_is_allowed() -> None:
    profile = {
        **RESUME_PROFILE,
        "leadership_evidence": [
            "Owned multi-year technical strategy for a data platform.",
            "Aligned cross-organizational adoption and influenced senior engineers.",
        ],
    }
    result = evaluate_job_admission(
        job={
            "title": "Senior Staff Data Engineer",
            "company_name": "Example",
            "location": "Remote",
            "description": (
                "Set multi-year technical strategy and drive "
                "cross-organizational adoption."
            ),
        },
        resume_profile=profile,
    )

    assert result.job_seniority_level == "senior_staff"
    assert result.seniority_match == "strong"
    assert not result.critical_skill_gaps


def test_principal_broad_experience_without_org_influence_has_gap() -> None:
    profile = {
        **RESUME_PROFILE,
        "leadership_evidence": [
            "Designed architecture, led projects, mentored teammates, and presented to leadership.",
        ],
    }
    result = evaluate_job_admission(
        job={
            "title": "Principal Data Engineer",
            "company_name": "Example",
            "location": "Remote",
            "description": (
                "Influence executives, drive multi-year technical "
                "strategy, and align organizations around data architecture."
            ),
        },
        resume_profile=profile,
    )

    assert result.seniority_match == "weak"
    assert any(
        "Principal" in gap
        for gap in result.critical_skill_gaps
    )


def test_principal_role_scope_gap_affects_admission() -> None:
    result = decision(
        "Principal Data Engineer",
        description=(
            "Own organization-wide architecture and set technical "
            "direction for foundational data products."
        ),
    )

    assert result.job_seniority_level == "principal"
    assert result.seniority_match == "weak"
    assert any(
        "Principal" in gap
        for gap in result.critical_skill_gaps
    )


def test_staff_cross_team_architecture_can_pass_without_staff_title() -> None:
    profile = {
        **RESUME_PROFILE,
        "leadership_evidence": [
            "Owned architecture across teams for shared data platforms.",
        ],
    }
    result = evaluate_job_admission(
        job={
            "title": "Staff Data Engineer",
            "company_name": "Example",
            "location": "Remote",
            "description": (
                "Own architecture across teams and guide shared "
                "Snowflake SQL data pipeline decisions."
            ),
        },
        resume_profile=profile,
    )

    assert result.job_seniority_level == "staff"
    assert result.seniority_match == "strong"
    assert result.admission_decision == "include"


def test_senior_project_leadership_does_not_require_staff_scope() -> None:
    result = decision(
        "Senior Data Engineer with Snowflake SQL Python",
        description=(
            "Lead project delivery for SQL and Snowflake pipelines. "
            "Responsibilities include ELT, data modeling, and orchestration."
        ),
    )

    assert result.job_seniority_level == "senior"
    assert result.seniority_match == "strong"
    assert not any(
        "organizational IC scope" in gap
        for gap in result.critical_skill_gaps
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


def test_project_only_skill_is_not_production_match() -> None:
    profile = {
        **RESUME_PROFILE,
        "production_skills": [
            "Python",
            "SQL",
            "AWS",
            "Snowflake",
        ],
        "project_skills": [
            "dbt",
        ],
    }

    result = evaluate_job_admission(
        job={
            "title": "Senior Analytics Engineer with dbt",
            "company_name": "Example",
            "location": "Remote",
            "description": "",
        },
        resume_profile=profile,
    )

    assert result.required_skill_match == "weak"
    assert "project-only dbt" in result.matched_resume_signals
    assert "dbt" not in result.matched_resume_signals


def main() -> None:
    test_includes_supported_data_engineering_title()
    test_includes_supported_analytics_engineering_title()
    test_excludes_forward_deployed_databricks_gap()
    test_excludes_databricks_gap()
    test_includes_production_databricks_experience()
    test_project_databricks_does_not_satisfy_production_gap()
    test_excludes_sap_gap()
    test_excludes_ml_gap()
    test_excludes_management_gap()
    test_senior_role_has_strong_seniority_match()
    test_staff_role_with_cross_team_scope_is_not_rejected_by_title()
    test_staff_role_without_scope_is_not_strong_seniority()
    test_senior_staff_role_without_org_scope_is_critical_gap()
    test_senior_staff_years_and_generic_senior_experience_has_scope_gap()
    test_senior_staff_standards_only_has_scope_gap()
    test_senior_staff_cross_functional_only_has_scope_gap()
    test_senior_staff_reusable_framework_only_has_scope_gap()
    test_senior_staff_role_with_org_scope_is_allowed()
    test_principal_broad_experience_without_org_influence_has_gap()
    test_principal_role_scope_gap_affects_admission()
    test_staff_cross_team_architecture_can_pass_without_staff_title()
    test_senior_project_leadership_does_not_require_staff_scope()
    test_excludes_title_family_only()
    test_excludes_no_description_tableau_gap()
    test_excludes_possible_match_with_one_broad_title_signal()
    test_project_only_skill_is_not_production_match()
    print("Job admission tests passed.")


if __name__ == "__main__":
    main()
