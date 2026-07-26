select *
from {{ ref('stg_resume_job_scores') }}
where risk_factors is not null
    and not json_valid(risk_factors)
