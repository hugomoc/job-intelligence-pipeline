# Stored-result evaluation (offline)

Aggregate counts and metrics only; no per-job identifiers.

Label rows: 50; joined rows: 50; unmatched/excluded rows: 0.
Stored-verified rows: 40; unsure labels: 20 (verified: 20); ambiguous rows: 3.
Stored scores with incomplete descriptions: 1.

## Resolution

| Metric | Result |
| --- | --- |
| precision | 100.0%; 95% Wilson CI [83.9%, 100.0%] (n=20) |
| false_accept_rate | 0.0%; 95% Wilson CI [0.0%, 16.1%] (n=20) |
| recall | insufficient data (n=0) |
| deferred_not_found_rate | 14.0%; 95% Wilson CI [7.0%, 26.2%] (n=50) |

Unsure and blank resolution labels are excluded from precision and false-accept denominators. Recall uses only rows with a supplied expected URL and requires a stored verified result. Deferred/not-found rate uses all joined label rows; AMBIGUOUS is counted separately.

URL comparison: src.verified_posting_identity.verified_posting_key_from_url, with normalize_official_url as the fallback (tracking removed; identity parameters retained).

## Fit scoring

| Metric | Result |
| --- | --- |
| mae | insufficient data (n=9) |
| spearman | insufficient data (n=9) |
| within_10 | insufficient data (n=9) |
| agreement | insufficient data (n=9) |

Confusion matrix (stored rows, human columns; n=9; insufficient data):

| Stored recommendation | apply | review | skip |
| --- | ---: | ---: | ---: |
| apply | 2 | 0 | 0 |
| review | 1 | 1 | 1 |
| skip | 2 | 2 | 0 |
| other | 0 | 0 | 0 |

Agreement by stored prompt version (nonstandard version names are hashed):

| Prompt version | Agreement |
| --- | --- |
| v2 | insufficient data (n=1) |
| v3 | insufficient data (n=8) |

MAE, rank correlation and within-10 use paired numeric ratings only. Decision agreement uses paired decisions only. Spearman uses average ranks for ties; constant ranks are undefined. Estimates with n < 10, or undefined correlation, are reported as insufficient data.

This is a stratified labeled sample, not a population-wide recall estimate. Stored results can be stale, incomplete or from different resume profiles; the latest score per exact record key is used without rescoring. Wilson intervals describe the selected labeled rows and do not correct selection bias. No persisted per-job search/AI request counts were found in the source schemas; cost metrics are omitted.
