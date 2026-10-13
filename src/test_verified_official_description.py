"""Offline regression coverage for known verified posting retrieval."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch
import unittest

from src import enrich_jobs as e
from src.enrichment.job_description import JobDescriptionResult, count_words
from src.enrichment import official_job_resolver as resolver
from src.verified_posting_identity import verified_posting_key_from_url, description_hash
from src.repositories.recommendation_repository import description_state

URL = 'https://jobs.ashbyhq.com/deepgram/7992391d-a890-41f7-85c8-849e0b878981'
TEXT = ('Responsibilities include SQL analytics engineering, semantic models, data pipelines '
        'and stakeholder partnership. Requirements include Python and dbt experience. ') * 20

def job():
    return dict(record_key='deepgram-test', title='Senior Analytics Engineer - Decision Intelligence',
        company_name='Deepgram', source='linkedin', apply_url='https://linkedin.com/jobs/view/123',
        location='Remote', description='', current_word_count=0, description_word_count=0,
        needs_description_enrichment=True, official_url_status='FOUND_VERIFIED',
        official_job_url=URL, verified_posting_key=verified_posting_key_from_url(URL),
        verified_posting_key_source='official_found_verified', verified_posting_key_trust='trusted',
        official_url_source='ashby', official_url_confidence=1.0)

def result(**changes):
    return replace(JobDescriptionResult(requested_url=URL, final_url=URL, status='enriched',
        http_status=200, extraction_method='jobposting_json_ld', description=TEXT,
        word_count=count_words(TEXT), error_message=None,
        resolved_title=job()['title'], resolved_company='Deepgram', resolved_location='Remote - US',
        resolved_title_source='jobposting_json_ld', resolved_company_source='jobposting_json_ld',
        resolved_location_source='jobposting_json_ld'), **changes)

class VerifiedDescriptionTests(unittest.TestCase):
    def run_job(self, j, static, dynamic=None, updated=True):
        with patch.object(resolver, 'fetch_job_description', return_value=static) as fetch, \
             patch.object(resolver, 'fetch_dynamic_job_description', return_value=dynamic) as browser, \
             patch.object(e, 'fetch_job_description', side_effect=AssertionError('source fetch')), \
             patch.object(e, 'resolve_official_job', side_effect=AssertionError('discovery')), \
             patch.object(e, 'update_job_description', return_value=updated) as save:
            outcome = e.process_enrichment_job(j, object())
            return outcome, fetch, browser, save

    def test_static_direct_no_discovery_and_hash(self):
        j=job(); outcome, fetch, browser, save=self.run_job(j, result())
        self.assertTrue(outcome.updated); fetch.assert_called_once(); browser.assert_not_called()
        self.assertEqual(fetch.call_args.args[0], URL)
        save.assert_called_once(); self.assertEqual(j['description_hash'], description_hash(TEXT))
        self.assertEqual(j['description_source'], 'verified_official_direct')
        self.assertTrue(outcome.identity_validation.accepted)

    def test_dynamic_ashby(self):
        outcome, _, browser, save=self.run_job(job(), result(status='no_description', description='', word_count=0), result())
        browser.assert_called_once_with(URL); self.assertTrue(outcome.updated); save.assert_called_once()

    def test_dynamic_failure(self):
        outcome, _, browser, save=self.run_job(job(), result(status='no_description', description='', word_count=0))
        browser.assert_called_once(); self.assertFalse(outcome.updated); save.assert_not_called()
        self.assertEqual(outcome.stored_status, 'no_description')

    def test_wrong_fetched_company(self):
        outcome, _, _, save=self.run_job(job(), result(resolved_company='Other Employer'))
        self.assertFalse(outcome.identity_validation.accepted); save.assert_not_called()
        self.assertEqual(outcome.official_resolution.status, 'INSUFFICIENT_IDENTITY')

    def test_redirect_key_conflict(self):
        outcome, _, _, save=self.run_job(job(), result(final_url=URL.replace('7992391d','9992391d')))
        self.assertFalse(outcome.identity_validation.accepted); save.assert_not_called()
        self.assertEqual(outcome.identity_validation.reason, 'trusted exact posting key conflict')

    def test_historical_third_party_not_fetched(self):
        j=job(); j['official_job_url']='https://www.builtinsf.com/job/senior-analytics-engineer/7239437'
        outcome, fetch, _, save=self.run_job(j, result())
        fetch.assert_not_called(); save.assert_not_called(); self.assertFalse(outcome.updated)

    def test_stored_key_conflict_not_fetched(self):
        j=job(); j['verified_posting_key']='greenhouse:other:123'
        outcome, fetch, _, save=self.run_job(j, result())
        fetch.assert_not_called(); save.assert_not_called()

    def test_untrusted_provenance_not_fetched(self):
        j=job(); j['verified_posting_key_trust']='untrusted'
        outcome, fetch, _, save=self.run_job(j, result())
        fetch.assert_not_called(); save.assert_not_called()

    def test_not_improved_preserves_hash(self):
        j=job(); j['description_hash']='old'
        outcome, _, _, save=self.run_job(j, result(), updated=False)
        self.assertFalse(outcome.updated); self.assertEqual(j['description_hash'],'old')

    def test_read_only_does_not_save(self):
        with patch.object(e,'fetch_candidate_description',return_value=result()), patch.object(e,'update_job_description') as save:
            outcome=e.fetch_verified_official_description(job(),object(),persist=False)
            save.assert_not_called(); self.assertTrue(outcome.identity_validation.accepted)

    def test_short_quality_failure_not_saved(self):
        outcome, _, _, save=self.run_job(job(), result(description='Responsibilities SQL',word_count=2))
        save.assert_not_called(); self.assertEqual(outcome.stored_status,'no_description')

    def test_real_persistence_and_full_jd_transition(self):
        from src.test_enrich_jobs_official_retry import with_temp_database, insert_raw_job
        from src import database
        def scenario():
            j=job()
            insert_raw_job(j['record_key'], source=j['source'], title=j['title'],
                           company_name=j['company_name'], apply_url=j['apply_url'], description='')
            self.assertNotEqual(description_state(j), 'FULL_JD')
            with patch.object(e, 'fetch_candidate_description', return_value=result()), \
                 patch.object(e, 'resolve_official_job', side_effect=AssertionError('discovery')):
                outcome=e.process_enrichment_job(j, object())
            e.save_enrichment_attempt(j, outcome.result, outcome.stored_status,
                outcome.stored_error, outcome.identity_validation, outcome.official_resolution)
            with database.get_connection() as c:
                desc, updated=c.execute('select description, description_updated_at from raw_jobs where record_key=?', [j['record_key']]).fetchone()
                saved=c.execute('select description_hash, description_source, status, verified_posting_key from job_enrichment_attempts where record_key=?', [j['record_key']]).fetchone()
            self.assertEqual(desc, TEXT); self.assertIsNotNone(updated)
            self.assertEqual(saved, (description_hash(TEXT), 'verified_official_direct','enriched',j['verified_posting_key']))
            j.update(description=desc, description_word_count=count_words(desc), enrichment_status=saved[2])
            self.assertEqual(description_state(j), 'FULL_JD')
        with_temp_database(scenario)

    def test_full_jd_is_not_refetched_or_overwritten(self):
        j=job(); j.update(description=TEXT, description_word_count=count_words(TEXT), current_word_count=count_words(TEXT))
        with patch.object(e,'fetch_job_description',side_effect=AssertionError('source')), \
             patch.object(e,'fetch_candidate_description',side_effect=AssertionError('official')), \
             patch.object(e,'update_job_description',side_effect=AssertionError('overwrite')):
            outcome=e.process_enrichment_job(j,object())
        self.assertFalse(outcome.updated)

    def test_incomplete_previously_enriched_is_eligible(self):
        j=job(); j.update(description_state='PARTIAL_JD',previous_status='enriched')
        self.assertTrue(e.needs_description_enrichment(j,80,False))
        j['previous_status']='blocked'
        self.assertFalse(e.needs_description_enrichment(j,80,False))
        self.assertTrue(e.needs_description_enrichment(j,80,True))

    def test_ashby_uuid_keys_are_exact_and_board_root_is_not_posting(self):
        self.assertEqual(verified_posting_key_from_url(URL), 'ashby:7992391d-a890-41f7-85c8-849e0b878981')
        self.assertEqual(verified_posting_key_from_url('https://jobs.ashbyhq.com/deepgram'), '')
        self.assertFalse(verified_posting_key_from_url(URL.replace('jobs.ashbyhq.com','fakeashbyhq.com')).startswith('ashby:'))

    def test_long_partial_replaced_by_shorter_verified_full_jd(self):
        from src.test_enrich_jobs_official_retry import with_temp_database, insert_raw_job
        from src import database
        def scenario():
            j=job(); j.update(description='page chrome ' * 500, current_word_count=1000, description_word_count=1000)
            insert_raw_job(j['record_key'], description=j['description'])
            with patch.object(e,'fetch_candidate_description',return_value=result()):
                outcome=e.process_enrichment_job(j,object())
            self.assertTrue(outcome.updated)
            with database.get_connection() as c:
                self.assertEqual(c.execute('select description from raw_jobs where record_key=?',[j['record_key']]).fetchone()[0],TEXT)
        with_temp_database(scenario)

    def test_missing_company_cannot_use_discovery_fields(self):
        j=job(); j.update(company='Deepgram')
        outcome, _, _, save=self.run_job(j,result(resolved_company=None))
        self.assertFalse(outcome.identity_validation.accepted); save.assert_not_called()

    def test_long_incomplete_static_uses_existing_browser(self):
        shell=result(description='page chrome ' * 200,word_count=400)
        outcome, _, browser, save=self.run_job(job(),shell,result())
        browser.assert_called_once_with(URL); save.assert_called_once()
        self.assertTrue(outcome.updated)

    def test_deepgram_quality_recovery_end_to_end(self):
        from src.test_enrich_jobs_official_retry import with_temp_database, insert_raw_job
        from src import database
        # No legacy requirements/experience phrase is needed for acceptance.
        text="What you'll work on\n" + ' '.join(('Design governed models and build reliable metrics for business teams. ' * 100).split()[:806])
        fetched=result(description=text,word_count=810)
        def scenario():
            j=job()
            insert_raw_job(j['record_key'],source=j['source'],title=j['title'],company_name=j['company_name'],apply_url=j['apply_url'],description='alert preview')
            with patch.object(resolver,'fetch_job_description',return_value=fetched) as http, \
                 patch.object(resolver,'fetch_dynamic_job_description') as browser, \
                 patch.object(e,'resolve_official_job',side_effect=AssertionError('public search')), \
                 patch.object(e,'fetch_job_description',side_effect=AssertionError('source fetch')):
                outcome=e.process_enrichment_job(j,object())
            self.assertTrue(outcome.identity_validation.accepted)
            self.assertTrue(outcome.updated); browser.assert_not_called(); http.assert_called_once()
            e.save_enrichment_attempt(j,outcome.result,outcome.stored_status,outcome.stored_error,outcome.identity_validation,outcome.official_resolution)
            with database.get_connection() as c:
                saved=c.execute('select description from raw_jobs where record_key=?',[j['record_key']]).fetchone()[0]
            j.update(description=saved,description_word_count=810,enrichment_status='enriched')
            self.assertEqual(description_state(j),'FULL_JD')
            from src.description_quality import assess_description_quality
            self.assertTrue(assess_description_quality(saved).full_jd)
        with_temp_database(scenario)

if __name__ == '__main__':
    unittest.main()
