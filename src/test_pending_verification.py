"""Offline transparent-discovery and search diagnostic regressions."""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from src.enrichment import official_job_resolver as resolver
from src.enrichment import ats
from src.enrichment.public_search import SearchResponse
from src.ui.pending_verification import pending_verification_rows, render_pending_verification

NOW = datetime(2026, 11, 19, 18, tzinfo=timezone.utc)


def snapshot_row(key='pending', **kwargs):
    job = dict(record_key=key, exact_posting_key='glassdoor|'+key,
        title='Data Engineer', company_name='Acme', location='Remote - US', source='glassdoor',
        apply_url='https://glassdoor.com/job-listing/engineer?jobListingId='+key,
        discovered_at=NOW-timedelta(days=1), first_discovered_at=NOW-timedelta(days=1),
        raw_description_word_count=0, description_quality_signals=False,
        description_state='NEEDS_ENRICHMENT', application_status='new',
        enrichment_status='blocked', enrichment_attempt_count=2, enrichment_attempted_at=NOW,
        official_url_status='OFFICIAL_NOT_FOUND', official_url_resolved_at=NOW,
        official_url_validation_reason='no official or known ATS candidates found',
        ai_score=None, has_current_complete_ai_assessment=False,
        _snapshot_new_source_visible=False, _snapshot_new_admitted=True)
    job.update(kwargs)
    return job


def test_pending_includes_nineteen_cooldown_records_without_dispatch():
    snapshot = [snapshot_row(str(n)) for n in range(19)]
    with patch.object(resolver,'search_candidates',side_effect=AssertionError('No search allowed')):
        rows = pending_verification_rows(snapshot, [], now=NOW)
    assert len(rows) == 19
    assert all(row['next_retry'] == NOW+timedelta(days=7) for row in rows)
    assert all('cooldown' in row['blocked_reason'] for row in rows)
    assert all(not any(key in row for key in ('ai_score','recommendation','actionability_status')) for row in rows)


def test_pending_respects_discovery_relevance_visibility_and_history():
    rows = [snapshot_row(), snapshot_row('old',first_discovered_at=NOW-timedelta(days=6)),
            snapshot_row('removed',application_status='removed'), snapshot_row('applied',application_status='applied'),
            snapshot_row('nurse',title='Registered Nurse'), snapshot_row('gap',critical_skill_gaps=['SAP']),
            snapshot_row('visible',source='linkedin',apply_url='https://www.linkedin.com/jobs/view/1/')]
    assert [r['record_key'] for r in pending_verification_rows(rows,[rows[-1]],now=NOW)] == ['pending']


def test_pending_full_jd_keeps_verification_separate_and_omits_scores():
    full = snapshot_row(raw_description_word_count=120,description_quality_signals=True,
        description_state='FULL_JD',has_current_complete_ai_assessment=True,ai_score=95,recommendation='apply')
    row = pending_verification_rows([full],[],now=NOW)[0]
    assert row['description_state'] == 'FULL_JD' and 'ai_score' not in row


def test_pending_renderer_is_unverified_and_has_no_scoring_or_apply_controls():
    from streamlit.testing.v1 import AppTest
    rows = pending_verification_rows([snapshot_row()],[],now=NOW)
    with patch('src.ui.pending_verification.pending_verification_rows',return_value=rows):
        app = AppTest.from_string("""
import streamlit as st
from src.ui.pending_verification import pending_verification_rows, render_pending_verification
render_pending_verification(st, pending_verification_rows([], []))
""")
        app.run()
    assert not app.exception
    assert any('unconfirmed' in info.value for info in app.info)
    text=' '.join(item.value for item in app.markdown)
    assert 'OFFICIAL_NOT_FOUND' in text and 'Next retry:' in text
    assert '95' not in text and 'APPLY' not in text
    assert not app.button


def test_pending_sql_first_discovery_preserves_old_alias_exclusion():
    from src.test_recommendation_repository import with_temp_review_database,seed_review_job
    from src import database
    from src.repositories.recommendation_repository import load_all_jobs
    def scenario():
        for key in ('older','newer'):
            seed_review_job(key,'Data Engineer',source='glassdoor',apply_url='https://glassdoor.com/jobs?jobListingId=123')
        with database.get_connection() as c:
            c.execute('UPDATE raw_jobs SET discovered_at=? WHERE record_key=?',[NOW-timedelta(days=10),'older'])
            c.execute('UPDATE raw_jobs SET discovered_at=? WHERE record_key=?',[NOW,'newer'])
        snapshot=load_all_jobs(resume_hash='unregistered',for_session_snapshot=True)
        assert len(snapshot)==1 and snapshot[0]['first_discovered_at']==NOW-timedelta(days=10)
        assert pending_verification_rows(snapshot,[],now=NOW)==[]
    with_temp_review_database(scenario)


