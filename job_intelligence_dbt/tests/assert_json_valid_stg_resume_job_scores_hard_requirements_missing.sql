select *
from {{ ref('stg_resume_job_scores') }}
where hard_requirements_missing is not null
    and not json_valid(hard_requirements_missing)
