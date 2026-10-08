"""Deterministic regression tests for post-drain selection and safe execution."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from src.enrichment.official_job_resolver import search_candidates
from src.ui import daily_workflow_service as daily
from src.targeted_enrichment import first_attempt, targeted_retry, select_targeted_retries
from src.repositories.recommendation_repository import calculate_enrichment_priority
from src.ui.job_visibility import is_visible_for_review_filter
from src.drain_scoring_backlog import drain_scores
from src.retry_targeted_enrichment import probe_search, load_targeted_queue
from src import retry_targeted_enrichment as retry_module
from src.ai.resume_matcher import ResumeMatcherError


def job(**kw):
    j=dict(record_key='new',exact_posting_key='linkedin|1',canonical_job_key='linkedin|1',
           title='Data Engineer',company_name='Company',application_status='new',
           title_classification='STRONG_MATCH',raw_description_word_count=0,
           description=None,attempt_count=0,previous_status=None,critical_skill_gaps=[],
           admission_decision='exclude',ai_score=None,official_url_status='SEARCH_DEFERRED',
           discovered_at=datetime.now(timezone.utc),email_date=None)
    j.update(kw);return j


def test_first_attempt_and_priority():
    fresh=job();old=job(attempt_count=9,previous_status='blocked',discovered_at=datetime.now(timezone.utc)-timedelta(days=120))
    assert first_attempt(fresh) and not first_attempt(old)
    p=calculate_enrichment_priority(fresh)
    assert 'exclude penalty' not in p.reason
    assert calculate_enrichment_priority(job(title_classification='FILTERED_OUT')).score < p.score
    assert 'exclude penalty' in calculate_enrichment_priority(job(critical_skill_gaps=['SAP'])).reason


def test_daily_first_attempt_precedes_old_retries():
    fresh = job(record_key='fresh',rule_score=0)
    old = job(record_key='old',attempt_count=3,previous_status='blocked',rule_score=100)
    observed=[]
    summary=SimpleNamespace(jobs_processed=1,descriptions_updated=0,
        totals=dict(enriched=0,blocked=1,no_description=0,fetch_error=0,invalid_url=0,not_improved=0),
        stopped_for_time_budget=False,elapsed_seconds=0,log_lines=())
    def process(**kw):observed.extend(kw['jobs']);return summary
    with patch.object(daily,'load_jobs_to_enrich',return_value=[old,fresh]), \
         patch.object(daily,'create_http_client'), \
         patch.object(daily,'process_enrichment_batch',side_effect=process):
        daily.run_description_enrichment(limit=1,retry_failed=True,resume_hash="resume")
    assert [j['record_key'] for j in observed]==['fresh']


def test_targeted_retries():
    assert targeted_retry(job(attempt_count=1))
    for title in ['Accountant','Product Manager','Software Engineer','Data Scientist','AI Trainer']:
        assert not targeted_retry(job(title=title))
    for changed in [dict(application_status='removed'),dict(application_status='applied'),
                    dict(raw_description_word_count=100,description='requirements '+'SQL '*100),
                    dict(ai_score=75),dict(critical_skill_gaps=['SAP']),dict(official_url_status='OFFICIAL_NOT_FOUND')]:
        assert not targeted_retry(job(**changed))
    a=job();b=job(record_key='other',exact_posting_key='linkedin|2',duplicate_fingerprint='same')
    assert len(select_targeted_retries([a,b]))==2
    assert len(select_targeted_retries([a,job(record_key='same-exact')]))==1


def test_targeted_retry_repeats_source_before_official_fallback():
    missing = job(attempt_count=1, previous_status='blocked', needs_description_enrichment=False)
    with patch.object(retry_module, 'get_connection') as connection, \
         patch.object(retry_module, 'load_jobs_to_enrich', return_value=[missing]):
        connection.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = [1]
        selected = load_targeted_queue('resume')
    assert len(selected) == 1 and selected[0]['needs_description_enrichment'] is True


def test_review_filters():
    j=job(has_current_complete_ai_assessment=False)
    assert is_visible_for_review_filter(j,'Needs review')
    assert not is_visible_for_review_filter(j,'Recommended')
    assert is_visible_for_review_filter(j,'All')
    assert not is_visible_for_review_filter(job(application_status='applied',has_current_complete_ai_assessment=False),'Recommended')
    assert not is_visible_for_review_filter(job(title='AI Trainer',has_current_complete_ai_assessment=False),'Needs review')
    assert is_visible_for_review_filter(job(title='AI Trainer',has_current_complete_ai_assessment=False),'Low fit')


def test_current_cache_and_stale_scoring():
    queue=[job(),job(record_key='verified-peer',canonical_job_key='linkedin|peer')];saved=[]
    def load():return queue[:]
    def save(**kw):saved.append(kw);queue.clear()
    report=drain_scores('resume',loader=load,profile={'target_roles':['Data Engineer']},
                        score=lambda **kw:SimpleNamespace(overall_score=75),save=save,delay=0,emit=lambda s:None)
    assert report['scored']==1 and report['cached_reused']==1 and report['remaining_eligible_unscored']==0
    report=drain_scores('resume',loader=load,profile={'target_roles':['Data Engineer']},
                        score=lambda **kw: (_ for _ in ()).throw(AssertionError('cached score regenerated')),delay=0,emit=lambda s:None)
    assert report['scored']==0 and len(saved)==1


def test_quota_and_probe_safety():
    def quota(**kw):raise ResumeMatcherError('429 RESOURCE_EXHAUSTED quota exceeded')
    r=drain_scores('resume',loader=lambda:[job()],profile={'target_roles':['Data Engineer']},score=quota,
                   save=lambda **kw: (_ for _ in ()).throw(AssertionError()),delay=0,emit=lambda s:None)
    assert r['scored']==0 and r['stop_reason'] and r['selected']==1
    calls=[]
    def search(j,c):calls.append(j);raise TimeoutError('provider unavailable')
    healthy,probes=probe_search([job(),job(record_key='second')],None,search=search)
    assert not healthy and len(calls)==1 and len(probes)==1


def test_public_search_page_health_and_challenge():
    response = '<html><a class="result__a" href="https://unrelated.example/jobs">result</a></html>'
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,text=response))) as client:
        healthy,probes = probe_search([job(company_name='Acme')],client)
    assert healthy and probes[0]['valid_results_page'] and probes[0]['candidates']==0
    response = '<form id="challenge-form">Bots use DuckDuckGo too</form>'
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,text=response))) as client:
        healthy,probes = probe_search([job(company_name='Acme')],client)
    assert not healthy and 'bot challenge' in probes[0]['error']


def main():
    test_first_attempt_and_priority();test_daily_first_attempt_precedes_old_retries();test_targeted_retries();test_targeted_retry_repeats_source_before_official_fallback();test_review_filters()
    test_current_cache_and_stale_scoring();test_quota_and_probe_safety();test_public_search_page_health_and_challenge()
    print('Targeted cleanup regression tests passed.')

if __name__=='__main__':main()
