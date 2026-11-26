"""Attach Description (V1) offline regressions.

Disposable DuckDB databases, fake providers, network blocked. Covers the approved
policies: Option B for historical resolution_rejected attempts, the V1 form
visibility, verified-official precedence and per-record rule refresh.
"""
import io
import json
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from src import database, description_attachment as attach, enrich_jobs, job_screening
from src.candidate_assessments import completed_record_keys
from src.enrichment.job_description import JobDescriptionResult
from src.repositories import recommendation_repository as repo
from src.score_jobs_ai import initialize_ai_tables
from src.test_candidate_assessments import NEW, OLD, profile, score, seed_score
from src.test_recommendation_repository import seed_review_job, with_temp_review_database
from src.test_scoring_queue_guards import ProviderFakes, confirm_candidate, run_service, screening_env
from src.ui.job_visibility import is_assessment_eligible_job

GD = "https://www.glassdoor.com/partner/jobListing.htm?pos=1&guid="
PASTE = "\n".join([
    "Senior Data Engineer at Example Co",
    "About the role",
    "Example Co is hiring a Senior Data Engineer to build the analytics platform used across the business.",
    "Responsibilities",
    "Build and maintain Python and SQL data pipelines that load product and finance data into Snowflake.",
    "Design dimensional data models and document them so analysts can rely on trusted tables.",
    "Own data quality testing, monitoring and incident follow-up for critical reporting datasets.",
    "Collaborate with analytics engineers and stakeholders to deliver reliable reporting automation.",
    "Requirements",
    "Five or more years of experience with SQL, Python and modern ELT tooling such as dbt and Airflow.",
    "Experience designing warehouse schemas, incremental models and data validation frameworks.",
    "Clear written communication and a habit of documenting decisions for the wider team.",
    "Preferred",
    "Experience with cloud data warehouses, orchestration, CI/CD for data and cost-aware query optimization.",
])
SECOND_PASTE = PASTE.replace("analytics platform", "customer data platform").replace(
    "Five or more years", "Six or more years")
OFFICIAL = "\n".join([
    "Senior Data Engineer, Example Co (official posting)",
    "Responsibilities",
    "Build SQL and Python pipelines into Snowflake, model data and maintain quality tests for reporting.",
    "Work with analysts and engineers to deliver documented, reliable datasets for the business.",
    "Requirements",
    "Several years of SQL, Python and dbt experience with warehouse modeling and orchestration tools.",
    "Strong written communication and ownership of production data systems end to end.",
    "Experience operating cloud warehouses, testing frameworks and incremental transformation models.",
    "Comfort partnering with finance and product teams on metric definitions and documentation.",
])
LONGER_SCRAPE = PASTE + "\n" + "\n".join(
    f"Additional listing paragraph {i}: benefits, perks and company culture details for applicants {i}."
    for i in range(30))


# ---------------------------------------------------------------- fixtures
def seed_glassdoor(key, description=None, *, status=None, posting=None, title="Senior Data Engineer", rule=90):
    seed_review_job(key, title, source="glassdoor", description=description,
                    apply_url=GD + key, application_status=status)
    with database.get_connection() as c:
        c.execute("UPDATE raw_jobs SET source_job_id = ? WHERE record_key = ?", [posting or key, key])
        c.execute("INSERT INTO job_matches(record_key,search_id,search_title,match_score,title_score,"
                  "location_score,keyword_score,freshness_score,matched_keywords,excluded_keywords,reasons,"
                  "is_recommended,needs_review) VALUES(?,'search','Data Engineer',?,90,90,90,90,'[]','[]','[]',"
                  "true,false)", [key, rule])


def seed_attempt(key, *, status="resolution_rejected", url="https://unrelated.example/jobs/9"):
    repo.initialize_job_eligibility_table()
    with database.get_connection() as c:
        c.execute("""INSERT INTO job_enrichment_attempts (record_key, source, requested_url, final_url, status,
                         description_word_count, error_message, attempt_count, official_job_url,
                         official_url_status, official_url_validation_reason)
                     VALUES (?, 'glassdoor', ?, ?, ?, 0, 'identity mismatch', 1, ?, 'OFFICIAL_NOT_FOUND',
                             'fetched page failed identity validation')""",
                  [key, GD + key, url, status, url])


