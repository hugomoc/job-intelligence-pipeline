from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.ai.resume_matcher import (
    MATCHER_PROMPT_VERSION,
    FitAnalysis,
    JobMatchAnalysis,
    RequirementMatch,
    ResumeMatcherError,
    build_job_match_prompt,
    build_resume_job_match,
    get_openai_job_match_schema,
    determine_recommendation,
    fit_analysis_to_job_match_analysis,
    fit_output_contradictions,
    validate_missing_qualifications,
)
from src.ai.resume_profiler import (
    CandidateEligibilityFacts,
    ResumeProfile,
    apply_candidate_eligibility_overrides,
)
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


def test_closed_databricks_or_spark_does_not_accept_snowflake() -> None:
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
            "Requirements include Databricks or Spark required for production data engineering.",
            "Responsibilities include lakehouse operations and production pipelines.",
        ]
        * 10
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


def test_closed_domo_or_tableau_does_not_accept_looker() -> None:
    profile = make_profile(
        production_skills=[
            "SQL",
            "Looker",
        ],
        tools_platforms=[
            "Looker",
        ],
    )
    description = " ".join(
        [
            "Requirements include Domo or Tableau required for executive dashboard delivery.",
            "Responsibilities include BI development, reporting, and stakeholder analytics.",
        ]
        * 10
    )

    match = scored_match(
        profile,
        "Senior BI Engineer",
        description,
    )

    assert match.overall_score <= 49
    assert any(
        "Domo" in item
        for item in match.analysis.hard_requirements_missing
    )


def test_open_bi_platform_alternatives_accept_power_bi() -> None:
    profile = make_profile(
        production_skills=[
            "SQL",
            "Power BI",
        ],
        tools_platforms=[
            "Power BI",
        ],
    )
    description = " ".join(
        [
            "Requirements include Domo, Tableau, Looker, or similar BI platform experience.",
            "Responsibilities include BI development, reporting, and stakeholder analytics.",
        ]
        * 10
    )

    match = scored_match(
        profile,
        "Senior BI Engineer",
        description,
    )

    assert match.overall_score == 95
    assert match.analysis.hard_requirements_missing == []


def test_closed_aws_or_azure_does_not_accept_gcp() -> None:
    profile = make_profile(
        cloud_platforms=[
            "GCP",
        ],
        production_skills=[
            "Google Cloud Platform",
        ],
    )

    assert validated_missing(
        profile,
        ["AWS or Azure required"],
    ) == ["AWS or Azure required"]


def test_open_equivalent_cloud_platform_accepts_gcp() -> None:
    profile = make_profile(
        cloud_platforms=[
            "GCP",
        ],
        production_skills=[
            "Google Cloud Platform",
        ],
    )

    assert validated_missing(
        profile,
        ["AWS, Azure, GCP, or equivalent cloud platform"],
    ) == []


def test_specific_platform_years_are_not_satisfied_by_general_experience() -> None:
    profile = make_profile(
        project_skills=["dbt"],
        production_skills=["Python", "SQL", "AWS"],
        tools_platforms=["dbt"],
    )
    description = (
        "Requirements\n"
        "3+ years of hands-on experience using dbt Cloud.\n"
        + "Build reliable SQL pipelines on AWS for business reporting. " * 12
    )
    match = scored_match(profile, "Data Analytics Engineer", description)
    assert match.overall_score == 49
    assert match.recommendation == "skip"
    assert any("dbt Cloud" in x for x in match.analysis.hard_requirements_missing)


