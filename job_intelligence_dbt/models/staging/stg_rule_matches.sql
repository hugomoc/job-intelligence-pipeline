with source as (
    select *
    from {{ source('job_pipeline', 'job_matches') }}
)

select
    record_key,
    search_id,
    search_title,
    match_score,
    title_score,
    location_score,
    keyword_score,
    freshness_score,
    matched_keywords,
    excluded_keywords,
    reasons,
    is_recommended,
    scored_at,
    coalesce(needs_review, false) as needs_review
from source
