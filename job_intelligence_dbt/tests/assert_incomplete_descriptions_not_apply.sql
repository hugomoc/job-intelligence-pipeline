select *
from {{ ref('mart_job_recommendations') }}
where recommendation = 'apply'
    and has_incomplete_description
