"""Offline regressions for Pinpoint and Rippling hosted job postings (Issue 10).

All fixtures are synthetic or URL shapes captured by the Issue 9 pilot; nothing is fetched.
Recognizing a platform must never verify a posting by itself.
"""
import json
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx

from src import enrich_jobs
from src.enrichment import employer_careers as careers, official_job_resolver as resolver
from src.enrichment.ats import ATS_DISCOVERY_CACHE
from src.enrichment.job_description import extract_job_description_from_html
from src.enrichment.job_identity import validate_job_identity
from src.enrichment.verified_identity_recovery import (
    classify_missing_verified_identity, supported_specific_posting_key,
)
from urllib.parse import urlsplit
from src.verified_posting_identity import (
    fetched_posting_scope_valid, hosted_tenant_posting_scope, is_known_official_or_ats_url,
    is_specific_job_posting_url, verified_posting_key_from_url,
)

UUID1 = '0f1e2d3c-4b5a-4968-8776-655443322110'
UUID2 = '1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d'
PIN = f'https://widget.pinpointhq.com/postings/{UUID1}'
RIP = f'https://ats.rippling.com/widget/jobs/{UUID1}'
CORP = 'https://widget.example.com/'
PIN_TENANT, RIP_TENANT = 'https://widget.pinpointhq.com/', 'https://ats.rippling.com/widget/jobs'
# URL shapes captured by the Issue 9 pilot search results.
CAPTURED = ('https://grasshopper.pinpointhq.com/postings/f8344737-0bea-45f2-9fd2-b1cc92a9b71c',
            'https://ats.rippling.com/dataspring/jobs/e09b5d62-8b7c-44f4-ad8c-2390a4f732e4')
JD = 'Responsibilities: Build reliable SQL data pipelines. Requirements: Experience in data engineering. ' * 20
HTML = {'content-type': 'text/html; charset=utf-8'}


def posting_html(url, *, title='Data Engineer', company='Widget', identifier=None, canonical='same',
                 country='United States', website=CORP, json_ld=True):
    node = {'@type': 'JobPosting', 'title': title, 'description': JD,
            'hiringOrganization': {'@type': 'Organization', 'name': company, 'sameAs': website},
            'jobLocation': {'address': {'addressCountry': country}}}
    if identifier:
        node['identifier'] = {'value': identifier}
    head = f'<title>{title} - {company}</title>'
    if canonical:
        head += f'<link rel="canonical" href="{url if canonical == "same" else canonical}">'
    body = f'<script type="application/ld+json">{json.dumps(node)}</script>' if json_ld else f'<main>{JD}</main>'
    return head + body


def corporate_html(link):
    return (f'<title>Widget | Careers</title><meta property="og:url" content="{CORP}">'
            f'<a href="{link}">Careers</a>')


def job(company='Widget'):
    return dict(source='builtin', record_key='synthetic', company_name=company,
                title='Data Engineer', location='United States (Remote)', description=JD)


def resolve(pages, url, *, source=None, redirects=None):
    redirects = redirects or {}

    def handler(request):
        target = str(request.url)
        if target in redirects:
            return httpx.Response(302, headers={'location': redirects[target]})
        return httpx.Response(200, headers=HTML, text=pages.get(target, '')) if target in pages \
            else httpx.Response(404, text='')
    with patch.object(resolver, 'fetch_dynamic_job_description', return_value=None), \
            httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        return resolver.resolve_from_candidates(job(), client, [resolver.OfficialJobCandidate(
            url, 'Data Engineer', source or resolver.ats_type_for_url(url) or 'search')], 'search')


class Base(unittest.TestCase):
    def setUp(self):
        careers.CAREERS_CACHE.clear()
        ATS_DISCOVERY_CACHE.clear()


