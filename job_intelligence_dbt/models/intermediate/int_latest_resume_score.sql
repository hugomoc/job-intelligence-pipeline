with ranked_scores as (
    select
        scores.*,
        jobs.exact_posting_key,
        jobs.canonical_job_key,
        row_number() over (
            partition by scores.resume_hash, jobs.exact_posting_key
            order by
                scores.scored_at desc nulls last,
                scores.overall_score desc,
                scores.record_key,
                scores.model_name,
                scores.prompt_version
        ) as resume_score_rank
    from {{ ref('stg_resume_job_scores') }}
        as scores
    inner join {{ ref('stg_jobs') }} as jobs
        on scores.record_key = jobs.record_key
)

select
    resume_hash,
    exact_posting_key,
    canonical_job_key,
    record_key as scored_record_key,
    overall_score as ai_score,
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
    model_name as ai_model_name,
    prompt_version as ai_prompt_version,
    scored_at as ai_scored_at
from ranked_scores
where resume_score_rank = 1
