"""Offline regression coverage for the October 20 scoring queue audit."""
from contextlib import contextmanager, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch
import io
import json
import unittest

from src import candidate_assessments as store, database, job_screening, score_backlog as backlog
from src.ai import resume_matcher as matcher
from src.ai.job_eligibility import JobEligibilityAnalysis, JobEligibilityDecision, ELIGIBILITY_PROMPT_VERSION
from src.repositories import recommendation_repository as repo
from src.score_jobs_ai import initialize_ai_tables
from src.test_candidate_assessments import OLD, NEW, JD, profile, score, seed_score
from src.test_recommendation_repository import with_temp_review_database, seed_review_job, seed_verified_attempt
from src.candidate_migration import make_preview, apply_preview


def seed_linked(key, posting, *, verified=None, state=None):
    seed_review_job(key, 'Senior Data Engineer', source='linkedin', description=JD,
                    apply_url=f'https://www.linkedin.com/jobs/view/{posting}/', application_status=state)
    with database.get_connection() as c:
        c.execute('UPDATE raw_jobs SET source_job_id=? WHERE record_key=?',[posting,key])
        c.execute("INSERT INTO job_matches(record_key,search_id,search_title,match_score,title_score,location_score,keyword_score,freshness_score,matched_keywords,excluded_keywords,reasons,is_recommended,needs_review) VALUES(?,'search','Data Engineer',90,90,90,90,90,'[]','[]','[]',true,false)",[key])
    if verified:
        seed_verified_attempt(key,'greenhouse:'+verified,description=JD,
            official_job_url='https://boards.greenhouse.io/example/jobs/'+verified)


def setup_alias(posting='4471830589', *, completed=True):
    initialize_ai_tables();profile(OLD);profile(NEW)
    seed_linked('verified',posting,verified='123')
    seed_linked('sibling',posting)
    if completed:seed_score(score('verified'))
    with database.get_connection() as c:
        preview=make_preview(c,'Fixture','Candidate',None,[OLD,NEW])
        c.execute('BEGIN');apply_preview(c,preview);c.execute('COMMIT')
    return preview


def fake_match():
    return SimpleNamespace(overall_score=85,recommendation='apply',analysis=SimpleNamespace(confidence='high'))


def mocked_backlog(jobs, *, screening=None, gemini=None, openai=None):
    replacements=dict(initialize_ai_tables=Mock(),get_model_name=Mock(return_value='fixture-model'),
        load_cached_profile=Mock(return_value=object()),load_cached_resume_text=Mock(return_value='synthetic SQL resume'),
        count_cached_canonical_scores=Mock(return_value=0),
        screen_unscreened_jobs=Mock(return_value=screening or SimpleNamespace(screened=0)),
        load_candidate_jobs=Mock(return_value=jobs),score_resume_against_job=gemini or Mock(return_value=fake_match()),
        score_resume_against_job_openai=openai or Mock(return_value=fake_match()),save_job_score=Mock(),run_dbt_build=Mock())
    output=io.StringIO()
    with patch.multiple(backlog,**replacements),redirect_stdout(output):
        summary=backlog.score_backlog('unregistered-fixture',100,0)
    return summary,output.getvalue(),replacements


