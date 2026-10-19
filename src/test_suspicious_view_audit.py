"""Offline regression coverage for audit/filter/scoring-state consistency."""
import ast
import unittest
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from src.ai.resume_matcher import MATCHER_PROMPT_VERSION,get_description_information,build_resume_job_match,requirement_is_demonstrated
from src.test_resume_matcher_validation import make_profile,make_analysis
from src.description_quality import assess_description_quality
from src.repositories.recommendation_repository import has_current_complete_ai_score
from src.ui.job_pipeline_state import with_scoring_queue_membership,derive_job_pipeline_state
from src.ui.job_visibility import filter_jobs_by_review_filter,count_job_review_buckets

JD=('Responsibilities include building reliable SQL analytics pipelines and data models. '
    'Qualifications include SQL expertise and collaboration with business teams. ')*20

class AuditTests(unittest.TestCase):
    def test_actual_streamlit_filter_nodes_reproduce_expected_fixture(self):
        m=ast.parse(Path('streamlit_app.py').read_text())
        fn=next(n for n in m.body if isinstance(n,ast.FunctionDef) and n.name=='to_date')
        titles=next(n for n in m.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='TITLE_FIT_LABELS' for t in n.targets))
        scope={'date':date};exec(compile(ast.Module(body=[fn,titles],type_ignores=[]),'streamlit_app.py','exec'),scope)
        fixture=[dict(record_key=str(i),application_status='new',sent_at=datetime(2026,10,6+i%7,tzinfo=timezone.utc),title_classification=('STRONG_MATCH','POSSIBLE_MATCH','FILTERED_OUT')[i%3]) for i in range(67)]
        extra=[dict(record_key='applied',application_status='applied',sent_at=date(2026,10,8)),dict(record_key='old',application_status='new',sent_at=datetime(2026,10,5,tzinfo=timezone.utc)),dict(record_key='future',application_status='new',sent_at=datetime(2026,10,13,tzinfo=timezone.utc))]
        scope.update(all_jobs=fixture+extra,status_filter='New',source_filter='All',city_filter='All',start_date=date(2026,10,6),end_date=date(2026,10,12),scored_filter='All',recommendation_filter='All',fit_priority_filter='All',description_state_filter='All',selected_title_fits=['Strong','Possible','Filtered out'],review_visibility_filter='All',filter_jobs_by_review_filter=filter_jobs_by_review_filter,count_job_review_buckets=count_job_review_buckets)
        w=next(n for n in m.body if isinstance(n,ast.With) and any(isinstance(x.context_expr,ast.Name) and x.context_expr.id=='jobs_tab' for x in n.items))
        started=False
        for node in w.body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='filtered_jobs' for t in node.targets): started=True
            if not started:continue
            exec(compile(ast.Module(body=[node],type_ignores=[]),'streamlit_app.py','exec'),scope)
            if isinstance(node,ast.Assign) and isinstance(node.value,ast.Call) and isinstance(node.value.func,ast.Name) and node.value.func.id=='filter_jobs_by_review_filter':break
        self.assertEqual([j['record_key'] for j in scope['filtered_jobs']],[str(i) for i in range(67)])

    def test_awaiting_requires_actual_cached_queue_membership(self):
        j=dict(exact_posting_key='exact:1',description_state='FULL_JD',application_status='new',ai_score=None)
        self.assertEqual(derive_job_pipeline_state(with_scoring_queue_membership(j,{'exact:1'})).label,'Awaiting AI scoring')
        self.assertEqual(derive_job_pipeline_state(with_scoring_queue_membership(j,set())).label,'Not eligible for AI scoring')
        for status in ('applied','removed'):
            self.assertNotEqual(derive_job_pipeline_state(with_scoring_queue_membership(dict(j,application_status=status),{'exact:1'})).label,'Awaiting AI scoring')

    def test_matcher_shared_quality_parity(self):
        for text in (JD,"What you'll work on\n"+JD, 'navigation chrome '*200,'Requirements SQL'):
            self.assertEqual(get_description_information({'description':text})[2],assess_description_quality(text).full_jd)
        self.assertFalse(get_description_information({'description':JD,'enrichment_status':'resolution_rejected'})[2])

    def test_real_hard_gap_caps_75_preferred_does_not(self):
        p=make_profile()
        job=dict(record_key='test',title='Data Engineer',description='Required certification: specialized XYZ certification. '+JD)
        hard=build_resume_job_match(p,job,'test',make_analysis(['specialized XYZ certification'],score=75),len(JD.split()),True)
        preferred=build_resume_job_match(p,job,'test',make_analysis([],['specialized XYZ certification'],score=75),len(JD.split()),True)
        self.assertEqual(hard.overall_score,49);self.assertEqual(preferred.overall_score,75)

    def test_aws_alternative_satisfied(self):
        p=make_profile(cloud_platforms=['AWS'])
        self.assertTrue(requirement_is_demonstrated('AWS, Azure, or GCP',p))

    def test_relevant_years_minimum_and_specialist_years(self):
        p=make_profile().model_copy(update={'years_of_relevant_experience':12})
        self.assertTrue(requirement_is_demonstrated('5+ years data engineering experience',p))
        self.assertFalse(requirement_is_demonstrated('5 years Kubernetes production experience',p))
        self.assertFalse(requirement_is_demonstrated('15 years data engineering experience',p))

    def test_explicit_warehouse_equivalent_satisfied(self):
        self.assertTrue(requirement_is_demonstrated('Snowflake or equivalent',make_profile(databases_warehouses=['BigQuery'])))

    def test_freshness_and_partial_transition(self):
        now=datetime.now(timezone.utc)
        j=dict(ai_score=75,ai_prompt_version=MATCHER_PROMPT_VERSION,description_complete=True,raw_description_word_count=200,description_state='FULL_JD',ai_scored_at=now,description_updated_at=now-timedelta(days=1))
        self.assertTrue(has_current_complete_ai_score(j))
        self.assertFalse(has_current_complete_ai_score(dict(j,description_updated_at=now+timedelta(seconds=1))))
        self.assertFalse(has_current_complete_ai_score(dict(j,description_complete=False)))
        self.assertFalse(has_current_complete_ai_score(dict(j,ai_prompt_version='v1')))

    def test_slash_separated_looker_alias_is_demonstrated(self):
        p=make_profile(tools_platforms=['Looker','LookML'])
        self.assertTrue(requirement_is_demonstrated('Deep hands-on Looker/LookML experience',p))

    def test_not_required_does_not_create_mandatory_gap(self):
        from src.ai.resume_matcher import detect_critical_requirement_gaps
        text=('Domo is helpful but not required. Responsibilities include SQL reporting. ')*20
        gaps,_=detect_critical_requirement_gaps(make_profile(tools_platforms=['Looker']),{'title':'Analytics Engineer'},text)
        self.assertFalse(any(g.severity=='mandatory' and 'Domo' in g.label for g in gaps))

    def test_real_cache_rejects_description_update_and_incomplete_transition(self):
        from src.test_enrich_jobs_official_retry import insert_raw_job
        from src.score_jobs_ai import load_cached_job_score,initialize_ai_tables,save_job_score
        from src import database
        def scenario():
            initialize_ai_tables()
            from src.enrich_jobs import initialize_enrichment_tables
            initialize_enrichment_tables()
            insert_raw_job('cache-test',description=JD)
            match=build_resume_job_match(make_profile(),dict(record_key='cache-test',title='Data Engineer',description=JD),'test-model',make_analysis([],score=75),len(JD.split()),True)
            save_job_score('test-resume',match)
            self.assertIsNotNone(load_cached_job_score('test-resume','cache-test','test-model'))
            with database.get_connection() as c:
                c.execute("UPDATE raw_jobs SET description_updated_at=CURRENT_TIMESTAMP+INTERVAL '1 day' WHERE record_key='cache-test'")
            self.assertIsNone(load_cached_job_score('test-resume','cache-test','test-model'))
            with database.get_connection() as c:
                c.execute("UPDATE raw_jobs SET description_updated_at=NULL,description='navigation chrome' WHERE record_key='cache-test'")
            self.assertIsNone(load_cached_job_score('test-resume','cache-test','test-model'))
            with database.get_connection() as c:
                c.execute("UPDATE raw_jobs SET description=? WHERE record_key='cache-test'",[JD])
                c.execute("UPDATE resume_job_scores SET description_complete=false WHERE record_key='cache-test'")
            self.assertIsNone(load_cached_job_score('test-resume','cache-test','test-model'))
        from tempfile import TemporaryDirectory
        old_dir,old_path=database.DATA_DIR,database.DATABASE_PATH
        with TemporaryDirectory() as directory:
            database.DATA_DIR=Path(directory);database.DATABASE_PATH=Path(directory)/'jobs.duckdb'
            try: scenario()
            finally: database.DATA_DIR,database.DATABASE_PATH=old_dir,old_path

    def test_real_loader_backlog_and_labels_agree_for_new_full_jd(self):
        from src.test_recommendation_repository import with_temp_review_database,seed_job,seed_resume_profile
        from src.score_jobs_ai import initialize_ai_tables
        from src.enrich_jobs import initialize_enrichment_tables
        from src.repositories.recommendation_repository import load_candidate_jobs
        from src.ui.daily_workflow_service import load_scoring_backlog_status
        from src import database
        def scenario():
            initialize_ai_tables();initialize_enrichment_tables();seed_resume_profile()
            eligible_jd='Responsibilities include Python SQL Snowflake AWS dbt data pipelines and analytics engineering experience. '*20
            for key in ('pending','applied','removed'):
                seed_job(key,eligible_jd,title='Data Engineer',source='linkedin')
            with database.get_connection() as c:
                c.execute("INSERT INTO application_status (record_key,status) VALUES ('applied','applied'),('removed','removed')")
            candidates=load_candidate_jobs('resume-1','model-1',100,0,reuse_any_model=True)
            self.assertEqual([j['record_key'] for j in candidates],['pending'])
            backlog=load_scoring_backlog_status('resume-1',0)
            self.assertEqual(backlog.unscored_candidates,1)
            candidate=candidates[0]
            self.assertEqual(derive_job_pipeline_state(with_scoring_queue_membership(candidate,backlog.candidate_posting_keys)).label,'Awaiting AI scoring')
        with_temp_review_database(scenario)

    def test_incomplete_jd_does_not_reach_api_clients(self):
        from unittest.mock import patch
        from src.ai import resume_matcher as matcher
        job=dict(record_key='partial',description='navigation chrome '*200)
        with patch.object(matcher,'get_gemini_client') as client:
            with self.assertRaises(matcher.ResumeMatcherError): matcher.score_resume_against_job(make_profile(),job)
            client.assert_not_called()
        with patch.dict('os.environ',{'OPENAI_API_KEY':'offline-placeholder'}),patch('openai.OpenAI') as client:
            with self.assertRaises(matcher.ResumeMatcherError): matcher.score_resume_against_job_openai(make_profile(),job)
            client.assert_not_called()

