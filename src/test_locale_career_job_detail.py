"""Locale-prefixed employer career postings (e.g. /en_US/careers/JobDetail/<slug>/<id>).

Discovery-only recognition: the URL may become a search candidate, but identity,
requisition and provenance checks are unchanged and still decide verification.
Offline: all HTTP is mocked.
"""
import json
import os
import unittest
from unittest.mock import patch

import httpx

from src.enrichment import employer_careers as careers, official_job_resolver as resolver
from src.enrichment.ats import ATS_DISCOVERY_CACHE
from src.enrichment.verified_identity_recovery import (
    classify_missing_verified_identity, supported_specific_posting_key,
)
from src.verified_posting_identity import is_specific_job_posting_url, verified_posting_key_from_url

# Captured by the Nov 24 Glassdoor verification diagnosis (search result, rejected before fetch).
MANTECH = 'https://careers.mantech.com/en_US/careers/JobDetail/Lead-Data-Engineer/66423'
HOST = 'https://careers.mantech.com'
JD = 'Responsibilities: Build reliable SQL data pipelines. Requirements: Experience in data engineering. ' * 20


def job():
    return dict(source='glassdoor', record_key='synthetic', company_name='MANTECH',
                title='Lead Data Engineer', location='Tampa, FL', description='')


def posting_html(url, title='Lead Data Engineer', company='MANTECH', identifier='66423'):
    node = {'@type': 'JobPosting', 'title': title, 'description': JD,
            'hiringOrganization': {'@type': 'Organization', 'name': company},
            'jobLocation': {'address': {'addressLocality': 'Tampa', 'addressRegion': 'FL',
                                        'addressCountry': 'US'}},
            'identifier': {'value': identifier}}
    return (f'<title>{title} - {company}</title><link rel="canonical" href="{url}">'
            f'<script type="application/ld+json">{json.dumps(node)}</script>')


class Base(unittest.TestCase):
    def setUp(self):
        careers.CAREERS_CACHE.clear()
        ATS_DISCOVERY_CACHE.clear()


class DiscoveryRecognitionTests(Base):
    def test_exact_mantech_posting_is_a_discovery_candidate(self):
        self.assertTrue(resolver.looks_like_individual_posting_url(MANTECH))
        self.assertEqual(resolver.discovery_url_decision(MANTECH, 'MANTECH'),
                         (True, 'employer_hostname_token_discovery_only'))

    def test_equivalent_locale_prefixed_postings_are_accepted(self):
        for path in ('/en-US/careers/JobDetail/Lead-Data-Engineer/66423',
                     '/en_us/careers/jobdetail/lead-data-engineer/66423',
                     '/en_GB/careers/JobDetail/Data-Engineer/1',
                     '/fr-CA/careers/JobDetail/Ingenieur-Donnees/70001',
                     '/de/careers/JobDetail/Dateningenieur/70002',
                     '/en_US/career/JobDetail/Lead-Data-Engineer/66423',
                     '/en_US/careers/JobDetail/66423'):
            with self.subTest(path=path):
                self.assertTrue(resolver.looks_like_individual_posting_url(HOST + path))
                self.assertTrue(resolver.discovery_url_decision(HOST + path, 'MANTECH')[0])

    def test_generic_career_landing_pages_remain_rejected(self):
        for path in ('/', '/en_US', '/en_US/careers', '/en_US/careers/Home',
                     '/en_US/careers/JobDetail', '/en_US/careers/JobDetail/Lead-Data-Engineer',
                     '/en_US/careers/JobDetail/Lead-Data-Engineer/66423/apply',
                     '/en_US/careers/JobDetail/Lead-Data-Engineer/66423a',
                     '/en_US/about/JobDetail/Lead-Data-Engineer/66423',
                     '/en_US/careers/join-our-team', '/en_US/careers/life-at/66423'):
            with self.subTest(path=path):
                self.assertFalse(resolver.looks_like_individual_posting_url(HOST + path))
                self.assertEqual(resolver.discovery_url_decision(HOST + path, 'MANTECH'),
                                 (False, 'unsupported_posting_path'))

    def test_career_search_and_listing_pages_remain_rejected(self):
        for path in ('/en_US/careers/SearchJobs', '/en_US/careers/SearchJobs/data-engineer',
                     '/en_US/careers/search-jobs', '/en_US/careers/job-listings',
                     '/en_US/careers/jobs/search', '/en_US/careers/all-jobs',
                     '/en_US/careers/JobDetail/search-results/66423'):
            with self.subTest(path=path):
                self.assertFalse(resolver.looks_like_individual_posting_url(HOST + path))
                self.assertFalse(resolver.discovery_url_decision(HOST + path, 'MANTECH')[0])

    def test_aggregator_domains_remain_rejected(self):
        path = '/en_US/careers/JobDetail/Lead-Data-Engineer/66423'
        for host in ('https://www.glassdoor.com', 'https://builtin.com', 'https://jobright.ai',
                     'https://www.dice.com'):
            with self.subTest(host=host):
                self.assertEqual(resolver.discovery_url_decision(host + path, 'MANTECH'),
                                 (False, 'third_party_domain'))

    def test_invalid_or_unsupported_urls_remain_rejected(self):
        path = '/en_US/careers/JobDetail/Lead-Data-Engineer/66423'
        for url in ('', 'not a url', 'ftp://careers.mantech.com' + path, 'careers.mantech.com' + path,
                    HOST + '/english/careers/JobDetail/Lead-Data-Engineer/66423',
                    HOST + '/e1_US/careers/JobDetail/Lead-Data-Engineer/66423',
                    HOST + '/en_USA/careers/JobDetail/Lead-Data-Engineer/66423'):
            with self.subTest(url=url):
                self.assertFalse(resolver.discovery_url_decision(url, 'MANTECH')[0])
        # A valid posting path on a host without employer or ATS evidence is still not a candidate.
        self.assertEqual(resolver.discovery_url_decision('https://careers.example.org' + path, 'MANTECH'),
                         (False, 'unrecognized_ats_or_employer_domain'))


