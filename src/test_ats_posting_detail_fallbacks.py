"""Greenhouse Job Board API and iCIMS in_iframe fallbacks (offline, mocked HTTP).

Fixture shapes mirror the Nov 24 JD recovery pilot responses (status codes,
redirects, canonical links, JSON structure); description text is synthetic.
"""
import html
import json
import unittest
from unittest.mock import patch

import httpx

from src.enrichment import official_job_resolver as resolver
from src.enrichment import retrieval_diagnostics as diag
from src.enrichment.ats import greenhouse
from src.enrichment.ats import ATS_DISCOVERY_CACHE
from src.enrichment import employer_careers as careers

JD = ('Responsibilities: design and build reliable SQL and Python data pipelines and models. '
      'Requirements: 7+ years of data engineering experience with Spark and cloud warehouses. ') * 30
HTMLT = {'content-type': 'text/html; charset=utf-8'}

GH_POSTING = 'https://job-boards.greenhouse.io/block/jobs/5473605008'
GH_API = 'https://boards-api.greenhouse.io/v1/boards/block/jobs/5473605008'
GH_LIST = 'https://boards-api.greenhouse.io/v1/boards/block/jobs?content=true'
MIRROR = 'https://block.xyz/careers/jobs/5473605008?gh_jid=5473605008'
IC_POSTING = 'https://careers-mercuryinsurance.icims.com/jobs/6939/lead-data-analytics-engineer/job'
IC_IFRAME = IC_POSTING + '?in_iframe=1'


def block_job():
    return dict(source='glassdoor', record_key='block', company_name='Block',
                title='Staff Data Engineer, Advanced Insights & Modeling', location='San Francisco, CA',
                description='', apply_url='https://www.glassdoor.com/partner/jobListing.htm?pos=1')


def mercury_job(company='Mercury Insurance'):
    return dict(source='builtin', record_key='mercury', company_name=company,
                title='Lead Data Analytics Engineer', location='Remote', description='',
                apply_url='https://builtin.com/job/lead-data-analytics-engineer/1')


def gh_detail(company='Block', job_id='5473605008', title=None, content=JD):
    return {'id': int(job_id), 'title': title or 'Staff Data Engineer, Advanced Insights & Modeling',
            'company_name': company, 'location': {'name': 'Bay Area, CA, United States of America'},
            'absolute_url': MIRROR, 'requisition_id': 'R0007250', 'updated_at': '2026-11-18T20:23:09-05:00',
            'content': html.escape(f'<p>{content}</p>')}


def icims_page(*, canonical, description=None, company='Mercury Insurance Services, LLC',
               title='Lead Data Analytics Engineer'):
    head = f'<title>{title}</title><link rel="canonical" href="{canonical}">'
    if description is None:
        return head + '<body><iframe src="?in_iframe=1"></iframe></body>'
    node = {'@type': 'JobPosting', 'title': title, 'description': description,
            'hiringOrganization': {'@type': 'Organization', 'name': company},
            'jobLocation': {'address': {'addressRegion': 'CA', 'addressCountry': 'US'}}}
    return head + f'<script type="application/ld+json">{json.dumps(node)}</script>'


class Mock:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def handler(self, request):
        url = str(request.url)
        self.calls.append(url)
        reply = self.routes.get(url)
        if reply is None:
            return httpx.Response(404, text='')
        if callable(reply):
            return reply(request)
        status, body, headers = reply
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body, headers=headers)
        return httpx.Response(status, text=body, headers={**HTMLT, **headers})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler), follow_redirects=True)


def resolve(job, routes, candidates):
    mock = Mock(routes)
    with patch.object(resolver, 'fetch_dynamic_job_description', return_value=None), mock.client() as client:
        out = resolver.resolve_from_candidates(job, client, candidates, 'ats')
    return out, mock.calls, job.get('retrieval_diagnostics', [])


class Base(unittest.TestCase):
    def setUp(self):
        careers.CAREERS_CACHE.clear()
        ATS_DISCOVERY_CACHE.clear()


