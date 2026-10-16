"""Offline regression tests for read-only LinkedIn description contamination audit."""
from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb

from src.audit_linkedin_expired_descriptions import (
    CLASS_CONFIRMED, CLASS_HIGH_CONFIDENCE, CLASS_IDENTITY_REVIEW, CLASS_LIKELY,
    CLASS_NO_SIGNAL, CLASS_REDIRECT_REVIEW, audit,
    classify_row, search_page_evidence, has_generic_search_results_header,
)


LISTING = """Get notified when a new job is posted.
Set alert
Sign in to set job alerts for Lead Data Analyst roles.
11,000+ Lead Data Analyst Jobs in United States
Lead Analyst Network Analytics
Actively Hiring
Be an early applicant
"""


def row(**overrides):
    base = dict(
        record_key="id", source="linkedin",
        title="Sr Business Data Analyst", company_name="Point32Health",
        apply_url="https://www.linkedin.com/jobs/view/4470007344/",
        description=LISTING,
        final_url="https://www.linkedin.com/jobs/lead-data-analyst-jobs?trk=expired_jd_redirect",
        attempt_status="enriched", identity_validation_reason="identity validation not required for this source",
        description_source="search", official_url_status="OFFICIAL_NOT_FOUND",
        verified_posting_key=None,
    )
    base.update(overrides)
    return base


def test_exact_point32health_example_is_confirmed() -> None:
    finding = classify_row(row())
    assert finding["classification"] == CLASS_CONFIRMED
    assert finding["verified_posting_key"] is None
    assert finding["description_words"] > 10
    assert len(finding["evidence_markers"]) >= 3


def test_long_linkedin_job_listing_text_is_flagged_without_redirect() -> None:
    finding = classify_row(row(
        final_url="https://www.linkedin.com/jobs/view/4470007344/",
        identity_validation_reason="identity accepted",
    ))
    assert finding["classification"] == CLASS_LIKELY


def test_redirect_alone_needs_review_not_proven_contamination() -> None:
    finding = classify_row(row(
        description="Brief original job alert from employer",
        attempt_status="no_description",
    ))
    assert finding["classification"] == CLASS_REDIRECT_REVIEW


def test_legacy_identity_bypass_only_flags_review() -> None:
    finding = classify_row(row(
        description=("Our data engineering team builds reliable models and pipelines. " * 20),
        final_url="https://www.linkedin.com/jobs/view/4470007344/",
    ))
    assert finding["classification"] == CLASS_IDENTITY_REVIEW


def test_legitimate_job_description_is_not_claimed_verified() -> None:
    finding = classify_row(row(
        description=("Build scalable analytics datasets, SQL and Python pipelines. " * 18),
        final_url="https://www.linkedin.com/jobs/view/4470007344/",
        identity_validation_reason="identity accepted",
    ))
    assert finding["classification"] == CLASS_NO_SIGNAL
    assert "not a verification" in finding["reason"]
    assert search_page_evidence(finding["description_preview"]) == []