def candidate():
    initialize_ai_tables(); profile(OLD); profile(NEW); confirm_candidate()


def row(table, key):
    with database.get_connection() as c:
        if not c.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                         [table]).fetchone()[0]:
            return []
        cursor = c.execute(f"SELECT * FROM {table} WHERE record_key = ?", [key])
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, r)) for r in cursor.fetchall()]


def description(key):
    return row("raw_jobs", key)[0]["description"]


def queue_keys(resume=NEW):
    return {j["record_key"] for j in repo.load_candidate_jobs(resume, None, 100, 0, reuse_any_model=True)}


def unscreened_keys(resume=NEW):
    return {j["record_key"] for j in repo.load_unscreened_job_eligibility_candidates(resume, 100)}


def count_queue(resume=NEW):
    return repo.count_unscored_candidate_jobs(resume, None, 0, reuse_any_model=True)


def enrichment_view(key):
    """The job dict as the enrichment loader would annotate it."""
    job = dict(row("raw_jobs", key)[0])
    attempts = row("job_enrichment_attempts", key)
    job["enrichment_status"] = attempts[0]["status"] if attempts else None
    with database.get_connection() as c:
        attach.annotate_description_attachments(c, [job])
    job["current_word_count"] = len(str(job.get("description") or "").split())
    job["raw_description_word_count"] = job["current_word_count"]
    job["description_state"] = repo.description_state(job)
    return job


@contextmanager
def no_ai_or_network():
    def boom(*a, **k):
        raise AssertionError("no AI or network request may happen during attachment")
    with patch.object(httpx.Client, "send", boom), patch.object(httpx.AsyncClient, "send", boom), \
         patch("src.ai.resume_matcher.score_resume_against_job", boom), \
         patch("src.ai.resume_matcher.score_resume_against_job_openai", boom), \
         patch.object(job_screening, "evaluate_job_eligibility", boom), \
         patch.object(job_screening, "evaluate_job_eligibility_openai", boom):
        yield


def save(key, text, **kw):
    with no_ai_or_network():
        return attach.save_attached_description(key, text, **kw)


def undo(key, **kw):
    with no_ai_or_network():
        return attach.undo_attached_description(key, **kw)


def result(text, *, status="enriched"):
    return JobDescriptionResult(requested_url=GD + "x", final_url="https://example.com/jobs/1", status=status,
                                http_status=200, extraction_method="json_ld", description=text,
                                word_count=len(text.split()), error_message=None)


# ------------------------------------------------------------------ tests
class Validation(unittest.TestCase):
    def test_full_jd_accepted_and_bad_pastes_rejected(self):
        ok = attach.validate_pasted_description(PASTE, title="Senior Data Engineer", company="Example Co")
        self.assertTrue(ok.accepted)
        self.assertIsNone(ok.warning)
        self.assertEqual(ok.word_count, len(PASTE.split()))
        for bad, fragment in (("", "Paste a job description first"),
                              ("Responsibilities: SQL and Python.", "short or incomplete"),
                              (" ".join(f"Search results filter by location view all jobs Data Engineer {i} "
                                        f"at company {i} apply now" for i in range(10)),
                               "listing or navigation"),
                              (" ".join(f"word{i}" for i in range(120)), "lacks the responsibilities"),
                              ("x" * (attach.MAX_PASTE_CHARACTERS + 1), "whole page")):
            with self.subTest(fragment=fragment):
                checked = attach.validate_pasted_description(bad, title="Senior Data Engineer", company="Example Co")
                self.assertFalse(checked.accepted)
                self.assertIn(fragment, checked.reason)

    def test_html_duplicates_and_formatting_artifacts_are_normalized(self):
        html_paste = "<div><h2>Responsibilities</h2><p>" + PASTE.replace("\n", "</p><p>") + "</p></div>"
        checked = attach.validate_pasted_description(html_paste, title="Senior Data Engineer", company="Example Co")
        self.assertTrue(checked.accepted)
        self.assertNotIn("<p>", checked.text)
        doubled = attach.validate_pasted_description(PASTE + "\n" + PASTE, title="Senior Data Engineer",
                                                     company="Example Co")
        self.assertTrue(doubled.accepted)
        self.assertGreater(doubled.duplicates_removed, 0)
        self.assertEqual(doubled.word_count, len(PASTE.split()))
        zero_width = attach.normalize_pasted_description("Data​ Engineer\r\n\r\n\r\n\r\nRole")[0]
        self.assertEqual(zero_width, "Data Engineer\n\nRole")

    def test_wrong_job_warning_warns_but_tolerates_common_genuine_cases(self):
        nursing = ("Responsibilities: provide patient care, administer medication, chart vitals, coordinate with "
                   "physicians and support families at Mercy Hospital. Requirements: active RN license. ") * 7
        self.assertEqual(attach.wrong_job_warning(nursing, "Senior Data Engineer", "Example Co"),
                         attach.WRONG_JOB_WARNING)
        checked = attach.validate_pasted_description(nursing, title="Senior Data Engineer", company="Example Co")
        self.assertTrue(checked.accepted)          # warning only; never blocks
        self.assertEqual(checked.warning, attach.WRONG_JOB_WARNING)
        no_company = PASTE.replace("Example Co", "our client")
        self.assertIsNone(attach.wrong_job_warning(no_company, "Senior Data Engineer", "Example Co"))
        self.assertIsNone(attach.wrong_job_warning(
            "Business intelligence engineering role building dashboards", "BI Engineer", "Acme"))
        self.assertIsNone(attach.wrong_job_warning("Acme Corp is hiring nurses", "Data Engineer", "Acme Corp"))


