select *
from {{ ref('stg_resume_job_scores') }}
where preferred_qualifications_missing is not null
    and not json_valid(preferred_qualifications_missing)
