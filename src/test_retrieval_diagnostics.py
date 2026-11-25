"""Offline tests for per-attempt JD retrieval diagnostics (no behaviour change).

All HTTP is mocked; Playwright is patched; DB work uses a temporary database.
"""
import json
import os
import unittest
from unittest.mock import patch

import httpx

from src import enrich_jobs
from src.enrichment import official_job_resolver as resolver
from src.enrichment import retrieval_diagnostics as diag
from src.enrichment.ats import workday
from src.enrichment.job_description import extract_job_description_from_html, fetch_job_description

WD = 'https://acme.wd1.myworkdayjobs.com/en-US/AcmeCareers/job/Data-Engineer_JR1'
JD = ('Responsibilities: Build reliable SQL and Python data pipelines. '
      'Requirements: 3+ years of data engineering experience. ') * 15
HTML = {'content-type': 'text/html; charset=utf-8'}


def posting_html(url, title='Data Engineer', company='Acme', identifier='JR1', description=JD):
    node = {'@type': 'JobPosting', 'title': title, 'description': description,
            'hiringOrganization': {'@type': 'Organization', 'name': company},
            'jobLocation': {'address': {'addressCountry': 'US'}}, 'identifier': {'value': identifier}}
    return (f'<title>{title}</title><link rel="canonical" href="{url}">'
            f'<script type="application/ld+json">{json.dumps(node)}</script>')


def client_for(responses):
    def handler(request):
        reply = responses.get(str(request.url))
        if reply is None:
            return httpx.Response(404, text='')
        if isinstance(reply, Exception):
            raise reply
        status, body, headers = reply
        return httpx.Response(status, text=body, headers={**HTML, **headers})
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def fetch(reply):
    with client_for({WD: reply}) as client:
        return fetch_job_description(WD, client)


def job(**kw):
    base = dict(record_key='rk-1', source='glassdoor', title='Data Engineer', company_name='Acme',
                location='Remote', description='', apply_url='https://www.glassdoor.com/partner/jobListing.htm?pos=1&guid=SECRET')
    base.update(kw)
    return base


