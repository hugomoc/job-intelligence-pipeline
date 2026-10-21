# Controlled v9 Gemini experiment (not production scoring)

This command is an **isolated** comparison tool. It does not import the production
scoring queue, open DuckDB, create assessments, call OpenAI, or modify any scores.
The first run should use **one frozen job and the resume used for its original score**.

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