class IdentityUnchangedTests(Base):
    def test_posting_identity_helpers_are_unchanged(self):
        # Values captured before the discovery change; recognition must not alter keys.
        key = 'official-url:' + MANTECH
        self.assertTrue(is_specific_job_posting_url(MANTECH))
        self.assertEqual(verified_posting_key_from_url(MANTECH), key)
        self.assertEqual(supported_specific_posting_key(MANTECH, allow_employer=True), key)
        self.assertNotEqual(verified_posting_key_from_url(MANTECH.replace('66423', '66424')), key)

    def test_stored_url_alone_cannot_recover_a_trusted_posting_key(self):
        # Same result as before the discovery change: a stored URL is not proof.
        finding = classify_missing_verified_identity(dict(
            record_key='r', official_url_status='FOUND_VERIFIED', official_job_url=MANTECH,
            official_url_source='search', official_url_confidence=1.0))
        self.assertEqual(finding.derived_key, '')
        self.assertIsNone(finding.proposed_trust)
        self.assertEqual(finding.reason, 'official URL is not a supported specific posting')


class VerificationNotGrantedByDiscoveryTests(Base):
    def resolve(self, pages):
        def handler(request):
            target = str(request.url)
            return (httpx.Response(200, headers={'content-type': 'text/html'}, text=pages[target])
                    if target in pages else httpx.Response(404, text=''))
        with patch.object(resolver, 'fetch_dynamic_job_description', return_value=None), \
                httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            return resolver.resolve_from_candidates(job(), client, [resolver.OfficialJobCandidate(
                MANTECH, 'Lead Data Engineer', 'search')], 'search')

    def test_discovery_acceptance_alone_never_yields_found_verified(self):
        cases = {
            'unreachable': {},
            'matching page without employer provenance': {MANTECH: posting_html(MANTECH)},
            'different title': {MANTECH: posting_html(MANTECH, title='Dental Hygienist')},
            'different employer': {MANTECH: posting_html(MANTECH, company='Unrelated Bank')},
            'different requisition': {MANTECH: posting_html(
                MANTECH.replace('66423', '99999'), identifier='99999')},
        }
        for name, pages in cases.items():
            with self.subTest(case=name):
                careers.CAREERS_CACHE.clear()
                found = self.resolve(pages)
                self.assertNotEqual(found.status, 'FOUND_VERIFIED', found.official_url_validation_reason)

    def test_search_now_forwards_the_posting_as_an_unproven_candidate_only(self):
        urls = (MANTECH, HOST + '/en_US/careers/Home', HOST + '/en_US/careers/SearchJobs',
                'https://www.glassdoor.com/Jobs/ManTech-data-engineer-Jobs-EI_IE13880.0,7_KO8,21.htm')
        payload = {'results': [{'url': u, 'title': 'Lead Data Engineer'} for u in urls]}
        with patch.dict(os.environ, {'TAVILY_API_KEY': 'offline-test', 'SERPER_API_KEY': ''}), \
                httpx.Client(transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json=payload))) as client:
            found = resolver.search_candidates(job(), client, provider='tavily')
        self.assertEqual([c.url for c in found], [MANTECH])
        self.assertIsNone(found[0].employer_provenance)


if __name__ == '__main__':
    unittest.main()
