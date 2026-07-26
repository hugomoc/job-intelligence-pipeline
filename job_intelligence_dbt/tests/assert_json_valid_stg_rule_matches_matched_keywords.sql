select *
from {{ ref('stg_rule_matches') }}
where matched_keywords is not null
    and not json_valid(matched_keywords)