class AliasGuards(unittest.TestCase):
    def test_perfectserve_and_lyra_exact_siblings_reuse_verified_completion(self):
        for posting in ['4471830589','4463184352']:
            def run():
                p=setup_alias(posting)
                with database.get_connection() as c:
                    self.assertEqual(store.completed_record_keys(c,NEW),{'verified','sibling'})
                    before=c.execute('SELECT * FROM candidate_active_assessments').fetchall()
                    shown=[{'record_key':'sibling'}];store.overlay_saved_scores(c,NEW,shown)
                    self.assertEqual(shown[0]['ai_score'],95)
                self.assertEqual(repo.load_candidate_jobs(NEW,None,10,0,reuse_any_model=True),[])
                with store.scoring_claim(NEW,{'record_key':'sibling'}) as acquired:self.assertFalse(acquired)
                with database.get_connection() as c:
                    self.assertEqual(c.execute('SELECT * FROM candidate_active_assessments').fetchall(),before)
                    self.assertEqual(c.execute('SELECT count(*) FROM candidate_assessments').fetchone()[0],1)
            with self.subTest(posting=posting):with_temp_review_database(run)

    def test_existing_accidental_duplicate_is_not_deleted_or_promoted(self):
        def run():
            p=setup_alias()
            duplicate={**score('sibling',NEW,88),'_exact_posting_identity':'exact:linkedin|4471830589'}
            with database.get_connection() as c:
                c.execute("INSERT INTO candidate_assessments VALUES('duplicate',?,?,'sibling',?,?,current_timestamp,'automatic',NULL)",[p['candidate']['candidate_id'],'exact:linkedin|4471830589',NEW,json.dumps(duplicate)])
                c.execute("INSERT INTO candidate_active_assessments VALUES(?,?,'duplicate')",[p['candidate']['candidate_id'],'exact:linkedin|4471830589'])
                before=c.execute('SELECT * FROM candidate_active_assessments ORDER BY job_identity').fetchall()
                self.assertEqual(store.assessment_lookup(c,p['candidate']['candidate_id'])['exact:linkedin|4471830589']['score']['overall_score'],95)
            with store.scoring_claim(NEW,{'record_key':'sibling'}) as acquired:self.assertFalse(acquired)
            with database.get_connection() as c:
                self.assertEqual(c.execute('SELECT * FROM candidate_active_assessments ORDER BY job_identity').fetchall(),before)
                self.assertEqual(c.execute('SELECT count(*) FROM candidate_assessments').fetchone()[0],2)
        with_temp_review_database(run)

    def test_conflicting_verified_requisition_and_different_posting_do_not_borrow(self):
        def run():
            setup_alias()
            seed_linked('conflict','4471830589',verified='456')
            seed_linked('other','999999')
            with store.scoring_claim(NEW,{'record_key':'sibling'}) as acquired:self.assertFalse(acquired)
            for key in ['conflict','other']:
                with store.scoring_claim(NEW,{'record_key':key}) as acquired:self.assertTrue(acquired)
            with database.get_connection() as c:
                self.assertNotIn('conflict',store.completed_record_keys(c,NEW))
                self.assertNotIn('other',store.completed_record_keys(c,NEW))
        with_temp_review_database(run)

    def test_concurrent_verified_and_exact_aliases_use_one_claim(self):
        def run():
            setup_alias(completed=False)
            ready=Event();release=Event()
            def worker():
                with store.scoring_claim(NEW,{'record_key':'sibling'}) as acquired:
                    ready.set();release.wait(10);return acquired
            with ThreadPoolExecutor(max_workers=1) as pool:
                first=pool.submit(worker);self.assertTrue(ready.wait(10))
                try:
                    with store.scoring_claim(NEW,{'record_key':'verified'}) as acquired:self.assertFalse(acquired)
                finally:release.set()
                self.assertTrue(first.result())
        with_temp_review_database(run)

    def test_completion_between_lookup_and_claim_insert_is_rechecked(self):
        def run():
            p=setup_alias()
            with database.get_connection() as c:
                completed=store.assessment_lookup(c,p['candidate']['candidate_id'])
            with patch.object(store,'assessment_lookup',side_effect=[{},completed]):
                details={}
                with store.scoring_claim(NEW,{'record_key':'sibling'},details=details) as acquired:
                    self.assertFalse(acquired)
                self.assertEqual(details['reason'],'candidate_already_completed')
            with database.get_connection() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM candidate_scoring_claims').fetchone()[0],0)
                self.assertEqual(c.execute('SELECT count(*) FROM candidate_assessments').fetchone()[0],1)
        with_temp_review_database(run)

    def test_explicit_manual_rescore_replaces_exact_pointer_after_alias_verification(self):
        def run():
            initialize_ai_tables();profile(OLD);profile(NEW)
            seed_linked('original','777777');seed_score(score('original'))
            with database.get_connection() as c:
                p=make_preview(c,'Fixture','Candidate',None,[OLD,NEW])
                c.execute('BEGIN');apply_preview(c,p);c.execute('COMMIT')
            seed_linked('verified-later','777777',verified='123')
            with store.scoring_claim(NEW,{'record_key':'original'},manual=True) as acquired:
                self.assertTrue(acquired)
                with database.get_connection() as c:
                    c.execute('BEGIN')
                    store.persist_score(c,NEW,score('original',NEW,88))
                    c.execute('COMMIT')
            with database.get_connection() as c:
                self.assertEqual(c.execute('SELECT job_identity FROM candidate_active_assessments').fetchall(),[('verified:greenhouse:123',)])
                self.assertEqual(c.execute('SELECT count(*) FROM candidate_assessments').fetchone()[0],2)
                self.assertEqual(c.execute('SELECT overall_score FROM resume_job_scores').fetchone()[0],95)
        with_temp_review_database(run)

    def test_automatic_backlog_makes_no_provider_call_for_completed_alias(self):
        def run():
            setup_alias()
            jobs=[dict(record_key='sibling',title='Senior Data Engineer',company_name='Fixture')]
            with patch.object(backlog,'screen_unscreened_jobs',return_value=SimpleNamespace(screened=0)),patch.object(backlog,'load_candidate_jobs',return_value=jobs),patch.object(backlog,'score_resume_against_job') as gemini,patch.object(backlog,'score_resume_against_job_openai') as openai,patch.object(backlog,'run_dbt_build'),patch.object(backlog,'get_model_name',return_value='test'),redirect_stdout(io.StringIO()) as output:
                result=backlog.score_backlog(NEW,10,0)
            gemini.assert_not_called();openai.assert_not_called()
            self.assertEqual(result.jobs_skipped,1)
            self.assertIn('candidate_already_completed',output.getvalue())
            self.assertIn('Position: 1/1',output.getvalue())
            self.assertIn('Record key: sibling',output.getvalue())
            self.assertIn('verified:greenhouse:123',output.getvalue())
        with_temp_review_database(run)


