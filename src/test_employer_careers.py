"""Offline corporate/inventory/provenance and application-route regressions."""
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from src.actionability import check_job, Settings
from src.enrichment import employer_careers as careers
from src.enrichment import official_job_resolver as resolver
from src.enrichment.ats import ATS_DISCOVERY_CACHE, discover_ats_candidates, ADAPTERS
from src.enrichment.job_description import extract_job_description_from_html
from src.enrichment.job_identity import compare_posting_evidence, description_similarity
from src.verified_posting_identity import posting_urls_equivalent, verified_posting_key_from_url

CORP = 'https://examplewidget.com/'
CAREERS = 'https://careers.examplewidget.com/'
SEARCH = CAREERS + 'search?k=Data+Engineer&board=12'
POST = CAREERS + 'job/allen/data-engineer/12345'
APPLY = 'https://examplewidget.avature.net/careers/ApplicationMethods?jobId=456'
DESCRIPTION = 'Responsibilities include building SQL pipelines and data models. Qualifications: Python and SQL engineering experience. ' * 30


def original(**kw):
    return dict(title='Data Engineer', company_name='Example Widget', location='Allen, TX', source='glassdoor', **kw)


def posting(company='Example Widget', req='456', canonical=POST, extra=''):
    node = dict({'@type': 'JobPosting'}, title='Data Engineer', hiringOrganization={'name': company},
                identifier={'value': req}, jobLocation={'name': 'Allen, Texas, United States'}, description=DESCRIPTION)
    return '<title>Data Engineer</title><link rel="canonical" href="' + canonical + '"><script type="application/ld+json">' + json.dumps(node) + '</script>' + extra


def trusted(**kw):
    job = original()
    job.update(official_job_url=POST, official_url_status='FOUND_VERIFIED', official_url_source='employer',
               official_resolved_company='Example Widget', verified_posting_key=verified_posting_key_from_url(POST),
               verified_posting_key_source='official_found_verified', verified_posting_key_trust='trusted',
               employer_careers_provenance=dict(company='example widget', corporate_url=CORP, careers_url=CAREERS))
    job.update(kw)
    return job


def target(req='456', role='Data Engineer', backlink=CAREERS, controls=True):
    return (f'<title>Applying to {role}</title><meta property="og:url" content="{APPLY.replace("456", req)}">'
            f'<a href="{backlink}">Employer careers</a><h1>{role}</h1><h2>Choose an option to apply</h2>'
            + ('<a role="button" tabindex="0">Copy &amp; Paste</a><form method="post"></form>' if controls else ''))


