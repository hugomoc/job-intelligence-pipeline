# Pytest and legacy runner comparison

No existing tests, `scripts/run_ci_tests.py`, or application code were changed. Proposed wrappers below are documentation only.

## Setup and commands

- Branch: `pytest-migration`.
- Both measured runners used Python 3.12 in `/tmp/job-pytest-collection-check`, pytest 9.1.1, and the pinned direct requirements. Transitive packages are shared by both measured runners.
- Legacy: `python scripts/run_ci_tests.py`; an external observer recorded its existing subprocess exit codes and wall times without changing the runner.
- Pytest: `python -m pytest -q src -p no:randomly`; an external reporting plugin recorded outcomes. The reverse run reversed the complete collected item list with an external collection hook. No random-order plugin was installed or used.
- All runs used disabled dotenv loading/telemetry, blocked external network access, and disposable databases outside the repository. The pytest session sets its database path before importing application modules. Manual exclusions are read dynamically from `MANUAL_ONLY` by the option enabled in `pytest.ini`.
- Timings are sequential wall-clock measurements on this machine, including runner startup and subprocess/fixture work; they are not a benchmark guarantee.

## Results

| Runner | Wall time | Result | Exit code |
| --- | ---: | --- | ---: |
| Legacy scripts | 167.35 s | 57/57 script exits successful | 0 |
| Pytest normal | 73.07 s | 657 passed | 0 |
| Pytest reverse | 71.61 s | 657 passed | 0 |

Pytest normal collected 657 items from 55/57 CI files. Reverse collected 657. Collection errors: normal 0; reverse 0.

Normal additionally reported 138 successful unittest subtests; reverse reported 138. Item counts below count each collected test once, not each subtest.

## Per-file comparison

“Extra script-only checks” describes checks that the script entry point runs but pytest does not collect. `unittest.main()` executes collectible unittest methods and is not an additional coverage gap. Merely printing a success message is not counted as a check.

