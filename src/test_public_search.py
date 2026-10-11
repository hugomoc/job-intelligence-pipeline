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
        env = patch.dict(os.environ, {'SERPER_API_KEY': 'mock-secret'})
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
            self.assertEqual(candidates[0].label, '')
            self.assertNotIn('official_job_url', lensa_job())
            trace_job=lensa_job()
            resolver.search_candidates(trace_job,client)
            self.assertEqual(trace_job['public_search_trace']['query'], 'Seismic Senior Analytics Engineer jobs')
            self.assertEqual(trace_job['public_search_trace']['organic_results'][0]['title'], 'Senior Analytics Engineer')
            self.assertTrue(trace_job['public_search_trace']['organic_results'][0]['candidate'])
        with patch.dict(os.environ, {'SERPER_API_KEY': ''}):
            self.assertEqual(selected_provider(), 'duckduckgo')

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
                self.assertFalse(circuit.open)

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
        with patch.dict(os.environ, {'SERPER_API_KEY': ''}), \
             httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,text='Bots use DuckDuckGo too'))) as client:
            with self.assertRaises(PermissionError): circuit.search(lensa_job(), client)
            self.assertTrue(circuit.open)
        with self.client() as client:
            self.assertFalse(circuit.open)
            self.assertEqual(len(circuit.search(lensa_job(), client)), 1)
        self.assertTrue(circuit.states['duckduckgo']['open'])

    def test_redirect_never_forwards_key(self):
        called=[]
        def handler(request):
            called.append(request.url.host)
            return httpx.Response(302, headers={'location':'https://wrong.example/jobs'})
        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            with self.assertRaises(PublicSearchError): resolver.search_candidates(lensa_job(), client)
        self.assertEqual(called, ['google.serper.dev'])

if __name__ == '__main__': unittest.main()