class ClausePolicyTests(unittest.TestCase):
    def check_clause(self, clause, gap, demonstrated, expected):
        from src.ai.resume_matcher import validate_hard_gap_clause
        profile = make_profile().model_copy(update={"programming_languages": [], "production_skills": demonstrated, "project_skills": [], "tools_platforms": [], "cloud_platforms": [], "databases_warehouses": []})
        result = validate_hard_gap_clause(gap, "Requirements\n" + clause, profile)
        self.assertEqual(result["keep_hard_gap"], expected, result)
        return result

    def test_java_python_present(self): self.check_clause("Expert skills in Java or Python", "Java", ["Python"], False)
    def test_java_python_absent(self): self.check_clause("Expert skills in Java or Python", "Java", [], True)
    def test_warehouse_snowflake(self): self.check_clause("Familiarity with columnar data warehouses such as ClickHouse, Snowflake, BigQuery, or Redshift", "ClickHouse", ["Snowflake"], False)
    def test_warehouse_bigquery(self): self.check_clause("Familiarity with columnar data warehouses such as ClickHouse, Snowflake, BigQuery, or Redshift", "ClickHouse", ["BigQuery"], False)
    def test_warehouse_absent(self): self.check_clause("Familiarity with columnar data warehouses such as ClickHouse, Snowflake, BigQuery, or Redshift", "ClickHouse", [], True)
    def test_comparable_oltp(self):
        result = self.check_clause("Postgres or comparable relational OLTP systems", "Postgres or comparable OLTP", ["SQL Server", "MySQL"], False)
        self.assertIn("sql_server", result["clauses"][0]["resume_evidence"])
    def test_equivalent_cdc(self): self.check_clause("Debezium, Kafka Connect, or similar", "Debezium", ["Production change data capture implementation"], False)
    def test_exact_qlik_remains(self): self.check_clause("3 years using QLIK Advana", "QLIK Advana", [], True)
    def test_preferred(self): self.check_clause("Databricks preferred", "Databricks", [], False)
    def test_optional_domo(self): self.check_clause("Domo helpful but not required", "Domo", [], False)
    def test_source_examples(self): self.check_clause("Integrate financial source systems, examples including NetSuite, Ramp, Pigment", "NetSuite", ["Ramp"], False)
    def test_unmapped_vendor_examples_need_review(self):
        result=self.check_clause("Experience with source-of-truth systems such as ERP (NetSuite), WMS, TMS (OSCA), or CPM (Pigment)", "NetSuite, Ramp, Pigment", [], False)
        self.assertIn("needs review", result["reason"])
    def test_all_vendors_explicit_remains_hard(self): self.check_clause("All source-of-truth systems such as ERP NetSuite and CPM Pigment are required", "NetSuite", [], True)
    def test_ideally_not_mandatory_vendor(self): self.check_clause("Practical experience with AI-assisted analytics, ideally Databricks Genie", "Databricks", [], False)
    def test_single_salesforce(self): self.check_clause("Expert knowledge of Salesforce data model", "Salesforce data model", [], True)
    def test_slash_alternatives(self): self.check_clause("Required Java/Python experience", "Java", ["Python"], False)
    def test_conjunction_parentheses_not_or(self): self.check_clause("Snowflake and BigQuery required (production)", "BigQuery", ["Snowflake"], True)
    def test_warehouse_cloud_is_not_warehouse(self): self.check_clause("ClickHouse, BigQuery, or Redshift required", "ClickHouse", ["AWS"], True)
    def test_separate_mandatory_not_rescued(self): self.check_clause("Java required. Python preferred.", "Java", ["Python"], True)
    def test_ambiguous_becomes_risk(self):
        from src.ai.resume_matcher import validate_missing_qualifications
        a=validate_missing_qualifications(make_analysis(["Salesforce"]), make_profile(), "We use Salesforce in our business.")
        self.assertEqual(a.hard_requirements_missing, [])
        self.assertTrue(any("Unconfirmed" in r for r in a.risk_factors))

if __name__=='__main__': unittest.main()
