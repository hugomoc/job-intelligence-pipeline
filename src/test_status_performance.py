"""Offline callback correctness and call-count budgets; never use production DB."""
import ast
import copy
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from src.repositories import recommendation_repository as repo
from src.ui import jobs_snapshot as session
from src.ui.daily_workflow_service import ScoringBacklogStatus
from src.test_recommendation_repository import (
    with_temp_review_database, seed_review_job, seed_verified_attempt, seed_resume_profile,
)
from src import database

DESC = 'Responsibilities include Python SQL Snowflake AWS data pipelines and analytics engineering requirements. '*20
STATUS_FIELDS = ('application_status','application_status_scope','application_status_source','application_status_record_key','application_status_updated_at')
def sample(key='a', **extra):
    row=dict(record_key=key,exact_posting_key=key,canonical_job_key=key,duplicate_fingerprint=key,
        source='linkedin',title='Senior Data Engineer',company_name='Example Co',
        application_status='new',description_state='FULL_JD',description_word_count=200,
        description_quality_signals=True,has_current_complete_ai_assessment=False,
        title_classification='STRONG_MATCH',title_match_score=100)
    row.update(extra);return row

def initialize_session(rows):
    return {'jobs_snapshot':rows,'jobs_snapshot_resume_hash':'resume-1',
        'jobs_snapshot_version':1,'jobs_snapshot_dirty':False,
        'scoring_backlog_snapshot':{'key':('resume-1',0,0),'status':ScoringBacklogStatus(
            'resume-1',10,len(rows),tuple(j['canonical_job_key'] for j in rows))}}

def receipt(rows,status='applied'):
    return repo.ApplicationStatusChange(('a',),'a',(),tuple(dict(record_key=j['record_key'],
        application_status=status,application_status_scope='exact',
        application_status_source='linkedin',application_status_record_key='a') for j in rows))

