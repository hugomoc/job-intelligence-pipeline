select candidate_id, job_identity
from {{ ref('mart_candidate_assessments') }}
group by candidate_id, job_identity
having count(*) <> 1
