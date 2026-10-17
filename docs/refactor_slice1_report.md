# Matcher leaf extraction: slice 1 report

Branch: `refactor/resume-matcher-leaves`. Baseline main commit: `a14c740`.

Only `src/ai/resume_matcher.py`, the three requested new modules, and this report changed. No tests, other source files, scripts, CI, prompts, SQL, or provider functions were edited. Baseline artifacts and verification logs are saved under `/tmp/resume-matcher-slice1/`.

## Changes and dependency preflight

- `matcher_contract.py` owns the 15 existing constants/tables/patterns and the two existing capability dataclasses.
- `matcher_evidence.py` owns the 18 requested evidence helpers.
- `matcher_capabilities.py` owns the five requested capability helpers.
- Every moved name is imported back into `src.ai.resume_matcher`; mutable data is defined once in its new owner, not copied.
- The Pydantic models, `ResumeJobMatch`, `ResumeMatcherError`, prompt/description/scoring/response functions and all provider orchestration remain in the original module. All 16 retained top-level function/class definitions are source-verbatim and AST-equal to baseline.

Before writing any source files, AST/symbol-table analysis checked every global and annotation name used by each moved function against destination declarations, imports and builtins. No missing name or forbidden direct import was found. The full per-function name-to-owner map is `/tmp/resume-matcher-slice1/dependency_checks.json`.

| New module | Source dependencies |
| --- | --- |
| `matcher_contract` | stdlib `re`, `dataclasses`, `typing`; existing `src.description_quality.MINIMUM_FULL_DESCRIPTION_WORDS` |
| `matcher_evidence` | stdlib `json`, `re`; earlier `matcher_contract`; existing `src.ai.resume_profiler.ResumeProfile` |
| `matcher_capabilities` | stdlib `re`, `typing`; earlier `matcher_contract` and `matcher_evidence`; existing `ResumeProfile` and shared `requirement_policy` helpers |

No new module imports `src.ai.resume_matcher` or a later module in the requested list, including imports nested in function bodies. Existing shared-policy deferred imports remain unchanged; this slice does not claim to remove every repository-wide cycle.

## Definition equality

`baseline_ast.json` contains `{name: ast.dump(node)}` for every original top-level function and class. Every requested moved function is listed below. Each also matches its exact original source block, including signatures, defaults, decorators, comments and body text.

| Moved function | New owner | AST equals baseline | Source verbatim |
| --- | --- | --- | --- |
| `normalize_match_text` | `matcher_evidence` | Yes | Yes |
| `contains_phrase` | `matcher_evidence` | Yes | Yes |
| `profile_to_text` | `matcher_evidence` | Yes | Yes |
| `profile_fields_to_text` | `matcher_evidence` | Yes | Yes |
| `regex_present` | `matcher_evidence` | Yes | Yes |
| `regex_count` | `matcher_evidence` | Yes | Yes |
| `interchangeable_family_for_group` | `matcher_evidence` | Yes | Yes |
| `production_resume_text` | `matcher_evidence` | Yes | Yes |
| `project_resume_text` | `matcher_evidence` | Yes | Yes |
| `profile_has_alias_group` | `matcher_evidence` | Yes | Yes |
| `mentioned_alias_groups` | `matcher_evidence` | Yes | Yes |
| `requirement_uses_alternatives` | `matcher_evidence` | Yes | Yes |
| `requirement_allows_open_equivalent` | `matcher_evidence` | Yes | Yes |
| `requirement_requires_all` | `matcher_evidence` | Yes | Yes |
| `acceptable_alternative_groups` | `matcher_evidence` | Yes | Yes |
| `significant_terms` | `matcher_evidence` | Yes | Yes |
| `requirement_is_demonstrated` | `matcher_evidence` | Yes | Yes |
| `remove_demonstrated_missing_items` | `matcher_evidence` | Yes | Yes |
| `requirement_has_satisfied_alternative` | `matcher_capabilities` | Yes | Yes |
| `capability_mentions_with_context` | `matcher_capabilities` | Yes | Yes |
| `capability_mandatory_mentions` | `matcher_capabilities` | Yes | Yes |
| `capability_is_central_to_job` | `matcher_capabilities` | Yes | Yes |
| `detect_critical_requirement_gaps` | `matcher_capabilities` | Yes | Yes |

Both moved dataclasses (`CriticalCapabilitySignal`, `CriticalRequirementGap`) also have identical AST dumps and verbatim source. All 15 moved constant declarations were separately checked for identical AST and source. Evidence: `source_ast_checks.json`.

## Legacy name identity

For every row, the evaluated expression was `src.ai.resume_matcher.NAME is src.ai.<owner>.NAME`.

