select
    resume_hash,
    record_key,
    count(*) as row_count
from {{ ref('stg_resume_job_scores') }}
group by 1, 2
having count(*) > 1