def test_databricks_operational_ownership_is_central_without_explicit_required_label() -> None:
    profile = make_profile(
        databases_warehouses=["Snowflake"],
        production_skills=["Snowflake", "SQL"],
    )
    description = " ".join(
        [
            "Responsibilities: Manage and optimize our Databricks environment.",
            "Develop production Databricks notebooks and data pipelines.",
            "Provide reliable SQL analytics reporting for stakeholders.",
        ]
        * 8
    )
    match = scored_match(profile, "Senior Analytics Engineer", description)

    assert match.recommendation == "review"
    assert match.overall_score == 74
    assert any(
        "Databricks" in risk for risk in match.analysis.risk_factors
    )


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
    assert MATCHER_PROMPT_VERSION == "v7"

    old_data_dir = database.DATA_DIR
    old_database_path = database.DATABASE_PATH

    with TemporaryDirectory() as temp_dir:
        database.DATA_DIR = Path(temp_dir)
        database.DATABASE_PATH = (
            Path(temp_dir) / "jobs.duckdb"
        )

        try:
            initialize_ai_tables()
            from src.enrich_jobs import initialize_enrichment_tables
            initialize_enrichment_tables()

            with database.get_connection() as connection:
                connection.execute(
                    "INSERT INTO raw_jobs (record_key, job_fingerprint, source, title, company_name, apply_url, description) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ['job-1', 'job-1', 'linkedin', 'Data Engineer', 'Example', 'https://linkedin.com/jobs/view/job-1',
                     'Responsibilities include SQL pipelines and analytics. Qualifications include Python experience. ' * 20],
                )
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


def test_ai_first_score_is_not_recomputed_from_components() -> None:
    analysis = make_analysis([], score=95).model_copy(
        update={
            "overall_score": 63,
            "recommendation": "review",
            "summary": (
                "Strong generic BI fit, but a central hands-on technology "
                "responsibility is not demonstrated."
            ),
        }
    )
    match = scored_match(
        make_profile(),
        "Business Intelligence Developer II",
        (
            "Responsibilities include developing extraction and transformation "
            "routines using a specialized programming language. "
        ) * 12,
        analysis=analysis,
    )
    assert match.overall_score == 63
    assert match.recommendation == "review"


def test_ai_first_allows_high_score_review_with_material_uncertainty() -> None:
    analysis = make_analysis([], score=90).model_copy(
        update={
            "overall_score": 75,
            "recommendation": "review",
            "risk_factors": [
                "Material requirement needs human confirmation."
            ],
        }
    )

    match = scored_match(
        make_profile(),
        "Staff Data Analytics Engineer",
        (
            "Responsibilities include Snowflake analytics engineering, "
            "semantic modeling, and product analytics. "
        ) * 12,
        analysis=analysis,
    )

    assert match.overall_score == 75
    assert match.recommendation == "review"


def test_candidate_eligibility_env_overrides_are_explicit() -> None:
    profile = make_profile()

    with patch.dict(
        "os.environ",
        {
            "CANDIDATE_CURRENT_COUNTRY": "United States",
            "CANDIDATE_CURRENT_REGION": "California",
            "CANDIDATE_PROFESSIONAL_LANGUAGES": "English, Portuguese",
            "CANDIDATE_CITIZENSHIP_COUNTRY_CODES": "US",
            "CANDIDATE_AUTHORIZED_WORK_COUNTRIES": "US",
            "CANDIDATE_REQUIRES_SPONSORSHIP": "false",
        },
        clear=False,
    ):
        updated = apply_candidate_eligibility_overrides(profile)

    facts = updated.personal_eligibility
    assert facts.current_country == "United States"
    assert facts.current_region == "California"
    assert facts.professional_languages == ["English", "Portuguese"]
    assert facts.citizenship_country_codes == ["US"]
    assert facts.authorized_work_countries == ["US"]
    assert facts.requires_sponsorship is False


def test_unknown_location_gap_cannot_force_skip() -> None:
    analysis = make_analysis(
        [
            "Location constraint: candidate profile does not establish "
            "residency in California, Oregon, Washington, Utah, or Texas"
        ],
        score=90,
    ).model_copy(
        update={
            "overall_score": 40,
            "recommendation": "skip",
        }
    )

    match = scored_match(
        make_profile(),
        "Staff Data Analytics Engineer",
        (
            "Remote candidates must be based in California, Oregon, Washington, "
            "Utah, or Texas. Responsibilities include SQL and data modeling. "
        ) * 10,
        analysis=analysis,
    )

    assert match.overall_score == 50
    assert match.recommendation == "review"
    assert match.analysis.hard_requirements_missing == []
    assert not any(
        "location" in risk.casefold()
        or "UNKNOWN personal eligibility" in risk
        for risk in match.analysis.risk_factors
    )


