select
    record_key,
    search_id,
    count(*) as row_count
from {{ ref('stg_rule_matches') }}
group by 1, 2
having count(*) > 1
