with source as (
    select *
    from {{ source('job_pipeline', 'resume_profiles') }}
)

select
    resume_hash,
    filename,
    model_name,
    profile_json,
    created_at
from source
