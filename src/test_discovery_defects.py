"""Offline scope, provenance and request regressions for Issue 3.

Synthetic cases always run. Saved-response cases skip only when the private
recovery archive is not installed; public responses are never fetched here.
"""
import json
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from unittest.mock import patch

import httpx

from src import actionability, enrich_jobs
from src.enrichment import employer_careers as careers, official_job_resolver as resolver
from src.enrichment.ats import discover_ats_candidates, ATS_DISCOVERY_CACHE, workday
from src.enrichment.ats.base import safe_get, anchors_from_html
from src.enrichment.job_description import extract_job_description_from_html
from src.enrichment.job_identity import validate_job_identity
from src.verified_posting_identity import (
    breezy_posting_scope, fetched_posting_scope_valid, is_known_official_or_ats_url,
    is_specific_job_posting_url, posting_urls_equivalent, verified_posting_key_from_url,
)

POST = 'https://widget.wd12.myworkdayjobs.com/en-US/Careers/job/Data-Engineer_JR123-1'
BREEZY = 'https://widget.breezy.hr/p/012345abcdef-data-engineer/apply'
CORP = 'https://widget.example.com/'
JD = 'Responsibilities: Build reliable SQL data pipelines. Requirements: Experience in data engineering. ' * 20


def wd_html(req='JR123', company='Widget', location='United States', title='Data Engineer'):
    node = {'@type': 'JobPosting', 'title': title, 'identifier': {'value': req},
            'hiringOrganization': {'name': company}, 'description': JD,
            'jobLocation': {'address': {'addressCountry': location}}}
    return '<script type="application/ld+json">' + json.dumps(node) + '</script>'


def breezy_html(tenant='widget', position_id='012345abcdef', country='United States'):
    company = {'friendly_id': tenant, 'name': 'Widget', 'url': CORP}
    return '<title>Data Engineer</title><script>' + json.dumps({'data': {
        'companyForClient': company, 'position': {'_id': position_id,
        'name': 'Data Engineer', 'description': '<p>' + JD + '</p>',
        'company': company, 'location': {'country': {'name': country},
        'name': 'Remote', 'remote_details': {'label': 'Remote'}}}}}) + '</script>'


