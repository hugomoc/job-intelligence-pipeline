select
    scores.resume_hash,
    jobs.exact_posting_key,
    jobs.canonical_job_key,
    jobs.canonical_record_key,
    scores.scored_record_key,
    rules.rule_record_key,
    jobs.duplicate_fingerprint,

    jobs.title as job_title,
    jobs.company_name,
    jobs.location,
    jobs.salary_text,
    jobs.source,
    jobs.apply_url,
    jobs.posted_age_text,
    jobs.discovered_at,

    rules.rule_score,
    rules.best_search_id,
    rules.best_search_title,
    rules.rule_title_score,
    rules.rule_location_score,
    rules.rule_keyword_score,
    rules.rule_freshness_score,
    rules.matched_keywords,
    rules.excluded_keywords,
    rules.rule_reasons,
    rules.rule_is_recommended,
    rules.rule_needs_review,

    scores.ai_score,
    case
        when not scores.description_complete
            and scores.recommendation = 'apply'
        then 'review'
        else scores.recommendation
    end as recommendation,
    scores.confidence,
    scores.title_fit,
    scores.skills_fit,
    scores.experience_fit,
    scores.seniority_fit,
    scores.industry_fit,
    scores.location_fit,
    scores.matching_strengths,
    scores.hard_requirements_missing,
    scores.preferred_qualifications_missing,
    scores.risk_factors,
    scores.summary,
    scores.description_word_count,
    scores.description_complete,
    not scores.description_complete as has_incomplete_description,
    scores.ai_model_name,
    scores.ai_prompt_version,
    scores.ai_scored_at,

    row_number() over (
        partition by scores.resume_hash
        order by
            scores.ai_score desc,
            case
                when scores.recommendation = 'apply'
                    and scores.description_complete
                then 3
                when scores.recommendation = 'review' then 2
                when scores.recommendation = 'apply'
                    and not scores.description_complete
                then 2
                when scores.recommendation = 'skip' then 1
                else 0
            end desc,
            rules.rule_score desc nulls last,
            jobs.title,
            jobs.company_name
    ) as ai_score_rank
from {{ ref('int_latest_resume_score') }} as scores
inner join {{ ref('int_jobs_deduplicated') }} as jobs
    on scores.exact_posting_key = jobs.exact_posting_key
left join {{ ref('int_best_rule_match') }} as rules
    on scores.exact_posting_key = rules.exact_posting_key