class RecognitionTests(Base):
    def test_pinpoint_and_rippling_postings_are_recognized_with_tenant_bound_keys(self):
        self.assertEqual(hosted_tenant_posting_scope(PIN), ('pinpoint', 'widget.pinpointhq.com', UUID1))
        self.assertEqual(hosted_tenant_posting_scope(RIP), ('rippling', 'widget', UUID1))
        self.assertEqual(verified_posting_key_from_url(PIN), f'pinpoint:widget.pinpointhq.com:{UUID1}')
        self.assertEqual(verified_posting_key_from_url(RIP), f'rippling:widget:{UUID1}')
        for url in (PIN, RIP):
            with self.subTest(url=url):
                self.assertTrue(is_specific_job_posting_url(url))
                self.assertEqual(verified_posting_key_from_url(url + '/'), verified_posting_key_from_url(url))
                self.assertEqual(verified_posting_key_from_url(url.replace(UUID1, UUID1.upper())),
                                 verified_posting_key_from_url(url))
                self.assertNotEqual(verified_posting_key_from_url(url.replace(UUID1, UUID2)),
                                    verified_posting_key_from_url(url))
                # Recognition is not trust: URL-only admission stays closed.
                self.assertFalse(is_known_official_or_ats_url(url))
        self.assertNotEqual(verified_posting_key_from_url(PIN.replace('widget.', 'other.')),
                            verified_posting_key_from_url(PIN))
        self.assertNotEqual(verified_posting_key_from_url(RIP.replace('/widget/', '/other/')),
                            verified_posting_key_from_url(RIP))

    def test_captured_pilot_urls_are_no_longer_discarded_by_discovery(self):
        for url, company in zip(CAPTURED, ('Grasshopper Bank', 'DataSpring')):
            with self.subTest(url=url):
                self.assertEqual(resolver.discovery_url_decision(url, company), (True, 'known_ats_discovery_only'))

    def test_generic_lookalike_and_unsafe_urls_are_rejected(self):
        bad = [
            'https://widget.pinpointhq.com/', 'https://widget.pinpointhq.com/postings',
            'https://widget.pinpointhq.com/postings/all', f'https://widget.pinpointhq.com/jobs/{UUID1}',
            f'https://widget.pinpointhq.com/postings/{UUID1}/apply', PIN.replace('https:', 'http:'),
            PIN.replace('.pinpointhq.com', '.pinpointhq.com.evil.com'), PIN.replace('https://', 'https://user@'),
            PIN.replace('.com/', '.com:8443/'), PIN.replace('widget.', 'www.'), PIN.replace('widget.', 'app.'),
            f'https://pinpointhq.com/postings/{UUID1}',
            'https://ats.rippling.com/', 'https://ats.rippling.com/widget/jobs',
            'https://ats.rippling.com/widget/jobs/not-a-posting', f'https://ats.rippling.com/jobs/{UUID1}',
            RIP.replace('https:', 'http:'), RIP.replace('ats.rippling.com', 'rippling.com'),
            RIP.replace('ats.rippling.com', 'ats.rippling.com.evil.com'),
            RIP.replace('ats.rippling.com', 'evil.ats.rippling.com'),
        ]
        for url in bad:
            with self.subTest(url=url):
                # Never a candidate: discovery and the posting scope both reject it.
                self.assertIsNone(hosted_tenant_posting_scope(url))
                self.assertFalse(resolver.discovery_url_decision(url, 'Widget')[0])
                parts = urlsplit(url)
                host = parts.hostname or ''
                if parts.username:
                    continue      # existing normalization keys the real host; discovery rejects it above
                if host.endswith(('pinpointhq.com', 'ats.rippling.com')):
                    # Platform hosts: only the exact posting form is specific or keyed.
                    self.assertFalse(is_specific_job_posting_url(url))
                    self.assertEqual(verified_posting_key_from_url(url), '')
                elif 'pinpointhq.com' in host or 'ats.rippling.com' in host:
                    # Lookalike hosts never yield a concrete key, even with employer provenance.
                    self.assertEqual(supported_specific_posting_key(url, allow_employer=True), '')

    def test_parsing_extracts_posting_owned_evidence(self):
        for url in (PIN, RIP):
            with self.subTest(url=url):
                result = extract_job_description_from_html(url, url, 200, posting_html(url, identifier=UUID1))
                self.assertEqual(result.status, 'enriched')
                self.assertEqual((result.resolved_title, result.resolved_company, result.resolved_job_id),
                                 ('Data Engineer', 'Widget', UUID1))
                self.assertIn('United States', result.resolved_location)
                self.assertEqual(result.resolved_company_source, 'jobposting_json_ld')
                self.assertEqual(result.canonical_url, url)
                self.assertEqual(result.resolved_employer_website, CORP)
                self.assertGreater(result.word_count, 100)


