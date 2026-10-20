"""Offline candidate identity, immutable migration, score-once and UI regressions."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from src import database
from src import candidate_assessments as store
from src.candidate_migration import make_preview, apply_preview, snapshot
from src.repositories import recommendation_repository as repo
from src.score_jobs_ai import initialize_ai_tables, save_job_score
from src.test_resume_matcher_validation import make_profile
from src.ai.resume_matcher import FitScore,fit_score_to_job_match_analysis,build_resume_job_match
from src.test_recommendation_repository import with_temp_review_database,seed_review_job,seed_verified_attempt
from src.ui import jobs_snapshot

OLD='119cff52d00396c30652896f4bba20460a9560bb5950732d9b823a3721794676'
NEW='cf1027efc9b602ee35e798dcf66b53fdcdd321fa2b85f92961c2ea74a902c0f4'
JD=('Responsibilities: Build Python SQL data pipelines and Snowflake warehouses. '
    'Requirements: Data modeling, testing, documentation and collaboration. ')*12
BASE=datetime(2026,10,19,tzinfo=timezone.utc)


def profile(h,filename='same.pdf'):
    with database.get_connection() as c:
        c.execute('INSERT INTO resume_profiles(resume_hash,filename,model_name,profile_json,redacted_resume_text,created_at) VALUES(?,?,?,?,?,?)',[h,filename,'test',make_profile().model_dump_json(),'Python SQL production resume',BASE])


def score(key,h=OLD,value=95,complete=True):
    return dict(resume_hash=h,record_key=key,overall_score=value,recommendation='apply' if value>=75 else 'review' if value>=50 else 'skip',
        title_fit=value,skills_fit=value,experience_fit=value,seniority_fit=value,industry_fit=value,location_fit=100,
        confidence='high',requirement_matches='[]',matching_strengths='[]',hard_requirements_missing='["Historical false SQL gap"]',
        preferred_qualifications_missing='[]',risk_factors='[]',summary='original evidence',description_word_count=200,
        description_complete=complete,model_name='original-provider',prompt_version='v8',scored_at=BASE.isoformat())


def seed_score(row):
    with database.get_connection() as c:
        c.execute('INSERT INTO resume_job_scores('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()))


def migrate(keys=('job',)):
    initialize_ai_tables();profile(OLD);profile(NEW)
    for key in keys:
        seed_review_job(key,'Senior Data Engineer',description=JD)
        seed_score(score(key))
    with database.get_connection() as c:
        preview=make_preview(c,'Hugo','Batista','https://www.linkedin.com/in/hugo-batista-7b963336/',[OLD,NEW])
        c.execute('BEGIN');apply_preview(c,preview);c.execute('COMMIT')
    return preview


class CandidateTests(unittest.TestCase):
    def test_linkedin_and_name_normalization(self):
        self.assertEqual(store.normalize_name('  HUGO  '),'hugo')
        for url in ['linkedin.com/in/Hugo-Batista/?trk=x','https://pt.linkedin.com/in/hugo-batista','http://www.linkedin.com/in/HUGO-BATISTA/']:
            self.assertEqual(store.normalize_linkedin_url(url),'https://www.linkedin.com/in/hugo-batista')
        for url in ['https://linkedin.com.evil.invalid/in/a','https://evil.invalid/in/a','https://linkedin.com/jobs/123','https://linkedin.com/in/a%2Fb']:
            with self.assertRaises(ValueError):store.normalize_linkedin_url(url)
        self.assertIsNone(store.normalize_linkedin_url(''))

    def test_names_never_automatically_merge_and_links_require_confirmation(self):
        def run():
            initialize_ai_tables();profile(OLD);profile(NEW)
            with database.get_connection() as c:
                store.initialize(c)
                a=store.register_candidate(c,'Hugo','Batista');b=store.register_candidate(c,'Hugo','Batista')
                self.assertNotEqual(a,b)
                with self.assertRaises(ValueError):store.associate_resume(c,a,OLD)
                store.associate_resume(c,a,OLD,confirmed=True)
                with self.assertRaises(ValueError):store.associate_resume(c,b,OLD,confirmed=True)
                store.associate_resume(c,a,NEW,confirmed=True)
                self.assertEqual(store.candidate_for_resume(c,OLD),store.candidate_for_resume(c,NEW))
        with_temp_review_database(run)

    def test_migration_preserves_211_scores_80_restorations_and_statuses(self):
        def run():
            keys=tuple('job-'+str(i) for i in range(211));p=migrate(keys)
            with database.get_connection() as c:
                self.assertEqual(snapshot(c),p['source_tables_sha256'])
                self.assertEqual(c.execute('SELECT count(*) FROM resume_job_scores').fetchone()[0],211)
                self.assertEqual(c.execute('SELECT count(*) FROM candidate_assessments').fetchone()[0],211)
                cid=p['candidate']['candidate_id']
                self.assertEqual(len(store.active_assessments(c,cid)),211)
                self.assertEqual(store.latest_candidate_resume(c,OLD),NEW)
                self.assertEqual(c.execute("SELECT count(*) FROM candidate_assessments WHERE json_extract_string(payload_json,'$.overall_score')='95'").fetchone()[0],211)
        with_temp_review_database(run)

    def test_drift_rolls_back_and_duplicate_history_is_reported(self):
        def run():
            initialize_ai_tables();profile(OLD);profile(NEW)
            seed_review_job('job','Data Engineer',description=JD)
            seed_score(score('job'));seed_score(score('job',NEW,88))
            with database.get_connection() as c:
                p=make_preview(c,'A','B',None,[OLD,NEW])
                self.assertEqual(len(p['duplicate_groups']),1)
                self.assertEqual(len(p['assessments']),2)
                c.execute("UPDATE resume_job_scores SET overall_score=90 WHERE resume_hash=?",[NEW])
                c.execute('BEGIN')
                with self.assertRaises(ValueError):apply_preview(c,p)
                c.execute('ROLLBACK')
                self.assertFalse(store.tables_exist(c))
        with_temp_review_database(run)

    def test_completed_assessment_survives_resume_prompt_model_and_jd_updates(self):
        def run():
            p=migrate()
            with database.get_connection() as c:
                c.execute("UPDATE raw_jobs SET description_updated_at=current_timestamp WHERE record_key='job'")
                self.assertEqual(store.completed_record_keys(c,NEW),{'job'})
            with store.scoring_claim(NEW,{'record_key':'job'}) as acquired:self.assertFalse(acquired)
            with database.get_connection() as c:
                jobs=[dict(record_key='job',ai_score=None)]
                store.overlay_saved_scores(c,NEW,jobs)
                self.assertEqual(jobs[0]['ai_score'],95)
                self.assertEqual(jobs[0]['hard_requirements_missing'],'["Historical false SQL gap"]')
                self.assertTrue(repo.has_current_complete_ai_score(jobs[0]))
            self.assertEqual(repo.count_cached_canonical_scores(NEW,'new-model','new-prompt'),1)
        with_temp_review_database(run)

    def test_verified_duplicates_share_score_but_other_requisition_and_candidate_do_not(self):
        def run():
            p=migrate()
            seed_review_job('copy','Senior Data Engineer',source='lensa',description=JD)
            seed_review_job('other','Senior Data Engineer',source='linkedin',description=JD)
            for key in ['job','copy']:
                seed_verified_attempt(key,'greenhouse:123',description=JD)
            seed_verified_attempt('other','greenhouse:456',description=JD,official_job_url='https://boards.greenhouse.io/example/jobs/456')
            with database.get_connection() as c:
                self.assertEqual(store.completed_record_keys(c,NEW),{'job','copy'})
                profile_hash='other-person';profile_row=make_profile().model_dump_json()
                c.execute('INSERT INTO resume_profiles(resume_hash,filename,model_name,profile_json) VALUES(?,?,?,?)',[profile_hash,'same.pdf','test',profile_row])
                other=store.register_candidate(c,'Hugo','Batista')
                store.associate_resume(c,other,profile_hash,confirmed=True)
                jobs=[{'record_key':'job','ai_score':95}]
                store.overlay_saved_scores(c,profile_hash,jobs)
                self.assertIsNone(jobs[0]['ai_score'])
        with_temp_review_database(run)

    def test_claim_excludes_repeated_run_and_failure_keeps_old_manual_score(self):
        def run():
            migrate()
            with store.scoring_claim(NEW,{'record_key':'job'},manual=True) as a:
                self.assertTrue(a)
                with store.scoring_claim(NEW,{'record_key':'job'},manual=True) as b:self.assertFalse(b)
            with self.assertRaises(RuntimeError):
                with store.scoring_claim(NEW,{'record_key':'job'},manual=True):raise RuntimeError('provider failure')
            with database.get_connection() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM candidate_scoring_claims').fetchone()[0],0)
                self.assertEqual(len(store.active_assessments(c,store.candidate_for_resume(c,NEW))),1)
        with_temp_review_database(run)

    def test_manual_rescore_appends_and_never_rewrites_legacy_evidence(self):
        def run():
            migrate()
            with database.get_connection() as c:before=snapshot(c)
            match=build_resume_job_match(make_profile(),{'record_key':'job','description':JD},'new-provider',fit_score_to_job_match_analysis(FitScore(fit_score=88)),200,True)
            with store.scoring_claim(NEW,{'record_key':'job'},manual=True):save_job_score(NEW,match)
            with database.get_connection() as c:
                self.assertEqual(snapshot(c),before)
                self.assertEqual(c.execute('SELECT count(*) FROM candidate_assessments').fetchone()[0],2)
                active=store.active_assessments(c,store.candidate_for_resume(c,NEW))
                self.assertEqual(next(iter(active.values()))['score']['overall_score'],88)
        with_temp_review_database(run)

    def test_incomplete_is_retryable_and_unassociated_resume_never_leaks(self):
        def run():
            initialize_ai_tables();profile(OLD);profile(NEW)
            seed_review_job('job','Data Engineer',description=JD);seed_score(score('job',complete=False))
            with database.get_connection() as c:
                p=make_preview(c,'Hugo','Batista',None,[OLD,NEW])
                self.assertEqual(len(p['skipped']),1)
                apply_preview(c,p)
                self.assertEqual(store.completed_record_keys(c,NEW),set())
                with self.assertRaises(store.AssessmentConflict):store.append_assessment(c,p['candidate']['candidate_id'],'exact:x',score('job',complete=False))
        with_temp_review_database(run)

    def test_score_order_uses_ingestion_not_enrichment_and_null_last(self):
        rows=[dict(record_key=k,canonical_job_key=k,exact_posting_key=k,source='linkedin',ai_score=v,discovered_at=d,application_status='new') for k,v,d in [('older',95,BASE),('newer',95,BASE+timedelta(days=1)),('null',None,BASE+timedelta(days=100)),('zero',0,None),('top',98,BASE-timedelta(days=1000))]]
        self.assertEqual([r['record_key'] for r in repo.prepare_job_display(rows)],['top','newer','older','zero','null'])

    def test_candidate_revision_refreshes_snapshot_without_calls_on_interaction(self):
        state={};loader=Mock(return_value=[{'record_key':'a','ai_score':95}])
        jobs_snapshot.jobs_snapshot(state,OLD,loader,dataset_token=('candidate',1))
        jobs_snapshot.jobs_snapshot(state,OLD,loader,dataset_token=('candidate',1))
        self.assertEqual(loader.call_count,1)
        jobs_snapshot.jobs_snapshot(state,OLD,loader,dataset_token=('candidate',2))
        self.assertEqual(loader.call_count,2)

    def test_cached_upload_associates_new_bytes_without_scoring_or_provider_calls(self):
        from types import SimpleNamespace
        from src.ui import job_recommendation_service as service
        def run():
            p=migrate()
            profile('reexported-bytes','renamed-export.pdf')
            uploaded=SimpleNamespace(resume_hash='reexported-bytes',filename='renamed-export.pdf',text='Python SQL resume')
            with patch.object(service,'extract_resume',return_value=uploaded), patch.object(service,'profile_resume',side_effect=AssertionError('No AI')), patch.object(service,'score_resume_against_job',side_effect=AssertionError('No scoring')):
                service.prepare_resume_upload(object(),candidate_id=p['candidate']['candidate_id'])
            with database.get_connection() as c:
                self.assertEqual(store.candidate_for_resume(c,'reexported-bytes'),p['candidate']['candidate_id'])
                self.assertEqual(store.candidate_for_resume(c,OLD),p['candidate']['candidate_id'])
                self.assertEqual(len(store.active_assessments(c,p['candidate']['candidate_id'])),1)
        with_temp_review_database(run)

    def test_automatic_backlog_does_not_call_either_provider_for_completed_job(self):
        from src import score_backlog as backlog
        def run():
            migrate()
            with patch.object(backlog,'screen_unscreened_jobs') as screen, patch.object(backlog,'score_resume_against_job',side_effect=AssertionError('No Gemini')), patch.object(backlog,'score_resume_against_job_openai',side_effect=AssertionError('No OpenAI')):
                screen.return_value.screened=0
                result=backlog.score_backlog(OLD,10,0)
                self.assertEqual(result.resume_hash,NEW)
                self.assertEqual(result.candidates_selected,0)
                self.assertEqual(result.already_scored,1)
        with_temp_review_database(run)

    def test_concurrent_claims_allow_only_one_worker(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        def run():
            migrate()
            ready=Event();release=Event()
            def worker():
                with store.scoring_claim(NEW,{'record_key':'job'},manual=True) as acquired:
                    ready.set()
                    release.wait(10)
                    return acquired
            with ThreadPoolExecutor(max_workers=1) as pool:
                first=pool.submit(worker)
                self.assertTrue(ready.wait(10))
                try:
                    with store.scoring_claim(NEW,{'record_key':'job'},manual=True) as other:self.assertFalse(other)
                finally:release.set()
                self.assertTrue(first.result())
        with_temp_review_database(run)

    def test_changed_verified_identity_does_not_share_a_completed_score(self):
        def run():
            initialize_ai_tables();profile(OLD);profile(NEW)
            seed_review_job('job','Data Engineer',description=JD)
            seed_verified_attempt('job','greenhouse:123',description=JD)
            seed_score(score('job'))
            with database.get_connection() as c:
                p=make_preview(c,'Hugo','Batista',None,[OLD,NEW]);apply_preview(c,p)
                c.execute("UPDATE job_enrichment_attempts SET verified_posting_key='greenhouse:456',official_job_url='https://boards.greenhouse.io/example/jobs/456' WHERE record_key='job'")
                self.assertEqual(store.completed_record_keys(c,NEW),set())
                jobs=[{'record_key':'job','ai_score':95}];store.overlay_saved_scores(c,NEW,jobs)
                self.assertIsNone(jobs[0]['ai_score'])
        with_temp_review_database(run)

    def test_failed_verified_refresh_does_not_erase_exact_source_completion(self):
        def run():
            initialize_ai_tables();profile(OLD);profile(NEW)
            seed_review_job('job','Data Engineer',description=JD)
            seed_verified_attempt('job','greenhouse:123',description=JD)
            seed_score(score('job'))
            with database.get_connection() as c:
                p=make_preview(c,'Hugo','Batista',None,[OLD,NEW]);apply_preview(c,p)
                c.execute("UPDATE job_enrichment_attempts SET official_url_status='BLOCKED' WHERE record_key='job'")
                self.assertEqual(store.completed_record_keys(c,NEW),{'job'})
                jobs=[{'record_key':'job','ai_score':None}];store.overlay_saved_scores(c,NEW,jobs)
                self.assertEqual(jobs[0]['ai_score'],95)
        with_temp_review_database(run)

    def test_repository_loads_candidate_score_without_a_mart_refresh(self):
        def run():
            migrate()
            with database.get_connection() as c:
                c.execute('UPDATE raw_jobs SET description_updated_at=current_timestamp')
            jobs=repo.load_all_jobs(NEW)
            row=next(j for j in jobs if j['record_key']=='job')
            self.assertEqual((row['ai_score'],row['recommendation']),(95,'apply'))
            self.assertTrue(row['has_current_complete_ai_assessment'])
        with_temp_review_database(run)

    def test_default_date_filter_has_no_age_cutoff(self):
        import ast
        from pathlib import Path
        from datetime import date
        tree=ast.parse(Path('streamlit_app.py').read_text())
        func=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='default_date_range')
        namespace={'date':date,'timedelta':timedelta}
        exec(compile(ast.Module(body=[func],type_ignores=[]),'date-default','exec'),namespace)
        dates=[date(2020,1,1),date(2026,10,20)]
        self.assertEqual(namespace['default_date_range'](dates),(min(dates),max(dates)))

    def test_unconfirmed_new_resume_stops_before_profiling(self):
        from types import SimpleNamespace
        from src.ui import job_recommendation_service as service
        def run():
            migrate()
            uploaded=SimpleNamespace(resume_hash='unconfirmed-person',filename='same.pdf',text='different resume')
            with patch.object(service,'extract_resume',return_value=uploaded), patch.object(service,'profile_resume',side_effect=AssertionError('Must not call AI')) as provider:
                with self.assertRaises(service.JobRecommendationServiceError):service.prepare_resume_upload(object())
                provider.assert_not_called()
        with_temp_review_database(run)

    def test_completed_filter_is_applied_to_a_real_eligible_scoring_queue(self):
        def run():
            migrate()
            with database.get_connection() as c:
                c.execute("INSERT INTO job_matches(record_key,search_id,search_title,match_score,title_score,location_score,keyword_score,freshness_score,matched_keywords,excluded_keywords,reasons,is_recommended,needs_review) VALUES('job','search','Data Engineer',90,90,90,90,90,'[]','[]','[]',true,false)")
                active=c.execute('SELECT * FROM candidate_active_assessments').fetchone()
                c.execute('DELETE FROM candidate_active_assessments')
            control=repo.load_candidate_jobs(NEW,'different-model',10,0,prompt_version='future-prompt',reuse_any_model=True)
            self.assertEqual([r['record_key'] for r in control],['job'])
            with database.get_connection() as c:c.execute('INSERT INTO candidate_active_assessments VALUES(?,?,?)',active)
            self.assertEqual(repo.load_candidate_jobs(NEW,'different-model',10,0,prompt_version='future-prompt',reuse_any_model=True),[])
        with_temp_review_database(run)

    def test_new_job_scores_once_with_latest_resume_and_retains_original_scores(self):
        from src import score_backlog as backlog
        def run():
            migrate()
            seed_review_job('new-job','Senior Data Engineer',description=JD)
            with database.get_connection() as c:
                c.execute("INSERT INTO job_matches(record_key,search_id,search_title,match_score,title_score,location_score,keyword_score,freshness_score,matched_keywords,excluded_keywords,reasons,is_recommended,needs_review) VALUES('new-job','search','Data Engineer',90,90,90,90,90,'[]','[]','[]',true,false)")
            def fake_provider(**kwargs):
                return build_resume_job_match(kwargs['resume_profile'],kwargs['job'],'fixture-provider',fit_score_to_job_match_analysis(FitScore(fit_score=88)),200,True)
            with patch.object(backlog,'screen_unscreened_jobs') as screen, patch.object(backlog,'score_resume_against_job',side_effect=fake_provider) as provider, patch.object(backlog,'score_resume_against_job_openai',side_effect=AssertionError('No fallback')), patch.object(backlog,'run_dbt_build'):
                screen.return_value.screened=0
                result=backlog.score_backlog(OLD,10,0)
                self.assertEqual((result.resume_hash,result.scores_saved),(NEW,1))
                self.assertEqual(provider.call_count,1)
                again=backlog.score_backlog(OLD,10,0)
                self.assertEqual((again.candidates_selected,again.scores_saved),(0,0))
                self.assertEqual(provider.call_count,1)
            with database.get_connection() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM resume_job_scores').fetchone()[0],1)
                self.assertEqual(c.execute('SELECT count(*) FROM candidate_assessments').fetchone()[0],2)
        with_temp_review_database(run)


if __name__=='__main__':unittest.main()
