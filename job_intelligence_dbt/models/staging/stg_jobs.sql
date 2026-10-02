with source as (
    select *
    from {{ source('job_pipeline', 'raw_jobs') }}
),

identity_fields as (
    select
        *,
        regexp_replace(
            lower(coalesce(source, '')),
            '[^a-z0-9]+',
            ' ',
            'g'
        ) as normalized_source,
        coalesce(nullif(job_fingerprint, ''), record_key) as duplicate_fingerprint,
        regexp_extract(
            coalesce(apply_url, ''),
            '/jobs/view/([0-9]+)',
            1
        ) as linkedin_job_id,
        regexp_extract(
            coalesce(apply_url, ''),
            '[?&]gh_jid=([^&]+)',
            1
        ) as greenhouse_job_id,
        regexp_extract(
            coalesce(apply_url, ''),
            '[?&](jobId|job_id|jid|postingId|requisitionId)=([^&]+)',
            2
        ) as query_job_id
    from source
),

jobs_with_exact_identity as (
    select
        *,
        case
            when nullif(source_job_id, '') is not null
                then normalized_source || '|' || lower(trim(source_job_id))
            when nullif(linkedin_job_id, '') is not null
                then normalized_source || '|linkedin:' || linkedin_job_id
            when nullif(greenhouse_job_id, '') is not null
                then normalized_source || '|gh_jid:' || greenhouse_job_id
            when nullif(query_job_id, '') is not null
                then normalized_source || '|job_id:' || query_job_id
            when regexp_matches(
                lower(coalesce(apply_url, '')),
                '(sendgrid\\.net|sg3email\\.lensa\\.com|/ls/click)'
            )
                then normalized_source || '|record:' || record_key
            when nullif(apply_url, '') is not null
                then normalized_source || '|' || lower(trim(apply_url))
            else normalized_source || '|record:' || record_key
        end as exact_posting_key
    from identity_fields
)

select
    record_key,
    job_fingerprint as duplicate_fingerprint,
    exact_posting_key,
    exact_posting_key as canonical_job_key,
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
from jobs_with_exact_identity