def test_explicit_california_location_removes_location_gap() -> None:
    profile = make_profile().model_copy(
        update={
            "personal_eligibility": CandidateEligibilityFacts(
                current_country="United States",
                current_region="California",
                authorized_work_countries=["US"],
                requires_sponsorship=False,
            )
        }
    )
    analysis = make_analysis(
        [
            "Location constraint: candidate profile does not establish "
            "residency in California, Oregon, Washington, Utah, or Texas"
        ],
        score=90,
    ).model_copy(
        update={
            "overall_score": 40,
            "recommendation": "skip",
        }
    )

    match = scored_match(
        profile,
        "Staff Data Analytics Engineer",
        (
            "Remote candidates must be based in California, Oregon, Washington, "
            "Utah, or Texas. Responsibilities include SQL and data modeling. "
        ) * 10,
        analysis=analysis,
    )

    assert match.overall_score == 50
    assert match.recommendation == "review"
    assert match.analysis.hard_requirements_missing == []


def test_preferred_language_mismatch_does_not_force_skip() -> None:
    profile = make_profile().model_copy(
        update={
            "personal_eligibility": CandidateEligibilityFacts(
                professional_languages=["English", "Portuguese"],
            )
        }
    )
    analysis = make_analysis([], score=90).model_copy(
        update={
            "overall_score": 75,
            "recommendation": "review",
        }
    )

    match = scored_match(
        profile,
        "Senior BI Analyst",
        (
            "Polish language proficiency is preferred, not required. "
            "Responsibilities include SQL, dashboards, and Python. "
        ) * 10,
        analysis=analysis,
    )

    assert match.overall_score == 75
    assert match.recommendation == "review"


def test_confirmed_mandatory_language_mismatch_forces_skip_from_review() -> None:
    profile = make_profile().model_copy(
        update={
            "personal_eligibility": CandidateEligibilityFacts(
                current_country="United States",
                current_region="California",
                professional_languages=["English", "Portuguese"],
            )
        }
    )
    analysis = make_analysis([], score=90).model_copy(
        update={
            "overall_score": 75,
            "recommendation": "review",
        }
    )

    match = scored_match(
        profile,
        "Senior BI Analyst",
        (
            "Fluency in both Polish and English is required. "
            "Responsibilities include SQL, dashboards, and Python. "
        ) * 10,
        analysis=analysis,
    )

    assert match.overall_score == 49
    assert match.recommendation == "skip"
    assert any(
        "Fluency in both Polish and English is required" in gap
        for gap in match.analysis.hard_requirements_missing
    )


def test_confirmed_missing_required_language_can_remain_skip() -> None:
    profile = make_profile().model_copy(
        update={
            "personal_eligibility": CandidateEligibilityFacts(
                current_country="United States",
                current_region="California",
                professional_languages=["English", "Portuguese"],
            )
        }
    )
    analysis = make_analysis(
        ["Demonstrated professional fluency in Polish"],
        score=90,
    ).model_copy(
        update={
            "overall_score": 40,
            "recommendation": "skip",
        }
    )

    match = scored_match(
        profile,
        "Senior BI Analyst",
        (
            "Fluency in both Polish and English is required. "
            "Responsibilities include SQL, dashboards, and Python. "
        ) * 10,
        analysis=analysis,
    )

    assert match.overall_score == 40
    assert match.recommendation == "skip"
    assert match.analysis.hard_requirements_missing == [
        "Demonstrated professional fluency in Polish"
    ]


def test_openai_schema_makes_nested_requirement_rows_strict() -> None:
    schema = get_openai_job_match_schema()
    requirement_schema = schema["$defs"]["RequirementMatch"]

    assert requirement_schema["additionalProperties"] is False
    assert set(requirement_schema["required"]) == {
        "job_requirement",
        "candidate_background",
        "match",
    }


