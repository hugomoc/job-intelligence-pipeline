"""Offline persistence regressions for verified posting identity consistency."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import duckdb

from src import database, enrich_jobs as enrichment
from src.enrichment.job_description import JobDescriptionResult
from src.enrichment.job_identity import validate_job_identity
from src.enrichment.official_job_resolver import OfficialJobResolutionResult
from src.verified_posting_identity import verified_posting_key_from_url


ASHBY_URL = "https://jobs.ashbyhq.com/optro/72669bd1-552c-40f2-b271-17b3d3b3cc8f"
GREENHOUSE_URL = "https://boards.greenhouse.io/optro/jobs/123"
VERIFIED_AT = datetime(2026, 10, 11, 19, 33, 36, tzinfo=timezone.utc)


def posting():
    return dict(record_key="optro", source="welcometothejungle",
                title="Senior Analytics Engineer (Commercial)", company_name="Optro",
                location="Remote (within the US)", apply_url="https://example.com/alert")


def fetched(url=ASHBY_URL):
    return JobDescriptionResult(
        requested_url=url, final_url=url, status="enriched", http_status=200,
        extraction_method="json_ld", description="Responsibilities include SQL and dbt.",
        word_count=6, error_message=None,
        resolved_title="Senior Analytics Engineer, Commercial", resolved_company="Optro",
        resolved_location="Remote - United States",
    )


def accepted_validation():
    return validate_job_identity(
        original_title=posting()["title"], original_company="Optro",
        resolved_title=fetched().resolved_title, resolved_company="Optro",
    )


def verified_resolution(url=ASHBY_URL):
    return OfficialJobResolutionResult(
        status="FOUND_VERIFIED", official_job_url=url, official_url_source="ashby",
        official_url_resolved_at=VERIFIED_AT, official_url_confidence=1.0,
        official_url_validation_reason="identity accepted",
        resolved_title=fetched().resolved_title, resolved_company="Optro",
        resolved_location=fetched().resolved_location,
        identity_validation=accepted_validation(),
    )


class VerifiedIdentityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        for name, value in (("DATA_DIR", directory),
                            ("DATABASE_PATH", directory / "jobs.duckdb")):
            patcher = patch.object(database, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        enrichment.initialize_enrichment_tables()

    def saved(self):
        with database.get_connection() as connection:
            return connection.execute("SELECT * FROM job_enrichment_attempts").fetchall()

    def assert_refused_before_connection(self, job, result, resolution, validation=None):
        with patch.object(enrichment, "get_connection") as connect:
            with self.assertRaises(ValueError):
                enrichment.save_enrichment_attempt(
                    job, result, identity_validation=validation,
                    official_resolution=resolution,
                )
            connect.assert_not_called()
        self.assertEqual(self.saved(), [])

    def test_ashby_and_greenhouse_persist_matching_trusted_metadata(self):
        for index, url in enumerate((ASHBY_URL, GREENHOUSE_URL)):
            with self.subTest(url=url):
                job = dict(posting(), record_key=str(index))
                resolution = replace(verified_resolution(url),
                                     official_url_source="greenhouse" if index else "ashby")
                enrichment.save_enrichment_attempt(job, fetched(url), official_resolution=resolution)
                with database.get_connection() as connection:
                    row = connection.execute(
                        "SELECT official_job_url, official_url_status, verified_posting_key, "
                        "verified_posting_key_source, verified_posting_key_trust "
                        "FROM job_enrichment_attempts WHERE record_key=?", [str(index)],
                    ).fetchone()
                self.assertEqual(row, (url, "FOUND_VERIFIED", verified_posting_key_from_url(url),
                                       "official_found_verified", "trusted"))

    def resolve_fetched(self, url, *, final_url=None, source="greenhouse"):
        from src.enrichment import official_job_resolver as resolver
        job = posting()
        result = fetched(final_url or url)
        candidate = resolver.OfficialJobCandidate(url=url, label="fixture discovery", source=source)
        with patch.object(resolver, "fetch_candidate_description", return_value=result):
            resolution = resolver.resolve_from_candidates(job, object(), [candidate], source="ats")
        return job, result, resolution

    def test_resolver_cannot_verify_generic_redirect_or_missing_ats_id(self):
        cases = (
            (GREENHOUSE_URL, "https://boards.greenhouse.io/optro", "greenhouse"),
            (GREENHOUSE_URL, "https://careers.example.com/careers", "greenhouse"),
            ("https://jobs.ashbyhq.com/optro", None, "ashby"),
            ("https://careers.example.com/careers", None, "employer"),
        )
        for url, final_url, source in cases:
            with self.subTest(url=url, final_url=final_url):
                job, result, resolution = self.resolve_fetched(url, final_url=final_url, source=source)
                self.assertNotEqual(resolution.status, "FOUND_VERIFIED")
                self.assertIsNone(resolution.official_job_url)
                self.assertFalse(job["candidate_validation_trace"][0]["accepted"])
                self.assertIn("specific posting identity", resolution.official_url_validation_reason)

    def test_resolver_search_provenance_cannot_authorize_employer_url(self):
        job, result, resolution = self.resolve_fetched(
            "https://careers.example.com/jobs/123", source="search")
        self.assertNotEqual(resolution.status, "FOUND_VERIFIED")
        # This used to be FOUND_VERIFIED but rejected by the unchanged save guard.
        self.assert_refused_before_connection(job, result,
            replace(verified_resolution(result.final_url), official_url_source="search"))

    def test_resolver_and_save_accept_same_specific_ats_keys_and_preserve_ids(self):
        urls = ((GREENHOUSE_URL + "?utm_source=fixture", "greenhouse"),
                ("https://boards.greenhouse.io/optro?gh_jid=123&utm_source=fixture", "greenhouse"),
                ("https://optro.wd1.myworkdayjobs.com/en-US/Careers/job/Remote/Engineer_R123?jobId=R123", "workday"),
                (ASHBY_URL, "ashby"))
        for index, (url, source) in enumerate(urls):
            with self.subTest(url=url):
                job, result, resolution = self.resolve_fetched(url, source=source)
                job["record_key"] = f"posting-{index}"
                self.assertEqual(resolution.status, "FOUND_VERIFIED")
                self.assertEqual(resolution.official_job_url, url)
                enrichment.save_enrichment_attempt(job, result, official_resolution=resolution)
                with database.get_connection() as connection:
                    row = connection.execute(
                        "SELECT official_job_url, verified_posting_key FROM job_enrichment_attempts "
                        "WHERE record_key=?", [job["record_key"]]).fetchone()
                self.assertEqual(row, (url, verified_posting_key_from_url(url)))
                changed = url.replace("123", "456").replace("72669bd1", "82669bd1")
                self.assertNotEqual(verified_posting_key_from_url(url), verified_posting_key_from_url(changed))

    def test_rejection_diagnostics_keep_posting_identity_but_drop_credentials(self):
        emitted = []
        url = "https://user:private-pass@careers.example.com/jobs/123?jobId=123&token=private-token#private-fragment"
        with patch.object(enrichment, "process_enrichment_job",
                          return_value=self.batch_result(posting(), resolution=replace(
                              verified_resolution(), official_job_url=url, official_url_source="search"))), patch.object(
                enrichment, "save_enrichment_attempt", side_effect=ValueError("specific posting URL required")):
            summary = enrichment.process_enrichment_batch([posting()], object(), emit=emitted.append)
        text = "\n".join(emitted)
        for label in ("record_key=optro", "source=welcometothejungle", "original apply_url=",
                      "candidate official_job_url=", "official_url_source=search", "verified_posting_key=",
                      "verified_posting_key_source=", "verified_posting_key_trust=", "jobId=123"):
            self.assertIn(label, text)
        for secret in ("private-pass", "private-token", "private-fragment", "user:"):
            self.assertNotIn(secret, text)
        self.assertEqual(summary.persistence_error, 1)
        self.assertEqual(self.saved(), [])

    def test_missing_or_wrong_generated_metadata_cannot_persist_found_verified(self):
        for metadata in ((None, None, None, None, None),
                         ("greenhouse:999", "official_found_verified", VERIFIED_AT, 1.0, "trusted")):
            with self.subTest(metadata=metadata):
                with patch.object(enrichment, "verified_key_metadata_for_save", return_value=metadata):
                    self.assert_refused_before_connection(posting(), fetched(), verified_resolution())

    def test_independent_resolved_metadata_can_recompute_missing_validation(self):
        resolution = replace(verified_resolution(), identity_validation=None)
        enrichment.save_enrichment_attempt(posting(), fetched(), official_resolution=resolution)
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT identity_validation_reason, identity_confidence, verified_posting_key "
                "FROM job_enrichment_attempts").fetchone(),
                ("identity accepted", 1.0, verified_posting_key_from_url(ASHBY_URL)))

    def test_status_and_url_without_independent_evidence_are_refused(self):
        resolution = replace(verified_resolution(), identity_validation=None,
                             resolved_title=None, resolved_company=None)
        self.assert_refused_before_connection(posting(), fetched(), resolution)

    def test_missing_resolved_company_cannot_use_original_company_as_proof(self):
        resolution = replace(verified_resolution(), identity_validation=None, resolved_company=None)
        self.assert_refused_before_connection(posting(), fetched(), resolution)

    def test_positive_validation_cannot_hide_missing_or_conflicting_resolved_metadata(self):
        for changes in (dict(resolved_company=None), dict(resolved_company="Other Employer"),
                        dict(resolved_title=None), dict(resolved_title="Software Engineer")):
            with self.subTest(changes=changes):
                self.assert_refused_before_connection(
                    posting(), fetched(), replace(verified_resolution(), **changes))

    def test_supplied_rejection_vetoes_accepted_resolution(self):
        rejection = replace(accepted_validation(), accepted=False, reason="company mismatch")
        self.assert_refused_before_connection(posting(), fetched(), verified_resolution(), rejection)

    def test_resolution_rejection_vetoes_supplied_acceptance(self):
        resolution = replace(verified_resolution(), identity_validation=replace(
            accepted_validation(), accepted=False, reason="posting key conflict"))
        self.assert_refused_before_connection(posting(), fetched(), resolution, accepted_validation())

    def test_third_party_generic_and_malformed_urls_are_refused(self):
        for url in ("https://www.builtinsf.com/job/data-engineer/123",
                    "https://lensa.com/data-engineer/jobs/123",
                    "https://jobs.ashbyhq.com/optro",
                    "https://boards.greenhouse.io/optro",
                    "https://example.com/careers", "https://[malformed/jobs/123",
                    ASHBY_URL.replace("jobs.ashbyhq.com", "fakeashbyhq.com"),
                    GREENHOUSE_URL.replace("boards.greenhouse.io", "greenhouse.io.evil.example")):
            with self.subTest(url=url):
                self.assert_refused_before_connection(posting(), fetched(url), verified_resolution(url))

    def test_existing_key_conflicts_and_conflict_trust_are_refused(self):
        for metadata in (
            dict(verified_posting_key="greenhouse:999"),
            dict(verified_posting_key="greenhouse:999", verified_posting_key_trust="trusted",
                 verified_posting_key_source="official_found_verified"),
            dict(verified_posting_key=verified_posting_key_from_url(ASHBY_URL),
                 verified_posting_key_trust="conflict"),
        ):
            with self.subTest(metadata=metadata):
                self.assert_refused_before_connection(dict(posting(), **metadata), fetched(),
                                                      verified_resolution())

    def test_existing_official_url_conflict_is_refused_even_without_key(self):
        job = dict(posting(), official_job_url=GREENHOUSE_URL, official_url_status="FOUND_VERIFIED")
        self.assert_refused_before_connection(job, fetched(), verified_resolution())

    def test_fetched_posting_key_conflict_is_refused(self):
        self.assert_refused_before_connection(posting(), fetched(GREENHOUSE_URL), verified_resolution())

    def test_verified_source_redirect_keeps_its_provenance(self):
        resolution = replace(verified_resolution(), official_url_source="source_redirect")
        enrichment.save_enrichment_attempt(posting(), fetched(), official_resolution=resolution)
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT verified_posting_key_source FROM job_enrichment_attempts").fetchone()[0],
                "source_redirect_verified")

    def test_employer_posting_requires_explicit_employer_provenance(self):
        url = "https://careers.optro.example/jobs/12345"
        self.assert_refused_before_connection(posting(), fetched(url), verified_resolution(url))
        resolution = replace(verified_resolution(url), official_url_source="employer")
        enrichment.save_enrichment_attempt(posting(), fetched(url), official_resolution=resolution)
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT verified_posting_key FROM job_enrichment_attempts").fetchone()[0],
                verified_posting_key_from_url(url))

    def test_transient_fetch_failure_preserves_established_matching_identity(self):
        key = verified_posting_key_from_url(ASHBY_URL)
        job = dict(posting(), official_url_status="FOUND_VERIFIED", official_job_url=ASHBY_URL,
                   verified_posting_key=key, verified_posting_key_source="source_redirect_verified",
                   verified_posting_key_trust="trusted", verified_posting_key_verified_at=VERIFIED_AT,
                   verified_posting_key_confidence=0.99)
        result = replace(fetched(), status="fetch_error", http_status=None, final_url=None,
                         resolved_title=None, resolved_company=None, description="", word_count=0)
        resolution = replace(verified_resolution(), identity_validation=None,
                             resolved_title=None, resolved_company=None)
        enrichment.save_enrichment_attempt(job, result, official_resolution=resolution)
        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT status, official_url_status, official_job_url, verified_posting_key, "
                "verified_posting_key_source, verified_posting_key_trust, "
                "verified_posting_key_confidence, verified_posting_key_verified_at "
                "FROM job_enrichment_attempts").fetchone()
        self.assertEqual(row, ("fetch_error", "FOUND_VERIFIED", ASHBY_URL, key,
                               "source_redirect_verified", "trusted", 0.99, VERIFIED_AT))

    def test_transient_employer_fetch_preserves_existing_trust_despite_search_discovery_label(self):
        url = "https://careers.optro.example/jobs/12345"
        key = verified_posting_key_from_url(url)
        job = dict(posting(), official_url_status="FOUND_VERIFIED", official_job_url=url,
                   official_url_source="search", verified_posting_key=key,
                   verified_posting_key_source="official_found_verified",
                   verified_posting_key_trust="trusted", verified_posting_key_verified_at=VERIFIED_AT,
                   verified_posting_key_confidence=0.99)
        result = replace(fetched(url), status="fetch_error", http_status=None, final_url=None,
                         resolved_title=None, resolved_company=None, description="", word_count=0)
        resolution = replace(verified_resolution(url), official_url_source="search",
                             identity_validation=None, resolved_title=None, resolved_company=None)
        enrichment.save_enrichment_attempt(job, result, official_resolution=resolution)
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT official_url_status,verified_posting_key,verified_posting_key_trust "
                "FROM job_enrichment_attempts").fetchone(), ("FOUND_VERIFIED", key, "trusted"))

    def test_transient_fetch_failure_cannot_establish_legacy_identity(self):
        job = dict(posting(), official_url_status="FOUND_VERIFIED", official_job_url=ASHBY_URL)
        result = replace(fetched(), status="fetch_error", resolved_title=None, resolved_company=None)
        resolution = replace(verified_resolution(), identity_validation=None,
                             resolved_title=None, resolved_company=None)
        self.assert_refused_before_connection(job, result, resolution)

    def test_failed_replacement_preserves_previous_attempt_atomically(self):
        enrichment.save_enrichment_attempt(posting(), fetched(), official_resolution=verified_resolution())
        before = self.saved()
        invalid_job = dict(posting(), source=None)
        with self.assertRaises(duckdb.ConstraintException):
            enrichment.save_enrichment_attempt(invalid_job, fetched(),
                                               official_resolution=verified_resolution())
        self.assertEqual(self.saved(), before)


    def batch_result(self, job, *, resolution=None):
        return enrichment.EnrichmentProcessingResult(
            result=fetched(), stored_status="enriched", stored_error=None,
            identity_validation=None,
            official_resolution=resolution or verified_resolution(), updated=False,
        )

    def test_batch_rejected_verified_identity_never_changes_raw_description(self):
        enrichment.save_enrichment_attempt(posting(), fetched(),
                                           official_resolution=verified_resolution())
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE raw_jobs SET description='original trusted description', "
                "description_updated_at=TIMESTAMPTZ '2026-01-01 00:00:00+00' "
                "WHERE record_key='optro'"
            )
            before_raw = connection.execute(
                "SELECT description, description_updated_at FROM raw_jobs "
                "WHERE record_key='optro'"
            ).fetchone()
        before_attempt = self.saved()
        malicious = replace(verified_resolution(),
            official_job_url="https://maximus.avature.net/careers/FolderDetail?folderId=43413",
            official_url_source="search")
        proposed = replace(fetched(), description="Unverified replacement description " * 25,
                           word_count=75)
        processed = enrichment.EnrichmentProcessingResult(
            result=proposed, stored_status="enriched", stored_error=None,
            identity_validation=None, official_resolution=malicious,
            updated=True, pending_description=proposed,
        )
        with patch.object(enrichment, "process_enrichment_job", return_value=processed):
            summary = enrichment.process_enrichment_batch([posting()], client=object())
        with database.get_connection() as connection:
            after_raw = connection.execute(
                "SELECT description, description_updated_at FROM raw_jobs "
                "WHERE record_key='optro'"
            ).fetchone()
        self.assertEqual(after_raw, before_raw)
        self.assertEqual(self.saved(), before_attempt)
        self.assertEqual(summary.descriptions_updated, 0)
        self.assertEqual(summary.persistence_error, 1)

    def test_db_constraint_rolls_back_attempt_and_pending_description(self):
        enrichment.save_enrichment_attempt(posting(), fetched(),
                                           official_resolution=verified_resolution())
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE raw_jobs SET description='valid description' WHERE record_key='optro'"
            )
            before_raw = connection.execute(
                "SELECT description, description_updated_at FROM raw_jobs "
                "WHERE record_key='optro'"
            ).fetchone()
        before_attempt = self.saved()
        invalid = dict(posting(), source=None)
        with self.assertRaises(duckdb.ConstraintException):
            enrichment.save_enrichment_attempt(
                invalid, fetched(), official_resolution=verified_resolution(),
                description_update=fetched(),
            )
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT description, description_updated_at FROM raw_jobs "
                "WHERE record_key='optro'"
            ).fetchone(), before_raw)
        self.assertEqual(self.saved(), before_attempt)

    def test_batch_guard_rejection_retains_attempt_and_saves_following_job(self):
        enrichment.save_enrichment_attempt(posting(), fetched(), official_resolution=verified_resolution())
        before = self.saved()
        following = dict(posting(), record_key="following")
        # Actual failing URL shape from the Maximus daily run: keep guard intact.
        rejected = replace(verified_resolution(),
                           official_job_url="https://maximus.avature.net/careers/FolderDetail?folderId=43413",
                           official_url_source="search")
        save = enrichment.save_enrichment_attempt
        with patch.object(enrichment, "process_enrichment_job", side_effect=[
                self.batch_result(posting(), resolution=rejected), self.batch_result(following),
        ]) as process, patch.object(enrichment, "save_enrichment_attempt", wraps=save) as persist:
            summary = enrichment.process_enrichment_batch([posting(), following], client=object())
        self.assertEqual((process.call_count, persist.call_count), (2, 2))
        self.assertEqual([call.kwargs["job"]["record_key"] for call in persist.call_args_list],
                         ["optro", "following"])
        self.assertTrue(all(call.kwargs["status"] == "enriched" for call in persist.call_args_list))
        self.assertEqual(summary.jobs_processed, 2)
        self.assertEqual(summary.persistence_error, 1)
        self.assertEqual(summary.totals["persistence_error"], 1)
        self.assertEqual(summary.totals["fetch_error"], 0)
        self.assertEqual(summary.totals["enriched"], 1)
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT * FROM job_enrichment_attempts WHERE record_key='optro'").fetchall(), before)
            self.assertEqual(connection.execute(
                "SELECT status,verified_posting_key_trust FROM job_enrichment_attempts "
                "WHERE record_key='following'").fetchone(), ("enriched", "trusted"))
        log = "\n".join(summary.log_lines)
        for value in ("optro", "Senior Analytics Engineer", "Optro", "ValueError",
                      "Verified official identity requires a specific official posting URL"):
            self.assertIn(value, log)

    def test_batch_processing_failure_continues_with_separate_fetch_count(self):
        first = dict(posting(), record_key="processing-failure")
        following = dict(posting(), record_key="following")
        with patch.object(enrichment, "process_enrichment_job", side_effect=[
                RuntimeError("synthetic processor failure"), self.batch_result(following),
        ]):
            summary = enrichment.process_enrichment_batch([first, following], client=object())
        self.assertEqual(summary.jobs_processed, 2)
        self.assertEqual(summary.totals["fetch_error"], 1)
        self.assertEqual(summary.persistence_error, 0)
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT record_key,status FROM job_enrichment_attempts ORDER BY record_key").fetchall(),
                [("following", "enriched"), ("processing-failure", "fetch_error")])
        self.assertTrue(any("RuntimeError: synthetic processor failure" in line
                            and "processing-failure" in line for line in summary.log_lines))

    def test_batch_database_constraint_failure_retains_prior_attempt(self):
        enrichment.save_enrichment_attempt(posting(), fetched(), official_resolution=verified_resolution())
        before = self.saved()
        invalid = dict(posting(), source=None)
        following = dict(posting(), record_key="following")
        with patch.object(enrichment, "process_enrichment_job", side_effect=[
                self.batch_result(invalid), self.batch_result(following),
        ]):
            summary = enrichment.process_enrichment_batch([invalid, following], client=object())
        self.assertEqual((summary.jobs_processed, summary.persistence_error), (2, 1))
        self.assertEqual(summary.totals["fetch_error"], 0)
        self.assertTrue(any("ConstraintException" in line for line in summary.log_lines))
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT * FROM job_enrichment_attempts WHERE record_key='optro'").fetchall(), before)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM job_enrichment_attempts WHERE record_key='following'").fetchone(), (1,))

    def test_fetch_and_persistence_failures_are_both_reported_when_both_occur(self):
        following = dict(posting(), record_key="following")
        actual_save = enrichment.save_enrichment_attempt
        def save(**kwargs):
            if kwargs["job"]["record_key"] == "optro":
                raise OSError("database unavailable")
            return actual_save(**kwargs)
        with patch.object(enrichment, "process_enrichment_job", side_effect=[
                RuntimeError("fetch failed"), self.batch_result(following),
        ]), patch.object(enrichment, "save_enrichment_attempt", side_effect=save):
            summary = enrichment.process_enrichment_batch([posting(), following], client=object())
        self.assertEqual(summary.jobs_processed, 2)
        self.assertEqual(summary.totals["fetch_error"], 1)
        self.assertEqual(summary.persistence_error, 1)
        self.assertEqual(summary.totals["enriched"], 1)

    def test_batch_diagnostics_redact_secrets_urls_and_log_injection(self):
        emitted = []
        sensitive = ("raw-environment-secret", "header-secret", "password-secret", "query-secret")
        failure = ValueError(
            "Unsafe identity Authorization: Bearer header-secret password=password-secret "
            "raw-environment-secret https://user:pass@example.com/track?token=query-secret\nforged log")
        unsafe_resolution = replace(verified_resolution(),
            official_job_url="https://example.com/track?token=query-secret")
        with patch.dict("os.environ", {"SERVICE_API_KEY": "raw-environment-secret"}), patch.object(
                enrichment, "process_enrichment_job",
                return_value=self.batch_result(posting(), resolution=unsafe_resolution)), patch.object(
                enrichment, "save_enrichment_attempt", side_effect=failure):
            summary = enrichment.process_enrichment_batch([posting()], client=object(), emit=emitted.append)
        self.assertEqual(list(summary.log_lines), emitted)
        log = "\n".join(emitted)
        for secret in sensitive:
            self.assertNotIn(secret, log)
        self.assertNotIn("https://", log)
        self.assertIn("ValueError: Unsafe identity", log)
        self.assertIn("record_key=optro", log)
        self.assertTrue(all("\n" not in line for line in emitted))

    def test_rejected_save_still_obeys_rate_limit_stop(self):
        class Stopped:
            stopped = True
        jobs = [posting(), dict(posting(), record_key="following")]
        with patch.object(enrichment, "process_enrichment_job",
                          return_value=self.batch_result(posting())) as process, patch.object(
                enrichment, "save_enrichment_attempt", side_effect=ValueError("rejected")) as save:
            summary = enrichment.process_enrichment_batch(jobs, client=object(), rate_limit_guard=Stopped())
        self.assertEqual((summary.jobs_processed, summary.persistence_error), (1, 1))
        self.assertEqual((process.call_count, save.call_count), (1, 1))


if __name__ == "__main__":
    unittest.main()