def test_scan_uses_readonly_connection_and_ignores_empty_rows() -> None:
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "fixture.duckdb"
        with duckdb.connect(str(path)) as connection:
            connection.execute("""
                CREATE TABLE raw_jobs(
                    record_key VARCHAR PRIMARY KEY, source VARCHAR,
                    title VARCHAR, company_name VARCHAR,
                    apply_url VARCHAR, description VARCHAR)
            """)
            connection.execute("""
                CREATE TABLE job_enrichment_attempts(
                    record_key VARCHAR PRIMARY KEY,
                    final_url VARCHAR, status VARCHAR,
                    identity_validation_reason VARCHAR,
                    description_source VARCHAR,
                    official_url_status VARCHAR,
                    verified_posting_key VARCHAR)
            """)
            for candidate in [
                row(record_key="bad"),
                row(record_key="safe",
                    description="Real job description with qualifications and responsibilities.",
                    final_url="https://www.linkedin.com/jobs/view/4470007344/",
                    identity_validation_reason="identity accepted"),
                row(record_key="empty", description=None),
                row(record_key="not-linkedin", source="lensa",
                    apply_url="https://lensa.com/jobs/example",
                    final_url="https://jobs.example.com/example",
                    description=LISTING),
            ]:
                connection.execute(
                    "INSERT INTO raw_jobs VALUES (?,?,?,?,?,?)",
                    [candidate[k] for k in
                     ("record_key", "source", "title", "company_name",
                      "apply_url", "description")],
                )
                connection.execute(
                    "INSERT INTO job_enrichment_attempts VALUES (?,?,?,?,?,?,?)",
                    [candidate[k] for k in
                     ("record_key", "final_url", "attempt_status",
                      "identity_validation_reason", "description_source",
                      "official_url_status", "verified_posting_key")],
                )

        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = audit(path)
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        assert before == after, "Audit must not modify the database"
        assert result["read_only"] is True
        assert result["network_requests"] == 0
        assert result["scanned_stored_descriptions"] == 2
        assert result["flagged_records"] == 1
        assert result["findings"][0]["record_key"] == "bad"


GENERIC_RESULTS = (
    "Get notified about new Senior Data Engineer jobs in United States . "
    "Sign in to create job alert 11,000+ Senior Data Engineer Jobs in "
    "United States. Sr. Data Engineer Carrot San Francisco Be an early "
    "applicant Actively Hiring."
)
SPANISH_RESULTS = (
    "Get notified about new Ingeniero jobs in Spain . Sign in to create "
    "job alert 11,000+ Ingeniero Jobs in Spain. SIEM Engineer Madrid."
)


def test_generic_results_header_recognized_despite_stale_attempt_status() -> None:
    for status in ("not_improved", "blocked", "enriched"):
        finding = classify_row(row(
            description=GENERIC_RESULTS, attempt_status=status,
            final_url="https://www.linkedin.com/jobs/view/4470007344/",
        ))
        assert finding["classification"] == CLASS_HIGH_CONFIDENCE
        assert "generic results-list header and job-count" in finding["evidence_markers"]
    assert has_generic_search_results_header(GENERIC_RESULTS)


def test_expired_spanish_linkedin_search_results_are_detected() -> None:
    finding = classify_row(row(
        description=SPANISH_RESULTS, attempt_status="enriched",
        final_url="https://es.linkedin.com/jobs/ingeniero-empleos?trk=expired_jd_redirect",
    ))
    assert finding["classification"] == CLASS_HIGH_CONFIDENCE
    assert has_generic_search_results_header(SPANISH_RESULTS)


def test_job_page_chrome_not_sufficient_to_quarantine() -> None:
    genuine_page = (
        "Senior Data Engineer 9amHealth United States "
        "Join or sign in to find your next job Join to apply for the Senior "
        "Data Engineer role at 9amHealth. New to LinkedIn? Join now. "
        "Get notified when a new job is posted. Set alert. "
        "Sign in to set job alerts. "
        "Design dbt models and Snowflake pipelines for health analytics."
    )
    finding = classify_row(row(
        description=genuine_page,
        final_url="https://www.linkedin.com/jobs/view/4470007344/",
        identity_validation_reason="identity accepted",
    ))
    assert finding["classification"] != CLASS_HIGH_CONFIDENCE
    assert not has_generic_search_results_header(genuine_page)


def main() -> None:
    test_generic_results_header_recognized_despite_stale_attempt_status()
    test_expired_spanish_linkedin_search_results_are_detected()
    test_job_page_chrome_not_sufficient_to_quarantine()
    test_exact_point32health_example_is_confirmed()
    test_long_linkedin_job_listing_text_is_flagged_without_redirect()
    test_redirect_alone_needs_review_not_proven_contamination()
    test_legacy_identity_bypass_only_flags_review()
    test_legitimate_job_description_is_not_claimed_verified()
    test_scan_uses_readonly_connection_and_ignores_empty_rows()
    print("LinkedIn redirected-description offline audit tests passed.")


if __name__ == "__main__":
    main()
