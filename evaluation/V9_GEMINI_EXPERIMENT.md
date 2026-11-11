# v9 provider comparison: Gemini vs OpenAI on the five frozen jobs

Both providers get the **same prompt (v9.2) and schema**, and every response
goes through the same checks and the same decision policy
(`evaluation/v9_scoring.py`). Differences in the results therefore come from
the model, not the harness. Nothing here touches v8, its fallback, production
scoring or DuckDB writes.

## How the final score and recommendation are decided

The model proposes `fit_score` and a requirement ledger. The harness checks
every citation against the JD and resume, then works out an *effective* status
for each requirement:

- An invented resume quote, or a named technology missing from its own quote
  (e.g. Spark offered for "Kafka or RabbitMQ"), never counts as met.
- Portfolio, project or skills-list evidence for a production or min-years
  requirement is at most **partial**.
- Tenure needs employment evidence that shows a duration.
- A "preferred / nice to have / bonus" clause labelled mandatory is treated as
  preferred, and the mislabel is flagged.
- A requirement whose JD quote cannot be found is never counted as a gap.

The model's score is then capped where its own verified evidence contradicts it:

| Verified evidence | Score cap |
|---|---|
| 2+ central mandatory groups not demonstrated | 44 |
| 1 central mandatory group not demonstrated | 64 |
| Central mandatory only partially evidenced | 74 |
| Unverifiable citations, mislabels, or inconsistent groups | 74 |
| Central mandatory evidence unclear | 84 |
| Peripheral mandatory group not demonstrated | 89 |

Recommendation, in priority order:

1. **SKIP**: a confirmed eligibility blocker (supplied by you), or 2+ central
   mandatory gaps, or a final score below 45.
2. **LOW PRIORITY**: one central mandatory gap, or a final score of 45-64.
3. **HUMAN REVIEW**: unverifiable citations or mislabels; no central mandatory
   requirement extracted; **every mandatory requirement met but the model
   scored below 65** (too-low scores are flagged, not silently skipped);
   partial or unclear central evidence; JD eligibility not confirmed; or
   a final score of 65-74.
4. **APPLY**: a final score of 75 or more with every central mandatory
   requirement met and no other condition above triggered.

The raw `model_score`, the `final_score`, and every reason for a cap or a
review are saved in the results.

Eligibility facts come from you, never from a model. Optional per-case fields
in `--input` files: `"eligibility_blockers": ["..."]` (forces SKIP) and
`"eligibility_confirmed": true` (resolves JD eligibility rows the resume
cannot prove).

## Run both providers on the five frozen jobs

Dry run first (no API calls):

```sh
python -m evaluation.run_v9 --provider gemini --from-db all --output ~/job-eval/v9/gemini_dry.json
python -m evaluation.run_v9 --provider openai --from-db all --output ~/job-eval/v9/openai_dry.json
```

Live (paid, explicit; five calls each):

```sh
python -m evaluation.run_v9 --provider gemini --from-db all --live --max-calls 5 \
  --output ~/job-eval/v9/gemini_live.json
python -m evaluation.run_v9 --provider openai --from-db all --live --max-calls 5 \
  --output ~/job-eval/v9/openai_live.json
```

The default models are `gemini-3.5-flash` and `gpt-4.1`; `--model` overrides them.
`python -m evaluation.run_v9_gemini` still works and means `--provider gemini`.

## Compare against your reviewed assessments

Write a private reference file, e.g. `~/job-eval/v9/reference.json`, with your
independently reviewed conclusions:

```json
[
  {"case_id": "aurora", "recommendation": "LOW PRIORITY", "score": 55,
   "score_tolerance": 10,
   "central_mandatory": {"dbt": "not_demonstrated", "Looker": "met"}}
]
```

(The values above are illustrative, not your review.) Then:

```sh
python -m evaluation.compare_v9 --reference ~/job-eval/v9/reference.json \
  --results ~/job-eval/v9/gemini_live.json ~/job-eval/v9/openai_live.json
```

It reports, per provider: correct recommendations (and how many were too
optimistic or too pessimistic), score error before and after the evidence
caps, agreement with your requirement statuses (including requirements the model
never extracted), and unsupported resume and JD citations. Five cases is a smoke
test: confirm on held-out jobs before choosing a provider for production.

---

## Troubleshoot a Gemini HTTP 400 without spending API calls

Use a **fresh private output filename**:

