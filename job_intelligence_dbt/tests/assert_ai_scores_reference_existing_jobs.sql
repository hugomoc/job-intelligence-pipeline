select scores.record_key
from {{ ref('stg_resume_job_scores') }} as scores
left join {{ ref('stg_jobs') }} as jobs
    on scores.record_key = jobs.record_key
where jobs.record_key is null