class Persistence(unittest.TestCase):
    def test_save_is_exact_record_atomic_and_records_provenance(self):
        def run():
            candidate()
            seed_glassdoor("target"); seed_glassdoor("sibling", posting="target-copy")
            sibling_matches = row("job_matches", "sibling")
            before = row("raw_jobs", "target")[0]
            outcome = save("target", PASTE, resume_hash=NEW)
            self.assertEqual(outcome.status, "saved")
            after = row("raw_jobs", "target")[0]
            self.assertEqual(after["description"], attach.normalize_pasted_description(PASTE)[0])
            self.assertIsNotNone(after["description_updated_at"])
            self.assertNotEqual(after["description_updated_at"], before["description_updated_at"])
            meta = row(attach.ATTACHMENT_TABLE, "target")[0]
            self.assertEqual((meta["provenance"], meta["status"], meta["has_previous"]),
                             ("user_pasted", "active", True))
            self.assertIsNone(meta["previous_description"])
            from src.verified_posting_identity import description_hash
            self.assertEqual(meta["description_hash"], description_hash(after["description"]))
            self.assertIsNotNone(meta["attached_at"])
            self.assertEqual(row("raw_jobs", "sibling")[0]["description"], None)      # never propagated
            self.assertEqual(row("job_matches", "sibling"), sibling_matches)          # only target refreshed
            self.assertTrue(row("job_matches", "target"))
            self.assertNotEqual([r["keyword_score"] for r in row("job_matches", "target")], [90])
            self.assertEqual(row("job_enrichment_attempts", "target"), [])           # no fabricated attempt
        with_temp_review_database(run)

    def test_identical_save_is_a_strict_no_op(self):
        def run():
            candidate(); seed_glassdoor("target")
            save("target", PASTE)
            snapshot = (row("raw_jobs", "target"), row(attach.ATTACHMENT_TABLE, "target"),
                        row("job_matches", "target"))
            outcome = save("target", "  " + PASTE.replace("\n", "\n\n") + "  ")
            self.assertEqual(outcome.status, "unchanged")
            self.assertEqual((row("raw_jobs", "target"), row(attach.ATTACHMENT_TABLE, "target"),
                              row("job_matches", "target")), snapshot)
        with_temp_review_database(run)

    def test_failure_mid_save_rolls_back_everything(self):
        def run():
            candidate(); seed_glassdoor("target", "Short partial text only.")
            snapshot = (row("raw_jobs", "target"), row("job_matches", "target"))
            with patch.object(attach, "refresh_rule_matches_for_record", side_effect=RuntimeError("injected")):
                with self.assertRaises(RuntimeError):
                    save("target", PASTE)
            self.assertEqual((row("raw_jobs", "target"), row("job_matches", "target")), snapshot)
            self.assertEqual(row(attach.ATTACHMENT_TABLE, "target"), [])
        with_temp_review_database(run)

    def test_one_level_undo_replace_and_restore_of_partial_original(self):
        def run():
            candidate(); seed_glassdoor("target", "Partial listing teaser for a data role.")
            save("target", PASTE)
            first = description("target")
            self.assertEqual(save("target", SECOND_PASTE).status, "saved")
            meta = row(attach.ATTACHMENT_TABLE, "target")[0]
            self.assertTrue(meta["previous_was_user_pasted"])
            self.assertEqual(meta["previous_description"], first)    # only one previous version kept
            self.assertEqual(undo("target").status, "undone")
            self.assertEqual(description("target"), first)
            meta = row(attach.ATTACHMENT_TABLE, "target")[0]
            self.assertEqual((meta["status"], meta["has_previous"]), ("active", False))
            self.assertEqual(undo("target").message, "Nothing to undo.")
            self.assertEqual(description("target"), first)
        with_temp_review_database(run)
        def run_original():
            candidate(); seed_glassdoor("target", "Partial listing teaser for a data role.")
            save("target", PASTE)
            self.assertEqual(undo("target").status, "undone")
            self.assertEqual(description("target"), "Partial listing teaser for a data role.")
            self.assertEqual(row(attach.ATTACHMENT_TABLE, "target"), [])
            self.assertEqual(enrichment_view("target")["description_state"], "PARTIAL_JD")
        with_temp_review_database(run_original)

    def test_service_refuses_scored_applied_removed_and_complete_scraped_jobs(self):
        def run():
            initialize_ai_tables(); profile(OLD); profile(NEW)
            seed_glassdoor("scored"); seed_score(score("scored", NEW))
            confirm_candidate()                           # migrates the score to a completed assessment
            seed_glassdoor("applied", status="applied"); seed_glassdoor("removed", status="removed")
            seed_glassdoor("scraped", OFFICIAL)
            self.assertIn("already scored", save("scored", PASTE, resume_hash=NEW).message)
            self.assertIn("Applied or removed", save("applied", PASTE).message)
            self.assertIn("Applied or removed", save("removed", PASTE).message)
            self.assertIn("already has a complete description", save("scraped", PASTE).message)
            for key in ("applied", "removed", "scraped"):
                self.assertEqual(row(attach.ATTACHMENT_TABLE, key), [])
        with_temp_review_database(run)