| CI file (`src/`) | Script exit | Script time | Pytest normal | Pytest reverse | Extra script-only checks |
| --- | ---: | ---: | --- | --- | --- |
| `test_status_performance.py` | 0 | 41.93 s | 23 passed | 23 passed | No; entry point uses `unittest.main()`. |
| `test_actionability.py` | 0 | 5.11 s | 45 passed | 45 passed | No; entry point uses `unittest.main()`. |
| `test_suspicious_view_audit.py` | 0 | 5.37 s | 33 passed | 33 passed | No; entry point uses `unittest.main()`. |
| `test_cached_fit_audit.py` | 0 | 4.16 s | 15 passed | 15 passed | No; entry point uses `unittest.main()`. |
| `test_audit_49_scores.py` | 0 | 2.45 s | 5 passed | 5 passed | No; entry point uses `unittest.main()`. |
| `test_promote_reviewed_49.py` | 0 | 2.71 s | 7 passed | 7 passed | No; entry point uses `unittest.main()`. |
| `test_shared_requirement_policy.py` | 0 | 4.25 s | 61 passed | 61 passed | No; entry point uses `unittest.main()`. |
| `test_description_quality.py` | 0 | 3.21 s | 12 passed | 12 passed | No; entry point uses `unittest.main()`. |
| `test_verified_official_description.py` | 0 | 3.79 s | 19 passed | 19 passed | No; entry point uses `unittest.main()`. |
| `test_aggregator_visibility.py` | 0 | 8.29 s | 7 passed | 7 passed | No; `main()` invokes collectible tests. |
| `test_ats_adapters.py` | 0 | 2.31 s | 13 passed | 13 passed | No; `main()` invokes collectible tests. |
| `test_ats_metadata.py` | 0 | 0.28 s | 11 passed | 11 passed | No; entry point uses `unittest.main()`. |
| `test_audit_verified_posting_keys.py` | 0 | 2.19 s | 4 passed | 4 passed | No; `main()` invokes collectible tests. |
| `test_audit_linkedin_expired_descriptions.py` | 0 | 0.99 s | 9 passed | 9 passed | No; `main()` invokes collectible tests. |
| `test_quarantine_confirmed_linkedin_descriptions.py` | 0 | 2.31 s | 7 passed | 7 passed | No; `main()` invokes collectible tests. |
| `test_bebee_parser.py` | 0 | 0.20 s | 1 passed | 1 passed | No; `main()` invokes collectible tests. |
| `test_builtin_parser.py` | 0 | 0.18 s | 1 passed | 1 passed | No; `main()` invokes collectible tests. |
| `test_config.py` | 0 | 0.11 s | 0 collected | 0 collected | Yes: load config and assert source IDs, names, and mailbox folders in `main()`. |
| `test_daily_workflow_service.py` | 0 | 4.74 s | 6 passed | 6 passed | No; `main()` invokes collectible tests. |
| `test_drain_enrichment_backlog.py` | 0 | 3.84 s | 15 passed | 15 passed | No; entry point uses `unittest.main()`. |
| `test_enrich_jobs_official_retry.py` | 0 | 12.72 s | 45 passed | 45 passed | No; `main()` invokes collectible tests. |
| `test_glassdoor_parser.py` | 0 | 0.20 s | 2 passed | 2 passed | No offline gap; mailbox inspection is disabled by `RUN_LIVE_EMAIL_TESTS=0`. |
| `test_indeed_parser.py` | 0 | 0.19 s | 0 collected | 0 collected | Yes: `check_location_pattern()` and `check_sample_email()`. |
| `test_job_admission.py` | 0 | 2.00 s | 27 passed | 27 passed | No; `main()` invokes collectible tests. |
| `test_targeted_cleanup.py` | 0 | 2.42 s | 10 passed | 10 passed | No; `main()` invokes collectible tests. |
| `test_public_search.py` | 0 | 2.23 s | 18 passed | 18 passed | No; entry point uses `unittest.main()`. |
| `test_job_description_enrichment.py` | 0 | 3.20 s | 3 passed | 3 passed | No; `main()` invokes collectible tests. |
| `test_job_eligibility.py` | 0 | 2.15 s | 2 passed | 2 passed | No; `main()` invokes collectible tests. |
| `test_job_identity.py` | 0 | 0.24 s | 7 passed | 7 passed | No; `main()` invokes collectible tests. |
| `test_identity_evidence_boundary.py` | 0 | 2.10 s | 13 passed | 13 passed | No; entry point uses `unittest.main()`. |
| `test_location_identity.py` | 0 | 2.17 s | 5 passed | 5 passed | No; entry point uses `unittest.main()`. |
| `test_job_pipeline_state.py` | 0 | 0.08 s | 4 passed | 4 passed | No; `main()` invokes collectible tests. |
| `test_job_review_visibility.py` | 0 | 2.12 s | 10 passed | 10 passed | No; `main()` invokes collectible tests. |
| `test_job_title_filter.py` | 0 | 0.08 s | 6 passed | 6 passed | No; `main()` invokes collectible tests. |
| `test_jobdiva_careers_resolver.py` | 0 | 0.31 s | 1 passed | 1 passed | No; `main()` invokes collectible tests. |
| `test_jobleads_parser.py` | 0 | 0.16 s | 1 passed | 1 passed | No; `main()` invokes collectible tests. |
| `test_joblookup_parser.py` | 0 | 0.16 s | 1 passed | 1 passed | No; `main()` invokes collectible tests. |
| `test_jobot_parser.py` | 0 | 0.16 s | 1 passed | 1 passed | No; `main()` invokes collectible tests. |
| `test_jobright_parser.py` | 0 | 0.16 s | 2 passed | 2 passed | No; `main()` invokes collectible tests. |
| `test_ladders_parser.py` | 0 | 0.16 s | 4 passed | 4 passed | No; `main()` invokes collectible tests. |
| `test_lensa_identity_validation.py` | 0 | 0.34 s | 2 passed | 2 passed | No; `main()` invokes collectible tests. |
| `test_lensa_matching.py` | 0 | 0.31 s | 6 passed | 6 passed | No; `main()` invokes collectible tests. |
| `test_lensa_parser.py` | 0 | 0.18 s | 6 passed | 6 passed | No; `main()` invokes collectible tests. |
| `test_linkedin_parser.py` | 0 | 0.34 s | 3 passed | 3 passed | No; `main()` invokes collectible tests. |
| `test_official_job_resolver.py` | 0 | 3.43 s | 14 passed | 14 passed | No; `main()` invokes collectible tests. |
| `test_pagination.py` | 0 | 0.09 s | 6 passed | 6 passed | No; `main()` invokes collectible tests. |
| `test_recommendation_repository.py` | 0 | 15.59 s | 43 passed | 43 passed | No; `main()` invokes collectible tests. |
| `test_remotehunter_parser.py` | 0 | 0.16 s | 2 passed | 2 passed | No; `main()` invokes collectible tests. |
| `test_resume_matcher_validation.py` | 0 | 3.56 s | 29 passed | 29 passed | No; `main()` invokes collectible tests. |
| `test_streamlit_labels.py` | 0 | 0.07 s | 1 passed | 1 passed | No; `main()` invokes collectible tests. |
| `test_verified_posting_identity.py` | 0 | 0.08 s | 7 passed | 7 passed | No; `main()` invokes collectible tests. |
| `test_verified_display_deduplication.py` | 0 | 2.19 s | 24 passed | 24 passed | No; entry point uses `unittest.main()`. |
| `test_verified_identity_persistence.py` | 0 | 6.49 s | 26 passed | 26 passed | No; entry point uses `unittest.main()`. |
| `test_verified_identity_recovery.py` | 0 | 2.33 s | 24 passed | 24 passed | No; entry point uses `unittest.main()`. |
| `test_welcometothejungle_parser.py` | 0 | 0.18 s | 2 passed | 2 passed | No; `main()` invokes collectible tests. |
| `test_wellfound_parser.py` | 0 | 0.18 s | 1 passed | 1 passed | No; `main()` invokes collectible tests. |
| `test_ziprecruiter_parser.py` | 0 | 0.37 s | 5 passed | 5 passed | No; `main()` invokes collectible tests. |