class SessionTests(unittest.TestCase):
    def test_initial_load_once_and_interactions_zero(self):
        state={};jobs_loader=Mock(return_value=[sample()]);score_loader=Mock(return_value=ScoringBacklogStatus('resume-1',10,1,('a',)))
        operations_loader=Mock(return_value=dict(raw_jobs=10,exact_postings=9,enrichment_attempts={}))
        def render():
            status=session.scoring_snapshot(state,'resume-1',0,score_loader)
            session.jobs_snapshot(state,'resume-1',jobs_loader)
            rows=session.display_snapshot(state)
            session.operation_snapshot(state,'resume-1',0,rows,status,operations_loader)
            return rows
        render();self.assertEqual(jobs_loader.call_count,1)
        for _ in range(5):render() # filters, pagination, page-size, expanders
        writer=Mock(return_value=receipt([sample()]))
        session.patch_status(state,'a','applied',writer)
        rows=render()
        self.assertEqual(jobs_loader.call_count,1);self.assertEqual(score_loader.call_count,1)
        self.assertEqual(operations_loader.call_count,1);self.assertEqual(writer.call_count,1)
        self.assertEqual(rows[0]['application_status'],'applied')
        self.assertFalse([j for j in rows if j['application_status']=='new'])
        self.assertFalse(state['jobs_snapshot_dirty'])
        self.assertEqual(state['scoring_backlog_snapshot']['status'].unscored_candidates,0)
        self.assertEqual(state['operations_snapshot']['metrics']['applied_jobs'],1)
    def test_removed_filter_immediate(self):
        state=initialize_session([sample(),sample('unrelated')])
        session.patch_status(state,'a','removed',Mock(return_value=receipt([sample()],'removed')))
        rows=session.display_snapshot(state)
        self.assertEqual([j['record_key'] for j in rows if j['application_status']=='new'],['unrelated'])
        self.assertEqual([j['record_key'] for j in rows if j['application_status']=='removed'],['a'])
    def test_all_mutations_and_resume_invalidate(self):
        for reason in ('ingestion','enrichment','scoring','explicit refresh','resume changed'):
            state=initialize_session([sample()]);loader=Mock(return_value=[sample()])
            session.invalidate_data(state,reason)
            self.assertTrue(state['jobs_snapshot_dirty']);self.assertNotIn('scoring_backlog_snapshot',state)
            session.jobs_snapshot(state,'resume-1',loader);session.jobs_snapshot(state,'resume-1',loader)
            self.assertEqual(loader.call_count,1)
        state=initialize_session([sample()]);loader=Mock(return_value=[sample()])
        session.jobs_snapshot(state,'resume-2',loader);self.assertEqual(loader.call_count,1)
        self.assertEqual(state['jobs_snapshot_resume_hash'],'resume-2')
    def test_actionability_version_updates_without_reload(self):
        state=initialize_session([sample()]);loader=Mock()
        state['jobs_snapshot'][0]['actionability_status']='UNKNOWN'
        old=state['jobs_snapshot_version'];session.actionability_saved(state)
        self.assertGreater(state['jobs_snapshot_version'],old)
        session.jobs_snapshot(state,'resume-1',loader);loader.assert_not_called()
    def test_sidebar_scope_delta_only_exact_not_inherited(self):
        rows=[sample(),sample('verified-counterpart')];state=initialize_session(rows)
        session.patch_status(state,'a','applied',Mock(return_value=receipt(rows)))
        summary=state['scoring_backlog_snapshot']['status']
        self.assertEqual(summary.unscored_candidates,1)
        self.assertEqual(summary.candidate_posting_keys,('verified-counterpart',))
        self.assertEqual(summary.cached_scores,10)
    def test_restore_new_recounts_sidebar_not_jobs(self):
        state=initialize_session([sample()]);session.patch_status(state,'a','new',Mock(return_value=receipt([sample()],'new')))
        self.assertNotIn('scoring_backlog_snapshot',state);self.assertFalse(state['jobs_snapshot_dirty'])
    def test_session_ownership(self):
        a=initialize_session([sample()]);b=initialize_session([sample()])
        session.patch_status(a,'a','removed',Mock(return_value=receipt([sample()],'removed')))
        self.assertEqual(b['jobs_snapshot'][0]['application_status'],'new')
    def test_duplicate_display_representative_can_switch(self):
        state=initialize_session([sample('a',duplicate_fingerprint='same',ai_score=90),sample('b',duplicate_fingerprint='same',ai_score=80)])
        self.assertEqual(session.display_snapshot(state)[0]['record_key'],'a')
        session.patch_status(state,'a','applied',Mock(return_value=receipt([sample()])))
        self.assertEqual(session.display_snapshot(state)[0]['record_key'],'b')
    def test_no_jobs_loader_in_operations_with_supplied_jobs(self):
        def scenario():
            repo.initialize_job_eligibility_table()
            with patch.object(repo,'load_all_jobs',side_effect=AssertionError('unexpected full reload')),patch.object(repo,'count_unscored_candidate_jobs',side_effect=AssertionError('unexpected recount')):
                metrics=repo.load_pipeline_operation_metrics('resume-1',0,reviewable_jobs=[sample()],unscored_candidates=3)
                self.assertEqual(metrics['reviewable_jobs'],1);self.assertEqual(metrics['ai_score_eligible'],3)
        with_temp_review_database(scenario)
    def test_status_callbacks_have_no_rerun_or_network(self):
        tree=ast.parse(Path('streamlit_app.py').read_text())
        listing=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='render_job_listing')
        calls=[n for n in ast.walk(listing) if isinstance(n,ast.Call)]
        self.assertFalse(any(isinstance(n.func,ast.Attribute) and n.func.attr=='rerun' for n in calls))
        buttons=[n for n in calls if any(isinstance(a,ast.Constant) and a.value in ('Mark applied','Mark removed') for a in n.args)]
        self.assertEqual(len(buttons),2)
        self.assertTrue(all(any(k.arg=='on_click' and isinstance(k.value,ast.Name) and k.value.id=='change_application_status' for k in n.keywords) for n in buttons))
        callback=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='change_application_status')
        self.assertNotIn('refresh_daily',ast.unparse(callback));self.assertNotIn('st.rerun',ast.unparse(callback))
    def test_visibility_batch_reads_policy_once(self):
        from src.ui.job_visibility import count_job_review_buckets
        from src.actionability import Settings
        rows=[sample(str(i),has_current_complete_ai_assessment=True,ai_score=90,recommendation='apply') for i in range(100)]
        with patch('src.actionability.settings',return_value=Settings()) as settings:
            counts=count_job_review_buckets(rows)
            self.assertEqual(counts['needs_review'],100);self.assertEqual(settings.call_count,1)
    def test_baseline_control_flow_count_budget(self):
        # Saved-jobs view before this patch: interrupted click run does not reach
        # Operations. Initial = Jobs + Operations; click = Jobs + second full run.
        old_calls={'jobs':0,'operations':0,'sidebar':0}
        def old_render(clicked=False):
            old_calls['sidebar']+=1;old_calls['jobs']+=1
            if clicked:return  # explicit st.rerun interrupts before Operations
            old_calls['operations']+=1;old_calls['jobs']+=1
        old_render();self.assertEqual(old_calls,dict(jobs=2,operations=1,sidebar=1))
        old_calls.update(jobs=0,operations=0,sidebar=0);old_render(True);old_render()
        self.assertEqual(old_calls,dict(jobs=3,operations=1,sidebar=2))

