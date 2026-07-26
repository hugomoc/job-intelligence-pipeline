select *
from {{ ref('stg_resume_job_scores') }}
where recommendation = 'apply'
    and overall_score < 75
