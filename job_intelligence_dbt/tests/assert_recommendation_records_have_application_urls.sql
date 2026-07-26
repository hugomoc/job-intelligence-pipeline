select *
from {{ ref('mart_job_recommendations') }}
where apply_url is null
    or trim(apply_url) = ''