def measure_streamlit_status_interaction(source):
    """Real Streamlit reruns; repository/network boundaries are fully mocked."""
    from streamlit.testing.v1 import AppTest
    from datetime import datetime, timezone
    from src.test_job_review_visibility import complete_ai_job
    from src.ui.daily_workflow_service import ScoringBacklogStatus
    backend = {}
    templates = []
    for key, number in (('a',123),('b',456)):
        url=f'https://job-boards.greenhouse.io/example/jobs/{number}'
        row=complete_ai_job(record_key=key,source='linkedin',company_name='Example Co',
            location='Remote',title='Senior Analytics Engineer',ai_score=90,recommendation='apply',
            canonical_job_key=key,exact_posting_key=key,duplicate_fingerprint=key,
            sent_at=datetime.now(timezone.utc),apply_url=url,official_job_url=url,
            verified_posting_key=f'greenhouse:{number}',actionability_posting_key=f'greenhouse:{number}',
            actionability_official_url=url,description=DESC,matching_strengths=[],
            hard_requirements_missing=[],preferred_qualifications_missing=[],risk_factors=[],
            title_fit=90,skills_fit=90,experience_fit=90,seniority_fit=90,industry_fit=90,location_fit=90)
        templates.append(row)
    def load(**kwargs):
        rows=copy.deepcopy(templates)
        for row in rows:
            if row['record_key'] in backend:row['application_status']=backend[row['record_key']]
        return rows
    jobs_loader=Mock(side_effect=load)
    def operations(**kwargs):
        rows=kwargs.get('reviewable_jobs')
        if rows is None:rows=jobs_loader()
        metrics=repo.reviewable_job_metrics(rows)
        metrics.update(raw_jobs=2,exact_postings=2,enrichment_attempts={})
        return metrics
    operations_loader=Mock(side_effect=operations)
    score_loader=Mock(return_value=ScoringBacklogStatus('resume-1',2,0,()))
    def write(record_key,status,reviewable_jobs=None):
        backend[record_key]=status
        return repo.ApplicationStatusChange((record_key,),record_key,(),({
            'record_key':record_key,'application_status':status,'application_status_scope':'exact',
            'application_status_source':'linkedin','application_status_record_key':record_key},))
    writer=Mock(side_effect=write)
    network=Mock(side_effect=AssertionError('Unexpected actionability network request'))
    with patch.object(repo,'load_all_jobs',jobs_loader),patch.object(repo,'load_pipeline_operation_metrics',operations_loader),patch.object(repo,'update_application_status',writer),patch('src.ui.daily_workflow_service.load_scoring_backlog_status',score_loader),patch('src.actionability.refresh_daily',network):
        instrumented=source.replace('st.set_page_config(', "st.session_state['_perf_render_runs'] = st.session_state.get('_perf_render_runs', 0) + 1\nst.set_page_config(",1)
        app=AppTest.from_string(instrumented,default_timeout=30)
        app.session_state['show_saved_jobs']=True;app.session_state['resume_hash']='resume-1'
        app.run();assert not app.exception,str(app.exception)
        initial={'load_all_jobs':jobs_loader.call_count,'operations_sql':operations_loader.call_count,'sidebar_counts':score_loader.call_count}
        next(box for box in app.selectbox if box.label=='Status').set_value('New').run()
        assert not app.exception,str(app.exception)
        jobs_loader.reset_mock();operations_loader.reset_mock();score_loader.reset_mock();writer.reset_mock()
        before_runs=app.session_state['_perf_render_runs']
        app.button(key='apply-a').click().run();assert not app.exception,str(app.exception)
        applied={'load_all_jobs':jobs_loader.call_count,'operations_sql':operations_loader.call_count,'sidebar_counts':score_loader.call_count,'writes':writer.call_count,'actionability_network':network.call_count,'reruns':app.session_state['_perf_render_runs']-before_runs}
        assert 'apply-a' not in {button.key for button in app.button}
        assert backend['a']=='applied'
        jobs_loader.reset_mock();operations_loader.reset_mock();score_loader.reset_mock();writer.reset_mock()
        before_runs=app.session_state['_perf_render_runs']
        app.button(key='remove-b').click().run();assert not app.exception,str(app.exception)
        assert backend['b']=='removed'
        removed={'load_all_jobs':jobs_loader.call_count,'operations_sql':operations_loader.call_count,'sidebar_counts':score_loader.call_count,'writes':writer.call_count,'actionability_network':network.call_count,'reruns':app.session_state['_perf_render_runs']-before_runs}
        return {'initial':initial,'mark_applied':applied,'mark_removed':removed}