class OptionBAndScoring(unittest.TestCase):
    def test_rejected_job_scoreable_only_while_hash_matches_and_evidence_is_kept(self):
        def screening_description(key):
            rows = [j for j in repo.load_unscreened_job_eligibility_candidates(NEW, 100) if j["record_key"] == key]
            return rows[0]["description"] if rows else "absent"

        def run():
            candidate(); seed_glassdoor("rejected"); seed_attempt("rejected")
            # A trusted-source (LinkedIn) rejected job shows the screening loader's SQL masking.
            seed_glassdoor("li-rejected", posting="li-1"); seed_attempt("li-rejected")
            with database.get_connection() as c:
                c.execute("UPDATE raw_jobs SET source = 'linkedin', apply_url = "
                          "'https://www.linkedin.com/jobs/view/7700001/' WHERE record_key = 'li-rejected'")
            attempt_before = row("job_enrichment_attempts", "rejected")
            self.assertNotIn("rejected", queue_keys())
            self.assertEqual(count_queue(), 0)
            self.assertIsNone(screening_description("li-rejected"))          # masked before
            self.assertEqual(save("rejected", PASTE).status, "saved")
            self.assertEqual(save("li-rejected", PASTE).status, "saved")
            self.assertIn("rejected", queue_keys())
            self.assertEqual(count_queue(), 2)
            self.assertEqual(screening_description("li-rejected"), description("li-rejected"))  # unmasked
            # The pre-existing D2 loader defect still drops unverified aggregator jobs from
            # screening; that path is unused while SCREENING_MAX_PER_RUN=0.
            self.assertNotIn("rejected", unscreened_keys())
            view = enrichment_view("rejected")
            self.assertEqual((view["enrichment_status"], view["description_state"]), ("resolution_rejected", "FULL_JD"))
            self.assertTrue(is_assessment_eligible_job(view))
            from src.ai.resume_matcher import get_description_information
            self.assertTrue(get_description_information(view)[2])
            self.assertEqual(row("job_enrichment_attempts", "rejected"), attempt_before)   # evidence intact
            from src.actionability import trusted_key
            self.assertFalse(trusted_key(view))                                           # never verified
            # Hash mismatch (any later change of the text) removes the exception everywhere.
            with database.get_connection() as c:
                c.execute("UPDATE raw_jobs SET description = ? WHERE record_key IN ('rejected', 'li-rejected')",
                          [OFFICIAL])
            self.assertNotIn("rejected", queue_keys())
            self.assertEqual(count_queue(), 0)
            self.assertIsNone(screening_description("li-rejected"))          # masked again
            view = enrichment_view("rejected")
            self.assertEqual(view["description_state"], "ENRICHMENT_REJECTED")
            self.assertFalse(is_assessment_eligible_job(view))
        with_temp_review_database(run)

    def test_pasted_job_reaches_scoring_with_screening_disabled(self):
        def run():
            candidate(); seed_glassdoor("pasted")
            self.assertNotIn("pasted", queue_keys())
            save("pasted", PASTE)
            fakes = ProviderFakes()
            with screening_env(None):
                outcome = run_service(None, fakes)
            self.assertEqual((fakes.screened, outcome.screening_attempts), (0, 0))
            self.assertIn("pasted", fakes.saved)
            with database.get_connection() as c:
                self.assertEqual(c.execute("SELECT count(*) FROM resume_job_eligibility").fetchone()[0], 0)
        with_temp_review_database(run)

    def test_saved_exclusion_still_blocks_a_pasted_job(self):
        def run():
            candidate(); seed_glassdoor("excluded")
            save("excluded", PASTE)
            key = next(j["canonical_job_key"] for j in repo.load_candidate_jobs(NEW, None, 100, 0, reuse_any_model=True)
                       if j["record_key"] == "excluded")
            with database.get_connection() as c:
                c.execute("INSERT INTO resume_job_eligibility(resume_hash,canonical_job_key,record_key,decision,"
                          "confidence,reason,matched_resume_signals,missing_or_mismatched_signals,model_name,"
                          "prompt_version,screened_at) VALUES(?,?,'excluded','exclude','high','fixture','[]','[]',"
                          "'fixture','v2',current_timestamp)", [NEW, key])
            self.assertNotIn("excluded", queue_keys())
        with_temp_review_database(run)


