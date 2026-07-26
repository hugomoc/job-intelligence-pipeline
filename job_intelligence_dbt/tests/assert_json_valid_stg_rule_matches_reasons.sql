select *
from {{ ref('stg_rule_matches') }}
where reasons is not null
    and not json_valid(reasons)
