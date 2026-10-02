with ranked_jobs as (
    select
        *,
        row_number() over (
            partition by exact_posting_key
            order by
                case when description is not null and trim(description) <> '' then 1 else 0 end desc,
                description_updated_at desc nulls last,
                discovered_at desc nulls last,
                record_key
        ) as canonical_job_rank
    from {{ ref('stg_jobs') }}
)

select
    record_key as canonical_record_key,
    duplicate_fingerprint,
    exact_posting_key,
    canonical_job_key,
    source,
    source_job_id,
    title,
    company_name,
    location,
    salary_text,
    description,
    posted_age_text,
    apply_url,
    email_message_id,
    email_subject,
    email_date,
    source_folder,
    discovered_at,
    description_updated_at
from ranked_jobs
where canonical_job_rank = 1
