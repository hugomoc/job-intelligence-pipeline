from pathlib import Path
from tempfile import TemporaryDirectory

from src.ai.resume_matcher import (
    MATCHER_PROMPT_VERSION,
    JobMatchAnalysis,
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
    databases_warehouses: list[str] | None = None,
    cloud_platforms: list[str] | None = None,
    tools_platforms: list[str] | None = None,
    data_engineering_capabilities: list[str] | None = None,
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
        project_skills=[],
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
        ai_ml_experience=[],
        industries=[],
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
) -> JobMatchAnalysis:
    return JobMatchAnalysis(
        title_fit=80,
        skills_fit=80,
        experience_fit=80,
        seniority_fit=80,
        industry_fit=70,
        location_fit=70,
        confidence="high",
        matching_strengths=[],
        hard_requirements_missing=hard_missing,
        preferred_qualifications_missing=(
            preferred_missing or []
        ),
        risk_factors=[],
        summary="Synthetic analysis.",
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


def test_prompt_version_controls_cache_reuse() -> None:
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
                        'v1'
                    )
                    """
                )

            assert load_cached_job_score(
                resume_hash="resume-1",
                record_key="job-1",
                model_name="gemini-test",
                prompt_version="v1",
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


def main() -> None:
    test_cloud_platform_alternatives_are_not_missing()
    test_data_warehouse_alternatives_are_not_missing()
    test_git_and_cicd_are_not_missing()
    test_explicit_azure_remains_missing()
    test_all_warehouses_requirement_can_remain_missing()
    test_agile_can_remain_missing_when_absent()
    test_agile_is_not_missing_when_demonstrated()
    test_prompt_version_controls_cache_reuse()
    print("Resume matcher validation tests passed.")


if __name__ == "__main__":
    main()
