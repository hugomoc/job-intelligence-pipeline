"""Offline regressions for recovery from this record's independent evidence."""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import unittest

from src.enrichment.verified_identity_recovery import (
    CLASS_INCONSISTENT,
    CLASS_RECOVERABLE,
    CLASS_UNSAFE,
    classify_missing_verified_identity,
    supported_specific_posting_key,
)


ASHBY_URL = "https://jobs.ashbyhq.com/optro/72669bd1-552c-40f2-b271-17b3d3b3cc8f"
ASHBY_KEY = "ashby:72669bd1-552c-40f2-b271-17b3d3b3cc8f"
VERIFIED_AT = datetime(2026, 10, 11, 19, 33, 36, tzinfo=timezone.utc)


def own_verified_row(**changes):
    """The independently stored evidence shape of the legacy Optro record."""
    row = dict(
        record_key="legacy-optro", source="welcometothejungle",
        title="Senior Analytics Engineer (Commercial)", company_name="Optro",
        location="Remote (within the US)", status="enriched", http_status=200,
        apply_url="https://welcometothejungle.com/jobs/source-listing",
        requested_url=ASHBY_URL, final_url=ASHBY_URL,
        extraction_method="json_ld",
        resolved_candidate_title="Senior Analytics Engineer, Commercial",
        resolved_candidate_company="Optro",
        resolved_candidate_location="United States; Remote - United States",
        identity_confidence=1.0, title_similarity=1.0, company_similarity=1.0,
        identity_validation_reason="identity accepted",
        official_job_url=ASHBY_URL, official_url_status="FOUND_VERIFIED",
        official_url_source="ashby", official_url_resolved_at=VERIFIED_AT,
        official_url_confidence=1.0, official_url_validation_reason="identity accepted",
        official_resolved_title="Senior Analytics Engineer, Commercial",
        official_resolved_company="Optro",
        official_resolved_location="United States; Remote - United States",
        verified_posting_key=None, verified_posting_key_source=None,
        verified_posting_key_trust=None, reused_description=False,
        matched_prior_record_key=None, matched_prior_source=None,
        description_source="ashby",
    )
    row.update(changes)
    return row