def test_future_search_diagnostics_show_each_url_reason_and_remove_credentials(caplog):
    urls = ('https://jobs.ashbyhq.com/acme/specific-id',
            'https://careers.acme.com/en-us/details/123',
            'https://glassdoor.com/job-listing/role?jobListingId=123',
            'https://another.example/jobs/123',
            'https://user:CANARY_SECRET@example.com/jobs/123?api_key=CANARY_SECRET',
            'https://careers.acme.com/jobs/CANARY%5FSECRET')
    def handle(request):
        assert request.url.host=='api.tavily.com'
        return httpx.Response(200,json={'results':[{'url':u,'title':'CANARY_SECRET'} for u in urls]})
    job=dict(title='Data Engineer',company_name='Acme')
    with patch.dict(os.environ,{'TAVILY_API_KEY':'CANARY_SECRET','SERPER_API_KEY':'','PUBLIC_SEARCH_PROVIDER':''}), \
         httpx.Client(transport=httpx.MockTransport(handle)) as client, \
         caplog.at_level(logging.INFO,logger=resolver.__name__):
        resolver.search_candidates(job,client)
    events=job['public_search_diagnostics']
    assert len(events)==1 and len(events[0]['returned_urls'])==6
    reasons={r['reason'] for r in events[0]['returned_urls']}
    assert {'known_ats_discovery_only','unsupported_posting_path','third_party_domain','unrecognized_ats_or_employer_domain'} <= reasons
    safe=json.dumps(events)+caplog.text
    assert 'CANARY_SECRET' not in safe and 'CANARY%5FSECRET' not in safe
    assert 'user:' not in safe and 'api_key=' not in safe
    assert 'specific-id' in safe


def test_negative_ats_discovery_does_not_prevent_a_fresh_probe():
    adapter=SimpleNamespace(discover_candidates=Mock(side_effect=[[],[object()]]))
    with patch.dict(ats.ATS_DISCOVERY_CACHE,{},clear=True),patch.object(ats,'ADAPTERS',(adapter,)):
        job=dict(title='Data Engineer',company_name='Acme')
        assert ats.discover_ats_candidates(job,object())==[]
        recovered = ats.discover_ats_candidates(job,object())
        assert len(recovered)==1
        assert ats.discover_ats_candidates(job,object())==recovered
        assert adapter.discover_candidates.call_count==2


def test_existing_discovery_blind_spots_remain_explicit_without_acceptance_changes():
    assert resolver.discovery_url_decision('https://careers.acme.com/en-us/details/123/data-engineer','Acme')==(False,'unsupported_posting_path')
    assert resolver.discovery_url_decision('https://provider.example/job/123','Acme')==(False,'unrecognized_ats_or_employer_domain')
    assert resolver.discovery_url_decision('https://boards.greenhouse.io/embed/job_app?for=acme&token=123','Acme')==(False,'unsupported_posting_path')


def test_error_and_cached_searches_are_logged_without_another_request():
    calls=[]
    with patch.dict(os.environ,{'TAVILY_API_KEY':'offline','SERPER_API_KEY':'','PUBLIC_SEARCH_PROVIDER':''}), \
         httpx.Client(transport=httpx.MockTransport(lambda r:(calls.append(r),httpx.Response(200,json={'results':[]}))[1])) as client:
        job=dict(title='Data Engineer',company_name='Acme')
        resolver.search_candidates(job,client)
        resolver.search_candidates(job,client)
        assert len(calls)==1 and job['public_search_diagnostics'][-1]['cache_hit']
        assert job['public_search_diagnostics'][-1]['search_results']==0
    with patch.dict(os.environ,{'TAVILY_API_KEY':'offline','SERPER_API_KEY':'','PUBLIC_SEARCH_PROVIDER':''}), \
         httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(429))) as client:
        job=dict(title='Data Engineer',company_name='Acme')
        try:resolver.search_candidates(job,client)
        except resolver.PublicSearchError:pass
        assert job['public_search_diagnostics'][0]['error_category']=='rate_limit'


if __name__ == '__main__':
    for name,fn in list(globals().items()):
        if name.startswith('test_') and callable(fn) and name!='test_future_search_diagnostics_show_each_url_reason_and_remove_credentials':fn()
