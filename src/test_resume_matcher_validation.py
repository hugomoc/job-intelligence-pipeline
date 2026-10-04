from pathlib import Path
from tempfile import TemporaryDirectory

from src.ai.resume_matcher import (
    MATCHER_PROMPT_VERSION,
    JobMatchAnalysis,
    build_resume_job_match,
    determine_recommendation,
    validate_missing_qualifications,
)
from src.ai.resume_profiler import ResumeProfile
from src import database
from src.score_jobs_ai import (
    initialize_ai_tables,
    load_cached_job_score,
)


def make_profile(
    production_skills: list[str] | None = None,
    project_skills: list[str] | None = None,
    databases_warehouses: list[str] | None = None,
    cloud_platforms: list[str] | None = None,
    tools_platforms: list[str] | None = None,
    data_engineering_capabilities: list[str] | None = None,
    ai_ml_experience: list[str] | None = None,
    industries: list[str] | None = None,
) -> ResumeProfile:
    return ResumeProfile(
        professional_summary=(
            "Synthetic profile for matcher validation."
        ),
        target_roles=["Data Engineer"],
        current_or_recent_titles=["Data Engineer"],
        seniority="senior",
        years_of_relevant_experience=8,
        production_skills=production_skills or [],
        project_skills=project_skills or [],
        data_engineering_capabilities=(
            data_engineering_capabilities or []
        ),
        bi_analytics_skills=[],
        programming_languages=["SQL", "Python"],
        databases_warehouses=(
            databases_warehouses or []
        ),
        cloud_platforms=cloud_platforms or [],
        tools_platforms=tools_platforms or [],
        ai_ml_experience=ai_ml_experience or [],
        industries=industries or [],
        certifications=[],
        education=[],
        quantified_achievements=[],
        leadership_evidence=[],
        strengths_for_job_matching=[],
        profile_confidence="high",
    )


def make_analysis(
    hard_missing: list[str],
    preferred_missing: list[str] | None = None,
    score: int = 95,
) -> JobMatchAnalysis:
    return JobMatchAnalysis(
        title_fit=score,
        skills_fit=score,
        experience_fit=score,
        seniority_fit=score,
        industry_fit=score,
        location_fit=score,
        confidence="high",
        matching_strengths=[],
        hard_requirements_missing=hard_missing,
        preferred_qualifications_missing=(
            preferred_missing or []
        ),
        risk_factors=[],
        summary="Synthetic analysis.",
    )


def scored_match(
    profile: ResumeProfile,
    title: str,
    description: str,
    analysis: JobMatchAnalysis | None = None,
):
    return build_resume_job_match(
        resume_profile=profile,
        job={
            "record_key": "job-1",
            "title": title,
            "company_name": "Example Co",
            "location": "Remote",
            "description": description,
        },
        model_name="unit-test",
        analysis=analysis or make_analysis([]),
        description_word_count=len(description.split()),
        description_complete=True,
    )


def validated_missing(
    profile: ResumeProfile,
    hard_missing: list[str],
) -> list[str]:
    analysis = validate_missing_qualifications(
        analysis=make_analysis(hard_missing),
        resume_profile=profile,
    )

    return analysis.hard_requirements_missing


def test_cloud_platform_alternatives_are_not_missing() -> None:
    profile = make_profile(
        production_skills=[
            "Built production AWS data pipelines."
        ],
        cloud_platforms=["AWS"],
        tools_platforms=["Amazon S3", "AWS Glue"],
    )

    assert validated_missing(
        profile,
        [
            (
                "Experience with a cloud platform "
                "such as AWS, Azure, or GCP"
            )
        ],
    ) == []


def test_data_warehouse_alternatives_are_not_missing() -> None:
    profile = make_profile(
        databases_warehouses=[
            "Snowflake",
            "Amazon Redshift",
        ]
    )

    assert validated_missing(
        profile,
        [
            (
                "Experience with Snowflake, Redshift, "
                "or BigQuery"
            )
        ],
    ) == []


def test_git_and_cicd_are_not_missing() -> None:
    profile = make_profile(
        tools_platforms=[
            "Git",
            "Jenkins",
        ],
        data_engineering_capabilities=[
            "Automated deployment workflows"
        ],
    )

    assert validated_missing(
        profile,
        ["Experience with CI/CD and Git"],
    ) == []


