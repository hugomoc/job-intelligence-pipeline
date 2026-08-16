from src.ai.job_eligibility import (
    JobEligibilityAnalysis,
    build_job_eligibility_prompt,
)
from src.ai.resume_profiler import ResumeProfile


def minimal_resume_profile() -> ResumeProfile:
    return ResumeProfile(
        professional_summary="Senior data engineer with SQL, Python, dbt and Snowflake experience.",
        target_roles=["Data Engineer", "Analytics Engineer"],
        current_or_recent_titles=["Senior Data Engineer"],
        seniority="senior",
        years_of_relevant_experience=12,
        production_skills=["SQL", "Python", "dbt", "Snowflake"],
        project_skills=[],
        data_engineering_capabilities=["ELT", "data modeling"],
        bi_analytics_skills=["analytics engineering"],
        programming_languages=["Python", "SQL"],
        databases_warehouses=["Snowflake"],
        cloud_platforms=["AWS"],
        tools_platforms=["Airflow", "AWS"],
        ai_ml_experience=[],
        industries=[],
        certifications=[],
        education=[],
        quantified_achievements=[],
        leadership_evidence=[],
        strengths_for_job_matching=["SQL", "Python", "dbt"],
        profile_confidence="high",
    )


def test_title_only_prompt_marks_missing_description() -> None:
    prompt = build_job_eligibility_prompt(
        resume_profile=minimal_resume_profile(),
        job={
            "title": "SAP ABAP Developer",
            "company_name": "Example Co",
            "description": None,
        },
    )

    assert "Description available: no" in prompt
    assert "SAP ABAP Developer" in prompt


def test_job_eligibility_analysis_validates_decisions() -> None:
    analysis = JobEligibilityAnalysis(
        decision="exclude",
        confidence="high",
        reason="SAP ABAP is outside the candidate's demonstrated background.",
        matched_resume_signals=[],
        missing_or_mismatched_signals=["SAP ABAP"],
    )

    assert analysis.decision == "exclude"


def main() -> None:
    test_title_only_prompt_marks_missing_description()
    test_job_eligibility_analysis_validates_decisions()
    print("Job eligibility tests passed.")


if __name__ == "__main__":
    main()