class ClassificationTests(unittest.TestCase):
    def test_access_categories_are_distinct(self):
        cases = {
            'denied_401': ((401, 'no', {}), diag.ACCESS_DENIED),
            'denied_403': ((403, 'Forbidden', {}), diag.ACCESS_DENIED),
            'rate_limited': ((429, 'slow down', {'Retry-After': '120'}), diag.RATE_LIMITED),
            'js_required': ((200, '<title>W</title>Please enable JavaScript and cookies to continue', {}),
                            diag.JAVASCRIPT_REQUIRED),
            'bot_check': ((200, '<title>Check</title>Please verify you are human (captcha)', {}), diag.BOT_CHECK),
            'not_found': ((404, '', {}), diag.POSTING_NOT_FOUND),
            'server_error': ((503, '', {}), diag.HTTP_ERROR),
        }
        for name, (reply, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(diag.access_classification(fetch(reply)), expected)
        limited = fetch((429, 'slow down', {'Retry-After': '120'}))
        self.assertEqual((limited.access_signal, limited.retry_after), ('http_429', '120'))
        self.assertEqual(fetch((403, '', {})).access_signal, 'http_403')
        self.assertTrue(fetch((200, 'Please enable JavaScript and cookies', {})).access_signal
                        .startswith('javascript_required:'))
        with client_for({WD: httpx.ConnectTimeout('timed out')}) as client:
            self.assertEqual(diag.access_classification(fetch_job_description(WD, client)), diag.NETWORK_ERROR)

    def test_extraction_failure_and_quality_are_reported_separately(self):
        shell = fetch((200, '<html><body><div id="root"></div></body></html>', {}))
        self.assertEqual(diag.access_classification(shell), 'accessible')
        self.assertEqual(diag.failure_category(shell, None), diag.EXTRACTION_FAILED)
        full = fetch((200, posting_html(WD), {}))
        self.assertEqual((diag.description_quality(full), diag.failure_category(full, 'accepted: ok')), ('full', None))
        partial = fetch((200, posting_html(WD, description='Build data pipelines with SQL and Python today. ' * 3), {}))
        self.assertEqual(diag.description_quality(partial), 'partial')
        self.assertEqual(diag.failure_category(full, 'rejected: title mismatch'), diag.IDENTITY_REJECTED)


class BrowserFallbackTests(unittest.TestCase):
    def candidate(self):
        return resolver.OfficialJobCandidate(WD, 'Data Engineer', 'workday')

    def test_blocked_skips_browser_and_says_why(self):
        with patch.object(resolver, 'fetch_dynamic_job_description', side_effect=AssertionError('no browser')), \
                client_for({WD: (403, '', {})}) as client:
            result = resolver.fetch_candidate_description(self.candidate(), client)
        self.assertEqual(result.status, 'blocked')
        self.assertIn('skipped', result.browser_fallback)
        self.assertIn('not used to bypass', result.browser_fallback)

    def test_no_description_attempts_browser_and_records_outcome(self):
        rendered = extract_job_description_from_html(WD, WD, 200, posting_html(WD), 'playwright')
        for dynamic, expected in ((None, 'attempted: browser unavailable or failed'),
                                  (rendered, 'attempted: rendered description used')):
            with self.subTest(expected), patch.object(resolver, 'fetch_dynamic_job_description', return_value=dynamic), \
                    client_for({WD: (200, '<html><body></body></html>', {})}) as client:
                self.assertEqual(resolver.fetch_candidate_description(self.candidate(), client).browser_fallback, expected)


class CandidateDiagnosticsTests(unittest.TestCase):
    def resolve(self, responses, j=None, candidates=None):
        j = j or job()
        with patch.object(resolver, 'fetch_dynamic_job_description', return_value=None), client_for(responses) as client:
            out = resolver.resolve_from_candidates(j, client, candidates or [
                resolver.OfficialJobCandidate(WD, 'Data Engineer', 'workday')], 'search')
        return out, j['retrieval_diagnostics']

    def test_blocked_candidate_records_status_category_and_endpoint_gap(self):
        out, entries = self.resolve({WD: (429, '', {'Retry-After': '60'})})
        self.assertEqual(out.status, resolver.OFFICIAL_BLOCKED)
        [entry] = entries
        self.assertEqual((entry['stage'], entry['http_status'], entry['access'], entry['retry_after']),
                         ('candidate:search', 429, diag.RATE_LIMITED, '60'))
        self.assertEqual(entry['identity'], 'not evaluated: fetch did not return a page')
        self.assertEqual(entry['failure'], diag.RATE_LIMITED)
        self.assertIn('no workday posting-detail endpoint', entry['posting_detail_endpoint'])
        self.assertIn('skipped', entry['browser_fallback'])

    def test_verified_and_identity_rejected_candidates(self):
        out, [entry] = self.resolve({WD: (200, posting_html(WD), {})})
        self.assertEqual(out.status, resolver.OFFICIAL_FOUND_VERIFIED)
        self.assertEqual((entry['failure'], entry['description_quality']), (None, 'full'))
        self.assertTrue(entry['identity'].startswith('accepted'))
        self.assertGreater(entry['word_count'], 80)
        out, [entry] = self.resolve({WD: (200, posting_html(WD, title='Dental Hygienist'), {})})
        self.assertNotEqual(out.status, resolver.OFFICIAL_FOUND_VERIFIED)
        self.assertEqual(entry['failure'], diag.IDENTITY_REJECTED)
        self.assertTrue(entry['identity'].startswith('rejected'))

    def test_blocked_host_skip_is_recorded(self):
        a, b = 'https://careers.acme.com/jobs/1', 'https://careers.acme.com/jobs/2'
        _, entries = self.resolve({a: (403, '', {}), b: (200, posting_html(b), {})}, candidates=[
            resolver.OfficialJobCandidate(a, 'Data Engineer', 'search'),
            resolver.OfficialJobCandidate(b, 'Data Engineer', 'search')])
        self.assertEqual([e.get('fetch_status') for e in entries], ['blocked', 'skipped_blocked_host'])


class WorkdayListingTests(unittest.TestCase):
    def test_listing_status_and_candidate_count_are_recorded(self):
        root = 'https://acme.wd1.myworkdayjobs.com/AcmeCareers'
        endpoint = 'https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs'
        for status, payload, outcome in ((200, {'jobPostings': [{'title': 'Data Engineer',
                                          'externalPath': '/job/Remote/Data-Engineer_JR1'}]}, 'listing returned'),
                                         (403, None, 'non-200')):
            j = job()
            transport = httpx.MockTransport(lambda r, s=status, p=payload: httpx.Response(s, json=p) if p
                                            else httpx.Response(s))
            with self.subTest(status=status), httpx.Client(transport=transport) as client:
                workday.discover_from_search_homepage(j, client, root)
                [entry] = j['retrieval_diagnostics']
                self.assertEqual((entry['stage'], entry['http_status'], entry['candidate_url']),
                                 ('workday_listing', status, endpoint))
                self.assertTrue(entry['outcome'].startswith(outcome))


class SanitizationAndRunLogTests(unittest.TestCase):
    def test_urls_keep_identity_only_and_lines_hold_no_page_content(self):
        dirty = 'https://user:pw@careers.acme.com/jobs/1?gh_jid=42&token=abc&utm_source=x'
        self.assertEqual(diag.sanitize_url(dirty), 'https://careers.acme.com/jobs/1?gh_jid=42')
        j = job()
        result = extract_job_description_from_html(dirty, dirty, 200, posting_html(dirty))
        entry = diag.record(j, stage='candidate:search', url=dirty, result=result, identity='accepted: ok')
        line = enrich_jobs.safe_enrichment_log_message(diag.format_line(entry))
        for leaked in ('pw@', 'token=abc', 'utm_source', 'Responsibilities', '<script'):
            self.assertNotIn(leaked, line)
        self.assertIn('careers.acme.com/jobs/1?gh_jid=42', line)
        self.assertNotIn('description', {k for k in entry if k != 'description_quality'})

    def test_batch_run_log_contains_source_and_candidate_diagnostics(self):
        from src.test_enrich_jobs_official_retry import with_temp_database, insert_raw_job
        blocked = resolver.OfficialJobResolutionResult(
            status=resolver.OFFICIAL_BLOCKED, official_job_url=None, official_url_source='search',
            official_url_resolved_at=resolver.now_utc(), official_url_confidence=None,
            official_url_validation_reason='all official candidates were blocked',
            resolved_title=None, resolved_company=None, resolved_location=None)

        def fake_resolve(job, client, **kwargs):
            # Stand-in for real resolution: one blocked Workday candidate.
            with client_for({WD: (403, '', {})}) as c:
                diag.record(job, stage='candidate:search', url=WD,
                            result=resolver.fetch_candidate_description(
                                resolver.OfficialJobCandidate(WD, 'Data Engineer', 'workday'), c),
                            identity='not evaluated: fetch did not return a page')
            return blocked

        def scenario():
            insert_raw_job('rk-1', source='glassdoor', title='Data Engineer', company_name='Acme',
                           apply_url=job()['apply_url'])
            lines = []
            with patch.object(enrich_jobs, 'resolve_official_job', side_effect=fake_resolve), \
                    client_for({job()['apply_url']: (403, 'Forbidden', {})}) as client:
                enrich_jobs.process_enrichment_batch([job()], client, emit=lines.append)
            diag_lines = [l for l in lines if l.startswith('Retrieval diagnostic:')]
            self.assertEqual(len(diag_lines), 2, lines)
            self.assertIn('stage=source', diag_lines[0])
            self.assertIn('http_status=403; fetch_status=blocked; access=access_denied', diag_lines[0])
            self.assertNotIn('SECRET', ' '.join(lines))           # tracking query dropped
            self.assertIn('stage=candidate:search', diag_lines[1])
            self.assertIn('posting_detail_endpoint=not attempted', diag_lines[1])
        with_temp_database(scenario)


if __name__ == '__main__':
    unittest.main()