def job():
    return dict(source='linkedin', record_key='synthetic', company_name='Widget',
                title='Data Engineer', location='United States (Remote)', description=JD)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        careers.CAREERS_CACHE.clear()
        ATS_DISCOVERY_CACHE.clear()

    def test_short_workday_requires_fetched_req_and_tenant(self):
        result = extract_job_description_from_html(POST, POST, 200, wd_html())
        self.assertTrue(fetched_posting_scope_valid(POST, result))
        for other in (replace(result, resolved_job_id=None),
                      replace(result, resolved_job_id='JR124'),
                      replace(result, resolved_job_id_source='workday_posting_url')):
            with self.subTest(result=other.resolved_job_id):
                self.assertFalse(fetched_posting_scope_valid(POST, other))
        key = verified_posting_key_from_url(POST)
        self.assertNotEqual(key, verified_posting_key_from_url(POST.replace('widget.', 'other.')))
        self.assertNotEqual(key, verified_posting_key_from_url(POST.replace('/Careers/', '/Other/')))
        self.assertFalse(is_known_official_or_ats_url(POST))
        self.assertFalse(fetched_posting_scope_valid(POST + '?reqid=JR124', result))

    def test_long_workday_keys_stable_and_locale_aliases_bounded(self):
        long = POST.replace('/job/', '/job/Boston/')
        self.assertEqual(verified_posting_key_from_url(long), 'workday:Data-Engineer_JR123-1')
        self.assertTrue(posting_urls_equivalent(long, POST, 'JR123'))
        self.assertFalse(posting_urls_equivalent(long, POST.replace('/Careers/', '/Other/'), 'JR123'))
        self.assertFalse(posting_urls_equivalent(long, POST.replace('widget.', 'other.'), 'JR123'))
        self.assertFalse(posting_urls_equivalent(long, POST, 'JR12'))

    def test_missing_req_company_title_or_conflicting_location_still_rejected(self):
        variants = [wd_html(req=''), wd_html(req='JR124'), wd_html(company='Unrelated'),
                    wd_html(location='Canada'), wd_html(title='Dentist')]
        for html in variants:
            with self.subTest(html=html[:90]):
                with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={'content-type':'text/html; charset=utf-8'}, text=html))) as client:
                    result = resolver.resolve_from_candidates(job(), client,
                        [resolver.OfficialJobCandidate(POST, 'Data Engineer', 'workday')], 'workday')
                self.assertNotEqual(result.status, 'FOUND_VERIFIED')

    def test_breezy_aliases_are_tenant_and_position_bound(self):
        base = BREEZY.removesuffix('/apply')
        self.assertEqual(verified_posting_key_from_url(base), verified_posting_key_from_url(BREEZY))
        self.assertTrue(posting_urls_equivalent(base, BREEZY, '012345abcdef'))
        self.assertNotEqual(verified_posting_key_from_url(base), verified_posting_key_from_url(base.replace('widget.', 'other.')))
        self.assertNotEqual(verified_posting_key_from_url(base), verified_posting_key_from_url(base.replace('012345abcdef', 'fedcba543210')))
        for bad in (BREEZY.replace('.breezy.hr', '.breezy.hr.evil.com'), BREEZY.replace('https:', 'http:'),
                    'https://widget.breezy.hr/', 'https://widget.breezy.hr/p/all'):
            self.assertIsNone(breezy_posting_scope(bad))
        self.assertFalse(is_known_official_or_ats_url(BREEZY))
        self.assertTrue(resolver.discovery_url_decision(BREEZY, 'Widget')[0])

    def test_other_breezy_paths_cannot_bypass_employer_binding(self):
        for path in ('/p/123', '/jobs/123', '/careers/req123', '/?jobid=012345abcdef'):
            url='https://widget.breezy.hr'+path
            self.assertFalse(is_specific_job_posting_url(url))
            self.assertEqual(verified_posting_key_from_url(url),'')

    def test_breezy_needs_reciprocal_fetched_employer_link(self):
        for corporate, accepted in [('<a href="https://widget.breezy.hr/">Careers</a>', True),
                                     ('Widget careers', False),
                                     ('<a href="https://other.breezy.hr/">Careers</a>', False)]:
            with self.subTest(corporate=corporate):
                careers.CAREERS_CACHE.clear()
                branded = f'<title>Widget | Careers</title><meta property="og:url" content="{CORP}">'
                pages = {BREEZY: breezy_html(), CORP: branded + corporate}
                with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={'content-type':'text/html; charset=utf-8'}, text=pages.get(str(req.url), '')))) as client:
                    result = resolver.resolve_from_candidates(job(), client,
                        [resolver.OfficialJobCandidate(BREEZY, 'Data Engineer', 'breezy')], 'search')
                self.assertEqual(result.status == 'FOUND_VERIFIED', accepted)
                if accepted:
                    self.assertEqual(result.employer_provenance['basis'], 'fetched_reciprocal_employer_careers_link')
                    self.assertEqual(result.resolved_company, 'Widget')

    def test_reciprocal_links_without_independent_corporate_brand_do_not_verify(self):
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={'content-type':'text/html; charset=utf-8'}, text=breezy_html() if str(req.url)==BREEZY else
                '<a href="https://widget.breezy.hr/">Careers</a>'))) as client:
            found=resolver.resolve_from_candidates(job(),client,
                [resolver.OfficialJobCandidate(BREEZY,'Data Engineer','breezy')],'search')
        self.assertNotEqual(found.status,'FOUND_VERIFIED')

    def test_breezy_mismatched_payload_and_country_do_not_verify(self):
        # This is an HTTP-fixture identity test, not a renderer test. Negative
        # payloads may activate the existing optional dynamic-description path.
        # Prevent an actual Playwright/Node launch while preserving rejection.
        with patch.object(resolver, 'fetch_dynamic_job_description', return_value=None):
            for html in (breezy_html(tenant='wrong'), breezy_html(position_id='fedcba543210'),
                         breezy_html(country='Canada')):
                careers.CAREERS_CACHE.clear()
                with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={'content-type':'text/html; charset=utf-8'}, text=html if str(req.url) == BREEZY else
                        f'<title>Widget</title><meta property="og:url" content="{CORP}">'
                        '<a href="https://widget.breezy.hr/">Careers</a>'))) as client:
                    result = resolver.resolve_from_candidates(job(), client,
                        [resolver.OfficialJobCandidate(BREEZY, 'Data Engineer', 'breezy')], 'search')
                self.assertNotEqual(result.status, 'FOUND_VERIFIED')

    def test_breezy_discovery_anchor_does_not_grant_identity(self):
        candidates = anchors_from_html(f'<a href="{BREEZY}">Data Engineer</a>', CORP, 'employer', 'Widget')
        self.assertEqual(len(candidates), 1)
        self.assertIsNone(candidates[0].employer_provenance)
        parsed = extract_job_description_from_html(BREEZY, BREEZY, 200, breezy_html())
        self.assertFalse(fetched_posting_scope_valid(BREEZY, parsed))
        self.assertFalse(fetched_posting_scope_valid(BREEZY + '?jobid=fedcba543210', parsed))

    def test_persistence_rechecks_fetched_scope_before_opening_db(self):
        parsed = extract_job_description_from_html(POST, POST, 200, wd_html())
        identity = validate_job_identity(original_title='Data Engineer', original_company='Widget',
            resolved_title='Data Engineer', resolved_company='Widget')
        found = resolver.OfficialJobResolutionResult('FOUND_VERIFIED', POST, 'workday',
            resolver.now_utc(), 1.0, 'identity accepted', 'Data Engineer', 'Widget',
            'United States', parsed, identity)
        with patch.object(enrich_jobs, 'get_connection', side_effect=AssertionError('No DB expected')):
            for invalid in (replace(parsed, resolved_job_id=None), replace(parsed, resolved_job_id='JR124')):
                with self.assertRaises(ValueError):
                    enrich_jobs.save_enrichment_attempt(job(), invalid, official_resolution=found,
                                                       identity_validation=identity)
            self.assertTrue(enrich_jobs._validate_official_identity_for_save(job(), parsed, identity, found).accepted)

    def test_application_shell_is_not_actionability(self):
        parsed = extract_job_description_from_html(BREEZY, BREEZY, 200, breezy_html())
        proof = dict(company='widget', corporate_url=CORP, careers_url='https://widget.breezy.hr/',
                     basis='fetched_reciprocal_employer_careers_link')
        trusted = job() | dict(official_job_url=BREEZY, official_url_status='FOUND_VERIFIED',
            official_url_source='employer', official_resolved_company='Widget',
            verified_posting_key=verified_posting_key_from_url(BREEZY),
            verified_posting_key_source='official_found_verified', verified_posting_key_trust='trusted',
            employer_careers_provenance=proof)
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={'content-type':'text/html; charset=utf-8'}, text=breezy_html()))) as client:
            self.assertEqual(actionability.check_job(trusted, client, actionability.Settings()).status, 'UNKNOWN')