class EnrichmentPrecedence(unittest.TestCase):
    def test_unverified_text_never_overwrites_a_paste_even_from_a_stale_dict(self):
        def run():
            candidate(); seed_glassdoor("target"); seed_attempt("target", status="blocked")
            stale = enrichment_view("target")                       # loaded before the paste
            save("target", PASTE)
            pasted = description("target")
            fresh = enrichment_view("target")
            longer = result(LONGER_SCRAPE)
            self.assertFalse(enrich_jobs.description_update_is_improvement(fresh, longer))
            # Immediate write path, stale dict: the SQL guard rechecks provenance.
            self.assertFalse(enrich_jobs.update_job_description(stale, longer))
            self.assertEqual(description("target"), pasted)
            # Deferred batch write path, stale dict.
            enrich_jobs.save_enrichment_attempt(job=stale, result=longer, status="enriched",
                                                description_update=longer)
            self.assertEqual(description("target"), pasted)
            attempt = row("job_enrichment_attempts", "target")[0]
            self.assertEqual((attempt["status"], attempt["error_message"]),
                             ("not_improved", attach.USER_PASTED_RETAINED))
            self.assertEqual(row(attach.ATTACHMENT_TABLE, "target")[0]["status"], "active")
        with_temp_review_database(run)

    def test_source_refetch_in_process_job_keeps_the_paste(self):
        def run():
            candidate(); seed_glassdoor("target"); seed_attempt("target", status="blocked")
            save("target", PASTE)
            job = enrichment_view("target")
            job.update(needs_description_enrichment=True, apply_url=GD + "target", source="glassdoor")
            accepted = SimpleNamespace(accepted=True, reason="identity accepted", confidence=1.0)
            with patch.object(enrich_jobs, "fetch_job_description", return_value=result(LONGER_SCRAPE)), \
                 patch.object(enrich_jobs, "validate_enrichment_identity", return_value=accepted), \
                 patch.object(enrich_jobs, "should_attempt_official_resolution", return_value=False):
                processed = enrich_jobs.process_enrichment_job(job, None, defer_description_write=True)
            self.assertIsNone(processed.pending_description)
            self.assertEqual((processed.stored_status, processed.stored_error),
                             ("not_improved", attach.USER_PASTED_RETAINED))
        with_temp_review_database(run)

    def test_verified_official_supersedes_regardless_of_length_and_keeps_the_paste(self):
        def run():
            candidate(); seed_glassdoor("target"); seed_attempt("target")
            save("target", PASTE)
            pasted = description("target")
            job = enrichment_view("target")
            official = result(OFFICIAL)
            self.assertLess(official.word_count, job["current_word_count"])
            self.assertTrue(enrich_jobs.description_update_is_improvement(job, official, trusted_official=True))
            enrich_jobs.save_enrichment_attempt(job=job, result=official, status="enriched",
                                                description_update=official, description_update_trusted=True)
            self.assertEqual(description("target"), OFFICIAL)
            meta = row(attach.ATTACHMENT_TABLE, "target")[0]
            self.assertEqual((meta["status"], meta["superseded_by"], meta["pasted_description"]),
                             ("superseded", "verified_official", pasted))      # paste kept, not lost
            outcome = undo("target")
            self.assertEqual(outcome.status, "rejected")                        # no Undo over official text
            self.assertEqual(description("target"), OFFICIAL)
            self.assertEqual(save("target", SECOND_PASTE).status, "rejected")   # form hidden; service refuses
            view = enrichment_view("target")
            self.assertFalse(view["manual_description_active"])                # paste exception ended
            # The verified attempt is now the latest attempt (existing INSERT OR REPLACE
            # semantics), so the job stands on its own verified evidence.
            self.assertEqual((view["enrichment_status"], view["description_state"]), ("enriched", "FULL_JD"))
            self.assertEqual(attach.attachment_form_mode(view), None)
            # Verified FULL_JD below quality cannot supersede.
            self.assertFalse(enrich_jobs.description_update_is_improvement(
                enrichment_view("target") | {"manual_description_active": True},
                result("Responsibilities: SQL."), trusted_official=True))
        with_temp_review_database(run)

    def test_verified_official_helper_requires_found_verified_and_accepted_identity(self):
        ok = SimpleNamespace(accepted=True)
        self.assertTrue(enrich_jobs.verified_official_description(SimpleNamespace(status="FOUND_VERIFIED"), ok))
        self.assertFalse(enrich_jobs.verified_official_description(SimpleNamespace(status="OFFICIAL_NOT_FOUND"), ok))
        self.assertFalse(enrich_jobs.verified_official_description(
            SimpleNamespace(status="FOUND_VERIFIED"), SimpleNamespace(accepted=False)))
        self.assertFalse(enrich_jobs.verified_official_description(None, ok))

    def test_employer_verification_scheduling_is_unchanged(self):
        def run():
            candidate()
            for key, source in (("gd", "glassdoor"), ("li", "linkedin")):
                seed_glassdoor(key); seed_attempt(key)
                with database.get_connection() as c:
                    c.execute("UPDATE raw_jobs SET source = ?, apply_url = ? WHERE record_key = ?",
                              [source, ("https://www.linkedin.com/jobs/view/12345/" if source == "linkedin"
                                        else GD + key), key])
                before = enrichment_view(key) | {"official_url_status": "OFFICIAL_NOT_FOUND"}
                save(key, PASTE)
                after = enrichment_view(key) | {"official_url_status": "OFFICIAL_NOT_FOUND"}
                for extra in ({}, {"requires_trusted_posting_key": True}):
                    self.assertEqual(enrich_jobs.needs_official_resolution(before | extra),
                                     enrich_jobs.needs_official_resolution(after | extra), (key, extra))
                self.assertFalse(enrich_jobs.needs_description_enrichment(after, 80, True))  # no pointless refetch
        with_temp_review_database(run)


