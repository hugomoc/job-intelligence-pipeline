select
    current_timestamp as summarized_at,
    (
        select count(*)
        from {{ ref('stg_jobs') }}
    ) as raw_job_count,
    (
        select count(*)
        from {{ ref('int_jobs_deduplicated') }}
    ) as deduplicated_job_count,
    (
        select count(distinct record_key)
        from {{ ref('stg_rule_matches') }}
    ) as rule_matched_job_count,
    (
        select count(distinct resume_hash)
        from {{ ref('int_latest_resume_score') }}
    ) as scored_resume_count,
    (
        select count(*)
        from {{ ref('int_latest_resume_score') }}
        where recommendation = 'apply'
    ) as ai_apply_count,
    (
        select count(*)
        from {{ ref('int_latest_resume_score') }}
        where recommendation = 'review'
    ) as ai_review_count,
    (
        select count(*)
        from {{ ref('int_latest_resume_score') }}
        where recommendation = 'skip'
    ) as ai_skip_count,
    (
        select count(*)
        from {{ ref('int_latest_resume_score') }}
        where not description_complete
    ) as incomplete_description_score_count,
    (
        select count(*)
        from {{ ref('mart_job_recommendations') }}
    ) as recommendation_record_count,
    (
        select max(ai_scored_at)
        from {{ ref('int_latest_resume_score') }}
    ) as latest_ai_scored_at,
    (
        select max(discovered_at)
        from {{ ref('stg_jobs') }}
    ) as latest_job_discovered_at