class ProbeTests(unittest.TestCase):
    def setUp(self):
        careers.CAREERS_CACHE.clear()
        ATS_DISCOVERY_CACHE.clear()

    def test_exact_get_failure_cache_expires_and_never_becomes_company_negative(self):
        calls = []
        clock = [100.0]
        def send(req):
            calls.append(str(req.url))
            if 'offline' in req.url.path:
                raise httpx.ConnectError('offline', request=req)
            return httpx.Response(404)
        with httpx.Client(transport=httpx.MockTransport(send)) as client, patch(
                'src.enrichment.ats.base.time.monotonic', side_effect=lambda: clock[0]):
            for path, ttl in [('missing', 30), ('offline', 15)]:
                u='https://tenant.example.com/' + path
                safe_get(client,u); safe_get(client,u)
                self.assertEqual(calls.count(u),1)
                clock[0]+=ttl+1
                safe_get(client,u)
                self.assertEqual(calls.count(u),2)
            safe_get(client,'https://tenant.example.com/other')
        self.assertFalse(ATS_DISCOVERY_CACHE)

    def test_probe_cache_bounds_and_no_caching_of_provider_failure_statuses(self):
        calls = []
        with httpx.Client(transport=httpx.MockTransport(lambda req: (
                calls.append(str(req.url)) or httpx.Response(int(req.url.path.strip('/')))))) as client:
            for code in (401,403,429,500,503):
                safe_get(client,f'https://example.com/{code}');safe_get(client,f'https://example.com/{code}')
            self.assertEqual(len(calls),10)
            for i in range(140):safe_get(client,f'https://example.com/404?tenant={i}')
            self.assertLessEqual(len(client._ats_probe_cache),128)

    def test_invalid_dns_and_unknown_workday_tenants_make_zero_requests(self):
        with httpx.Client(transport=httpx.MockTransport(lambda req: self.fail('Unexpected HTTP'))) as client:
            self.assertIsNone(safe_get(client,'https://careers-widget_company.icims.com/jobs/search'))
            self.assertEqual(workday.discover_candidates(job(),client),[])

    def test_verified_platform_skips_unrelated_guesses_but_expiry_restores_fallback(self):
        careers.CAREERS_CACHE['widget']=(time.monotonic(),dict(company='widget',platforms=['workday']))
        with httpx.Client(transport=httpx.MockTransport(lambda req:self.fail('Unexpected HTTP'))) as client:
            with patch('src.enrichment.ats.ADAPTERS', (workday,)), patch.object(
                    workday,'discover_candidates',return_value=[]) as adapter:
                self.assertEqual(discover_ats_candidates(job(),client),[])
                adapter.assert_not_called()
                careers.CAREERS_CACHE['widget']=(0,dict(company='widget',platforms=['workday']))
                discover_ats_candidates(job(),client)
                adapter.assert_called_once()

    def test_corporate_connection_cache_defers_only_exact_url_for_15_seconds(self):
        calls=[];clock=[100.0]
        def send(req):calls.append(str(req.url));raise httpx.ConnectError('offline',request=req)
        with httpx.Client(transport=httpx.MockTransport(send)) as client, patch(
                'src.enrichment.employer_careers.time.monotonic',side_effect=lambda:clock[0]):
            pages=careers.PublicPages(client,[])
            pages.get(CORP);pages.get(CORP)
            self.assertEqual(len(calls),1)
            clock[0]+=16;pages.get(CORP)
            self.assertEqual(len(calls),2)


