# Evidence-based fit calibration — offline-only first stage

This is an **isolated, deterministic evidence-consistency checker**, not a
replacement scorer and not a new production scoring prompt.

Use `evaluation.evidence_checks.evaluate_cases(cases)` with manually reviewed,
private dictionaries structured as follows:

```python
{
    "case_id": "local-case-001",
    "stored_score": 95,
    "review_label": "questionable",  # strongly_supported | questionable | inflated
    "requirements": [
        {
            "requirement": "Example qualification wording",
            "mandatory": True,
            "status": "not_demonstrated",  # met | partial | not_demonstrated | unclear
            "resume_evidence": "",
        },
    ],
}
```

For `met`, provide a direct resume evidence excerpt; a tool name alone
does not prove a duration of production use. **Not demonstrated is not proof
the applicant lacks the skill.** Never infer mandatory status from the mere
presence of a tool in a JD. Obtain a reviewer classification from the full
posting. Do not insert the full private job description or resume in source
control.

The checker flags unsupported or uncertain mandatory evidence, especially
when a saved score is >=95. It emits **no replacement scores**, changes no
recommendations, and does not touch DuckDB or model providers. Aggregation
reports the reviewed sample's labels and descriptive flag counts; it does not
claim population-wide accuracy.

## Prior private audit

The October 20 local audit listed 11 high-scoring jobs:

- Strongly supported: CenterWell, Constructor
- Questionable: Tesla, Afresh, Bellese, Resilience, Sidecar, Torc, Warp
- Inflated relative to supplied evidence: Aurora Solar, Netomi

**These are review labels, not reconstructed ground-truth scores.** The detailed
audit was stored outside GitHub under a local `/tmp` directory. To run a
meaningful 11-case evaluation, import its exact requirement tables and resume
citations into a private local input. This repository does **not** invent those
details or assign automatically generated requirements from these labels.

## Workflow and limits

1. Transcribe or load the 11 audit cases from a private location; check
   citation fidelity against stored full JD and the scored resume.
2. Run `python -m evaluation.run_evidence_audit --input /path/to/private_evidence_benchmark_11.json` locally to print **aggregate-only** results, or use `evaluate_cases` for private case-level flags. Do not commit the private input or full findings.
3. Review false positives, especially CenterWell and Constructor, and inspect
   unsupported mandatory items for Aurora Solar and Netomi.
4. Expand beyond this small, intentionally high-score-heavy cohort.
5. Only after offline validation, separately propose a structured evidence
   generation prompt and controlled provider evaluation for newly scored jobs.

The existing `evaluation/run_eval.py` measures agreement with numerical
human labels; it remains unchanged. This module evaluates **requirement
evidence consistency** and never substitutes for that numerical evaluation.

To test:

```sh
python -m pytest -q src/test_evidence_checks.py
python src/test_evidence_checks.py
```

No API credentials, production database, historical scores, or pipeline
execution are required.