def test_requirement_matches_are_explanatory_only() -> None:
    analysis = make_analysis([], score=95).model_copy(
        update={
            "overall_score": 82,
            "recommendation": "apply",
            "requirement_matches": [
                RequirementMatch(
                    job_requirement="Advanced SQL",
                    candidate_background="Extensive production SQL experience",
                    match="excellent",
                ),
                RequirementMatch(
                    job_requirement="Healthcare domain experience",
                    candidate_background="No direct healthcare experience demonstrated",
                    match="gap",
                ),
            ],
        }
    )

    match = scored_match(
        make_profile(),
        "Staff Data Analytics Engineer",
        (
            "Responsibilities include advanced SQL, analytics engineering, "
            "data modeling, and healthcare analytics. "
        ) * 12,
        analysis=analysis,
    )

    assert match.overall_score == 82
    assert match.recommendation == "apply"
    assert len(match.analysis.requirement_matches) == 2
    assert match.analysis.requirement_matches[1].match == "gap"


def test_ai_first_rejects_apply_with_missing_core_requirement() -> None:
    analysis = make_analysis(
        ["Develop extraction and transformation routines using ABAP"],
        score=95,
    ).model_copy(
        update={
            "overall_score": 87,
            "recommendation": "apply",
        }
    )

    try:
        scored_match(
            make_profile(),
            "Business Intelligence Developer II",
            (
                "Responsibilities include developing extraction and transformation "
                "routines and user exits using ABAP. "
            ) * 12,
            analysis=analysis,
        )
    except ResumeMatcherError as error:
        assert "APPLY cannot include" in str(error)
    else:
        raise AssertionError("AI-first guardrail accepted APPLY with a core gap")


def test_ai_first_prompt_uses_actual_resume_and_requirements_rules() -> None:
    profile = make_profile(
        production_skills=["Cached profile skill that should not drive v7 scoring"],
    )
    prompt = build_job_match_prompt(
        profile,
        {
            "title": "Senior BI Analyst",
            "company_name": "Example",
            "location": "Remote",
            "description": "unused",
        },
        (
            "Location: Poland. Fluency in Polish and English. "
            "Responsibilities include SQL, dashboards, and Python."
        ),
        True,
        resume_text=(
            "ACTUAL RESUME EVIDENCE\n"
            "Built SQL pipelines and dashboards in production."
        ),
    )

    assert "ACTUAL RESUME EVIDENCE" in prompt
    assert "Cached profile skill that should not drive v7 scoring" not in prompt
    assert "WHOLE-REQUIREMENT EVIDENCE" in prompt
    assert "GAP VS PARTIAL" in prompt
    assert "OR / AND" in prompt
    assert "REQUIRED VS PREFERRED" in prompt
    assert "EQUIVALENT TECHNOLOGIES" in prompt
    assert "WORK ARRANGEMENT" in prompt
    assert "Do NOT include any of these in fit_score" in prompt
    assert "SUMMARY CONSISTENCY" in prompt



def test_v7_fit_score_is_used_directly_without_component_formula() -> None:
    fit = FitAnalysis(
        fit_score=85,
        required_matches=[
            "Advanced SQL",
            "Dimensional modeling",
        ],
        required_partial=[
            "Deep hands-on dbt experience",
        ],
        required_gaps=[],
        preferred_matches=["Looker"],
        preferred_partial=[],
        preferred_gaps=["Marketplace experience"],
        work_arrangement="Hybrid — San Francisco, CA — Tuesdays and Wednesdays",
        summary=(
            "Strong requirements fit; dbt depth is only partially demonstrated."
        ),
    )

    analysis = fit_analysis_to_job_match_analysis(fit)

    assert analysis.overall_score == 85
    assert analysis.recommendation == "apply"
    assert analysis.location_fit == 100
    assert analysis.hard_requirements_missing == []
    assert any(
        "Deep hands-on dbt experience" in risk
        for risk in analysis.risk_factors
    )