class FetchedScopeTests(Base):
    def proof(self, careers_url, **extra):
        return dict(company='widget', corporate_url=CORP, careers_url=careers_url,
                    basis='fetched_reciprocal_employer_careers_link', **extra)

    def test_fetched_identity_and_exact_tenant_provenance_are_both_required(self):
        for url, tenant, other in ((PIN, PIN_TENANT, 'https://other.pinpointhq.com/'),
                                   (RIP, RIP_TENANT, 'https://ats.rippling.com/other/jobs')):
            with self.subTest(url=url):
                result = extract_job_description_from_html(url, url, 200, posting_html(url))
                self.assertTrue(fetched_posting_scope_valid(url, result, self.proof(tenant)))
                # Bound by identifier when no canonical link is declared.
                by_id = extract_job_description_from_html(url, url, 200, posting_html(url, canonical=None, identifier=UUID1))
                self.assertTrue(fetched_posting_scope_valid(url, by_id, self.proof(tenant)))
                rejected = {
                    'no provenance': (result, None),
                    'other tenant': (result, self.proof(other)),
                    'company mismatch': (result, dict(self.proof(tenant), company='unrelated')),
                    'unknown basis': (result, dict(self.proof(tenant), basis='search_snippet')),
                    'unbound posting': (extract_job_description_from_html(url, url, 200, posting_html(url, canonical=None)),
                                        self.proof(tenant)),
                    'other canonical': (extract_job_description_from_html(url, url, 200, posting_html(
                        url, canonical=url.replace(UUID1, UUID2))), self.proof(tenant)),
                    'other identifier': (extract_job_description_from_html(url, url, 200, posting_html(
                        url, canonical=None, identifier=UUID2)), self.proof(tenant)),
                    'no structured posting': (extract_job_description_from_html(url, url, 200, posting_html(
                        url, json_ld=False)), self.proof(tenant)),
                }
                for name, (res, proof) in rejected.items():
                    with self.subTest(case=name):
                        self.assertFalse(fetched_posting_scope_valid(url, res, proof))
                self.assertFalse(fetched_posting_scope_valid(url + f'?jobid={UUID2}', result, self.proof(tenant)))

    def test_save_rechecks_hosted_scope_before_opening_the_database(self):
        parsed = extract_job_description_from_html(PIN, PIN, 200, posting_html(PIN))
        identity = validate_job_identity(original_title='Data Engineer', original_company='Widget',
                                         resolved_title='Data Engineer', resolved_company='Widget')
        found = resolver.OfficialJobResolutionResult('FOUND_VERIFIED', PIN, 'employer', resolver.now_utc(), 1.0,
                                                     'identity accepted', 'Data Engineer', 'Widget', 'United States',
                                                     parsed, identity)
        with patch.object(enrich_jobs, 'get_connection', side_effect=AssertionError('No DB expected')):
            with self.assertRaises(ValueError):
                enrich_jobs.save_enrichment_attempt(job(), parsed, official_resolution=found,
                                                   identity_validation=identity)

    def test_stored_rows_cannot_be_recovered_without_fresh_verification(self):
        for url in (PIN, RIP):
            finding = classify_missing_verified_identity(dict(
                record_key='r', official_url_status='FOUND_VERIFIED', official_job_url=url,
                official_url_source='employer', official_url_confidence=1.0))
            self.assertIn('requires fresh fetched', finding.reason)


