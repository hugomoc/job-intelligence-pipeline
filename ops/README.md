# Maintenance commands

Run from the repository root with `python -m ops.<script_name>` (omit `.py`).
Use `--help` for arguments. These are manual commands; moving them here does
not change their behavior or authorize running them against production.

| Script | Purpose | Database writes |
| --- | --- | --- |
| `audit_49_scores.py` | Inspect saved 49% SKIP scores and their hard-gap evidence. | No; uses a read-only connection. Optional JSON report. |
| `audit_cached_fit.py` | Replay cached assessments against current deterministic policy. | Only with `--apply` and a reviewed fingerprint allowlist; backs up first. Default is read-only. |
| `audit_enrichment_identity.py` | Report identity mismatches in stored enrichment attempts. | Initializes schema; does not change job or score data. |
| `audit_linkedin_expired_descriptions.py` | Classify stored LinkedIn descriptions contaminated by search-page redirects. | No; uses a read-only connection and writes an audit report. |
| `audit_verified_posting_keys.py` | Audit verified-key provenance and optionally repair unambiguous evidence. | Initializes schema by default; `--repair` updates key provenance and trust metadata. |
| `backfill_verified_posting_reuse.py` | Reuse descriptions supported by strict, stored official identity evidence without network calls. | Yes; initializes schema and persists verified description reuse through the enrichment loader. |
| `promote_reviewed_49.py` | Propose REVIEW corrections for historic 49% SKIPs whose saved hard gaps are unsupported. | Only with `--apply` and a fingerprint approval file; backs up and updates transactionally. Default is read-only. |
| `quarantine_confirmed_linkedin_descriptions.py` | Quarantine audited LinkedIn search-page descriptions. | Only with `--apply` and matching count confirmation; backs up and updates transactionally. Default writes only a plan report. |
| `reclassify_titles.py` | Recompute deterministic title classification fields for existing jobs. | Yes; initializes schema and updates `raw_jobs` title-filter fields. |