| Moved name | Owner | `is` result |
| --- | --- | --- |
| `MINIMUM_COMPLETE_DESCRIPTION_WORDS` | `matcher_contract` | True |
| `MATCHER_PROMPT_VERSION` | `matcher_contract` | True |
| `SIGNIFICANT_TERM_PATTERN` | `matcher_contract` | True |
| `TECH_ALIASES` | `matcher_contract` | True |
| `CLOUD_GROUPS` | `matcher_contract` | True |
| `WAREHOUSE_GROUPS` | `matcher_contract` | True |
| `DATA_PROCESSING_GROUPS` | `matcher_contract` | True |
| `BI_PLATFORM_GROUPS` | `matcher_contract` | True |
| `INTERCHANGEABLE_TECH_GROUPS` | `matcher_contract` | True |
| `SIGNAL_ALIAS_GROUPS` | `matcher_contract` | True |
| `CriticalCapabilitySignal` | `matcher_contract` | True |
| `CriticalRequirementGap` | `matcher_contract` | True |
| `CRITICAL_CAPABILITY_SIGNALS` | `matcher_contract` | True |
| `HARD_MANDATORY_CONTEXT_PATTERNS` | `matcher_contract` | True |
| `MANDATORY_REQUIREMENT_CONTEXT_PATTERNS` | `matcher_contract` | True |
| `CRITICAL_REQUIREMENT_CONTEXT_PATTERNS` | `matcher_contract` | True |
| `OPTIONAL_REQUIREMENT_CONTEXT_PATTERNS` | `matcher_contract` | True |
| `normalize_match_text` | `matcher_evidence` | True |
| `contains_phrase` | `matcher_evidence` | True |
| `profile_to_text` | `matcher_evidence` | True |
| `profile_fields_to_text` | `matcher_evidence` | True |
| `regex_present` | `matcher_evidence` | True |
| `regex_count` | `matcher_evidence` | True |
| `interchangeable_family_for_group` | `matcher_evidence` | True |
| `production_resume_text` | `matcher_evidence` | True |
| `project_resume_text` | `matcher_evidence` | True |
| `profile_has_alias_group` | `matcher_evidence` | True |
| `mentioned_alias_groups` | `matcher_evidence` | True |
| `requirement_uses_alternatives` | `matcher_evidence` | True |
| `requirement_allows_open_equivalent` | `matcher_evidence` | True |
| `requirement_requires_all` | `matcher_evidence` | True |
| `acceptable_alternative_groups` | `matcher_evidence` | True |
| `significant_terms` | `matcher_evidence` | True |
| `requirement_is_demonstrated` | `matcher_evidence` | True |
| `remove_demonstrated_missing_items` | `matcher_evidence` | True |
| `requirement_has_satisfied_alternative` | `matcher_capabilities` | True |
| `capability_mentions_with_context` | `matcher_capabilities` | True |
| `capability_mandatory_mentions` | `matcher_capabilities` | True |
| `capability_is_central_to_job` | `matcher_capabilities` | True |
| `detect_critical_requirement_gaps` | `matcher_capabilities` | True |

All **40/40** identities passed. Evidence: `identity_checks.json`.

## Fresh-interpreter import order

Each row used a separate Python process. The named module was the first explicitly requested application import; no other listed application module was pre-imported by the harness. Every process exited successfully.

| Imported first | Exit code |
| --- | ---: |
| `src.ai.resume_matcher` | 0 |
| `src.ai.requirement_policy` | 0 |
| `src.ai.job_eligibility` | 0 |
| `src.repositories.recommendation_repository` | 0 |
| `src.enrich_jobs` | 0 |
| `src.ui.job_visibility` | 0 |
| `src.actionability` | 0 |

Evidence: `fresh_imports.json`. Imports used disabled dotenv loading and blocked external network access.

## Test and patch verification

| Check | Result |
| --- | --- |
| Baseline `python -m pytest --collect-only -q src` | 659 collected |
| Baseline `python -m pytest -q src` | 659 passed, 138 subtests passed |
| After `python -m pytest --collect-only -q src` | 659 collected; IDs and ordering exactly equal baseline |
| After `python -m pytest -q src` | 659 passed, 138 subtests passed |
| Reverse-order pytest | 659 passed, 138 subtests passed |
| `python scripts/run_ci_tests.py` | All 57 scripts passed before and after extraction |
| Compilation | `python -m compileall -q src scripts streamlit_app.py` passed before and after |

The reverse run used the same external `/tmp/pytest-migration-tools/compare_plugin.py` collection hook used for `docs/pytest_comparison.md`, with `PYTEST_COMPARISON_ORDER=reverse` and `-p no:randomly`. Its collected item list equals the complete baseline list reversed, and its outcomes match the normal run. No existing test was changed.

The existing `src/test_suspicious_view_audit.py::AuditTests::test_incomplete_jd_does_not_reach_api_clients` test passed in both full pytest runs. The provider functions still resolve globals in `src.ai.resume_matcher.__dict__`.

An additional offline smoke check supplied a valid FULL_JD and patched `src.ai.resume_matcher.get_gemini_client` with a sentinel exception. The Gemini scoring path called the patched getter exactly once; the sentinel stopped execution before any provider or network request. This verifies that the old patch location still intercepts a path that would request a client, in addition to the existing incomplete-JD regression. Evidence: `client_patch.json`.

All runs used disposable state outside the repository, disabled dotenv loading, and blocked external network access. No live scoring/enrichment or production database workflow ran.

## Line counts

| File | Before | After |
| --- | ---: | ---: |
| `src/ai/resume_matcher.py` | 1969 | 1090 |
| `src/ai/matcher_contract.py` | 0 (did not exist) | 289 |
| `src/ai/matcher_evidence.py` | 0 (did not exist) | 497 |
| `src/ai/matcher_capabilities.py` | 0 (did not exist) | 279 |

The original module is intentionally not a thin shim in slice 1: its models, prompt, response normalization, scoring and provider functions stay in place as requested. No forwarding shim or runtime rebinding mechanism was added.

## Evidence files under `/tmp`

- `baseline_ast.json`, `baseline_source.py`: complete definition/source baseline.
- `baseline_collect.log`, `baseline_pytest.log`, `baseline_ci.log`: baseline results.
- `dependency_checks.json`, `source_ast_checks.json`, `identity_checks.json`, `fresh_imports.json`, `client_patch.json`: structural, identity, import and compatibility checks.
- `baseline_nodeids.json`, `after_nodeids.json`, `after_collect.log`: exact collection parity.
- `normal_pytest.log`, `reverse_pytest.log`, `normal_results.json`, `reverse_results.json`, `after_ci.log`: post-extraction results.
