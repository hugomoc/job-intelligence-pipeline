with source as (
    select *
    from {{ source('job_pipeline', 'raw_jobs') }}
)

select
    record_key,
    job_fingerprint,
    coalesce(nullif(job_fingerprint, ''), record_key) as canonical_job_key,
    source,
    source_job_id,
    title,
    company_name,
    location,
    salary_text,
    description,
    apply_url,
    email_message_id,
    email_subject,
    email_date,
    source_folder,
    discovered_at,
    description_updated_at
from source