class StreamlitTests(unittest.TestCase):
    def test_actual_streamlit_callback_call_budget(self):
        result=measure_streamlit_status_interaction(Path('streamlit_app.py').read_text())
        self.assertEqual(result['initial'],dict(load_all_jobs=1,operations_sql=1,sidebar_counts=1))
        self.assertEqual(result['mark_applied'],dict(load_all_jobs=0,operations_sql=0,sidebar_counts=0,writes=1,actionability_network=0,reruns=1))
        self.assertEqual(result['mark_removed'],result['mark_applied'])


class RepositoryTests(unittest.TestCase):
    def seed(self):
        seed_resume_profile()
        for key,source,url in [('a','linkedin','https://www.linkedin.com/jobs/view/111/'),
                ('duplicate','linkedin','https://www.linkedin.com/jobs/view/111/?utm_source=email'),
                ('shared','wellfound','https://wellfound.com/jobs/222'),
                ('other','wellfound','https://wellfound.com/jobs/333'),
                ('untrusted','wellfound','https://wellfound.com/jobs/444')]:
            seed_review_job(key,'Senior Data Engineer',source=source,description=DESC,apply_url=url)
            seed_verified_attempt(key,'greenhouse:999' if key=='other' else 'greenhouse:123',source=source,
                description=DESC,official_job_url='https://boards.greenhouse.io/example/jobs/999' if key=='other' else 'https://boards.greenhouse.io/example/jobs/123',trusted_verified_key=key!='untrusted')
    def assert_fresh(self,state):
        # A session retains every exact posting, even when display cards collapse.
        cached_physical={j['record_key']:j for j in state['jobs_snapshot']}
        fresh_physical={j['record_key']:j for j in repo.load_all_jobs('resume-1',for_session_snapshot=True)}
        self.assertEqual(set(cached_physical),set(fresh_physical))
        for key in fresh_physical:
            self.assertEqual({f:cached_physical[key].get(f) for f in STATUS_FIELDS},
                {f:fresh_physical[key].get(f) for f in STATUS_FIELDS},key)
        cached={j['record_key']:j for j in session.display_snapshot(state)}
        fresh={j['record_key']:j for j in repo.load_all_jobs('resume-1')}
        self.assertEqual(set(cached),set(fresh))
        for key in fresh:
            self.assertEqual({f:cached[key].get(f) for f in STATUS_FIELDS},{f:fresh[key].get(f) for f in STATUS_FIELDS},key)
    def test_applied_removed_exact_and_trusted_propagation_match_fresh(self):
        def scenario():
            self.seed();state=initialize_session(repo.load_all_jobs('resume-1',for_session_snapshot=True))
            self.assert_fresh(state)
            for status in ('applied','removed','new'):
                change=session.patch_status(state,'a',status,repo.update_application_status)
                self.assertEqual(set(change.record_keys),{'a','duplicate'})
                self.assert_fresh(state)
                with database.get_connection() as c:
                    self.assertEqual(c.execute("select count(*) from application_status where status=?",[status]).fetchone()[0],2)
                cached={j['record_key']:j for j in state['jobs_snapshot']}
                display=session.display_snapshot(state)
                trusted=[j for j in display if j.get('verified_posting_key')=='greenhouse:123' and j.get('verified_posting_key_trust')=='trusted']
                self.assertEqual(len(trusted),1)
                self.assertEqual(trusted[0]['application_status'],status)
                self.assertEqual(cached['shared']['application_status'],status)
                self.assertEqual(cached['other']['application_status'],'new')
                self.assertEqual(cached['untrusted']['application_status'],'new')
        with_temp_review_database(scenario)
    def test_exact_status_overrides_verified_fallback(self):
        def scenario():
            self.seed();repo.update_application_status('shared','applied')
            state=initialize_session(repo.load_all_jobs('resume-1',for_session_snapshot=True))
            session.patch_status(state,'a','removed',repo.update_application_status);self.assert_fresh(state)
            shared=next(j for j in state['jobs_snapshot'] if j['record_key']=='shared')
            self.assertEqual(shared['application_status'],'applied');self.assertEqual(shared['application_status_scope'],'exact')
            trusted=[j for j in session.display_snapshot(state) if j.get('verified_posting_key')=='greenhouse:123' and j.get('verified_posting_key_trust')=='trusted']
            self.assertEqual(len(trusted),1)
            self.assertEqual(trusted[0]['application_status'],'removed')
        with_temp_review_database(scenario)
    def test_hidden_admission_record_becomes_visible_history_without_reload(self):
        def scenario():
            self.seed()
            seed_review_job('hard-gap','Databricks Data Engineer',source='wellfound',description=DESC,
                apply_url='https://wellfound.com/jobs/555')
            seed_verified_attempt('hard-gap','greenhouse:123',source='wellfound',description=DESC)
            state=initialize_session(repo.load_all_jobs('resume-1',for_session_snapshot=True))
            self.assertNotIn('hard-gap',{j['record_key'] for j in session.display_snapshot(state)})
            session.patch_status(state,'a','applied',repo.update_application_status)
            historical=next(j for j in state['jobs_snapshot'] if j['record_key']=='hard-gap')
            self.assertEqual(historical['application_status'],'applied')
            self.assertEqual(historical['application_status_scope'],'verified_posting')
            trusted=[j for j in session.display_snapshot(state) if j.get('verified_posting_key')=='greenhouse:123' and j.get('verified_posting_key_trust')=='trusted']
            self.assertEqual(len(trusted),1)
            self.assertEqual(trusted[0]['application_status'],'applied')
            self.assert_fresh(state)
        with_temp_review_database(scenario)
    def seed_cross_source_opening(self):
        seed_resume_profile()
        for key,source,url in (
                ('linkedin-opening','linkedin','https://www.linkedin.com/jobs/view/111/'),
                ('lensa-opening','lensa','https://lensa.com/job-v1/example/data-engineer/opening-222'),
                ('wttj-opening','welcometothejungle','https://www.welcometothejungle.com/en/companies/example/jobs/data-engineer')):
            seed_review_job(key,'Senior Data Engineer',source=source,description=DESC,apply_url=url)
            seed_verified_attempt(key,'greenhouse:123',source=source,description=DESC)

    def test_collapsed_trusted_opening_applied_removed_restored_without_reappearing(self):
        def scenario():
            self.seed_cross_source_opening()
            state=initialize_session(repo.load_all_jobs('resume-1',for_session_snapshot=True))
            self.assertEqual(len(state['jobs_snapshot']),3)
            self.assertEqual(len(session.display_snapshot(state)),1)
            for status in ('applied','removed','new'):
                representative=session.display_snapshot(state)[0]['record_key']
                change=session.patch_status(state,representative,status,repo.update_application_status)
                self.assertEqual(change.record_keys,(representative,))
                self.assertEqual(len(state['jobs_snapshot']),3)
                self.assertTrue(all(j['application_status']==status for j in state['jobs_snapshot']))
                cards=session.display_snapshot(state)
                self.assertEqual(len(cards),1)
                self.assertEqual(cards[0]['application_status'],status)
                for bucket in ('new','applied','removed'):
                    self.assertEqual(len([j for j in cards if j['application_status']==bucket]),int(bucket==status))
                metrics=repo.reviewable_job_metrics(cards)
                self.assertEqual(metrics['reviewable_jobs'],1)
                self.assertEqual(metrics[status+'_jobs'],1)
                self.assert_fresh(state)
        with_temp_review_database(scenario)

    def test_workday_different_tenants_never_inherit_status(self):
        from src.verified_posting_identity import verified_posting_key_from_url
        def scenario():
            seed_resume_profile()
            urls = (
                "https://alpha.wd1.myworkdayjobs.com/en-US/careers/job/US/Data-Engineer_REQ123",
                "https://beta.wd2.myworkdayjobs.com/en-US/careers/job/US/Data-Engineer_REQ123",
            )
            for i, (company, url) in enumerate(zip(("Alpha Corp", "Beta Corp"), urls)):
                record = ("alpha", "beta")[i]
                seed_review_job(record, "Senior Data Engineer", source=("linkedin", "lensa")[i],
                                description=DESC, apply_url=url)
                seed_verified_attempt(record, verified_posting_key_from_url(url),
                                      source=("linkedin", "lensa")[i], description=DESC)
                with database.get_connection() as connection:
                    connection.execute(
                        "UPDATE raw_jobs SET company_name=? WHERE record_key=?",
                        [company, record],
                    )
                    connection.execute(
                        "UPDATE job_enrichment_attempts SET official_job_url=? WHERE record_key=?",
                        [url, record],
                    )
            assert verified_posting_key_from_url(urls[0]) == verified_posting_key_from_url(urls[1])
            repo.update_application_status("alpha", "removed")
            rows = repo.load_all_jobs("resume-1", for_session_snapshot=True)
            by_id = {j["record_key"]: j for j in rows}
            self.assertEqual(by_id["alpha"]["application_status"], "removed")
            self.assertEqual(by_id["beta"]["application_status"], "new")
            self.assertEqual(len(repo.prepare_job_display(rows)), 2)
        with_temp_review_database(scenario)

    def test_latest_explicit_status_selects_one_card_without_rewriting_other_history(self):
        def scenario():
            self.seed_cross_source_opening()
            repo.update_application_status('linkedin-opening','applied')
            repo.update_application_status('lensa-opening','removed')
            with database.get_connection() as connection:
                connection.execute("UPDATE application_status SET updated_at=TIMESTAMPTZ '2000-01-01 00:00:00+00' WHERE record_key='linkedin-opening'")
                connection.execute("UPDATE application_status SET updated_at=TIMESTAMPTZ '2001-01-01 00:00:00+00' WHERE record_key='lensa-opening'")
            state=initialize_session(repo.load_all_jobs('resume-1',for_session_snapshot=True))
            physical={j['record_key']:j for j in state['jobs_snapshot']}
            self.assertEqual(physical['linkedin-opening']['application_status'],'applied')
            self.assertEqual(physical['linkedin-opening']['application_status_scope'],'exact')
            self.assertEqual(physical['lensa-opening']['application_status'],'removed')
            self.assertEqual(physical['lensa-opening']['application_status_scope'],'exact')
            cards=session.display_snapshot(state)
            self.assertEqual(len(cards),1)
            self.assertEqual(cards[0]['application_status'],'removed')
            self.assert_fresh(state)
            session.patch_status(state,'linkedin-opening','applied',repo.update_application_status)
            cards=session.display_snapshot(state)
            self.assertEqual(len(cards),1)
            self.assertEqual(cards[0]['application_status'],'applied')
            # The older exact removed history remains intact in its source row.
            lensa=next(j for j in state['jobs_snapshot'] if j['record_key']=='lensa-opening')
            self.assertEqual(lensa['application_status'],'removed')
            self.assertEqual(lensa['application_status_scope'],'exact')
            self.assert_fresh(state)
            with database.get_connection() as connection:
                self.assertEqual(connection.execute("SELECT status FROM application_status WHERE record_key='lensa-opening'").fetchone(),('removed',))
        with_temp_review_database(scenario)

    def test_third_party_key_no_cross_status(self):
        def scenario():
            self.seed()
            with database.get_connection() as c:
                c.execute("update job_enrichment_attempts set official_job_url='https://www.builtinsf.com/job/role/123' where record_key='shared'")
            state=initialize_session(repo.load_all_jobs('resume-1',for_session_snapshot=True))
            session.patch_status(state,'a','applied',repo.update_application_status);self.assert_fresh(state)
            self.assertEqual(next(j for j in state['jobs_snapshot'] if j['record_key']=='shared')['application_status'],'new')
        with_temp_review_database(scenario)
    def test_sidebar_membership_delta_matches_real_candidate_count(self):
        from src.ui.daily_workflow_service import load_scoring_backlog_status
        def scenario():
            self.seed()
            with database.get_connection() as c:
                c.execute("INSERT INTO job_matches (record_key,search_id,search_title,match_score,title_score,location_score,keyword_score,freshness_score,matched_keywords,excluded_keywords,reasons,is_recommended,needs_review) VALUES ('a','search','Data Engineer',90,90,100,90,80,'[]','[]','[]',true,false)")
            rows=repo.load_all_jobs('resume-1',for_session_snapshot=True)
            state=initialize_session(rows)
            baseline=load_scoring_backlog_status('resume-1',0)
            self.assertGreater(baseline.unscored_candidates,0)
            state['scoring_backlog_snapshot']['status']=baseline
            session.patch_status(state,'a','applied',repo.update_application_status)
            updated=state['scoring_backlog_snapshot']['status']
            fresh=load_scoring_backlog_status('resume-1',0)
            self.assertEqual(updated.unscored_candidates,fresh.unscored_candidates)
            self.assertEqual(updated.candidate_posting_keys,fresh.candidate_posting_keys)
            self.assertEqual(updated.cached_scores,fresh.cached_scores)
        with_temp_review_database(scenario)
    def test_non_status_tables_unchanged(self):
        def scenario():
            self.seed()
            with database.get_connection() as c:
                before={t:c.execute(f'select * from {t}').fetchall() for t in ('raw_jobs','resume_job_scores','job_enrichment_attempts')}
            state=initialize_session(repo.load_all_jobs('resume-1',for_session_snapshot=True))
            session.patch_status(state,'a','applied',repo.update_application_status)
            with database.get_connection() as c:
                for table,rows in before.items():self.assertEqual(rows,c.execute(f'select * from {table}').fetchall())
        with_temp_review_database(scenario)
    def test_invalid_and_missing_targets_safe(self):
        def scenario():
            self.assertRaises(ValueError,repo.update_application_status,'missing','bad')
            self.assertEqual(repo.update_application_status('missing','applied').record_keys,())
        with_temp_review_database(scenario)

if __name__=='__main__':unittest.main()
