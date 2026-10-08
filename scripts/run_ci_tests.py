"""Run deterministic offline tests for GitHub Actions CI.

This repository has standalone script-style tests instead of a pytest suite.
The list below intentionally excludes manual integration probes that require
Yahoo credentials, local resume files, Playwright/browser navigation, or AI
API keys.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

CI_TESTS = [
    "src/test_aggregator_visibility.py",
    "src/test_ats_adapters.py",
    "src/test_audit_verified_posting_keys.py",
    "src/test_bebee_parser.py",
    "src/test_builtin_parser.py",
    "src/test_config.py",
    "src/test_daily_workflow_service.py",
    "src/test_drain_enrichment_backlog.py",
    "src/test_enrich_jobs_official_retry.py",
    "src/test_glassdoor_parser.py",
    "src/test_indeed_parser.py",
    "src/test_job_admission.py",
    "src/test_targeted_cleanup.py",
    "src/test_job_description_enrichment.py",
    "src/test_job_eligibility.py",
    "src/test_job_identity.py",
    "src/test_job_pipeline_state.py",
    "src/test_job_review_visibility.py",
    "src/test_job_title_filter.py",
    "src/test_jobdiva_careers_resolver.py",
    "src/test_jobleads_parser.py",
    "src/test_joblookup_parser.py",
    "src/test_jobot_parser.py",
    "src/test_jobright_parser.py",
    "src/test_ladders_parser.py",
    "src/test_lensa_identity_validation.py",
    "src/test_lensa_matching.py",
    "src/test_lensa_parser.py",
    "src/test_linkedin_parser.py",
    "src/test_official_job_resolver.py",
    "src/test_pagination.py",
    "src/test_recommendation_repository.py",
    "src/test_remotehunter_parser.py",
    "src/test_resume_matcher_validation.py",
    "src/test_streamlit_labels.py",
    "src/test_verified_posting_identity.py",
    "src/test_welcometothejungle_parser.py",
    "src/test_wellfound_parser.py",
    "src/test_ziprecruiter_parser.py",
]

EXCLUDED_TESTS = {
    "src/test_glassdoor_reader.py": "manual Yahoo mailbox reader",
    "src/test_lensa_resolver.py": "manual Playwright/live Lensa resolver",
    "src/test_linkedin_reader.py": "manual Yahoo mailbox reader",
    "src/test_resume_extractor.py": "manual local resume file CLI",
    "src/test_resume_profiler.py": "manual AI profile CLI requiring a resume/API key",
    "src/test_yahoo_connection.py": "manual Yahoo IMAP credential check",
    "src/test_yahoo_reader.py": "manual Yahoo mailbox reader",
    "src/test_ziprecruiter_reader.py": "manual Yahoo mailbox reader",
}


def validate_test_inventory() -> None:
    discovered = {
        path.as_posix()
        for path in sorted((REPO_ROOT / "src").glob("test_*.py"))
    }
    discovered = {
        str(Path(path).relative_to(REPO_ROOT))
        if Path(path).is_absolute()
        else path
        for path in discovered
    }
    classified = set(CI_TESTS) | set(EXCLUDED_TESTS)
    unclassified = sorted(discovered - classified)
    missing = sorted(classified - discovered)

    if unclassified or missing:
        if unclassified:
            print("Unclassified test files:")
            for path in unclassified:
                print(f" - {path}")

        if missing:
            print("Classified test files missing from repository:")
            for path in missing:
                print(f" - {path}")

        raise SystemExit(1)


def run_test(test_path: str) -> None:
    print(f"\n=== Running {test_path} ===", flush=True)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["RUN_LIVE_EMAIL_TESTS"] = "0"

    subprocess.run(
        [sys.executable, test_path],
        cwd=REPO_ROOT,
        env=environment,
        check=True,
    )


def main() -> None:
    validate_test_inventory()

    print("Excluded from deterministic CI:")
    for path, reason in sorted(EXCLUDED_TESTS.items()):
        print(f" - {path}: {reason}")

    for test_path in CI_TESTS:
        run_test(test_path)

    print("\nAll deterministic offline CI tests passed.")


if __name__ == "__main__":
    main()