class StatusGuards(unittest.TestCase):
    def test_applied_removed_inheritance_and_exact_new_precedence_match_display(self):
        for state in ['applied','removed']:
            def run():
                setup_alias(completed=False)
                with database.get_connection() as c:
                    c.execute("INSERT INTO application_status(record_key,status) VALUES('verified',?)",[state])
                seed_linked('same-verified','999001',verified='123')
                seed_linked('different','999002',verified='456')
                with database.get_connection() as c:
                    self.assertEqual(repo.automatic_statuses(c)['same-verified'],state)
                selected=repo.load_candidate_jobs(NEW,None,100,0,reuse_any_model=True)
                self.assertEqual({j['record_key'] for j in selected},{'different'})
                self.assertEqual(repo.count_unscored_candidate_jobs(NEW,None,0,reuse_any_model=True),1)
                self.assertEqual({j['record_key'] for j in repo.load_unscreened_job_eligibility_candidates(NEW,100)},{'different'})
                with store.scoring_claim(NEW,{'record_key':'same-verified'}) as acquired:self.assertFalse(acquired)
                with database.get_connection() as c:
                    c.execute("INSERT INTO application_status(record_key,status) VALUES('same-verified','new')")
                    self.assertEqual(repo.automatic_statuses(c)['same-verified'],'new')
                self.assertIn('same-verified',{j['record_key'] for j in repo.load_candidate_jobs(NEW,None,100,0,reuse_any_model=True)})
            with self.subTest(state=state):with_temp_review_database(run)

    def test_untrusted_verified_status_does_not_propagate(self):
        def run():
            setup_alias(completed=False)
            with database.get_connection() as c:
                c.execute("INSERT INTO application_status(record_key,status) VALUES('verified','removed')")
            seed_linked('untrusted','999001',verified='123')
            with database.get_connection() as c:
                c.execute("UPDATE job_enrichment_attempts SET verified_posting_key_trust='untrusted' WHERE record_key='untrusted'")
                self.assertEqual(repo.automatic_statuses(c)['untrusted'],'new')
        with_temp_review_database(run)

    def test_equal_queue_ranks_are_ordered_by_record_key(self):
        def run():
            initialize_ai_tables()
            for key in ['z','a','m']:seed_linked(key,'id-'+key)
            self.assertEqual([j['record_key'] for j in repo.load_candidate_jobs('unregistered',None,100,0,reuse_any_model=True)],['a','m','z'])
        with_temp_review_database(run)


