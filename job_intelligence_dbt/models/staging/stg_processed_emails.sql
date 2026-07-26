with source as (
    select *
    from {{ source('job_pipeline', 'processed_emails') }}
)

select
    email_key,
    source,
    email_message_id,
    email_subject,
    email_date,
    source_folder,
    processed_at
from source
