select *
from {{ ref('stg_resume_job_scores') }}
where matching_strengths is not null
    and not json_valid(matching_strengths)