class Tests(unittest.TestCase):
    def setUp(self):
        careers.CAREERS_CACHE.clear()
        ATS_DISCOVERY_CACHE.clear()

    def client(self, mapping):
        self.requests = []
        def respond(req):
            self.requests.append(str(req.url))
            response = mapping.get(str(req.url))
            if isinstance(response, httpx.Response):
                return response
            return httpx.Response(200 if response is not None else 404, text=response or '', headers={'content-type': 'text/html'})
        return httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True)

    def pages(self):
        corporate = '<script type="application/ld+json">' + json.dumps({'@type': 'Organization', 'name': 'Example Widget', 'url': CORP}) + '</script><a href="' + CAREERS + '">Careers</a>'
        home = ('<title>Example Widget Careers</title><a href="' + CORP + '">Corporate</a>'
                '<form action="/search"><input type="search" name="k"><input type="hidden" name="board" value="12"></form>')
        return {CORP: corporate, CAREERS: home, SEARCH: f'<title>Example Widget</title><a href="{POST}">Data Engineer</a>',
                POST: posting(extra=f'<a href="{APPLY}">Apply</a>'), APPLY: target()}

    def test_independent_end_to_end_no_search_or_known_posting_seed(self):
        job = original()
        with self.client(self.pages()) as client, patch.object(resolver, 'search_candidates', side_effect=AssertionError('No search')), patch.object(resolver, 'discover_ats_candidates', side_effect=AssertionError('No guessing')):
            result = resolver.resolve_official_job(job, client)
            self.assertEqual(result.status, 'FOUND_VERIFIED')
            self.assertEqual(result.description_result.description, DESCRIPTION.strip())
            self.assertEqual(self.requests[:3], [CORP, CAREERS, SEARCH])
            self.assertEqual(result.employer_provenance['inventory_url'], SEARCH)
            self.assertIn('unproven', result.posting_identity_evidence['historical_match'])
            active = trusted(employer_careers_provenance=job['employer_careers_provenance'])
            checked = check_job(active, client, Settings())
            self.assertEqual(checked.status, 'ACTIVE')
            self.assertEqual(checked.evidence['application_url'], APPLY)
            self.assertFalse(checked.evidence['application_submission_tested'])

    # S2 R1: company-token parent-site probes remain discovery hints only.
    def bask_pages(self, *, branding=True, posting_host=None):
        apex = 'https://bask.health/'
        careers_url = 'https://jobs.bask.health/careers'
        post_url = 'https://jobs.bask.health/careers/905C5C2826'
        # The fetched page, not the search result, supplies ownership evidence.
        organization = {'@type': 'Organization',
                        'name': 'Bask Health' if branding else 'Wrong Employer',
                        'url': apex}
        return {
            apex: '<script type="application/ld+json">' + json.dumps(organization) + '</script>'
                  + f'<a href="{careers_url}">Careers</a>',
            careers_url: '<title>Bask Health Careers</title><a href="' + apex + '">Corporate</a>'
                         + '<a href="' + (posting_host or post_url) + '">Data Engineer</a>',
            post_url: posting(company='Bask Health', req='905C5C2826', canonical=post_url),
        }

    def test_s2_parent_site_probe_establishes_existing_employer_chain(self):
        job = original() | {'company_name': 'Bask Health'}
        pages = self.bask_pages()
        with self.client(pages) as client:
            candidates = careers.discover_employer_candidates(
                job, client, corporate_leads=['https://jobs.bask.health/careers'])
        self.assertIn('https://bask.health/', self.requests)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source, 'employer')
        self.assertEqual(candidates[0].employer_provenance['corporate_url'], 'https://bask.health/')
        reasons = [event.get('reason') for event in job['employer_discovery_trace']]
        self.assertIn('corporate_brand_confirmed', reasons)
        self.assertIn('published_careers_link_found', reasons)
        self.assertIn('careers_inventory_candidates', reasons)

    def test_s2_missing_or_mismatched_corporate_brand_never_grants_provenance(self):
        for wrong in ('wrong_name', 'wrong_host', 'no_org'):
            with self.subTest(wrong=wrong):
                careers.CAREERS_CACHE.clear()
                job = original() | {'company_name': 'Bask Health'}
                pages = self.bask_pages()
                apex = 'https://bask.health/'
                if wrong == 'wrong_name':
                    pages[apex] = pages[apex].replace('Bask Health', 'Other Company')
                elif wrong == 'wrong_host':
                    pages[apex] = pages[apex].replace('"url": "https://bask.health/"',
                                                     '"url": "https://unrelated.com/"')
                else:
                    pages[apex] = '<title>Bask Health</title><a href="https://jobs.bask.health/careers">Careers</a>'
                with self.client(pages) as client:
                    self.assertFalse(careers.discover_employer_candidates(
                        job, client, corporate_leads=['https://jobs.bask.health/careers']))
                self.assertIn('corporate_brand_unconfirmed',
                              [event.get('reason') for event in job['employer_discovery_trace']])

    def test_s2_aggregator_corporate_leads_filtered_before_two_slot_limit(self):
        leads = ['https://indeed.com/viewjob?jk=123', 'https://talent.com/view',
                 'https://jobgether.com/offer/42', 'https://tavily.com/knowledge/company/bask',
                 'https://jobs.bask.health/careers']
        for url in leads[:4]:
            self.assertTrue(careers.excluded_corporate_lead(url))
        self.assertFalse(careers.excluded_corporate_lead(leads[4]))
        job = original() | {'company_name': 'Bask Health'}
        with self.client(self.bask_pages()) as client:
            candidates = careers.discover_employer_candidates(job, client, corporate_leads=leads)
        self.assertEqual(len(candidates), 1)
        self.assertNotIn('https://indeed.com/viewjob?jk=123', self.requests)
        self.assertIn('https://bask.health/', self.requests)

    def test_s2_derived_parents_never_displace_valid_guessed_com_root(self):
        first = 'https://jobs.examplewidget.org/careers'
        second = 'https://careers.examplewidget.net/search'
        with self.client(self.pages()) as client:
            candidates = careers.discover_employer_candidates(
                original(), client, corporate_leads=[first, second])
        self.assertEqual([c.url for c in candidates], [POST])
        self.assertIn(CORP, self.requests)
        self.assertLess(self.requests.index(CORP), self.requests.index(CAREERS))

    def test_s2_derived_probe_strict_host_tokens_and_public_urls(self):
        self.assertEqual(careers.parent_roots_from_careers_leads(
            'Allia Health Group', ['https://www.unitedhealthgroup.com/careers']), [])
        self.assertEqual(careers.parent_roots_from_careers_leads(
            'General Motors (GM)', ['https://search-careers.gm.com/en/jobs']), [])
        self.assertEqual(careers.parent_roots_from_careers_leads(
            'Bask Health', ['https://bask.health/', 'https://jobgether.com/offer']), [])
        self.assertEqual(careers.parent_roots_from_careers_leads(
            'Sevita', ['https://jobs.sevitahealth.com/us/en/c/information-technology-jobs']),
            ['https://sevitahealth.com/'])
        self.assertEqual(careers.parent_roots_from_careers_leads(
            'Bask Health', ['https://127.0.0.1/jobs', 'http://jobs.bask.health/']), [])

    def test_s2_employer_posting_on_unrelated_host_cannot_inherit_proof(self):
        wrong = 'https://unrelated.com/jobs/data-engineer/99881'
        job = original() | {'company_name': 'Bask Health'}
        with self.client(self.bask_pages(posting_host=wrong)) as client:
            candidates = careers.discover_employer_candidates(
                job, client, corporate_leads=['https://jobs.bask.health/careers'])
        self.assertEqual(candidates, [])
        self.assertNotIn(wrong, self.requests)

    def test_s2_search_posting_still_rejected_without_corporate_proof(self):
        from src.enrichment.ats.base import OfficialJobCandidate
        post_url = 'https://jobs.bask.health/careers/905C5C2826'
        job = original() | {'company_name': 'Bask Health',
                            'public_search_trace': {'organic_results': [
                                {'url': 'https://jobs.bask.health/careers'}]}}
        pages = self.bask_pages(branding=False)
        with self.client(pages) as client, \
             patch.object(resolver, 'discover_ats_candidates', return_value=[]), \
             patch.object(resolver, 'search_candidates', return_value=[
                 OfficialJobCandidate(post_url, 'Data Engineer', 'search')]):
            result = resolver.resolve_official_job(job, client)
        self.assertNotEqual(result.status, 'FOUND_VERIFIED')
        self.assertIsNone(result.official_job_url)
        self.assertIn('corporate_brand_unconfirmed',
                      [event.get('reason') for event in job['employer_discovery_trace']])

    def test_s2_employer_chain_trace_reaches_run_log_safely(self):
        from src import enrich_jobs
        job = original() | {'record_key': 's2-test'}

        def failed_with_trace(**kwargs):
            job['employer_discovery_trace'] = [
                dict(url='https://jobs.bask.health/careers?token=private-secret',
                     method='GET', http_status=200),
                dict(url='https://bask.health/', reason='corporate_brand_unconfirmed'),
                dict(url='https://jobs.bask.health/careers', reason='careers_inventory_candidates',
                     candidates_found=0),
            ]
            raise RuntimeError('synthetic offline error')

        with patch.object(enrich_jobs, 'process_enrichment_job', side_effect=failed_with_trace), \
             patch.object(enrich_jobs, 'save_enrichment_attempt', return_value=None):
            summary = enrich_jobs.process_enrichment_batch([job], None)
        lines = [line for line in summary.log_lines if 'Employer chain diagnostic:' in line]
        self.assertEqual(len(lines), 3)
        self.assertIn('jobs.bask.health/careers', lines[0])
        self.assertNotIn('token=private-secret', '\n'.join(lines))
        self.assertIn('corporate_brand_unconfirmed', lines[1])
        self.assertIn('careers_inventory_candidates', lines[2])
        self.assertIn('"candidates_found": 0', lines[2])

    # S2 R1 corrections (independent review): per-root guard, token rule,
    # request budget, brand-failure category and per-attempt trace lifecycle.
    def test_s2_aggregator_named_employers_make_no_aggregator_request(self):
        for company, host in (('Glassdoor', 'glassdoor.com'), ('Lensa', 'lensa.com')):
            with self.subTest(company=company):
                self.assertIn(f'https://{host}/', careers.corporate_roots(company))
                job = original() | {'company_name': company}
                with self.client({}) as client:
                    careers.discover_employer_candidates(
                        job, client, corporate_leads=[f'https://www.{host}/Overview/{company}'])
                self.assertFalse([u for u in self.requests if host in u])

    def test_s2_per_root_guard_covers_every_root_source(self):
        unsafe = ['https://glassdoor.com/', 'https://www.lensa.com/', 'http://bask.health/',
                  'https://user:pw@bask.health/', 'https://127.0.0.1/', 'https://bask.local/']
        job = original() | {'company_name': 'Bask Health'}
        # Even if an upstream filter regressed, no unsafe root reaches the transport.
        with self.client({}) as client, \
             patch.object(careers, 'corporate_roots', return_value=unsafe[:3]), \
             patch.object(careers, 'parent_roots_from_careers_leads', return_value=unsafe[3:]):
            careers.discover_employer_candidates(job, client, corporate_leads=unsafe)
        self.assertEqual(self.requests, [])

    def test_s2_similarly_named_corporate_domains_remain_eligible(self):
        for url in ('https://careers.myindeed.com/', 'https://jobs.talentcorp.com/',
                    'https://lensatechnology.com/', 'https://glassdoorwindows.com/'):
            self.assertFalse(careers.excluded_corporate_lead(url), url)
            self.assertFalse(resolver.is_aggregator_domain(url), url)
        job = original() | {'company_name': 'Lensa Technology'}
        with self.client({}) as client:
            careers.discover_employer_candidates(
                job, client, corporate_leads=['https://jobs.lensatechnology.com/open'])
        # The lead and the guessed .com (also the derived parent, deduplicated).
        self.assertEqual(self.requests, ['https://jobs.lensatechnology.com/open',
                                         'https://lensatechnology.com/'])

    def test_s2_first_token_rule_is_significant_prefix_only(self):
        derive = careers.parent_roots_from_careers_leads
        # Preserved recoveries.
        self.assertEqual(derive('Bask Health', ['https://jobs.bask.health/careers']), ['https://bask.health/'])
        self.assertEqual(derive('Sevita', ['https://jobs.sevitahealth.com/us/en']), ['https://sevitahealth.com/'])
        # Articles and stop-words are skipped; short tokens never probe.
        self.assertEqual(derive('A Cloud Guru', ['https://jobs.datastaffing.com/x']), [])
        self.assertEqual(derive('The Home Depot', ['https://careers.homedepot.com/job/1']),
                         ['https://homedepot.com/'])
        self.assertEqual(derive('The Home Depot', ['https://careers.thehomedepotjobs.com/']), [])
        for company, lead in (('AT&T', 'https://jobs.att.com/'), ('HP', 'https://jobs.hp.com/'),
                              ('GE Healthcare', 'https://jobs.gehealthcare.com/')):
            self.assertEqual(derive(company, [lead]), [], company)
        # Prefix, not substring.
        self.assertEqual(derive('Sevita', ['https://jobs.notsevitahealth.com/']), [])
        self.assertEqual(derive('Mercury', ['https://careers.hgmercury.com/']), [])
        # Suffix forms: a public suffix never becomes a probe; invalid hosts are unsafe.
        self.assertEqual(derive('Bask Health', ['https://bask.co.uk/']), [])
        self.assertEqual(derive('Bask Health', ['https://jobs.bask.co.uk/']), ['https://bask.co.uk/'])
        for lead in ('https://jobs.bask-.health/', 'https://jobs.bask.health:8443/',
                     'https://jobs.bask.internal/', 'https://www.glassdoor.com/bask'):
            self.assertEqual(derive('Bask Health', [lead]), [], lead)

    def redirect_client(self, branded=None):
        """Every unknown URL redirects forever (each hop is a counted GET)."""
        self.requests = []
        branded = branded or {}
        def respond(req):
            url = str(req.url)
            self.requests.append(url)
            if url in branded:
                return httpx.Response(200, text=branded[url], headers={'content-type': 'text/html'})
            return httpx.Response(301, headers={'location': url.rstrip('/') + '/hop'})
        return httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True)

    def test_s2_twelve_counted_gets_per_pass_including_redirects_and_parents(self):
        leads = ['https://jobs.bask.health/careers', 'https://careers.baskhr.com/open']
        job = original() | {'company_name': 'Bask Health'}
        with self.redirect_client() as client:
            careers.discover_employer_candidates(job, client, corporate_leads=leads)
            first_pass = list(self.requests)
            # Second pass on the same client: its own 12-GET ceiling, cache reused.
            careers.discover_employer_candidates(
                job, client, corporate_leads=['https://jobs.baskone.com/a', 'https://careers.basktwo.com/b'])
        self.assertEqual(len(first_pass), 12)
        # Deterministic: leads, guessed .com, then derived parents; 3 GETs per root.
        self.assertEqual([first_pass[i].split('/hop')[0] for i in (0, 3, 6, 9)],
                         [*leads, 'https://baskhealth.com/', 'https://bask.health/'])
        exhausted = [e['url'] for e in job['employer_discovery_trace']
                     if e.get('reason') == 'public_request_budget_exhausted']
        self.assertEqual(exhausted[0], 'https://baskhr.com/')
        second_pass = self.requests[12:]
        self.assertEqual(len(second_pass), 12)
        self.assertNotIn('https://baskhealth.com/', second_pass)  # cached: no new request
        self.assertLessEqual(len(self.requests), 24)

    def test_s2_correct_guessed_com_stays_reachable_under_redirect_pressure(self):
        corp = 'https://baskhealth.com/'
        site = {corp: '<script type="application/ld+json">' + json.dumps(
                    {'@type': 'Organization', 'name': 'Bask Health', 'url': corp})
                    + '</script><a href="https://baskhealth.com/careers">Careers</a>'}
        job = original() | {'company_name': 'Bask Health'}
        with self.redirect_client(site) as client:
            careers.discover_employer_candidates(
                job, client, corporate_leads=['https://jobs.bask.health/careers', 'https://careers.baskhr.com/'])
        self.assertEqual(self.requests[6], corp)
        self.assertIn('corporate_brand_confirmed', [e.get('reason') for e in job['employer_discovery_trace']])
        # Derived parents were never needed once the guessed root proved the brand.
        self.assertNotIn('https://bask.health/', self.requests)
        self.assertLessEqual(len(self.requests), 12)

    def test_s2_brand_failure_category_is_recorded_without_page_content(self):
        apex = 'https://bask.health/'
        cases = {
            'organization_data_missing': '<title>Bask Health</title>',
            'organization_name_mismatch': {'name': 'Other Company', 'url': apex},
            'organization_host_mismatch': {'name': 'Bask Health', 'url': 'https://unrelated.com/'},
        }
        for expected, page in cases.items():
            with self.subTest(expected=expected):
                careers.CAREERS_CACHE.clear()
                if isinstance(page, dict):
                    page = '<script type="application/ld+json">' + json.dumps({'@type': 'Organization', **page}) + '</script>'
                job = original() | {'company_name': 'Bask Health'}
                with self.client({apex: page}) as client:
                    careers.discover_employer_candidates(job, client, corporate_leads=['https://jobs.bask.health/'])
                events = [e for e in job['employer_discovery_trace'] if e.get('reason') == 'corporate_brand_unconfirmed']
                self.assertEqual([e['detail'] for e in events if e['url'] == apex], [expected])
                self.assertFalse(careers.corporate_brand_matches(page, 'Bask Health', apex))
                self.assertTrue(all(set(e) <= {'url', 'reason', 'detail'} for e in events))

    def run_logged_batches(self, pages, batches=2):
        """Real process_enrichment_job (and its reset); both employer passes run,
        then the attempt fails offline. Returns employer log lines per batch."""
        from src import enrich_jobs
        from src.enrichment.ats.base import OfficialJobCandidate
        job = original() | {'company_name': 'Bask Health', 'record_key': 's2-life',
                            'apply_url': 'https://www.builtin.com/job/data-engineer/1'}
        def fake_search(searched, _client, provider=None):
            searched['public_search_trace'] = {'organic_results': [
                {'url': 'https://jobs.bask.health/careers?utm_source=x&token=private-secret'}]}
            return []
        def resolve_then_fail(url, client):
            careers.CAREERS_CACHE.clear()
            resolver.resolve_official_job(job, client)
            raise RuntimeError('synthetic offline failure after resolution')
        output = []
        with self.client(pages) as client, \
             patch.object(enrich_jobs, 'fetch_job_description', side_effect=resolve_then_fail), \
             patch.object(enrich_jobs, 'save_enrichment_attempt', return_value=None), \
             patch.object(resolver, 'discover_ats_candidates', return_value=[]), \
             patch.object(resolver, 'search_candidates', side_effect=fake_search), \
             patch.object(resolver, 'fetch_dynamic_job_description', return_value=None):
            for _ in range(batches):
                client._employer_public_pages = {}
                summary = enrich_jobs.process_enrichment_batch([job], client)
                output.append([l for l in summary.log_lines if 'Employer chain diagnostic:' in l])
        return job, output

    def test_s2_trace_reset_per_attempt_and_each_event_logged_once(self):
        from src import enrich_jobs
        stale = original() | {'employer_discovery_trace': [dict(url='https://stale.example/', reason='stale')]}
        with patch.object(enrich_jobs, 'official_status_value', side_effect=RuntimeError('stop')):
            with self.assertRaises(RuntimeError):
                enrich_jobs.process_enrichment_job(stale, None)
        self.assertEqual(stale['employer_discovery_trace'], [])

        job, (first, second) = self.run_logged_batches(self.bask_pages(branding=False))
        self.assertEqual(len(first), len(job['employer_discovery_trace']))
        self.assertEqual(first, second)  # a reused dict does not replay old events
        self.assertNotIn('stale', '\n'.join(first))
        # Both passes contributed: the first-pass guessed .com and the second-pass parent.
        self.assertTrue(any('baskhealth.com' in l for l in first))
        self.assertTrue(any('bask.health' in l and 'organization_name_mismatch' in l for l in first))
        self.assertEqual(len(first), len(set(first)))
        joined = '\n'.join(first)
        self.assertNotIn('private-secret', joined)
        self.assertNotIn('utm_source', joined)
        self.assertTrue(all('"record_key": "s2-life"' in l for l in first))

    def test_s2_successful_chain_is_logged_on_failed_attempt(self):
        job, (lines,) = self.run_logged_batches(self.bask_pages(), batches=1)
        joined = '\n'.join(lines)
        for reason in ('corporate_brand_confirmed', 'published_careers_link_found', 'careers_inventory_candidates'):
            self.assertEqual(joined.count(reason), 1, reason)
        self.assertIn('"candidates_found": 1', joined)
        self.assertNotIn('<script', joined)

    def test_s2_credentials_and_queries_are_sanitized_in_run_log(self):
        from src import enrich_jobs
        job = original() | {'record_key': 's2-sanitize'}
        def failed(**kwargs):
            kwargs['job']['employer_discovery_trace'] = [
                dict(url='https://user:hunter2@jobs.bask.health/careers?session=abc#frag', reason='unsafe_public_url')]
            raise RuntimeError('offline')
        with patch.object(enrich_jobs, 'process_enrichment_job', side_effect=failed), \
             patch.object(enrich_jobs, 'save_enrichment_attempt', return_value=None):
            summary = enrich_jobs.process_enrichment_batch([job], None)
        lines = [l for l in summary.log_lines if 'Employer chain diagnostic:' in l]
        self.assertEqual(len(lines), 1)
        for secret in ('hunter2', 'user:', 'session=abc', '#frag'):
            self.assertNotIn(secret, lines[0])
        self.assertIn('jobs.bask.health', lines[0])

    def test_hostname_probe_and_job_self_claim_not_corporate_proof(self):
        with self.client({CORP: posting()}) as client:
            self.assertEqual(careers.discover_employer_candidates(original(), client), [])
            self.assertNotIn(CAREERS, self.requests)

    def test_verified_careers_cache_reused_for_different_title(self):
        with self.client(self.pages()) as client:
            careers.discover_employer_candidates(original(), client)
        with self.client(self.pages()) as client:
            careers.discover_employer_candidates(original(), client)
            self.assertNotIn(CORP, self.requests)
            self.assertIn(CAREERS, self.requests)

    def test_negative_discovery_is_not_cached(self):
        with self.client({}) as client:
            self.assertEqual(careers.discover_employer_candidates(original(), client), [])
        with self.client(self.pages()) as client:
            self.assertTrue(careers.discover_employer_candidates(original(), client))

    def test_negative_ats_inventory_is_not_cached(self):
        with self.client({}) as client:
            with patch.multiple(ADAPTERS[0], discover_candidates=lambda **kw: []):
                # Disable all adapters, preventing network and isolating cache behavior.
                with patch('src.enrichment.ats.ADAPTERS', (ADAPTERS[0],)):
                    discover_ats_candidates(original(), client)
        self.assertFalse(ATS_DISCOVERY_CACHE)

    def test_public_get_budget_counts_redirects_and_failures(self):
        with self.client({CORP: httpx.Response(302, headers={'location': CAREERS}), CAREERS: 'ok'}) as client:
            pages = careers.PublicPages(client, [], limit=1)
            self.assertIsNone(pages.get(CORP))
            self.assertEqual(self.requests, [CORP])
        for url in ('https://127.0.0.1/', 'http://example.com/', 'https://user:pass@example.com/', 'https://host.local/'):
            self.assertFalse(careers.public_https_url(url))

    def test_invalid_employer_hostname_rejected_before_dispatch(self):
        job = original() | {'company_name': 'Tech Ladies®'}
        bad = 'https://techladies®.com/'
        self.assertEqual(careers.corporate_roots(job['company_name']), [])
        self.assertFalse(careers.public_https_url(bad))
        with self.client({}) as client:
            trace = []
            pages = careers.PublicPages(client, trace)
            self.assertIsNone(pages.get(bad))
            self.assertEqual(pages.attempts, 0)
            self.assertEqual(trace[-1]['reason'], 'unsafe_public_url')
            self.assertEqual(careers.discover_employer_candidates(job, client), [])
            self.assertEqual(self.requests, [])
        self.assertEqual(job['company_name'], 'Tech Ladies®')

    def test_invalid_lead_does_not_prevent_valid_employer_discovery(self):
        with self.client(self.pages()) as client:
            candidates = careers.discover_employer_candidates(original(), client,
                corporate_leads=['https://techladies®.com/', CORP])
            self.assertEqual([candidate.url for candidate in candidates], [POST])
            self.assertEqual(self.requests[:3], [CORP, CAREERS, SEARCH])
            self.assertEqual(candidates[0].employer_provenance['corporate_url'], CORP)

    def test_invalid_guesses_preserve_search_fallback_and_request_budget(self):
        import os
        from src.enrichment.public_search import SearchRequestBudget, TAVILY_URL

        for maximum, expected in [(1, 'OFFICIAL_NOT_FOUND'), (0, 'SEARCH_DEFERRED')]:
            with self.subTest(maximum=maximum):
                job = original() | {'company_name': 'Tech Ladies®'}
                with self.client({TAVILY_URL: httpx.Response(200, json={'results': []})}) as client, \
                     patch.dict(os.environ, {'TAVILY_API_KEY': 'mock-key', 'SERPER_API_KEY': '',
                                             'PUBLIC_SEARCH_PROVIDER': 'tavily'}), \
                     patch.object(resolver, 'discover_ats_candidates', return_value=[]) as ats:
                    client.search_request_budget = SearchRequestBudget(maximum)
                    result = resolver.resolve_official_job(job, client, resolver.PublicSearchCircuit())
                    self.assertEqual(result.status, expected)
                    self.assertIsNone(result.official_job_url)
                    ats.assert_called_once_with(job=job, client=client)
                    self.assertEqual(client.search_request_budget.attempted, maximum)
                    self.assertEqual(self.requests, [TAVILY_URL] if maximum else [])

    def test_valid_domains_and_supported_idna_remain_usable(self):
        self.assertEqual(careers.corporate_roots('Example Widget'), [CORP])
        for url in (CORP, CAREERS, 'https://examplewidget.com:443/',
                    'https://bücher.example/', 'https://xn--bcher-kva.example/'):
            with self.subTest(url=url):
                self.assertTrue(careers.public_https_url(url))
                with self.client({str(httpx.URL(url)): 'ok'}) as client:
                    self.assertIsNotNone(careers.PublicPages(client, []).get(url))
                    self.assertEqual(self.requests, [str(httpx.URL(url))])

    def test_invalid_and_unsafe_urls_never_reach_transport(self):
        urls = ('http://example.com/', 'ftp://example.com/', 'https://bad..example.com/',
                'https://bad_name.example.com/', 'https://-bad.example.com/',
                'https://bad-.example.com/', 'https://techladies®.com/',
                'https://[broken/', 'https://127.0.0.1/', 'https://[::1]/',
                'https://169.254.169.254/', 'https://10.0.0.1/',
                'https://host.local/', 'https://host.internal/', 'https://localhost/',
                'https://user:pass@example.com/', 'https://example.com:444/')
        with self.client({}) as client:
            for url in urls:
                with self.subTest(url=url):
                    self.assertFalse(careers.public_https_url(url))
                    self.assertIsNone(careers.PublicPages(client, []).get(url))
            self.assertEqual(self.requests, [])

    def test_redirects_cannot_reach_invalid_or_unsafe_destinations(self):
        for target_url in ('https://techladies®.com/', 'https://bad_name.example.com/',
                           'https://127.0.0.1/', 'http://example.com/',
                           'https://user:pass@example.com/', 'https://example.com:444/'):
            with self.subTest(target_url=target_url):
                with self.client({CORP: httpx.Response(302, headers={'location': target_url.encode('utf-8')})}) as client:
                    self.assertIsNone(careers.PublicPages(client, []).get(CORP))
                    self.assertEqual(self.requests, [CORP])

    def test_client_invalid_url_is_contained_but_unrelated_errors_propagate(self):
        with self.client({}) as client, patch.object(client, 'get', side_effect=httpx.InvalidURL('invalid URL')):
            trace = []
            self.assertIsNone(careers.PublicPages(client, trace).get(CORP))
            self.assertEqual(trace[-1]['reason'], 'InvalidURL')
        with self.client({}) as client, patch.object(client, 'get', side_effect=RuntimeError('unrelated bug')):
            with self.assertRaisesRegex(RuntimeError, 'unrelated bug'):
                careers.PublicPages(client, []).get(CORP)

    def test_invalid_guess_fallback_does_not_verify_mismatch_or_careers_homepage(self):
        for url, html in [(POST, posting(company='Unrelated Employer')),
                          (CAREERS, '<title>Example Widget Careers</title>')]:
            with self.subTest(url=url):
                job = original() | {'company_name': 'Tech Ladies®'}
                candidate = resolver.OfficialJobCandidate(url, 'Data Engineer', 'search')
                with self.client({url: html}) as client, \
                     patch.object(resolver, 'discover_ats_candidates', return_value=[]), \
                     patch.object(resolver, 'search_candidates', return_value=[candidate]) as fallback, \
                     patch.object(resolver, 'fetch_dynamic_job_description', return_value=None):
                    result = resolver.resolve_official_job(job, client)
                fallback.assert_called_once_with(job, client)
                self.assertNotEqual(result.status, 'FOUND_VERIFIED')
                self.assertIsNone(result.official_job_url)

    def test_application_bad_evidence_and_missing_provenance(self):
        for changes, html in [({}, target(req='999')), ({}, target(role='Sales Manager')),
                              ({}, target(backlink='https://other.com/')), ({}, target(controls=False)),
                              ({'official_url_source': 'search', 'employer_careers_provenance': {}}, target())]:
            with self.subTest(changes=changes, html=html):
                with self.client({POST: posting(extra=f'<a href="{APPLY}">Apply</a>'), APPLY: html}) as client:
                    self.assertEqual(check_job(trusted(**changes), client, Settings()).status, 'UNKNOWN')

    def test_empty_apply_fragment_fails_and_real_form_succeeds(self):
        for content, expected in [('<a href="#applynow">Apply</a><a id="applynow"></a>', 'UNKNOWN'),
                                  (f'<a href="{POST}">Apply</a>', 'UNKNOWN'),
                                  ('<a href="#applynow">Apply</a><section id="applynow"><form id="application"><button>Apply</button></form></section>', 'ACTIVE')]:
            with self.client({POST: posting(extra=content)}) as client:
                self.assertEqual(check_job(trusted(), client, Settings()).status, expected)

    def test_conflicting_canonical_stays_unresolved(self):
        with self.client({POST: posting(canonical=POST.replace('12345', '12346'), extra='<form id="application"><button>Apply</button></form>')}) as client:
            self.assertEqual(check_job(trusted(), client, Settings()).status, 'UNKNOWN')
        self.assertTrue(posting_urls_equivalent(POST, CAREERS + 'en/job/allen/data-engineer/12345'))
        self.assertFalse(posting_urls_equivalent(POST, POST.replace('12345', '12346'), '456'))

    def test_description_support_cannot_override_requisition_conflict(self):
        result = extract_job_description_from_html(POST, POST, 200, posting())
        evidence = compare_posting_evidence(original(description=DESCRIPTION, original_requisition_id='999'), result)
        self.assertEqual(evidence['description_similarity'], 1)
        self.assertTrue(evidence['requisition_conflict'])
        candidate = resolver.OfficialJobCandidate(POST, 'Data Engineer', 'employer')
        self.assertIsNone(resolver.validate_candidate_result(original(original_requisition_id='999'), candidate, result))
        self.assertIsNone(resolver.validate_candidate_result(original(company_name_override='x') | {'company_name': 'Unrelated'}, candidate, result))
        self.assertIsNone(resolver.validate_candidate_result(original() | {'location': 'Boston, MA'}, candidate, result))

    def test_previously_verified_employer_reuses_careers_host(self):
        with self.client(self.pages()) as client:
            candidates = careers.discover_employer_candidates(trusted(), client)
            self.assertTrue(candidates)
            self.assertNotIn(CORP, self.requests)

    def test_other_verified_posting_can_seed_employer_without_borrowing_its_job(self):
        self.assertTrue(careers.remember_verified_careers(trusted()))
        with self.client(self.pages()) as client:
            candidates = careers.discover_employer_candidates(original(), client)
            self.assertTrue(candidates)
            self.assertNotIn(CORP, self.requests)
        careers.CAREERS_CACHE.clear()
        self.assertIsNone(careers.remember_verified_careers(trusted(verified_posting_key='official-url:https://wrong.com/job/456')))
        self.assertFalse(careers.CAREERS_CACHE)

    def test_public_search_corporate_lead_still_requires_fetched_proof(self):
        mapping = self.pages()
        alternative = 'https://unrelatedbrand.com/'
        mapping[alternative] = mapping.pop(CORP).replace(CORP, alternative)
        with self.client(mapping) as client:
            candidates = careers.discover_employer_candidates(original(), client, corporate_leads=[alternative])
            self.assertTrue(candidates)
            self.assertEqual(candidates[0].employer_provenance['corporate_url'], alternative)

    def test_published_greenhouse_tenant_uses_existing_payload_parser(self):
        root = 'https://boards.greenhouse.io/widget'
        endpoint = 'https://boards-api.greenhouse.io/v1/boards/widget/jobs?content=true'
        job_url = 'https://job-boards.greenhouse.io/widget/jobs/654321'
        mapping = {CORP: self.pages()[CORP].replace(CAREERS, root), root: '<title>Jobs</title>',
                   endpoint: httpx.Response(200, json={'jobs': [{'id': 654321, 'title': 'Data Engineer', 'absolute_url': job_url}]})}
        with self.client(mapping) as client:
            candidates = careers.discover_employer_candidates(original(), client)
            self.assertEqual([c.url for c in candidates], [job_url])
            self.assertIn(endpoint, self.requests)

    def test_redirected_careers_without_employer_evidence_is_rejected(self):
        mapping = self.pages()
        mapping[CAREERS] = httpx.Response(302, headers={'location': 'https://unrelated.com/'})
        mapping['https://unrelated.com/'] = f'<a href="{POST}">Data Engineer</a>'
        with self.client(mapping) as client:
            self.assertFalse(careers.discover_employer_candidates(original(), client))

    def test_description_formatting_and_boilerplate(self):
        a = 'Build reliable SQL pipelines and maintain operational data models.'
        self.assertEqual(description_similarity(a, a.replace(' ', '\n')), 1)
        self.assertEqual(description_similarity(a + '\nEqual opportunity employer.', a + '\nPrivacy policy applies.'), 1)

    def test_workday_short_canonical_keeps_explicit_tenant_and_req(self):
        a = 'https://example.wd1.myworkdayjobs.com/Careers/job/Boston/Engineer_JR-123'
        b = 'https://example.wd1.myworkdayjobs.com/en-US/Careers/job/Engineer_JR-123'
        self.assertTrue(posting_urls_equivalent(a, b, 'JR-123'))
        self.assertFalse(posting_urls_equivalent(a, b.replace('Careers', 'Other'), 'JR-123'))
        self.assertFalse(posting_urls_equivalent(a, a.replace('Careers', 'Other'), 'JR-123'))

    def test_current_employer_opportunity_is_distinct_from_unproven_aggregator_history(self):
        from src import database, enrich_jobs
        from src.repositories import recommendation_repository as repo
        from src.test_recommendation_repository import with_temp_review_database
        from src.verified_posting_identity import normalize_official_url

        for historical_status in ('applied', 'removed'):
            with self.subTest(history=historical_status):
                def scenario():
                    enrich_jobs.initialize_enrichment_tables()
                    alert = original() | dict(source_job_id='aggregator-123',
                        apply_url='https://glassdoor.com/job-listing/data-engineer?jobListingId=123')
                    alert_metadata = dict(email_message_id='historical-alert')
                    database.insert_jobs([alert], alert_metadata)
                    alert_key = database.create_record_key(alert, 'historical-alert')
                    repo.update_application_status(alert_key, historical_status)
                    with self.client(self.pages()) as client:
                        resolved = resolver.resolve_official_job(original(), client)
                    self.assertEqual(resolved.status, 'FOUND_VERIFIED')
                    current = dict(source='employer', source_job_id=normalize_official_url(POST),
                        title=resolved.resolved_title, company_name=resolved.resolved_company,
                        location=resolved.resolved_location, apply_url=POST,
                        description=resolved.description_result.description)
                    event = 'employer-discovery:' + verified_posting_key_from_url(POST)
                    metadata = dict(email_message_id=event, folder='employer-discovery')
                    self.assertEqual(database.insert_jobs([current], metadata), (1, 0))
                    current['record_key'] = database.create_record_key(current, event)
                    self.assertNotEqual(current['record_key'], alert_key)
                    enrich_jobs.save_enrichment_attempt(current, resolved.description_result,
                        identity_validation=resolved.identity_validation, official_resolution=resolved,
                        description_update=resolved.description_result)
                    self.assertEqual(database.insert_jobs([current], metadata), (0, 1))
                    jobs = repo.load_all_jobs(for_session_snapshot=True)
                    rows = {j['record_key']: j for j in repo.prepare_job_display(jobs)}
                    self.assertIn(current['record_key'], rows)
                    self.assertEqual(rows[current['record_key']]['application_status'], 'new')
                    self.assertEqual(rows[current['record_key']]['description_state'], 'FULL_JD')
                    self.assertFalse(rows[current['record_key']]['has_current_complete_ai_assessment'])
                    self.assertEqual(rows[alert_key]['application_status'], historical_status)
                    with database.get_connection() as c:
                        self.assertEqual(c.execute('SELECT record_key,status FROM application_status').fetchall(),
                                         [(alert_key, historical_status)])
                        self.assertIsNone(c.execute('SELECT * FROM job_enrichment_attempts WHERE record_key=?', [alert_key]).fetchone())
                with_temp_review_database(scenario)


