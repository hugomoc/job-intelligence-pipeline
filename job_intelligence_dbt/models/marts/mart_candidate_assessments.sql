-- Optional until the explicitly approved candidate migration is applied.
-- Source snapshots are authoritative; rebuilding this derived table never scores jobs.
{% set assessments = adapter.get_relation(database=target.database, schema='main', identifier='candidate_assessments') if execute else none %}
{% set active = adapter.get_relation(database=target.database, schema='main', identifier='candidate_active_assessments') if execute else none %}
{% if assessments is not none and active is not none %}
select
    a.candidate_id,
    a.job_identity,
    a.assessment_id,
    a.record_key,
    a.resume_hash,
    cast(json_extract_string(a.payload_json, '$.overall_score') as integer) as ai_score,
    json_extract_string(a.payload_json, '$.recommendation') as recommendation,
    json_extract_string(a.payload_json, '$.model_name') as ai_model_name,
    json_extract_string(a.payload_json, '$.prompt_version') as ai_prompt_version,
    a.completed_at as ai_scored_at,
    a.payload_json as assessment_evidence
from {{ assessments }} a
inner join {{ active }} p
    on a.assessment_id = p.assessment_id and a.candidate_id = p.candidate_id
{% else %}
select
    cast(null as varchar) as candidate_id,
    cast(null as varchar) as job_identity,
    cast(null as varchar) as assessment_id,
    cast(null as varchar) as record_key,
    cast(null as varchar) as resume_hash,
    cast(null as integer) as ai_score,
    cast(null as varchar) as recommendation,
    cast(null as varchar) as ai_model_name,
    cast(null as varchar) as ai_prompt_version,
    cast(null as timestamptz) as ai_scored_at,
    cast(null as varchar) as assessment_evidence
where false
{% endif %}
