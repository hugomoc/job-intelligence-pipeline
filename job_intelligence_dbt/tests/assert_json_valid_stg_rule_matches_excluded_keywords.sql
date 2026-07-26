select *
from {{ ref('stg_rule_matches') }}
where excluded_keywords is not null
    and not json_valid(excluded_keywords)