def test_explicit_azure_remains_missing() -> None:
    profile = make_profile(
        cloud_platforms=["AWS"],
        production_skills=["AWS production workloads"],
    )

    assert validated_missing(
        profile,
        ["Azure experience"],
    ) == ["Azure experience"]


def test_all_warehouses_requirement_can_remain_missing() -> None:
    profile = make_profile(
        databases_warehouses=["Snowflake"]
    )

    assert validated_missing(
        profile,
        ["Experience with Snowflake, Redshift, and BigQuery"],
    ) == [
        "Experience with Snowflake, Redshift, and BigQuery"
    ]


def test_agile_can_remain_missing_when_absent() -> None:
    profile = make_profile(
        production_skills=["SQL data modeling"]
    )

    assert validated_missing(
        profile,
        ["Agile methodologies"],
    ) == ["Agile methodologies"]


def test_agile_is_not_missing_when_demonstrated() -> None:
    profile = make_profile(
        data_engineering_capabilities=[
            "Scrum delivery with sprint planning"
        ]
    )

    assert validated_missing(
        profile,
        ["Agile methodologies"],
    ) == []


def test_missing_critical_ai_specialization_caps_score_and_explains_major_gap() -> None:
    profile = make_profile(
        production_skills=[
            "Python",
            "SQL",
            "Snowflake",
            "AWS",
            "Airflow",
            "dbt",
        ],
        data_engineering_capabilities=[
            "production data pipelines",
        ],
    )
    description = " ".join(
        [
            "Role summary: build production RAG pipelines and agentic workflows.",
            "Responsibilities include MCP tool integrations and AI systems used by real users.",
            "Requirements include demonstrated experience building production LLM agents.",
        ]
        * 8
    )

    match = scored_match(
        profile,
        "Senior Data Engineer, AI Systems",
        description,
    )

    assert match.overall_score <= 49
    assert match.analysis.skills_fit <= 70
    assert any(
        "production RAG" in item
        or "agentic" in item
        for item in match.analysis.hard_requirements_missing
    )
    assert "Major gap" in match.analysis.summary


def test_missing_preferred_skill_does_not_trigger_critical_penalty() -> None:
    profile = make_profile(
        production_skills=[
            "Python",
            "SQL",
            "Snowflake",
            "AWS",
        ],
    )
    description = " ".join(
        [
            "Responsibilities include SQL modeling and Snowflake pipelines.",
            "Preferred experience with Domo dashboards is a bonus.",
        ]
        * 10
    )

    match = scored_match(
        profile,
        "Senior Data Engineer",
        description,
    )

    assert match.overall_score == 95
    assert match.analysis.hard_requirements_missing == []


def test_equivalent_warehouse_requirement_does_not_trigger_critical_penalty() -> None:
    profile = make_profile(
        databases_warehouses=[
            "Snowflake",
        ],
        production_skills=[
            "Snowflake",
            "SQL",
        ],
    )
    description = " ".join(
        [
            "Requirements include Snowflake, BigQuery, Redshift, or similar warehouse experience.",
            "Responsibilities include dimensional modeling and ELT pipelines.",
        ]
        * 10
    )

    match = scored_match(
        profile,
        "Senior Analytics Engineer",
        description,
    )

    assert match.overall_score == 95
    assert match.analysis.hard_requirements_missing == []


def test_databricks_in_alternative_warehouse_list_is_not_a_gap() -> None:
    profile = make_profile(
        databases_warehouses=[
            "Snowflake",
        ],
        production_skills=[
            "Snowflake",
            "SQL",
        ],
    )
    description = " ".join(
        [
            "Requirements include Snowflake, Databricks, BigQuery, or similar warehouse experience.",
            "Responsibilities include dimensional modeling and ELT pipelines.",
        ]
        * 10
    )

    match = scored_match(
        profile,
        "Senior Analytics Engineer",
        description,
    )

    assert match.overall_score == 95
    assert not any(
        "Databricks" in item
        for item in match.analysis.hard_requirements_missing
    )


def test_databricks_or_snowflake_required_accepts_snowflake() -> None:
    profile = make_profile(
        databases_warehouses=[
            "Snowflake",
        ],
        production_skills=[
            "Snowflake",
            "SQL",
        ],
    )
    description = " ".join(
        [
            "Requirements include Databricks or Snowflake required for production data modeling.",
            "Responsibilities include data warehouse optimization and ELT pipelines.",
        ]
        * 10
    )

    match = scored_match(
        profile,
        "Senior Data Engineer",
        description,
    )

    assert match.overall_score == 95
    assert match.analysis.hard_requirements_missing == []