## Failures and order sensitivity

No collected test failed in either order. No order-dependent outcome was observed in these two runs. This does not establish independence for every possible order.

Normal and reverse produced identical per-test outcomes.


## Checks exercised only by pytest

`src/test_resume_matcher_validation.py` defines two collectible functions that its script `main()` never calls:

- `test_specific_platform_years_are_not_satisfied_by_general_experience`
- `test_databricks_operational_ownership_is_central_without_explicit_required_label`

Both passed in normal and reverse pytest order. This is an additional difference in coverage, even though all 57 scripts exit successfully. No existing test or entry point was changed.

## Manual-only files

All eight are excluded before collection by the dynamically loaded inventory. None ran in this comparison.

- `src/test_glassdoor_reader.py` — manual Yahoo mailbox reader.
- `src/test_lensa_resolver.py` — manual Playwright/live Lensa resolver.
- `src/test_linkedin_reader.py` — manual Yahoo mailbox reader.
- `src/test_resume_extractor.py` — manual local resume file CLI.
- `src/test_resume_profiler.py` — manual AI profile CLI requiring a resume/API key.
- `src/test_yahoo_connection.py` — manual Yahoo IMAP credential check.
- `src/test_yahoo_reader.py` — manual Yahoo mailbox reader.
- `src/test_ziprecruiter_reader.py` — manual Yahoo mailbox reader.

## Proposed wrappers (not applied)

For `src/test_config.py`, the smallest wrapper preserves the existing config assertions:

```python
def test_config():
    main()
```

For `src/test_indeed_parser.py`, one wrapper invokes both offline checks directly and leaves mailbox inspection out:

```python
def test_offline_indeed_checks():
    check_location_pattern()
    check_sample_email()
```

Neither wrapper was added. Until they are approved and applied, pytest misses checks that the legacy runner executes.

## Interpretation

This looks like a small migration: both runtime orders passed, and the two proposed wrappers close the known script-entry coverage gaps. No runtime failures or order-sensitive outcomes were observed. Keep the legacy runner until those wrappers are approved and coverage parity is verified; passing the collected tests alone does not execute the two missing files.

Raw machine-readable observations and console logs are under `/tmp/pytest-{legacy,normal,reverse}-results.json` and `/tmp/pytest-{legacy,normal,reverse}-run.log`. Test configuration and this report are the only repository changes for this task, alongside the pytest requirement pin.