class VerifiedIdentityRecoveryTests(unittest.TestCase):
    def test_own_legacy_ashby_evidence_recovers_deterministically(self):
        row = own_verified_row()
        before = deepcopy(row)
        result = classify_missing_verified_identity(row)
        self.assertEqual(result.classification, CLASS_RECOVERABLE)
        self.assertEqual(result.derived_key, ASHBY_KEY)
        self.assertEqual(result.proposed_source, "official_found_verified")
        self.assertEqual(result.proposed_trust, "trusted")
        self.assertEqual(result.proposed_timestamp, VERIFIED_AT)
        self.assertEqual(result.proposed_confidence, 1.0)
        self.assertEqual(result, classify_missing_verified_identity(row))
        self.assertEqual(row, before)
        serialized = asdict(result)
        self.assertEqual(serialized["evidence"]["stored_fields"]["resolved_candidate_company"], "Optro")
        self.assertIn("do not persist per-field", serialized["evidence"]["provenance_caveat"])
        self.assertEqual(serialized["evidence"]["current_candidate_validation"]["location_compatibility"], "unknown")

    def test_own_greenhouse_evidence_recovers(self):
        url = "https://job-boards.greenhouse.io/optro/jobs/123"
        result = classify_missing_verified_identity(own_verified_row(
            requested_url=url, final_url=url, official_job_url=url,
            official_url_source="greenhouse", description_source="greenhouse"))
        self.assertEqual(result.classification, CLASS_RECOVERABLE)
        self.assertEqual(result.derived_key, "greenhouse:123")

    def test_supported_employer_requires_explicit_stored_employer_source(self):
        url = "https://careers.optro.example/jobs/senior-engineer-123"
        row = own_verified_row(requested_url=url, final_url=url, official_job_url=url,
                               official_url_source="employer")
        self.assertEqual(classify_missing_verified_identity(row).classification, CLASS_RECOVERABLE)
        self.assertEqual(supported_specific_posting_key(url), "")
        self.assertEqual(supported_specific_posting_key(url, allow_employer=True), "official-url:" + url)
        row["official_url_source"] = "search"
        self.assertEqual(classify_missing_verified_identity(row).classification, CLASS_UNSAFE)

    def test_third_party_url_cannot_be_trusted(self):
        for url in ("https://lensa.com/jobs/data-engineer/123",
                    "https://www.builtinsf.com/job/data-engineer/123"):
            with self.subTest(url=url):
                row = own_verified_row(official_job_url=url, requested_url=url, final_url=url,
                                       official_url_source="employer")
                self.assertEqual(classify_missing_verified_identity(row).classification, CLASS_UNSAFE)
                self.assertEqual(supported_specific_posting_key(url, allow_employer=True), "")

    def test_generic_url_cannot_be_trusted(self):
        for url in ("https://jobs.ashbyhq.com/optro", "https://example.com/careers",
                    "https://boards.greenhouse.io/optro"):
            with self.subTest(url=url):
                result = classify_missing_verified_identity(own_verified_row(official_job_url=url))
                self.assertEqual(result.classification, CLASS_UNSAFE)
                self.assertIsNone(result.proposed_trust)

    def test_well_formed_unsupported_workday_path_is_unsafe_not_malformed(self):
        url = "https://tenant.wd1.myworkdayjobs.com/en-US/careers/job/Engineer_REQ123"
        result = classify_missing_verified_identity(own_verified_row(official_job_url=url))
        self.assertEqual(result.classification, CLASS_UNSAFE)

    def test_ats_lookalikes_are_rejected_even_with_employer_opt_in(self):
        for host in ("greenhouse.io.evil.example", "notgreenhouse.io",
                     "jobs.lever.co.evil.example", "evilashbyhq.com",
                     "notmyworkdayjobs.com", "icims.com.evil.example",
                     "evil-workday.example"):
            url = "https://" + host + "/optro/jobs/123"
            with self.subTest(host=host):
                self.assertEqual(supported_specific_posting_key(url), "")
                self.assertEqual(supported_specific_posting_key(url, allow_employer=True), "")

    def test_conflicting_stored_key_cannot_be_repaired(self):
        result = classify_missing_verified_identity(own_verified_row(verified_posting_key="ashby:other"))
        self.assertEqual(result.classification, CLASS_INCONSISTENT)
        self.assertIsNone(result.proposed_source)

    def test_conflicting_official_url_evidence_cannot_be_repaired(self):
        other = ASHBY_URL.replace("72669bd1", "82669bd1")
        for field in ("final_url", "requested_url", "resolved_candidate_url", "apply_url"):
            with self.subTest(field=field):
                result = classify_missing_verified_identity(own_verified_row(**{field: other}))
                self.assertEqual(result.classification, CLASS_INCONSISTENT)
                self.assertIn(field, result.reason)

    def test_missing_independent_company_cannot_use_official_fallback(self):
        result = classify_missing_verified_identity(own_verified_row(resolved_candidate_company=None))
        self.assertEqual(result.classification, CLASS_UNSAFE)
        self.assertIn("resolved_candidate_company", result.reason)

    def test_url_and_found_status_alone_are_not_evidence(self):
        result = classify_missing_verified_identity(dict(
            record_key="url-only", official_url_status="FOUND_VERIFIED",
            official_job_url=ASHBY_URL, title="Senior Analytics Engineer", company_name="Optro"))
        self.assertEqual(result.classification, CLASS_UNSAFE)
        self.assertIsNone(result.proposed_trust)

    def test_wrong_fetched_company_is_rejected_even_when_official_company_matches(self):
        result = classify_missing_verified_identity(own_verified_row(resolved_candidate_company="Other Employer"))
        self.assertEqual(result.classification, CLASS_UNSAFE)
        self.assertIn("current candidate identity validation rejected", result.reason)

    def test_wrong_official_company_is_rejected_even_when_fetched_company_matches(self):
        result = classify_missing_verified_identity(own_verified_row(official_resolved_company="Other Employer"))
        self.assertEqual(result.classification, CLASS_UNSAFE)

    def test_rejected_or_missing_stored_validation_is_not_repairable(self):
        for field in ("identity_validation_reason", "official_url_validation_reason"):
            for reason in (None, "company mismatch", "identity accepted; location mismatch"):
                with self.subTest(field=field, reason=reason):
                    result = classify_missing_verified_identity(own_verified_row(**{field: reason}))
                    self.assertEqual(result.classification, CLASS_UNSAFE)

    def test_reuse_prior_relation_and_unknown_provenance_are_not_independent(self):
        for change in ({"reused_description": True}, {"reused_description": None},
                       {"matched_prior_record_key": "another-record"},
                       {"matched_prior_source": "lensa"},
                       {"description_source": "verified_reuse"},
                       {"resolved_candidate_company_source": "discovery_candidate"}):
            with self.subTest(change=change):
                self.assertEqual(classify_missing_verified_identity(own_verified_row(**change)).classification, CLASS_UNSAFE)

    def test_strong_structured_extraction_corroboration_is_required(self):
        result = classify_missing_verified_identity(own_verified_row(extraction_method="html_selector"))
        self.assertEqual(result.classification, CLASS_UNSAFE)

    def test_explicit_trust_rejection_cannot_be_overridden(self):
        for trust in ("conflict", "untrusted"):
            result = classify_missing_verified_identity(own_verified_row(verified_posting_key_trust=trust))
            self.assertEqual(result.classification, CLASS_INCONSISTENT)

    def test_missing_company_cannot_bootstrap_exact_key_proof(self):
        row = own_verified_row(company_name="", resolved_candidate_company="", official_resolved_company="")
        result = classify_missing_verified_identity(row)
        self.assertEqual(result.classification, CLASS_UNSAFE)

    def test_potential_location_conflict_requires_provenance(self):
        row = own_verified_row(resolved_candidate_location="London, United Kingdom",
                               official_resolved_location="London, United Kingdom")
        result = classify_missing_verified_identity(row)
        self.assertEqual(result.classification, CLASS_UNSAFE)
        self.assertIn("location provenance", result.reason)
        self.assertFalse(result.evidence["potential_location_conflict"]["source_is_stored"])

    def test_untrusted_location_source_cannot_erase_potential_conflict(self):
        row = own_verified_row(resolved_candidate_location="London, United Kingdom",
                               resolved_candidate_location_source="search_snippet")
        self.assertEqual(classify_missing_verified_identity(row).classification, CLASS_UNSAFE)

    def test_malformed_url_and_confidence_are_inconsistent(self):
        for changes in ({"official_job_url": "https://[broken/jobs/123"},
                        {"official_job_url": "https://jobs.ashbyhq.com:broken/optro/job/123"},
                        {"identity_confidence": float("nan")},
                        {"company_similarity": 2}, {"official_url_confidence": "bad"}):
            with self.subTest(changes=changes):
                self.assertEqual(classify_missing_verified_identity(own_verified_row(**changes)).classification, CLASS_INCONSISTENT)

    def test_repair_enables_normal_trusted_display_path_only(self):
        from src.repositories.recommendation_repository import prepare_job_display, trusted_display_posting_key
        from src.test_verified_display_deduplication import posting

        legacy = posting("legacy", source="welcometothejungle", official_job_url=ASHBY_URL,
                         verified_posting_key=None, verified_posting_key_source=None,
                         verified_posting_key_trust=None)
        other = posting("other", source="lensa", official_job_url=ASHBY_URL,
                        verified_posting_key=ASHBY_KEY)
        self.assertIsNone(trusted_display_posting_key(legacy))
        self.assertEqual(len(prepare_job_display([legacy, other])), 2)
        repair = classify_missing_verified_identity(own_verified_row())
        legacy.update(verified_posting_key=repair.derived_key,
                      verified_posting_key_source=repair.proposed_source,
                      verified_posting_key_trust=repair.proposed_trust)
        self.assertEqual(trusted_display_posting_key(legacy), ASHBY_KEY)
        self.assertEqual(len(prepare_job_display([legacy, other])), 1)


    def test_malformed_or_timezone_missing_verification_timestamp_is_not_recovered(self):
        for value in ("not-a-timestamp", "2026-10-11T19:33:36"):
            with self.subTest(value=value):
                result = classify_missing_verified_identity(own_verified_row(official_url_resolved_at=value))
                self.assertEqual(result.classification, CLASS_INCONSISTENT)
                self.assertIsNone(result.proposed_trust)

    def test_missing_company_is_recorded_even_when_reuse_provenance_is_unknown(self):
        result = classify_missing_verified_identity(own_verified_row(
            reused_description=None, resolved_candidate_company=None))
        self.assertEqual(result.classification, CLASS_UNSAFE)
        self.assertIn("resolved_candidate_company", result.evidence["missing_independent_identity_fields"])


if __name__ == "__main__":
    unittest.main()
