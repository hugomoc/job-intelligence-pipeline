with source as (
    select *
    from {{ source('job_pipeline', 'resume_job_scores') }}
)

select
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
    coalesce(prompt_version, 'v1') as prompt_version,
    scored_at
from source