def test_v7_required_gap_keeps_high_score_in_review_not_apply() -> None:
    fit = FitAnalysis(
        fit_score=78,
        required_matches=["SQL"],
        required_partial=[],
        required_gaps=["Required control-systems experience"],
        preferred_matches=[],
        preferred_partial=[],
        preferred_gaps=[],
        work_arrangement="Remote",
        summary="Strong SQL background but a required capability is absent.",
    )

    analysis = fit_analysis_to_job_match_analysis(fit)

    assert analysis.overall_score == 78
    assert analysis.recommendation == "review"
    assert analysis.hard_requirements_missing == [
        "Required control-systems experience"
    ]


def test_v7_output_validator_catches_match_negated_by_summary() -> None:
    fit = FitAnalysis(
        fit_score=75,
        required_matches=["SQL"],
        required_partial=[],
        required_gaps=[],
        preferred_matches=[
            "Start-up and healthcare experience",
        ],
        preferred_partial=[],
        preferred_gaps=[],
        work_arrangement="Remote",
        summary=(
            "Optional startup and healthcare experience is absent."
        ),
    )

    contradictions = fit_output_contradictions(fit)

    assert any(
        "MATCH as absent" in item
        for item in contradictions
    )


def test_v7_output_validator_allows_qualified_partial_summary() -> None:
    fit = FitAnalysis(
        fit_score=85,
        required_matches=["SQL"],
        required_partial=[
            "Deep hands-on experience in dbt and SQL",
        ],
        required_gaps=[],
        preferred_matches=[],
        preferred_partial=[],
        preferred_gaps=[],
        work_arrangement="Hybrid",
        summary=(
            "Deep professional production-grade dbt usage is only partially "
            "demonstrated rather than extensively proven."
        ),
    )

    assert fit_output_contradictions(fit) == []


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
    test_closed_databricks_or_spark_does_not_accept_snowflake()
    test_closed_domo_or_tableau_does_not_accept_looker()
    test_open_bi_platform_alternatives_accept_power_bi()
    test_closed_aws_or_azure_does_not_accept_gcp()
    test_open_equivalent_cloud_platform_accepts_gcp()
    test_databricks_preferred_only_is_not_a_major_gap()
    test_mandatory_core_databricks_requirement_remains_hard_gap()
    test_cloud_warehouse_alternatives_accept_redshift()
    test_nonmandatory_central_specialization_caps_to_review_not_skip()
    test_project_ai_does_not_satisfy_production_ai_requirement()
    test_production_ai_experience_satisfies_critical_requirement()
    test_repeated_specialization_is_treated_as_critical()
    test_prompt_version_controls_cache_reuse()
    test_ai_first_score_is_not_recomputed_from_components()
    test_ai_first_allows_high_score_review_with_material_uncertainty()
    test_candidate_eligibility_env_overrides_are_explicit()
    test_unknown_location_gap_cannot_force_skip()
    test_explicit_california_location_removes_location_gap()
    test_preferred_language_mismatch_does_not_force_skip()
    test_confirmed_mandatory_language_mismatch_forces_skip_from_review()
    test_confirmed_missing_required_language_can_remain_skip()
    test_openai_schema_makes_nested_requirement_rows_strict()
    test_requirement_matches_are_explanatory_only()
    test_ai_first_rejects_apply_with_missing_core_requirement()
    test_ai_first_prompt_uses_actual_resume_and_requirements_rules()
    test_v7_fit_score_is_used_directly_without_component_formula()
    test_v7_required_gap_keeps_high_score_in_review_not_apply()
    test_v7_output_validator_catches_match_negated_by_summary()
    test_v7_output_validator_allows_qualified_partial_summary()
    test_incomplete_description_cannot_be_apply()
    test_incomplete_description_can_still_be_skip()
    print("Resume matcher validation tests passed.")


if __name__ == "__main__":
    main()