class ResolutionTests(Base):
    def test_search_found_posting_verifies_only_with_reciprocal_corporate_link(self):
        for url, tenant, other in ((PIN, PIN_TENANT, 'https://other.pinpointhq.com/'),
                                   (RIP, RIP_TENANT, 'https://ats.rippling.com/other/jobs')):
            for corporate, accepted in ((corporate_html(tenant), True), (corporate_html(other), False),
                                        ('<title>Widget | Careers</title>', False),
                                        (f'<title>Unrelated</title><meta property="og:url" content="{CORP}">'
                                         f'<a href="{tenant}">Careers</a>', False)):
                with self.subTest(url=url, corporate=corporate[:60]):
                    careers.CAREERS_CACHE.clear()
                    found = resolve({url: posting_html(url), CORP: corporate}, url)
                    self.assertEqual(found.status == 'FOUND_VERIFIED', accepted, found.official_url_validation_reason)
                    if accepted:
                        self.assertEqual(found.employer_provenance['basis'], 'fetched_reciprocal_employer_careers_link')
                        self.assertEqual(verified_posting_key_from_url(found.official_job_url),
                                         verified_posting_key_from_url(url))

    def test_wrong_title_employer_location_or_requisition_is_not_verified(self):
        for url, tenant in ((PIN, PIN_TENANT), (RIP, RIP_TENANT)):
            for html in (posting_html(url, title='Dental Hygienist'), posting_html(url, company='Unrelated Bank'),
                         posting_html(url, country='Canada'),
                         posting_html(url, canonical=url.replace(UUID1, UUID2)),
                         posting_html(url, canonical=None, identifier=UUID2)):
                with self.subTest(url=url, html=html[:70]):
                    careers.CAREERS_CACHE.clear()
                    found = resolve({url: html, CORP: corporate_html(tenant)}, url)
                    self.assertNotEqual(found.status, 'FOUND_VERIFIED')

    def test_redirects_to_another_tenant_or_unsafe_target_are_not_verified(self):
        for url, moved in ((PIN, PIN.replace('widget.', 'other.')),
                           (PIN, PIN.replace('https:', 'http:')),
                           (RIP, RIP.replace('/widget/', '/other/')),
                           (RIP, RIP.replace('ats.rippling.com', 'ats.rippling.com.evil.com'))):
            with self.subTest(moved=moved):
                careers.CAREERS_CACHE.clear()
                found = resolve({moved: posting_html(moved), CORP: corporate_html(PIN_TENANT if 'pinpoint' in url else RIP_TENANT)},
                                url, redirects={url: moved})
                self.assertNotEqual(found.status, 'FOUND_VERIFIED')


class DiscoveryIntegrationTests(Base):
    def test_public_search_fallback_now_selects_hosted_postings_as_candidates_only(self):
        payload = {'results': [{'url': u, 'title': 'Data Engineer'} for u in
                               (PIN, RIP, 'https://ats.rippling.com/widget/jobs', 'https://www.indeed.com/viewjob?jk=1')]}
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'offline-test', 'SERPER_API_KEY': ''}), \
                httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
            found = resolver.search_candidates(job(), client, provider='tavily')
        self.assertEqual([(c.url, c.source) for c in found], [(PIN, 'pinpoint'), (RIP, 'rippling')])
        self.assertTrue(all(c.employer_provenance is None for c in found))

    def test_employer_first_discovery_binds_candidates_to_the_published_tenant(self):
        corporate_root = ('<script type="application/ld+json">' + json.dumps(
            {'@type': 'Organization', 'name': 'Widget', 'url': CORP}) + '</script>'
            f'<a href="{RIP_TENANT}">Careers</a>')
        inventory = (f'<title>Widget jobs</title><a href="/widget/jobs/{UUID1}">Data Engineer</a>'
                     f'<a href="/other/jobs/{UUID2}">Data Engineer</a>')
        pages = {CORP: corporate_root, RIP_TENANT: inventory, RIP: posting_html(RIP)}
        with patch.object(resolver, 'fetch_dynamic_job_description', return_value=None), \
                httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers=HTML, text=pages[str(r.url)])
                                                           if str(r.url) in pages else httpx.Response(404))) as client:
            candidates = careers.discover_employer_candidates(job(), client, corporate_leads=[CORP])
            self.assertEqual([c.url for c in candidates], [RIP])
            self.assertEqual(candidates[0].source, 'employer')
            found = resolver.resolve_from_candidates(job(), client, candidates, 'employer')
        self.assertEqual(found.status, 'FOUND_VERIFIED', found.official_url_validation_reason)
        self.assertEqual(found.employer_provenance['basis'], 'fetched_corporate_brand_and_published_careers_link')


if __name__ == '__main__':
    unittest.main()
