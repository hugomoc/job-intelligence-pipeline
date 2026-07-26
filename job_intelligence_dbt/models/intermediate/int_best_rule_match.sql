with ranked_matches as (
    select
        matches.*,
        jobs.canonical_job_key,
        row_number() over (
            partition by jobs.canonical_job_key
            order by
                matches.is_recommended desc,
                matches.needs_review desc,
                matches.match_score desc,
                matches.title_score desc,
                matches.search_id
        ) as rule_match_rank
    from {{ ref('stg_rule_matches') }}
        as matches
    inner join {{ ref('stg_jobs') }} as jobs
        on matches.record_key = jobs.record_key
)

select
    canonical_job_key,
    record_key as rule_record_key,
    search_id as best_search_id,
    search_title as best_search_title,
    match_score as rule_score,
    title_score as rule_title_score,
    location_score as rule_location_score,
    keyword_score as rule_keyword_score,
    freshness_score as rule_freshness_score,
    matched_keywords,
    excluded_keywords,
    reasons as rule_reasons,
    is_recommended as rule_is_recommended,
    needs_review as rule_needs_review,
    scored_at as rule_scored_at
from ranked_matches
where rule_match_rank = 1