class ArchivedPages(unittest.TestCase):
    root = Path('/private/tmp/known-employer-url-diagnosis-q8cydt3q')

    def test_actual_saved_incidents(self):
        if not self.root.exists():
            self.skipTest('Private archived public HTML is not installed on this machine')
        urls = dict(upstart='https://careers.upstart.com/jobs/senior-data-platform-engineer-076371c4-e43b-4489-8132-a304724cc54f',
                    jack_henry='https://careers.jackhenry.com/en/job/allen/data-engineer-ii/42859/103388183664',
                    sony='https://sonyglobal.wd1.myworkdayjobs.com/SonyGlobalCareers/job/San-Diego/Expert-Business-Intelligence-Analyst_JR-119921')
        for case, url in urls.items():
            with self.subTest(case=case):
                html = (self.root / (case + '.html')).read_text()
                parsed = extract_job_description_from_html(url, url, 200, html)
                self.assertEqual(parsed.status, 'enriched')
                self.assertGreater(parsed.word_count, 1000)
                equivalent = posting_urls_equivalent(url, parsed.canonical_url, parsed.resolved_job_id)
                self.assertEqual(equivalent, case != 'upstart')
                if case == 'upstart':
                    job = trusted() | dict(title=parsed.resolved_title, company_name=parsed.resolved_company,
                            official_job_url=url, verified_posting_key=verified_posting_key_from_url(url))
                    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=html))) as client:
                        self.assertEqual(check_job(job, client, Settings()).status, 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