class ScoreFreshness(unittest.TestCase):
    def test_ordinary_score_is_stale_after_verified_replacement_and_requeued(self):
        def run():
            initialize_ai_tables(); profile(OLD); profile(NEW)          # unmigrated: ordinary resume scores
            seed_glassdoor("target"); seed_attempt("target", status="blocked")
            save("target", PASTE)
            with database.get_connection() as c:
                c.execute("UPDATE raw_jobs SET description_updated_at = ? WHERE record_key = 'target'",
                          [datetime.now(timezone.utc) - timedelta(hours=2)])
            seed_score(dict(score("target", NEW), scored_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                            description_word_count=len(PASTE.split())))
            self.assertNotIn("target", queue_keys())
            display = next(j for j in repo.load_all_jobs(NEW, for_session_snapshot=True) if j["record_key"] == "target")
            self.assertTrue(repo.has_current_complete_ai_score(display))
            job = enrichment_view("target")
            with no_ai_or_network():
                self.assertTrue(enrich_jobs.update_job_description(job, result(OFFICIAL), trusted_official=True))
            self.assertIn("target", queue_keys())                         # refresh only via a scoring run
            display = next(j for j in repo.load_all_jobs(NEW, for_session_snapshot=True) if j["record_key"] == "target")
            self.assertFalse(repo.has_current_complete_ai_score(display))
        with_temp_review_database(run)

    def test_completed_assessment_semantics_kept_and_replacement_is_disclosed(self):
        def run():
            initialize_ai_tables(); profile(OLD); profile(NEW)
            seed_glassdoor("target"); seed_attempt("target", status="blocked")
            save("target", PASTE)
            seed_score(dict(score("target", NEW), scored_at=datetime.now(timezone.utc).isoformat()))
            confirm_candidate()                                            # migrates it to a completed assessment
            with database.get_connection() as c:
                self.assertIn("target", completed_record_keys(c, NEW))
            job = enrichment_view("target")
            with no_ai_or_network():
                enrich_jobs.update_job_description(job, result(OFFICIAL), trusted_official=True)
            with database.get_connection() as c:
                self.assertIn("target", completed_record_keys(c, NEW))     # historical semantics unchanged
            self.assertNotIn("target", queue_keys())
            display = next(j for j in repo.load_all_jobs(NEW, for_session_snapshot=True) if j["record_key"] == "target")
            self.assertTrue(attach.superseded_after_scoring(display))      # the card discloses it
            self.assertIsNone(attach.attachment_form_mode(display))
        with_temp_review_database(run)