class ScreeningAndProviderGuards(unittest.TestCase):
    def test_failed_screen_is_logged_held_for_this_run_and_retried_later(self):
        def run():
            initialize_ai_tables()
            jobs=[dict(record_key=k,canonical_job_key=k,title='Data Engineer',company_name='Fixture',description=JD) for k in ['failed','eligible','excluded']]
            def decision(job,kind):
                return JobEligibilityDecision(record_key=job['record_key'],canonical_job_key=job['canonical_job_key'],resume_hash='r',model_name='mock',prompt_version=ELIGIBILITY_PROMPT_VERSION,analysis=JobEligibilityAnalysis(decision=kind,confidence='high',reason='fixture'))
            err=matcher.ResumeMatcherError('request payload: private resume and secret')
            with patch.object(job_screening,'load_unscreened_job_eligibility_candidates',return_value=jobs),patch.object(job_screening,'evaluate_job_eligibility',side_effect=[err,decision(jobs[1],'eligible'),decision(jobs[2],'exclude')]),patch.object(job_screening,'save_job_eligibility_decision') as save,redirect_stdout(io.StringIO()) as log:
                summary=job_screening.screen_unscreened_jobs('r',object(),3,'mock')
            self.assertEqual(summary.failed_record_keys,('failed',))
            self.assertEqual((summary.failed,summary.eligible,summary.excluded),(1,1,1))
            self.assertEqual(save.call_count,2)
            self.assertIn('Record key: failed',log.getvalue());self.assertNotIn('private resume',log.getvalue())
            fresh=dict(record_key='genuinely-unscreened',canonical_job_key='genuinely-unscreened',title='Data Engineer',company_name='Fixture')
            result,text,mocks=mocked_backlog([jobs[0],fresh],screening=summary)
            self.assertEqual((result.scores_saved,result.jobs_skipped,result.screening_failures),(1,1,1))
            self.assertEqual(mocks['score_resume_against_job'].call_args.kwargs['job']['record_key'],'genuinely-unscreened')
            self.assertIn('screening_failed_this_execution',text)
            result,_,mocks=mocked_backlog([jobs[0]])
            self.assertEqual(result.scores_saved,1)  # A later, controlled run may retry.
        with_temp_review_database(run)

    def test_exhausted_503_falls_back_only_after_three_gemini_attempts(self):
        class ServiceUnavailable(Exception):
            code=503
        def run():
            initialize_ai_tables()
            client=SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=ServiceUnavailable('503 UNAVAILABLE'))))
            gemini=Mock(side_effect=lambda **kw: matcher._request_gemini_score(client,'mock','synthetic prompt'))
            with patch.object(matcher.time,'sleep') as sleep:
                result,text,mocks=mocked_backlog([dict(record_key='job',title='Data Engineer')],gemini=gemini)
            self.assertEqual(client.models.generate_content.call_count,3)
            self.assertEqual(sleep.call_count,2)
            mocks['score_resume_against_job_openai'].assert_called_once()
            self.assertEqual((result.openai_scores_saved,result.jobs_failed),(1,0))
            self.assertFalse(result.quota_exhausted)
            self.assertIn('Provider: Gemini',text)
        with_temp_review_database(run)

    def test_transient_503_that_recovers_does_not_switch_provider(self):
        def run():
            initialize_ai_tables()
            client=SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=[RuntimeError('503'),RuntimeError('503'),fake_match()])))
            with patch.object(matcher.time,'sleep'):
                result,_,mocks=mocked_backlog([dict(record_key='job')],gemini=Mock(side_effect=lambda **kw:matcher._request_gemini_score(client,'mock','fixture')))
            self.assertEqual(result.gemini_scores_saved,1)
            mocks['score_resume_against_job_openai'].assert_not_called()
        with_temp_review_database(run)

    def test_validation_and_policy_errors_do_not_trigger_503_fallback(self):
        for message in ['Invalid score: 503','Response preview: ServerError: 503 UNAVAILABLE','A current FULL_JD is required','OpenAI API error: HTTP 503 service unavailable']:
            self.assertFalse(backlog.should_switch_to_openai(matcher.ResumeMatcherError(message)))

    def test_upload_provider_stop_accounts_for_unattempted_positions(self):
        from src.ui import job_recommendation_service as service
        def run():
            initialize_ai_tables()
            prepared=SimpleNamespace(resume=SimpleNamespace(resume_hash='fixture',filename='fixture.docx',text='synthetic resume',word_count=100,page_count=1),profile=object(),model_name='fixture',profile_was_cached=True)
            jobs=[dict(record_key='job-'+str(i),title='Data Engineer',canonical_job_key='exact-'+str(i)) for i in range(3)]
            replacements=dict(prepare_resume_upload=Mock(return_value=prepared),start_scoring_run=Mock(return_value='run'),finish_scoring_run=Mock(),count_cached_canonical_scores=Mock(return_value=0),screen_unscreened_jobs=Mock(return_value=SimpleNamespace(screened=0)),load_candidate_jobs=Mock(return_value=jobs),score_resume_against_job=Mock(side_effect=matcher.ResumeMatcherError('429 RESOURCE_EXHAUSTED GenerateRequestsPerDay quota exceeded')),score_resume_against_job_openai=Mock(side_effect=matcher.ResumeMatcherError('OpenAI insufficient_quota')),run_dbt_build=Mock())
            with patch.multiple(service,**replacements),redirect_stdout(io.StringIO()):
                summary=service.process_resume_upload(object(),3,0)
            self.assertEqual((summary.candidate_count,summary.failed_score_count,summary.jobs_skipped),(3,1,2))
            text='\n'.join(summary.log_lines)
            self.assertIn('Position: 2/3',text);self.assertIn('Position: 3/3',text)
            self.assertIn('not_attempted_provider_stop',text)
        with_temp_review_database(run)

    def test_81_positions_reconcile_and_skip_logs_have_identity(self):
        def run():
            initialize_ai_tables()
            jobs=[dict(record_key='job-'+str(i),canonical_job_key='exact-'+str(i),title='Data Engineer') for i in range(1,82)]
            skips={18,19,23,24,54}
            @contextmanager
            def claim(h,job,**kw):
                i=int(job['record_key'].split('-')[-1]);kw['details'].update(reason='candidate_already_completed',identity='verified:greenhouse:'+str(i),exact_identity=job['canonical_job_key'])
                yield i not in skips
            def provider(**kw):
                if kw['job']['record_key'] in {'job-2','job-63'}:raise matcher.ResumeMatcherError('fixture validation failure')
                return fake_match()
            with patch.object(store,'scoring_claim',claim):result,text,_=mocked_backlog(jobs,gemini=Mock(side_effect=provider))
            self.assertEqual((result.candidates_selected,result.scores_saved,result.jobs_failed,result.jobs_skipped),(81,74,2,5))
            for i in skips:
                self.assertIn(f'Position: {i}/81',text);self.assertIn('Record key: job-'+str(i),text)
                self.assertIn('Canonical identity: verified:greenhouse:'+str(i),text)
            self.assertIn('selected=81; saved=74; failed=2; skipped=5',text)
        with_temp_review_database(run)


if __name__=='__main__':unittest.main()
