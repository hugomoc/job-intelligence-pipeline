# Offline evaluation of stored resolution and fit scores

This tool reads saved results and your hand labels. It does not resolve URLs,
score jobs, fetch pages, load dotenv, initialize tables or write to the
application database. Run commands from the repository root with its dependencies
installed. No new dependency is needed.

## Make and complete a private label sheet

```sh
python -m evaluation.make_label_sheet --output ~/job-eval/labels_todo.csv --n 50
```

`--database PATH` defaults to the application's `src.database.DATABASE_PATH`.
That module and the URL-identity helpers have only safe, pure imports; neither
API clients nor dotenv are imported by the evaluation modules. The exact
connection code in `read_stored_rows` is:

```python
with duckdb.connect(str(database.expanduser().resolve()), read_only=True) as connection:
```

Only a SELECT is executed. There are no initializers, migrations, update
statements or calls to the application's writable `get_connection()` helper.
A missing database or schema fails instead of creating or repairing it.

The default sample targets 40 stored-verified and 10 unresolved jobs, rotating
through available source/status groups with `--seed 0` for reproducibility.
Verified means `FOUND_VERIFIED` (or `VERIFIED`) with a nonempty stored URL;
unresolved means `OFFICIAL_NOT_FOUND`/`NOT_FOUND`, `SEARCH_DEFERRED`/`DEFERRED`,
or `AMBIGUOUS`. LinkedIn and aggregator origins are included when available.
Small or uneven pools can change the ratio or return fewer rows; missing
outcomes are not invented. Empty descriptions and quarantine-marked attempt
errors/identity reasons are excluded, including descriptions cleared by the
quarantine operation. The excerpt is the first 600 characters.

The sheet includes the record key, company, title, source, original apply URL,
stored official URL and excerpt. All four label fields start empty. Model
scores, recommendations and confidence are deliberately absent.

Copy `labels_todo.csv` to `~/job-eval/labels_done.csv` and label without looking
at model output:

- `resolution_label`: `correct` means the stored official URL identifies this
  same job and employer; `wrong_job` means it does not; `unsure` means you cannot
  establish the match. Leave blank if there is no stored URL to judge.
- `expected_official_url`: optional correct official posting URL, particularly
  for unresolved jobs. Use an absolute HTTP(S) URL. This provides the recall
  denominator; a row without it does not establish a missed resolution.
- `my_fit_rating`: 0–100; 90+ means apply now, 75–89 strong, 50–74 review, below
  50 skip. Judge requirements and your fit consistently. Leave blank when the
  excerpt is insufficient rather than guessing. Resolve ties consistently.
- `my_decision`: `apply`, `review` or `skip`, independently of the numeric rating.

`labels.example.csv` has six fictional examples using `example.invalid`.
It must be copied outside the repo before use; its keys will not match real jobs.

## Evaluate the completed labels

```sh
python -m evaluation.run_eval --labels ~/job-eval/labels_done.csv
```

`--database PATH` selects a saved database. `--out-dir` selects the aggregate
report directory (default `docs/`, producing `docs/eval_results.md`); an external
directory is also allowed. Private details always default to
`~/job-eval/results_detail.csv`. The detail file cannot be overwritten unless
`--force` is given. Output paths must differ from the database and label input
paths, including resolved symlinks. Existing aggregate reports can be replaced.

Labels join by exact `record_key`. Duplicate label keys and invalid labels or
ratings are rejected. The latest score per record is selected using `scored_at`
(with `resume_hash` as a deterministic tie-breaker); there is no fuzzy identity
join, profile merging, rescoring or current-prompt filtering. The private detail
CSV retains stored score context, prompt version, description completeness and
score timestamp. Unmatched/quarantined rows are counted and marked in details,
not silently counted as resolution errors.

Resolution precision and false-accept rate use only stored-verified rows labeled
`correct` or `wrong_job`; unsure labels are counted separately. Recall uses rows
with an expected URL and requires a stored-verified match. URL comparison uses
`src.verified_posting_identity.verified_posting_key_from_url`, falling back to
`normalize_official_url`: tracking parameters are removed but job IDs remain.
Deferred/not-found rate uses all joined label rows; ambiguous rows are counted
separately.

Fit metrics are MAE, Spearman correlation using average ranks for ties, share
within 10 points (inclusive), recommendation/decision confusion counts and
agreement by stored prompt version. Missing ratings/decisions do not enter their
respective denominators. Every estimate reports n. Proportions use 95% Wilson
intervals; n < 10 and undefined correlations display **insufficient data**.
Pure functions still expose the small-sample values for exact unit checks.

The public report renders only fixed text, counts and metrics. Nonstandard
prompt-version strings are hashed to avoid leaking arbitrary stored text;
ordinary numeric versions such as `v3` are shown. Unknown recommendation strings
are grouped under `other`. Per-job data is never rendered into Markdown.

This stratified, selectively labeled sample is not population-wide recall.
Stored scores can be stale, incomplete or associated with different resume
profiles. Cohort agreement is diagnostic rather than evidence that a prompt
change caused improvement. Wilson intervals do not remove selection bias.

## Schema and request-count inspection

The initial inspection used source-owned DDL and a disposable database built by
`scripts/build_ci_fixture_db.py`; the real database was not opened. Relevant
stored fields include:

- `job_enrichment_attempts`: record key/source, requested/final URL, fetch status,
  HTTP/extraction data, description word count, attempt count/timestamp and
  errors; candidate title/company/location and identity validation; official
  URL/status/source/resolved timestamp/confidence/reason and resolved metadata;
  verified posting key/provenance/trust; description hash/source and reuse data.
- `resume_job_scores`: resume hash and record key, overall and component scores,
  recommendation/confidence, strengths/missing requirements/risks/summary,
  description word count/completeness, model, prompt version and score timestamp.
- `raw_jobs`: exact record/fingerprint/source IDs, title/company/location/salary,
  description and apply URL, email/source metadata, discovery/description update
  timestamps, normalized title and title-filter classifications.

`public_search_metrics` is held on the HTTP client; `public_search_trace` and
`enrichment_timing` are runtime job dictionaries. Some diagnostic scripts can
export them separately, but the source table schemas do not persist these
request counts. No persisted per-job AI request count was found either. Attempt
counts and timings are not API request counts, so cost metrics are omitted.

## Privacy and offline verification

Private CSVs must be outside the repository. Both lexical paths and resolved
symlink targets are checked. Label-sheet creation refuses to overwrite files.
Keep all real labels/details outside git; `/evaluation/private/`, root-level
`labels*.csv` and `results_detail*.csv` are ignored as additional protection.
Only aggregate Markdown belongs under `docs/`. Do not commit databases, real
job extracts, labels, audit/approval JSON or environment/resume files.

```sh
python -m pytest -q src/test_eval_metrics.py
python -m evaluation.make_label_sheet --database /tmp/synthetic-eval.db --output /tmp/labels_todo.csv
```

All tests use synthetic temporary databases. The unmodified CI fixture has no
stored official-resolution outcomes and therefore yields a header-only label
sheet. Separate test fixtures add synthetic outcomes in disposable databases to
exercise the 80/20 split, source variety, empty/quarantined descriptions,
read-only connection, file guards, latest-score joins and aggregate privacy.
No evaluation against the real database is performed during this phase.
