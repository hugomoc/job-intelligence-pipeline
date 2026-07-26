select
    record_key,
    resume_hash,
    score_name,
    score_value
from {{ ref('stg_resume_job_scores') }}
unpivot (
    score_value for score_name in (
        overall_score,
        title_fit,
        skills_fit,
        experience_fit,
        seniority_fit,
        industry_fit,
        location_fit
    )
)
where score_value not between 0 and 100

union all

select
    record_key,
    null as resume_hash,
    score_name,
    score_value
from {{ ref('stg_rule_matches') }}
unpivot (
    score_value for score_name in (
        match_score,
        title_score,
        location_score,
        keyword_score,
        freshness_score
    )
)
where score_value not between 0 and 100