class GreenhouseTests(Base):
    def gh_candidate(self):
        return [resolver.OfficialJobCandidate(GH_POSTING, 'Staff Data Engineer, Advanced Insights & Modeling',
                                              'greenhouse')]

    def test_adapter_uses_greenhouse_posting_not_employer_mirror(self):
        [cand] = greenhouse.parse_greenhouse_jobs({'jobs': [gh_detail()]}, 'block', 'Block')
        self.assertEqual(cand.url, GH_POSTING)
        kept = greenhouse.parse_greenhouse_jobs({'jobs': [dict(gh_detail(), absolute_url=GH_POSTING)]},
                                                'block', 'Block')
        self.assertEqual(kept[0].url, GH_POSTING)

    def test_redirect_to_employer_mirror_recovers_via_job_board_api_and_verifies(self):
        routes = {GH_POSTING: (302, '', {'location': MIRROR}),
                  MIRROR: (200, '<html><body><div id="root"></div></body></html>', {}),
                  GH_API: (200, gh_detail(), {})}
        out, calls, entries = resolve(block_job(), routes, self.gh_candidate())
        self.assertEqual(out.status, resolver.OFFICIAL_FOUND_VERIFIED, out.official_url_validation_reason)
        self.assertEqual(out.official_job_url, GH_API)
        self.assertEqual(out.description_result.word_count, len(JD.split()))  # complete synthetic JD
        self.assertEqual(out.description_result.extraction_method, 'greenhouse_job_board_api')
        self.assertEqual(calls.count(GH_API), 1)
        self.assertTrue(entries[0]['posting_detail_endpoint'].startswith('used: greenhouse job board API'))

    def test_full_ats_discovery_flow_reproduces_and_fixes_the_nov_17_block_failure(self):
        listing = {'jobs': [gh_detail()]}
        routes = {GH_LIST: (200, listing, {}), GH_POSTING: (302, '', {'location': MIRROR}),
                  MIRROR: (200, '<html><body></body></html>', {}), GH_API: (200, gh_detail(), {})}
        mock = Mock(routes)
        with patch.object(resolver, 'fetch_dynamic_job_description', return_value=None), \
                patch('src.enrichment.employer_careers.discover_employer_candidates', return_value=[]), \
                mock.client() as client:
            out = resolver.resolve_official_job(block_job(), client)
        self.assertEqual(out.status, resolver.OFFICIAL_FOUND_VERIFIED, out.official_url_validation_reason)
        self.assertIn(GH_LIST, mock.calls)                 # guessed board 'block' via the existing adapter
        self.assertEqual(out.official_job_url, GH_API)     # the Greenhouse posting, never the mirror

    def test_wrong_employer_or_posting_is_rejected(self):
        for name, detail in (('other employer', gh_detail(company='Other Payments Co')),
                             ('other posting id', gh_detail(job_id='999')),
                             ('other title', gh_detail(title='Dental Hygienist'))):
            with self.subTest(name):
                routes = {GH_POSTING: (200, '<html><body></body></html>', {}), GH_API: (200, detail, {})}
                out, _, _ = resolve(block_job(), routes, self.gh_candidate())
                self.assertNotEqual(out.status, resolver.OFFICIAL_FOUND_VERIFIED)

    def test_explicit_refusal_or_missing_posting_never_reaches_the_api(self):
        for status in (401, 403, 429, 404):
            with self.subTest(status=status):
                routes = {GH_POSTING: (status, '', {'Retry-After': '60'} if status == 429 else {}),
                          GH_API: (200, gh_detail(), {})}
                out, calls, entries = resolve(block_job(), routes, self.gh_candidate())
                self.assertNotIn(GH_API, calls)
                self.assertNotEqual(out.status, resolver.OFFICIAL_FOUND_VERIFIED)
        out, calls, entries = resolve(block_job(), {GH_POSTING: (200, '<html></html>', {}),
                                                    GH_API: (429, '', {'Retry-After': '30'})}, self.gh_candidate())
        self.assertNotEqual(out.status, resolver.OFFICIAL_FOUND_VERIFIED)
        self.assertIn('attempted: greenhouse job board API blocked (HTTP 429)', entries[0]['posting_detail_endpoint'])

    def test_scope_parser_accepts_only_greenhouse_posting_urls(self):
        self.assertEqual(greenhouse.greenhouse_posting_scope(GH_POSTING)[1:], ('block', '5473605008'))
        for url in (MIRROR, 'http://job-boards.greenhouse.io/block/jobs/1', 'https://job-boards.greenhouse.io/block',
                    'https://evil.greenhouse.io.example.com/block/jobs/1', 'https://u:p@job-boards.greenhouse.io/b/jobs/1'):
            self.assertIsNone(greenhouse.greenhouse_posting_scope(url), url)