def test_databricks_preferred_only_is_not_a_major_gap() -> None:
    profile = make_profile(
        databases_warehouses=[
            "Snowflake",
        ],
        production_skills=[
            "Snowflake",
            "SQL",
        ],
    )
    description = " ".join(
        [
            "Responsibilities include Snowflake pipelines and SQL modeling.",
            "Databricks preferred for future lakehouse projects.",
        ]
        * 10
    )

    match = scored_match(
        profile,
        "Senior Data Engineer",
        description,
    )

    assert match.overall_score == 95
    assert match.analysis.hard_requirements_missing == []
    assert not any(
        "Databricks" in risk
        for risk in match.analysis.risk_factors
    )


def test_mandatory_core_databricks_requirement_remains_hard_gap() -> None:
    profile = make_profile(
        databases_warehouses=[
            "Snowflake",
        ],
        production_skills=[
            "Snowflake",
            "SQL",
        ],
    )
    description = " ".join(
        [
            "Production Databricks experience is required.",
            "Databricks is the core data platform for this role.",
            "Responsibilities include lakehouse operations and production pipelines.",
        ]
        * 8
    )

    match = scored_match(
        profile,
        "Senior Data Engineer",
        description,
    )

    assert match.overall_score <= 49
    assert any(
        "Databricks" in item
        for item in match.analysis.hard_requirements_missing
    )


def test_cloud_warehouse_alternatives_accept_redshift() -> None:
    profile = make_profile(
        databases_warehouses=[
            "Amazon Redshift",
        ],
        production_skills=[
            "Redshift",
            "SQL",
        ],
    )
    description = " ".join(
        [
            "Experience with a cloud warehouse such as Snowflake, BigQuery, Redshift, Databricks, or similar.",
            "Responsibilities include ELT, data modeling, and warehouse performance tuning.",
        ]
        * 10
    )

    match = scored_match(
        profile,
        "Senior Data Engineer",
        description,
    )

    assert match.overall_score == 95
    assert match.analysis.hard_requirements_missing == []


def test_nonmandatory_central_specialization_caps_to_review_not_skip() -> None:
    profile = make_profile(
        production_skills=[
            "Python",
            "SQL",
            "Snowflake",
        ],
        industries=[
            "financial services",
        ],
    )
    description = " ".join(
        [
            "The team builds analytics for healthcare claims teams.",
            "Responsibilities include claims analytics, data modeling, and claims reporting.",
            "You will partner with operations teams on healthcare claims data products.",
        ]
        * 8
    )

    match = scored_match(
        profile,
        "Senior Data Engineer, Claims Analytics",
        description,
    )

    assert match.recommendation == "review"
    assert match.overall_score == 74
    assert match.analysis.hard_requirements_missing == []
    assert any(
        "healthcare claims" in risk
        for risk in match.analysis.risk_factors
    )


def test_project_ai_does_not_satisfy_production_ai_requirement() -> None:
    profile = make_profile(
        production_skills=[
            "Python",
            "SQL",
            "Snowflake",
        ],
        project_skills=[
            "Personal RAG chatbot with agentic workflows and MCP tools",
        ],
    )
    description = " ".join(
        [
            "Required production experience building RAG and agentic AI systems.",
            "Real users depend on these LLM tool integrations.",
        ]
        * 10
    )

    match = scored_match(
        profile,
        "Senior Data Engineer, Agentic AI",
        description,
    )

    assert match.overall_score <= 49
    assert any(
        "Project-only" in risk
        for risk in match.analysis.risk_factors
    )


def test_production_ai_experience_satisfies_critical_requirement() -> None:
    profile = make_profile(
        production_skills=[
            "Python",
            "SQL",
        ],
        ai_ml_experience=[
            "Owned production RAG pipelines and deployed agentic LLM systems for real users.",
        ],
    )
    description = " ".join(
        [
            "Required production experience building RAG and agentic AI systems.",
            "Responsibilities include LLM tool integrations for real users.",
        ]
        * 10
    )

    match = scored_match(
        profile,
        "Senior Data Engineer, Agentic AI",
        description,
    )

    assert match.overall_score == 95
    assert match.analysis.hard_requirements_missing == []