class Visibility(unittest.TestCase):
    def job(self, **kw):
        return dict(dict(record_key="k", title="Senior Data Engineer", company_name="Example Co",
                         source="glassdoor", application_status="new", description=None,
                         raw_description_word_count=0, ai_score=None, enrichment_status=None), **kw)

    def test_form_visibility_matrix(self):
        partial = "Partial teaser."
        cases = {
            "attach": [self.job(), self.job(description=partial, raw_description_word_count=2),
                       self.job(enrichment_status="resolution_rejected")],
            "replace": [self.job(description=PASTE, raw_description_word_count=len(PASTE.split()),
                                 manual_description_active=True)],
            None: [self.job(description=PASTE, raw_description_word_count=len(PASTE.split())),
                   self.job(ai_score=80), self.job(candidate_assessment_id="a1", ai_score=80),
                   self.job(application_status="applied"), self.job(application_status="removed"),
                   self.job(description=OFFICIAL, raw_description_word_count=len(OFFICIAL.split()),
                            manual_description_status="superseded")],
        }
        for expected, jobs in cases.items():
            for job in jobs:
                with self.subTest(expected=expected, job=job):
                    self.assertEqual(attach.attachment_form_mode(job), expected)


def card_app(**overrides):
    """The real card with persistence callbacks recorded (pattern of test_streamlit_labels)."""
    from streamlit.testing.v1 import AppTest
    job = dict(record_key="gd-1", title="Senior Data Engineer", company_name="Example Co", location="Remote",
               source="glassdoor", apply_url=GD + "gd-1", ai_score=None, candidate_assessment_id=None,
               application_status="new", discovered_at="2026-11-20", title_classification="STRONG_MATCH",
               description=None, raw_description_word_count=0, description_state="NEEDS_ENRICHMENT",
               enrichment_status="blocked", has_current_complete_ai_assessment=False)
    job.update(overrides)
    source = Path("streamlit_app.py").read_text().split("\nst.set_page_config(", 1)[0]
    source += """
from src.description_attachment import AttachmentOutcome
def save_attached_description(record_key, text, resume_hash=None):
    st.session_state['save_call'] = (record_key, text)
    return AttachmentOutcome('saved', 'Description saved (test).', 150)
def undo_attached_description(record_key, resume_hash=None):
    st.session_state['undo_call'] = record_key
    return AttachmentOutcome('undone', 'Previous description restored.')
"""
    return AppTest.from_string(source + f"\nrender_job_listing({job!r})", default_timeout=30).run()


