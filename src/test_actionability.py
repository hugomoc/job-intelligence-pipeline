"""Offline actionability and unchanged fit/identity boundaries."""
import json,unittest
from datetime import datetime,timedelta,timezone
import duckdb,httpx
from src.actionability import Settings,check_job,effective_status,refresh,attach_cached
from src.ui.job_visibility import classify_job_review_bucket,filter_jobs_by_review_filter
from src.ui.job_links import select_job_open_target
NOW=datetime(2026,10,11,tzinfo=timezone.utc)
URL='https://job-boards.greenhouse.io/acme/jobs/123'
def job(**kw):
 d=dict(record_key='a',title='Data Engineer',company_name='Acme',source='greenhouse',application_status='new',official_url_status='FOUND_VERIFIED',official_job_url=URL,verified_posting_key='greenhouse:123',verified_posting_key_source='official_found_verified',verified_posting_key_trust='trusted',has_current_complete_ai_assessment=True,ai_score=93,recommendation='apply');d.update(kw);return d
def page(location='Remote - US',company='Acme',title='Data Engineer',ident='123',expiry=None,apply=True):
 node={'@type':'JobPosting','title':title,'hiringOrganization':{'name':company},'identifier':{'value':ident},'jobLocation':{'name':location},'description':'Responsibilities: You will build data pipelines. Requirements: Experience with Python and SQL. '*30}
 if expiry:node['validThrough']=expiry
 return '<script type="application/ld+json">'+json.dumps(node)+'</script><h1>'+title+'</h1><main>'+node['description']+'</main>'+('<form id="application"><button>Apply now</button></form>' if apply else '')
def client(html=None,code=200,final=None,error=None):
 def handler(request):
  if error:raise error('offline error',request=request)
  if final and str(request.url)!=final:return httpx.Response(302,headers={'location':final})
  return httpx.Response(code,text=html or '',headers={'content-type':'text/html'},request=httpx.Request('GET',final or str(request.url)))
 return httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=True)
def tagged(state='ACTIVE',**kw):
 d=job(actionability_status=state,actionability_checked_at=datetime.now(timezone.utc).isoformat(),actionability_posting_key='greenhouse:123',actionability_official_url=URL,actionability_candidate_context=Settings().context);d.update(kw);return d
