select
    resume_hash,
    exact_posting_key,
    count(*) as row_count
from {{ ref('mart_job_recommendations') }}
group by 1, 2
having count(*) > 1