class StreamlitForm(unittest.TestCase):
    def test_form_shows_original_context_validates_saves_and_invalidates(self):
        app = card_app()
        assert not app.exception, app.exception
        self.assertIn("Attach description", [e.label for e in app.expander])
        self.assertTrue(any(b.proto.url == GD + "gd-1" for b in app.get("link_button")))
        self.assertTrue(any("Senior Data Engineer" in m.value and "Example Co" in m.value for m in app.markdown))
        self.assertTrue(any("Unverified posting" in c.value for c in app.caption))
        self.assertEqual(len(app.form_submit_button), 1)
        self.assertFalse(app.form_submit_button(key="attach-save-gd-1").disabled)

        # Submit the latest text and click once: no preliminary .run() to
        # commit the textarea value. Invalid input stays visible and is not saved.
        app.text_area(key="attach-text-gd-1").set_value("Too short.")
        app.form_submit_button(key="attach-save-gd-1").click().run()
        assert not app.exception, app.exception
        self.assertTrue(any("short or incomplete" in e.value for e in app.error))
        self.assertNotIn("save_call", app.session_state)
        self.assertEqual(app.text_area(key="attach-text-gd-1").value, "Too short.")

        # A single Save submit with the new full JD reaches the service.
        app.text_area(key="attach-text-gd-1").set_value(PASTE)
        app.form_submit_button(key="attach-save-gd-1").click().run()
        assert not app.exception, app.exception
        self.assertEqual(app.session_state["save_call"], ("gd-1", PASTE))
        self.assertTrue(app.session_state["jobs_snapshot_dirty"])   # Jobs + backlog snapshots invalidated
        self.assertTrue(any("Description saved" in s.value for s in app.success))

    def test_empty_submit_shows_error_without_calling_save(self):
        app = card_app()
        app.form_submit_button(key="attach-save-gd-1").click().run()
        assert not app.exception, app.exception
        self.assertTrue(any("Paste a job description first." in e.value for e in app.error))
        self.assertNotIn("save_call", app.session_state)

    def test_wrong_job_warning_is_shown_but_does_not_block_one_click_save(self):
        app = card_app()
        nursing = ("Responsibilities: provide patient care, administer medication, chart vitals and coordinate "
                   "with physicians at Mercy Hospital. Requirements: active RN license. ") * 7
        app.text_area(key="attach-text-gd-1").set_value(nursing)
        app.form_submit_button(key="attach-save-gd-1").click().run()
        assert not app.exception, app.exception
        self.assertEqual(app.session_state["save_call"], ("gd-1", nursing))
        self.assertTrue(any(attach.WRONG_JOB_WARNING in w.value for w in app.warning))

    def test_replace_and_undo_for_own_paste_and_hidden_states(self):
        app = card_app(description=PASTE, raw_description_word_count=len(PASTE.split()),
                       description_state="FULL_JD", manual_description_active=True,
                       manual_description_has_previous=True, manual_description_status="active")
        self.assertIn("Replace pasted description", [e.label for e in app.expander])
        self.assertTrue(any("user pasted (unverified)" in c.value for c in app.caption))
        app.text_area(key="attach-text-gd-1").set_value(SECOND_PASTE)
        app.form_submit_button(key="attach-save-gd-1").click().run()
        assert not app.exception, app.exception
        self.assertEqual(app.session_state["save_call"], ("gd-1", SECOND_PASTE))
        app.button(key="attach-undo-gd-1").click().run()
        self.assertEqual(app.session_state["undo_call"], "gd-1")
        for overrides in (dict(ai_score=82, candidate_assessment_id="a1", recommendation="apply"),
                          dict(application_status="applied"), dict(application_status="removed"),
                          dict(description=OFFICIAL, raw_description_word_count=len(OFFICIAL.split()),
                               description_state="FULL_JD")):
            with self.subTest(overrides=overrides):
                labels = [e.label for e in card_app(**overrides).expander]
                self.assertNotIn("Attach description", labels)
                self.assertNotIn("Replace pasted description", labels)

if __name__ == "__main__":
    unittest.main()