class ArchivedDiscoveryTests(unittest.TestCase):
    root = Path('/private/tmp/issue3-http-only-xcvvpzac')

    def test_saved_tandem_highmark_and_breezy_candidates(self):
        if not self.root.exists():
            self.skipTest('Private recovery archive not installed')
        ledger=json.loads((self.root/'HTTP_ACCOUNTING.json').read_text())
        jobs=json.loads((self.root/'ELIGIBLE_22.json').read_text())
        results=json.loads((self.root/'RESULTS.json').read_text())
        def normalize(url):
            p=urlsplit(url);return urlunsplit((p.scheme,p.netloc,p.path or '/',p.query,''))
        rows={normalize(q['url']):q for q in ledger['requests'] if q.get('body_file') and not q.get('provider')}
        def send(req):
            event=rows.get(normalize(str(req.url)))
            if event is None:raise httpx.ConnectError('Archive miss; no network',request=req)
            headers={'content-type':'text/html; charset=utf-8'}
            if event.get('location'):headers['location']=event['location']
            return httpx.Response(event['http_status'],headers=headers,content=(self.root/event['body_file']).read_bytes())
        expected={'EasyPay Finance':True,'Tandem Diabetes Care':True,'Highmark':True}
        keys=set()
        for old in results:
            if old['company'] not in expected:continue
            original=next(j for j in jobs if j['record_key']==old['record_key'])
            urls=[t['url'] for t in old['candidate_validation_trace'] if t.get('fetch_status')=='enriched']
            if old['company']=='EasyPay Finance':urls=['https://easypayfinance.breezy.hr/p/0393434efbdd-lead-analytics-engineer/apply']
            for url in urls:
                with self.subTest(company=old['company'],url=url):
                    careers.CAREERS_CACHE.clear()
                    with patch.object(resolver,'fetch_dynamic_job_description',return_value=None), httpx.Client(
                            transport=httpx.MockTransport(send),follow_redirects=True) as client:
                        found=resolver.resolve_from_candidates(dict(original),client,
                            [resolver.OfficialJobCandidate(url,old['title'],resolver.ats_type_for_url(url) or 'search')], 'search')
                    different_senior='senior-data-engineer' in url and old['company']=='Highmark'
                    self.assertEqual(found.status=='FOUND_VERIFIED',not different_senior)
                    if found.status=='FOUND_VERIFIED':
                        keys.add(verified_posting_key_from_url(found.official_job_url))
                        self.assertTrue(enrich_jobs._validate_official_identity_for_save(
                            original,found.description_result,found.identity_validation,found).accepted)
        self.assertEqual(len(keys),3)  # Two EasyPay records, one employer position.


if __name__ == '__main__':
    unittest.main()