```sh
python -m evaluation.run_v9_gemini \
  --from-db aurora \
  --check-schema \
  --output ~/job-eval/v9/aurora_schema_check.json
```

This checks local Google GenAI SDK configuration serialization only;
`local_schema_serialized` does **not** guarantee server acceptance.
For future explicitly authorized live runs, HTTP 400 responses now include
only an allowlisted hint such as `unsupported_schema`, `invalid_model`,
`unsupported_generation_config`, or `other_invalid_request`.
These classifications are heuristic; no raw provider messages are saved.
Do not repeat the live test until the local check is reviewed.

# Controlled v9 Gemini experiment (not production scoring)

This command is an **isolated** comparison tool. It does not import the production
scoring queue, open DuckDB, create assessments, call OpenAI, or modify any scores.
The first run should use **one frozen job and the resume used for its original score**.

## No manual JSON preparation: read original active assessment from DuckDB

From the repository root after merging this PR:

```sh
git switch main
git pull --ff-only
python -m evaluation.run_v9_gemini \
  --from-db aurora \
  --output ~/job-eval/v9/aurora_dry_run.json
```

This is a zero-API-call dry run. It opens the existing local DuckDB **read-only**,
selects the exact frozen Aurora record key from the October 20 audit, joins its
current active assessment, complete stored job description and **that
assessment's original resume hash**. If the active record is missing,
ambiguous or missing the original text, it fails rather than selecting a
similarly named job or a different resume.

After checking the dry run, a single *explicitly authorized* Gemini request:

```sh
python -m evaluation.run_v9_gemini \
  --from-db aurora \
  --output ~/job-eval/v9/aurora_live.json \
  --live --max-calls 1
```

The five supported names are `aurora`, `netomi`, `tesla`,
`centerwell`, and `constructor`. Each invocation selects **one** case and
uses a fresh output path. The original score is not sent to Gemini.

The database path defaults to the application's `src.database.DATABASE_PATH`.
Use `--database /path/to/jobs.duckdb` only if you explicitly need an
alternative database. The dry run does not import Gemini clients or load API
keys. Private result JSON remains outside Git. No application assessments or
scores are written. If your active pointer has changed since the October 20
audit, this selects the current active assessment for the frozen record key,
not necessarily its earlier audit snapshot; check that before interpreting
the result.

## Private input

Create JSON outside the Git repository, for example at
`~/job-eval/v9/five_cases.json`. The file must contain a list of objects:

```json
[
  {
    "case_id": "local-aurora-001",
    "jd": "Paste the complete original job description here",
    "resume": "Paste the exact resume text used for scoring here",
    "original_score": 95
  }
]
```

The illustrative strings above are **not valid real benchmark data**. Use the
frozen full-JD and resume artifacts from the October 20 audit, not web excerpts
or an updated resume. The original score is recorded in the input only and
is **never included in the Gemini prompt**. This is not a ready-to-run
sample of private jobs.

## Dry run (zero API calls)

```sh
python -m evaluation.run_v9_gemini \
  --input ~/job-eval/v9/one_case.json \
  --output ~/job-eval/v9/one_case_dry_run.json
```

## One authorized live call

Provide `GEMINI_API_KEY` in the environment using the repository's usual
secret-handling setup. The script does not print it.

```sh
python -m evaluation.run_v9_gemini \
  --input ~/job-eval/v9/one_case.json \
  --output ~/job-eval/v9/one_case_live.json \
  --live --max-calls 1
```

The script refuses to overwrite any existing results and refuses input/output
paths inside the Git repository. It makes at most one request per input case;
there is **no correction retry, automatic fallback, or production write**.
The maximum-case gate is checked before any calls. Running five cases requires
`--max-calls 5` and a separate private output path. API calls may incur cost.

**Security:** Private results contain model-generated quotations from resume
and JD text. Keep both inputs and outputs outside Git; do not attach raw
results to public PRs or CI. Errors in the JSON output include an error type
only, not provider error messages that could echo private prompts.

The response is validated with `evaluation.v9_evidence.validate_ledger`.
A `validated` status means it satisfies current structural/consistency checks,
**not that every requirement was extracted or that the score is accurate**.
Inspect the requirement ledger against human-reviewed reference evidence.

Known limits: the isolated validator is intentionally conservative, and
quote fidelity alone cannot prove professional tenure or semantic equivalence.
After the one-job smoke test, compare Aurora, Netomi, Tesla, CenterWell, and
Constructor and run held-out tests before any production integration.