def test_repeated_specialization_is_treated_as_critical() -> None:
    profile = make_profile(
        production_skills=[
            "Python",
            "SQL",
            "Snowflake",
        ],
    )
    description = " ".join(
        [
            "Summary: Databricks is central to this platform.",
            "Responsibilities include Databricks pipelines and lakehouse operations.",
            "Qualifications require deep expertise with Databricks in production.",
        ]
        * 8
    )

    match = scored_match(
        profile,
        "Senior Data Engineer",
        description,
    )

    assert match.overall_score <= 49
    assert any(
        "Databricks" in item
        for item in match.analysis.hard_requirements_missing
    )


def test_prompt_version_controls_cache_reuse() -> None:
    assert MATCHER_PROMPT_VERSION == "v3"

    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH

    with TemporaryDirectory() as temp_dir:
        database.DATA_DIR = Path(temp_dir)
        database.DATABASE_PATH = (
            Path(temp_dir) / "jobs.duckdb"
        )

        try:
            initialize_ai_tables()

            with database.get_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO resume_job_scores (
                        resume_hash,
                        record_key,
                        overall_score,
                        recommendation,
                        title_fit,
                        skills_fit,
                        experience_fit,
                        seniority_fit,
                        industry_fit,
                        location_fit,
                        confidence,
                        matching_strengths,
                        hard_requirements_missing,
                        preferred_qualifications_missing,
                        risk_factors,
                        summary,
                        description_word_count,
                        description_complete,
                        model_name,
                        prompt_version
                    )
                    VALUES (
                        'resume-1',
                        'job-1',
                        70,
                        'review',
                        70,
                        70,
                        70,
                        70,
                        70,
                        70,
                        'medium',
                        '[]',
                        '[]',
                        '[]',
                        '[]',
                        'cached v1 result',
                        100,
                        true,
                        'gemini-test',
                        'v2'
                    )
                    """
                )

            assert load_cached_job_score(
                resume_hash="resume-1",
                record_key="job-1",
                model_name="gemini-test",
                prompt_version="v2",
            )
            assert load_cached_job_score(
                resume_hash="resume-1",
                record_key="job-1",
                model_name="gemini-test",
                prompt_version=MATCHER_PROMPT_VERSION,
            ) is None

        finally:
            database.DATA_DIR = old_data_dir
            database.DATABASE_PATH = (
                old_database_path
            )


def test_incomplete_description_cannot_be_apply() -> None:
    analysis = make_analysis(
        hard_missing=[],
    )

    assert determine_recommendation(
        overall_score=95,
        analysis=analysis,
        description_complete=False,
    ) == "review"


def test_incomplete_description_can_still_be_skip() -> None:
    analysis = make_analysis(
        hard_missing=[],
    )

    assert determine_recommendation(
        overall_score=40,
        analysis=analysis,
        description_complete=False,
    ) == "skip"


def main() -> None:
    test_cloud_platform_alternatives_are_not_missing()
    test_data_warehouse_alternatives_are_not_missing()
    test_git_and_cicd_are_not_missing()
    test_explicit_azure_remains_missing()
    test_all_warehouses_requirement_can_remain_missing()
    test_agile_can_remain_missing_when_absent()
    test_agile_is_not_missing_when_demonstrated()
    test_missing_critical_ai_specialization_caps_score_and_explains_major_gap()
    test_missing_preferred_skill_does_not_trigger_critical_penalty()
    test_equivalent_warehouse_requirement_does_not_trigger_critical_penalty()
    test_databricks_in_alternative_warehouse_list_is_not_a_gap()
    test_databricks_or_snowflake_required_accepts_snowflake()
    test_databricks_preferred_only_is_not_a_major_gap()
    test_mandatory_core_databricks_requirement_remains_hard_gap()
    test_cloud_warehouse_alternatives_accept_redshift()
    test_nonmandatory_central_specialization_caps_to_review_not_skip()
    test_project_ai_does_not_satisfy_production_ai_requirement()
    test_production_ai_experience_satisfies_critical_requirement()
    test_repeated_specialization_is_treated_as_critical()
    test_prompt_version_controls_cache_reuse()
    test_incomplete_description_cannot_be_apply()
    test_incomplete_description_can_still_be_skip()
    print("Resume matcher validation tests passed.")


if __name__ == "__main__":
    main()
