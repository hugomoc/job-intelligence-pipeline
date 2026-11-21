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
