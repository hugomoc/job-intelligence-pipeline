"""Offline Serper discovery, identity, credential and circuit regression tests."""
import json
import os
import unittest
from unittest.mock import patch
import httpx
from src.enrichment import official_job_resolver as resolver
from src.enrichment.public_search import PublicSearchError, selected_provider, provider_metrics
from src.retry_targeted_enrichment import probe_search
from src.targeted_enrichment import targeted_retry
from src.test_official_job_resolver import job_posting_html, lensa_job

URL = 'https://boards.greenhouse.io/seismic/jobs/123'

class SearchTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'SERPER_API_KEY': 'mock-secret', 'TAVILY_API_KEY': '', 'PUBLIC_SEARCH_PROVIDER': ''})
        env.start(); self.addCleanup(env.stop)

    def client(self, status=200, payload=None, page=None, transport_error=False):
        self.requests = []
        def handle(request):
            self.requests.append(request)
            if request.url.host == 'google.serper.dev':
                if transport_error:
                    raise httpx.ConnectError('mock-secret must never appear in errors', request=request)
                return httpx.Response(status, json=payload if payload is not None else {'organic': [
                    {'link': URL, 'title': 'Senior Analytics Engineer', 'snippet': 'Seismic perfect match'}]})
            self.assertNotIn('x-api-key', request.headers)
            return httpx.Response(200, text=page or '', headers={'content-type':'text/html'})
        return httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True)

    def test_selection_and_request_scoped_key_and_query(self):
        self.assertEqual(selected_provider(), 'serper')
        with self.client() as client:
            candidates = resolver.search_candidates(lensa_job(), client)
            self.assertEqual(len(candidates), 1)
            request = self.requests[0]
            self.assertEqual(request.url.host, 'google.serper.dev')
            self.assertEqual(request.headers['x-api-key'], 'mock-secret')
            self.assertEqual(json.loads(request.content)['q'], 'Seismic Senior Analytics Engineer jobs')
            self.assertNotIn('x-api-key', client.headers)
            self.assertEqual(lensa_job().get('verified_posting_key'), None)
            self.assertIsNone(candidates[0].title)
            self.assertIsNone(candidates[0].company)
            self.assertEqual(candidates[0].label, 'Senior Analytics Engineer')
            self.assertNotIn('official_job_url', lensa_job())
            trace_job=lensa_job()
            resolver.search_candidates(trace_job,client)
            self.assertEqual(trace_job['public_search_trace']['query'], 'Seismic Senior Analytics Engineer jobs')
            self.assertEqual(trace_job['public_search_trace']['organic_results'][0]['title'], 'Senior Analytics Engineer')
            self.assertTrue(trace_job['public_search_trace']['organic_results'][0]['candidate'])
        with patch.dict(os.environ, {'SERPER_API_KEY': ''}):
            self.assertEqual(selected_provider(), 'none')

    def resolve(self, client):
        with patch.object(resolver, 'discover_ats_candidates', return_value=[]):
            return resolver.resolve_official_job(lensa_job(), client, resolver.PublicSearchCircuit())

    def test_wrong_company_and_wrong_job_rejected(self):
        for title, company in [('Senior Analytics Engineer', 'Other Employer'), ('Accountant', 'Seismic')]:
            with self.client(page=job_posting_html(title, company)) as client:
                result = self.resolve(client)
            self.assertEqual(result.status, resolver.OFFICIAL_NOT_FOUND)
            self.assertIsNone(result.official_job_url)

    def test_valid_posting_accepted_through_existing_validator(self):
        with self.client(page=job_posting_html('Senior Analytics Engineer', 'Seismic')) as client:
            result = self.resolve(client)
            self.assertEqual(result.status, resolver.OFFICIAL_FOUND_VERIFIED)
            self.assertTrue(result.identity_validation.accepted)
            self.assertEqual(result.official_job_url, URL)
            self.assertGreater(result.description_result.word_count, 80)
            self.assertEqual(provider_metrics(client)['serper']['verified_official_resolutions'], 1)

    def test_snippet_cannot_supply_missing_posting_identity(self):
        with self.client(page='<main>Responsibilities include '+('SQL requirements '*100)+'</main>') as client, \
             patch.object(resolver, 'fetch_dynamic_job_description', return_value=None):
            result = self.resolve(client)
        self.assertNotEqual(result.status, resolver.OFFICIAL_FOUND_VERIFIED)

    def test_auth_and_quota_stop_only_serper(self):
        for status, message, category in [(401,'Unauthorized','auth_configuration'),
                                          (403,'Forbidden','auth_configuration'),
                                          (402,'Not enough credits','quota_exhausted'),
                                          (403,'Quota exhausted','quota_exhausted')]:
            circuit = resolver.PublicSearchCircuit()
            with self.client(status, {'message': message}) as client:
                with self.assertRaises(PublicSearchError) as caught:
                    circuit.search(lensa_job(), client)
                self.assertEqual(caught.exception.category, category)
                self.assertTrue(circuit.open)
                with self.assertRaises(resolver.SearchDeferredError):
                    circuit.search(lensa_job(), client)
                self.assertEqual(len(self.requests), 1)
            with patch.dict(os.environ, {'SERPER_API_KEY': ''}):
                # No configured paid provider means deferred; DDG is NOT automatic.
                self.assertTrue(circuit.open)
                self.assertEqual(selected_provider(), 'none')

    def test_rate_limit_and_network_server_errors_retryable(self):
        for status, network, category in [(429,False,'rate_limit'),(503,False,'transient_error'),(200,True,'transient_error')]:
            with self.client(status, {'message':'mock-secret'}, transport_error=network) as client:
                circuit = resolver.PublicSearchCircuit()
                with patch.object(resolver, "discover_ats_candidates", return_value=[]):
                    result = resolver.resolve_official_job(lensa_job(), client, circuit)
                self.assertEqual(result.status, resolver.OFFICIAL_ERROR)
                self.assertNotIn('mock-secret', result.error_message)
                retry = dict(lensa_job(), official_url_status=result.status, official_url_source='search',
                             application_status='new', description='', attempt_count=1)
                self.assertTrue(targeted_retry(retry))
                self.assertEqual(provider_metrics(client)['serper'][category], 1)
                self.assertFalse(circuit.open)
                self.assertEqual(circuit.errors, 1)

    def test_no_results_is_healthy_and_counted(self):
        with self.client(payload={'organic': []}) as client:
            healthy, probes = probe_search([lensa_job()], client)
            self.assertTrue(healthy)
            self.assertEqual(probes[0]['provider'], 'serper')
            self.assertEqual(provider_metrics(client)['serper']['no_result_searches'], 1)

    def test_duckduckgo_challenge_does_not_disable_serper(self):
        circuit = resolver.PublicSearchCircuit(threshold=1)
        with patch.dict(os.environ, {'SERPER_API_KEY': '', 'PUBLIC_SEARCH_PROVIDER': 'duckduckgo'}), \
             httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,text='Bots use DuckDuckGo too'))) as client:
            with self.assertRaises(PermissionError): circuit.search(lensa_job(), client)
            self.assertTrue(circuit.open)
        with self.client() as client:
            self.assertFalse(circuit.open)
            self.assertEqual(len(circuit.search(lensa_job(), client)), 1)
        self.assertTrue(circuit.states['duckduckgo']['open'])

    def tavily_client(self, tavily_status=200, tavily_payload=None, serper_status=200):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.host == 'api.tavily.com':
                self.assertNotIn('x-api-key', request.headers)
                self.assertEqual(request.headers.get('authorization'), 'Bearer mock-tavily')
                return httpx.Response(
                    tavily_status, json=tavily_payload if tavily_payload is not None
                    else {'results': [{'url': URL, 'title': 'Seismic opening'}]}
                )
            if request.url.host == 'google.serper.dev':
                self.assertEqual(request.headers.get('x-api-key'), 'mock-secret')
                self.assertNotIn('authorization', request.headers)
                return httpx.Response(serper_status, json={
                    'organic': [{'link': URL, 'title': 'Seismic opening'}]
                })
            self.assertNotIn('authorization', request.headers)
            self.assertNotIn('x-api-key', request.headers)
            return httpx.Response(200, text=job_posting_html('Senior Analytics Engineer', 'Seismic'),
                                  headers={'content-type': 'text/html'})
        return httpx.Client(transport=httpx.MockTransport(handler)), requests

    def test_tavily_primary_basic_search_one_credit_no_identity_from_title(self):
        from src.enrichment.public_search import available_providers
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'mock-tavily'}):
            self.assertEqual(available_providers(), ('tavily', 'serper'))
            client, calls = self.tavily_client()
            with client:
                candidate = resolver.search_candidates(lensa_job(), client)
                self.assertEqual(len(candidate), 1)
                self.assertIsNone(candidate[0].title)
                self.assertIsNone(candidate[0].company)
                request = calls[0]
                payload = json.loads(request.content)
                self.assertEqual(payload['search_depth'], 'basic')
                self.assertFalse(payload['auto_parameters'])
                self.assertEqual(payload['max_results'], 10)
                self.assertEqual(request.url.host, 'api.tavily.com')
                self.assertEqual(len(calls), 1)
                self.assertEqual(provider_metrics(client)['tavily']['search_requests'], 1)
                self.assertEqual(provider_metrics(client).get('serper'), None)

    def test_alert_separator_is_removed_only_from_query(self):
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'mock-tavily'}):
            client, calls = self.tavily_client()
            job = lensa_job()
            job['title'] = 'Senior Analytics Engineer |'
            with client:
                resolver.search_candidates(job, client)
            payload = json.loads(calls[0].content)
            self.assertEqual(payload['query'], 'Seismic Senior Analytics Engineer jobs')
            self.assertEqual(job['title'], 'Senior Analytics Engineer |')
            self.assertEqual(job['public_search_trace']['query'], payload['query'])

    def test_tavily_same_query_is_one_request_and_identity_remains_independent(self):
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'mock-tavily'}):
            client, calls = self.tavily_client()
            with client:
                first = lensa_job()
                second = dict(lensa_job(), record_key='another-aggregator-record')
                a = resolver.search_candidates(first, client)
                b = resolver.search_candidates(second, client)
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(a), 1)
                self.assertEqual(len(b), 1)
                self.assertIsNone(b[0].title)
                self.assertIsNone(b[0].company)
                self.assertTrue(second['public_search_trace']['cache_hit'])
                self.assertFalse(first['public_search_trace']['cache_hit'])
                self.assertEqual(provider_metrics(client)['tavily']['search_requests'], 1)

    def test_search_does_not_cache_provider_errors(self):
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'mock-tavily'}):
            client, calls = self.tavily_client(
                tavily_status=429, tavily_payload={'detail': 'rate limit'})
            with client:
                for _ in range(2):
                    with self.assertRaises(PublicSearchError):
                        resolver.search_candidates(lensa_job(), client, provider='tavily')
                self.assertEqual(len(calls), 2)
                self.assertEqual(provider_metrics(client)['tavily']['search_requests'], 2)

    def test_search_rejects_generic_career_and_job_category_urls(self):
        bad = [
            'https://pfizer.wd1.myworkdayjobs.com/PfizerCareers',
            'https://www.pfizer.com/about/careers/careers-digital',
            'https://www.pfizer.com/about/careers',
            'https://www.dice.com/jobs/q-Analytics%20Engineer-jobs',
            'https://www.dice.com/job-detail/992c0a89-bda6-41a2-b205-b34b2b7dc4eb',
            'https://jobs.ashbyhq.com/Acme',
        ]
        for url in bad:
            self.assertFalse(resolver.looks_like_official_or_ats_url(url, 'Pfizer'),
                             url)
        good = [
            'https://pfizer.wd1.myworkdayjobs.com/en-US/PfizerCareers/job/Austin/Data-Engineer_R123',
            'https://boards.greenhouse.io/seismic/jobs/123',
            'https://careers.seismic.com/jobs/123',
            'https://jobs.ashbyhq.com/acme/abc-123',
        ]
        for url in good:
            self.assertTrue(resolver.looks_like_official_or_ats_url(url, 'Seismic'),
                            url)

    def test_workday_homepage_can_discover_specific_posting_without_extra_tavily(self):
        root = 'https://pfizer.wd1.myworkdayjobs.com/PfizerCareers'
        posting = ('https://pfizer.wd1.myworkdayjobs.com/en-US/PfizerCareers/'
                   'job/New-York/Medical-Analytics-Data-Engineer_R123')
        endpoint = 'https://pfizer.wd1.myworkdayjobs.com/wday/cxs/pfizer/PfizerCareers/jobs'
        calls = []
        def handle(request):
            calls.append(str(request.url))
            if request.url.host == 'api.tavily.com':
                return httpx.Response(200, json={'results': [{
                    'title': 'Pfizer Careers', 'url': root
                }]})
            if str(request.url) == endpoint:
                query = json.loads(request.content)
                self.assertEqual(query['searchText'], 'Medical Analytics Data Engineer')
                return httpx.Response(200, json={'jobPostings': [
                    {'title': 'Medical Analytics Data Engineer',
                     'externalPath': '/job/New-York/Medical-Analytics-Data-Engineer_R123',
                     'locationsText': 'Remote'}
                ]})
            if str(request.url) == posting:
                return httpx.Response(200, text=job_posting_html(
                    'Medical Analytics Data Engineer', 'Pfizer'),
                    headers={'content-type': 'text/html'})
            raise AssertionError(str(request.url))
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'mock-tavily'}), \
             patch.object(resolver, 'discover_ats_candidates', return_value=[]), \
             httpx.Client(transport=httpx.MockTransport(handle)) as client:
            job = dict(lensa_job(), title='Medical Analytics Data Engineer',
                       company_name='Pfizer')
            outcome = resolver.resolve_official_job(job, client,
                                                   resolver.PublicSearchCircuit())
            self.assertEqual(outcome.status, resolver.OFFICIAL_FOUND_VERIFIED)
            self.assertEqual(outcome.official_job_url, posting)
            self.assertEqual(len([url for url in calls if 'api.tavily.com' in url]), 1)
            self.assertEqual(len([url for url in calls if '/wday/cxs/' in url]), 1)
            self.assertNotIn(root, calls)

    def test_tavily_quota_falls_back_to_serper_for_same_job(self):
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'mock-tavily'}):
            client, calls = self.tavily_client(tavily_status=432, tavily_payload={'detail': {'error': 'plan limit'}})
            with client:
                circuit = resolver.PublicSearchCircuit()
                job = lensa_job()
                results = circuit.search(job, client)
                self.assertEqual(len(results), 1)
                self.assertEqual([request.url.host for request in calls],
                                 ['api.tavily.com', 'google.serper.dev'])
                self.assertEqual(job['public_search_trace']['provider'], 'serper')
                self.assertEqual(job['public_search_trace']['attempted_providers'], ['tavily', 'serper'])
                self.assertTrue(circuit.state('tavily')['open'])
                self.assertFalse(circuit.open)
                self.assertEqual(provider_metrics(client)['tavily']['quota_exhausted'], 1)
                circuit.search(lensa_job(), client)
                self.assertEqual([request.url.host for request in calls].count('api.tavily.com'), 1)
                # Serper's successful response is reused for the same query;
                # the Tavily quota error remains uncached and its circuit open.
                self.assertEqual([request.url.host for request in calls].count('google.serper.dev'), 1)
                self.assertEqual(provider_metrics(client)['serper']['search_requests'], 1)

    def test_no_tavily_results_is_success_not_serper_fallback(self):
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'mock-tavily'}):
            client, calls = self.tavily_client(tavily_payload={'results': []})
            with client:
                circuit = resolver.PublicSearchCircuit()
                self.assertEqual(circuit.search(lensa_job(), client), [])
                self.assertEqual(len(calls), 1)
                self.assertFalse(circuit.open)
                self.assertEqual(provider_metrics(client)['tavily']['no_result_searches'], 1)

    def test_tavily_redirect_does_not_leak_bearer_to_another_host(self):
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'mock-tavily', 'SERPER_API_KEY': ''}):
            called = []
            def handler(request):
                called.append(request.url.host)
                return httpx.Response(302, headers={'location': 'https://wrong.example/jobs'})
            with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
                with self.assertRaises(PublicSearchError):
                    resolver.search_candidates(lensa_job(), client)
            self.assertEqual(called, ['api.tavily.com'])

    def test_redirect_never_forwards_key(self):
        called=[]
        def handler(request):
            called.append(request.url.host)
            return httpx.Response(302, headers={'location':'https://wrong.example/jobs'})
        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            with self.assertRaises(PublicSearchError): resolver.search_candidates(lensa_job(), client)
        self.assertEqual(called, ['google.serper.dev'])

if __name__ == '__main__': unittest.main()