class Tests(unittest.TestCase):
 def check(self,html=None,code=200,**kw):
  with client(html,code,**kw) as c:return check_job(job(),c,Settings(),NOW)
 def test_historical_aggregator_not_authority(self):
  with client(page()) as c:
   self.assertEqual(check_job(job(official_job_url='https://www.builtinsf.com/job/role/123',verified_posting_key='official-url:https://www.builtinsf.com/job/role/123'),c,Settings(),NOW).status,'UNKNOWN')
 def test_active_exact_ats(self):self.assertEqual(self.check(page()).status,'ACTIVE')
 def test_closed_200(self):self.assertEqual(self.check('<h1>This job has been removed</h1>').status,'INACTIVE')
 def test_closed_404_specific(self):self.assertEqual(self.check('<h1>Job not found</h1>',404).status,'INACTIVE')
 def test_404_alone_unknown(self):self.assertEqual(self.check('Not found',404).status,'UNKNOWN')
 def test_expired_jsonld(self):self.assertEqual(self.check(page(expiry='2026-10-01')).status,'INACTIVE')
 def test_timeout(self):self.assertEqual(self.check(error=httpx.ReadTimeout).status,'UNKNOWN')
 def test_network(self):self.assertEqual(self.check(error=httpx.ConnectError).status,'UNKNOWN')
 def test_bot(self):self.assertEqual(self.check('Verify you are human').status,'UNKNOWN')
 def test_403(self):self.assertEqual(self.check(page(),403).status,'UNKNOWN')
 def test_generic(self):self.assertEqual(self.check('<h1>Careers</h1><button>Apply</button>').status,'UNKNOWN')
 def test_redirect_search(self):self.assertEqual(self.check(page(),final='https://job-boards.greenhouse.io/acme').status,'UNKNOWN')
 def test_wrong_requisition(self):self.assertEqual(self.check(page(ident='456')).status,'UNKNOWN')
 def test_us(self):self.assertEqual(self.check(page('United States')).status,'ACTIVE')
 def test_europe(self):self.assertEqual(self.check(page('Europe only')).status,'LOCATION_MISMATCH')
 def test_canada(self):self.assertEqual(self.check(page('Canada')).status,'LOCATION_MISMATCH')
 def test_brazil(self):self.assertEqual(self.check(page('Brazil only')).status,'LOCATION_MISMATCH')
 def test_remote_unspecified(self):self.assertNotEqual(self.check(page('Remote')).status,'LOCATION_MISMATCH')
 def test_explicit_fetched_residency(self):self.assertEqual(self.check(page('Remote').replace('Responsibilities:', 'Candidates must be based in Europe. Responsibilities:')).status,'LOCATION_MISMATCH')
 def test_missing_location(self):self.assertNotEqual(self.check(page('')).status,'LOCATION_MISMATCH')
 def test_missing_apply(self):self.assertEqual(self.check(page(apply=False)).status,'UNKNOWN')
 def test_wrong_company(self):self.assertEqual(self.check(page(company='Other')).status,'UNKNOWN')
 def test_missing_company_requires_independent_proof(self):self.assertEqual(self.check(page(company='')).status,'UNKNOWN')
 def test_missing_company_exact_proof(self):
  with client(page(company='')) as c:self.assertEqual(check_job(job(verified_posting_key_source='direct_ats_source'),c,Settings(),NOW).status,'ACTIVE')
 def test_untrusted_no_fetch(self):
  with client(error=httpx.ConnectError) as c:self.assertEqual(check_job(job(verified_posting_key_trust=None),c,Settings(),NOW).reason,'Trusted exact official posting key unavailable')
 def test_fit_buckets(self):
  for status in ('INACTIVE','LOCATION_MISMATCH','UNKNOWN'):
   self.assertEqual(classify_job_review_bucket(tagged(status)),'needs_review')
  self.assertEqual(classify_job_review_bucket(tagged()),'recommended')
 def test_history(self):
  for status in ('applied','removed'):
   self.assertEqual(classify_job_review_bucket(tagged('INACTIVE',application_status=status)),'recommended')
 def test_ttl(self):
  self.assertEqual(effective_status(tagged(actionability_checked_at=(NOW-timedelta(hours=25)).isoformat()),Settings(),NOW),'UNKNOWN')
 def test_context_change(self):self.assertEqual(effective_status(tagged(),Settings(country='Canada')),'UNKNOWN')
 def test_key_change(self):self.assertEqual(effective_status(tagged(verified_posting_key='greenhouse:456')),'UNKNOWN')
 def test_url_change(self):self.assertEqual(effective_status(tagged(official_job_url=URL+'?different=1')),'UNKNOWN')
 def test_untrusted_cache(self):self.assertEqual(effective_status(tagged(verified_posting_key_trust=None)),'UNKNOWN')
 def test_inactive_link(self):self.assertIn('Inspect',select_job_open_target(tagged('INACTIVE')).label)
 def test_unknown_link(self):self.assertIn('unconfirmed',select_job_open_target(job()).note)
 def test_refresh_target_and_cache(self):
  db=duckdb.connect(':memory:');jobs=[job(),job(record_key='skip',recommendation='skip',ai_score=20),job(record_key='history',application_status='applied')]
  with client(page()) as c:
   m=refresh(jobs,db,c,Settings(),NOW);self.assertEqual(m['checks'],1)
   m=refresh(jobs,db,c,Settings(),NOW);self.assertEqual(m['checks'],0);self.assertEqual(m['cache_hits'],1)
  self.assertEqual(db.execute('select count(*) from job_actionability').fetchone()[0],1);db.close()
 def test_untrusted_unknown_does_not_starve_later_targets(self):
  db=duckdb.connect(':memory:');untrusted=job(verified_posting_key_trust=None)
  with client(page()) as c:
   self.assertEqual(refresh([untrusted],db,c,Settings(),NOW)['checks'],1)
   m=refresh([untrusted,job(record_key='trusted')],db,c,Settings(batch_size=1),NOW)
   self.assertEqual(m['cache_hits'],1);self.assertEqual(m['ACTIVE'],1)
  db.close()
 def test_unknown_cache(self):
  db=duckdb.connect(':memory:')
  with client(code=403) as c:
   self.assertEqual(refresh([job()],db,c,Settings(),NOW)['checks'],1)
   self.assertEqual(refresh([job()],db,c,Settings(),NOW)['cache_hits'],1)
  db.close()
 def test_scope_no_cross_record_reuse(self):
  db=duckdb.connect(':memory:')
  with client(page()) as c:refresh([job()],db,c,Settings(),NOW)
  other=job(record_key='other');attach_cached([other],db);self.assertNotIn('actionability_status',other);db.close()
 def test_batch_cap(self):
  db=duckdb.connect(':memory:')
  with client(page()) as c:m=refresh([job(record_key=str(n)) for n in range(4)],db,c,Settings(batch_size=2),NOW)
  self.assertEqual(m['checks'],2);self.assertEqual(m['deferred'],2);db.close()