class ICIMSTests(Base):
    def ic_candidate(self):
        return [resolver.OfficialJobCandidate(IC_POSTING, 'Lead Data Analytics Engineer', 'icims')]

    def routes(self, company='Mercury Insurance', iframe=None):
        return {IC_POSTING: (200, icims_page(canonical='https://www.mercuryinsurance.com/about/careers-jobs/'), {}),
                IC_IFRAME: iframe or (200, icims_page(canonical=IC_POSTING, description=JD, company=company), {})}

    def test_iframe_view_recovers_description_for_the_same_posting(self):
        out, calls, entries = resolve(mercury_job(), self.routes(), self.ic_candidate())
        self.assertEqual(calls, [IC_POSTING, IC_IFRAME])
        self.assertEqual(out.status, resolver.OFFICIAL_FOUND_VERIFIED, out.official_url_validation_reason)
        self.assertEqual(out.description_result.word_count, len(JD.split()))  # complete synthetic JD
        self.assertTrue(entries[0]['posting_detail_endpoint'].startswith('used: icims in_iframe view'))

    def test_legal_entity_variant_is_not_aliased(self):
        # Observed live: "Mercury Insurance Services, LLC" vs lead "Mercury Insurance".
        out, _, entries = resolve(mercury_job(), self.routes(company='Mercury Insurance Services, LLC'),
                                  self.ic_candidate())
        self.assertNotEqual(out.status, resolver.OFFICIAL_FOUND_VERIFIED)
        self.assertEqual(entries[0]['failure'], diag.IDENTITY_REJECTED)
        self.assertEqual(entries[0]['description_quality'], 'full')  # retrieved, not verified

    def test_iframe_redirect_to_another_posting_is_ignored(self):
        other = 'https://careers-mercuryinsurance.icims.com/jobs/7000/other/job'
        routes = self.routes(iframe=(302, '', {'location': other}))
        routes[other] = (200, icims_page(canonical=other, description=JD), {})
        out, _, entries = resolve(mercury_job(), routes, self.ic_candidate())
        self.assertNotEqual(out.status, resolver.OFFICIAL_FOUND_VERIFIED)
        self.assertIn('different posting, ignored', entries[0]['posting_detail_endpoint'])

    def test_refused_outer_page_never_requests_the_iframe(self):
        for status in (403, 429):
            with self.subTest(status=status):
                routes = self.routes()
                routes[IC_POSTING] = (status, '', {})
                _, calls, _ = resolve(mercury_job(), routes, self.ic_candidate())
                self.assertEqual(calls, [IC_POSTING])


class PilotOutcomeTests(Base):
    def test_workday_429_stops_without_browser_or_other_routes(self):
        url = 'https://crosscountrymortgage.wd1.myworkdayjobs.com/en-US/CCMCareers/job/Data-Engineer_JR03798'
        job = dict(source='glassdoor', record_key='cc', company_name='CrossCountry Mortgage', title='Data Engineer',
                   location='Remote', description='', apply_url='https://www.glassdoor.com/x')
        mock = Mock({url: (429, '<html>cloudflare</html>', {'server': 'cloudflare'})})
        with patch.object(resolver, 'fetch_dynamic_job_description', side_effect=AssertionError('no browser')), \
                mock.client() as client:
            out = resolver.resolve_from_candidates(job, client, [resolver.OfficialJobCandidate(url, 'Data Engineer',
                                                                                              'workday')], 'search')
        self.assertEqual(out.status, resolver.OFFICIAL_BLOCKED)
        self.assertEqual(mock.calls, [url])
        self.assertEqual(job['retrieval_diagnostics'][0]['access'], diag.RATE_LIMITED)

    def test_humana_louisville_posting_is_not_attached_to_the_frisco_lead(self):
        url = 'https://careers.humana.com/us/en/job/R-430106/Data-Engineer'
        node = {'@type': 'JobPosting', 'title': 'Data Engineer', 'description': JD, 'identifier': {'value': 'R-430106'},
                'jobLocation': {'address': {'addressLocality': 'Louisville', 'addressRegion': 'Kentucky',
                                            'addressCountry': 'United States of America'}}}
        page = f'<link rel="canonical" href="{url}"><script type="application/ld+json">{json.dumps(node)}</script>'
        job = dict(source='glassdoor', record_key='hum', company_name='Humana', title='Data Engineer',
                   location='Frisco, TX', description='', apply_url='https://www.glassdoor.com/x')
        out, _, entries = resolve(job, {url: (200, page, {})},
                                  [resolver.OfficialJobCandidate(url, 'Data Engineer', 'search')])
        self.assertNotEqual(out.status, resolver.OFFICIAL_FOUND_VERIFIED)
        self.assertEqual(entries[0]['failure'], diag.IDENTITY_REJECTED)

    def test_robotics_posting_specific_url_keeps_its_id_in_diagnostics(self):
        url = 'https://roboticstechno.com/view_job?id=39611'
        self.assertEqual(diag.sanitize_url(url), url)
        self.assertEqual(diag.sanitize_url('https://x.example/view?id=abc&token=SECRET'),
                         'https://x.example/view?id=abc')
        job = dict(source='glassdoor', record_key='rt', company_name='Robotics Technologies',
                   title='AWS Data Engineer', location='Juno Beach, FL', description='', apply_url='https://www.glassdoor.com/x')
        out, _, entries = resolve(job, {url: (200, '<html><title>Job</title><body>Apply now</body></html>', {})},
                                  [resolver.OfficialJobCandidate(url, 'AWS Data Engineer', 'search')])
        self.assertNotEqual(out.status, resolver.OFFICIAL_FOUND_VERIFIED)
        self.assertEqual(entries[0]['candidate_url'], url)
        self.assertEqual(entries[0]['identity'], 'rejected: fetched posting did not pass existing identity validation')


if __name__ == '__main__':
    unittest.main()
