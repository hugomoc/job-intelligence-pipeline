select
    resume_hash,
    canonical_job_key,
    count(*) as row_count
from {{ ref('mart_job_recommendations') }}
group by 1, 2
having count(*) > 1