class SafetyReview(unittest.TestCase):
 def test_builtin_shared_classification_and_no_key(self):
  from src.enrichment.official_job_resolver import is_aggregator_domain, is_aggregator_job, looks_like_official_or_ats_url
  from src.verified_posting_identity import verified_posting_key_from_url, is_known_official_or_ats_url
  for url in ('https://www.builtinsf.com/job/role/123', 'https://builtin.com/job/role/123'):
   self.assertTrue(is_aggregator_domain(url));self.assertFalse(looks_like_official_or_ats_url(url, "Super.com"))
   self.assertEqual(verified_posting_key_from_url(url),'');self.assertFalse(is_known_official_or_ats_url(url))
  self.assertFalse(is_aggregator_domain('https://builtinsf.com.example.org/job/123'))
  self.assertTrue(is_aggregator_job({'source':'builtin'}))
 def test_legacy_builtin_trust_not_reused(self):
  from src.enrich_jobs import authoritative_verified_key_for_job
  from src.repositories.recommendation_repository import register_exact_posting_identity_function
  url='https://www.builtinsf.com/job/role/123';key='official-url:'+url
  self.assertFalse(authoritative_verified_key_for_job(job(official_job_url=url,verified_posting_key=key)))
  db=duckdb.connect(':memory:');register_exact_posting_identity_function(db)
  self.assertFalse(db.execute('select posting_key_can_be_trusted(?)',[key]).fetchone()[0])
  self.assertTrue(db.execute("select posting_key_can_be_trusted('greenhouse:123')").fetchone()[0]);db.close()
 def test_legacy_builtin_key_cannot_share_personal_status(self):
  from src.test_aggregator_visibility import with_temp_database, seed_job, seed_enrichment_attempt
  from src import database
  from src.repositories.recommendation_repository import load_all_jobs
  url='https://www.builtinsf.com/job/senior-analytics-engineer/7239437'
  def scenario():
   for key in ('legacy-applied','distinct-new'):
    seed_job(key,'builtin',apply_url='https://builtin.com/job/role/'+key)
    seed_enrichment_attempt(key,'builtin','FOUND_VERIFIED',url)
   with database.get_connection() as connection:
    connection.execute("UPDATE job_enrichment_attempts SET verified_posting_key=?, verified_posting_key_source='official_found_verified', verified_posting_key_trust='trusted'",['official-url:'+url])
    connection.execute("INSERT INTO application_status (record_key,status) VALUES ('legacy-applied','applied')")
   loaded={j['record_key']:j for j in load_all_jobs(resume_hash='resume')}
   self.assertEqual(loaded['legacy-applied']['application_status'],'applied')
   self.assertEqual(loaded['distinct-new']['application_status'],'new')
  with_temp_database(scenario)
  # Even a misleading stored ATS-style key cannot make a third-party URL official.
  original=url
  def forged_scenario():
   for key in ('legacy-applied','distinct-new'):
    seed_job(key,'builtin',apply_url='https://builtin.com/job/role/'+key)
    seed_enrichment_attempt(key,'builtin','FOUND_VERIFIED',original)
   with database.get_connection() as connection:
    connection.execute("UPDATE job_enrichment_attempts SET verified_posting_key='greenhouse:123', verified_posting_key_source='official_found_verified', verified_posting_key_trust='trusted'")
    connection.execute("INSERT INTO application_status (record_key,status) VALUES ('legacy-applied','applied')")
   loaded={j['record_key']:j for j in load_all_jobs(resume_hash='resume')}
   self.assertEqual(loaded['distinct-new']['application_status'],'new')
  with_temp_database(forged_scenario)
 def test_legacy_builtin_safe_visible_unknown(self):
  from src.ui.job_visibility import is_user_reviewable_job
  j=tagged(official_job_url='https://www.builtinsf.com/job/role/123',source='builtin')
  self.assertTrue(is_user_reviewable_job(j));self.assertEqual(classify_job_review_bucket(j),'needs_review')
  self.assertIn('third-party',select_job_open_target(j).label)
  for status in ('applied','removed'):
   self.assertTrue(is_user_reviewable_job(job(source='builtin',application_status=status,official_url_status=None)))
 def test_batches_converge_with_unknowns_and_priority(self):
  db=duckdb.connect(':memory:');jobs=[job(record_key=str(i),ai_score=70+i) for i in range(55)]
  with client(code=403) as c:
   first=refresh(jobs,db,c,Settings(batch_size=25),NOW)
   self.assertEqual(first['checks'],25)
   checked={r[0] for r in db.execute('select record_key from job_actionability').fetchall()}
   self.assertEqual(checked,{str(i) for i in range(30,55)})
   self.assertEqual(refresh(jobs,db,c,Settings(batch_size=25),NOW)['checks'],25)
   self.assertEqual(refresh(jobs,db,c,Settings(batch_size=25),NOW)['checks'],5)
   self.assertEqual(refresh(jobs,db,c,Settings(batch_size=25),NOW)['checks'],0)
  db.close()
 def test_render_trigger_structure(self):
  # No execution of Streamlit or real workflows: check the network call is
  # dominated by an explicit trigger, not by filter/pagination rendering.
  import ast
  from pathlib import Path
  tree=ast.parse(Path('streamlit_app.py').read_text())
  calls=[]
  def visit(node,guards):
   if isinstance(node,ast.If):
    for child in node.body:visit(child,guards+[ast.unparse(node.test)])
    for child in node.orelse:visit(child,guards)
    return
   if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='refresh_daily':calls.append(guards)
   for child in ast.iter_child_nodes(node):visit(child,guards)
  visit(tree,[])
  self.assertEqual(len(calls),1)
  self.assertTrue(any('actionability_requested or check_current_postings_clicked'==g for g in calls[0]))
if __name__=='__main__':unittest.main()
