"""Display-only duplicate proof and representative selection; fully offline."""
from __future__ import annotations

import copy
import itertools
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from src.repositories.recommendation_repository import (
    exact_posting_identity,
    prepare_job_display,
    reviewable_job_metrics,
    trusted_display_posting_key,
    ui_duplicate_key,
)
from src.ui.pagination import paginate_items
from src.ui.job_visibility import count_job_review_buckets
from src.verified_posting_identity import verified_posting_key_from_url

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
URL = 'https://job-boards.greenhouse.io/example/jobs/123'
KEY = 'greenhouse:123'


def posting(record_key='a', **changes):
    row = dict(
        record_key=record_key, source='linkedin', source_job_id=record_key,
        exact_posting_key='linkedin:'+record_key,
        canonical_job_key='linkedin:'+record_key,
        duplicate_fingerprint=record_key,
        title='Senior Data Engineer', company_name='Example Co',
        location='Remote', apply_url='https://example.com/jobs/'+record_key,
        official_job_url=URL, official_url_status='FOUND_VERIFIED',
        verified_posting_key=KEY,
        verified_posting_key_source='official_found_verified',
        verified_posting_key_trust='trusted',
        description_state='FULL_JD', description_source='greenhouse',
        raw_description_word_count=240, description_word_count=240,
        description_complete=True, has_incomplete_description=False,
        description_quality_signals=True, description_hash='same-jd',
        description_updated_at=NOW-timedelta(hours=2),
        ai_score=90, ai_prompt_version='v3', ai_scored_at=NOW-timedelta(hours=1),
        recommendation='apply', confidence='high',
        has_current_complete_ai_assessment=True,
        fit_priority_label='High', title_classification='STRONG_MATCH',
        title_match_score=100, application_status='new',
        application_status_updated_at=None,
        matching_strengths=['representative '+record_key],
        sent_at=NOW,
    )
    row.update(changes)
    return row


class VerifiedDisplayTests(unittest.TestCase):
    def test_same_trusted_key_cross_source_is_one_card(self):
        rows = [posting('a'), posting('b', source='lensa')]
        self.assertEqual(ui_duplicate_key(rows[0]), ('verified_posting', KEY))
        self.assertEqual(len(prepare_job_display(rows)), 1)

    def test_same_source_different_source_ids_is_one_card(self):
        rows = [posting('a', source_job_id='100'), posting('b', source_job_id='200')]
        self.assertNotEqual(rows[0]['source_job_id'], rows[1]['source_job_id'])
        self.assertEqual(len(prepare_job_display(rows)), 1)

    def test_workday_same_requisition_different_employers_remain_separate(self):
        first = 'https://alpha.wd1.myworkdayjobs.com/en-US/careers/job/US/Data-Engineer_REQ123'
        second = 'https://beta.wd2.myworkdayjobs.com/en-US/careers/job/US/Data-Engineer_REQ123'
        key_first = verified_posting_key_from_url(first)
        key_second = verified_posting_key_from_url(second)
        self.assertEqual(key_first, key_second)  # Legacy key is tenant-blind.
        jobs = [
            posting('alpha', company_name='Alpha Corp', source='linkedin',
                    official_job_url=first, verified_posting_key=key_first),
            posting('beta', company_name='Beta Corp', source='lensa',
                    official_job_url=second, verified_posting_key=key_second),
        ]
        self.assertNotEqual(trusted_display_posting_key(jobs[0]),
                            trusted_display_posting_key(jobs[1]))
        self.assertEqual(len(prepare_job_display(jobs)), 2)

    def test_workday_same_tenant_same_requisition_collapses_only_once(self):
        url = 'https://alpha.wd1.myworkdayjobs.com/en-US/careers/job/US/Data-Engineer_REQ123'
        key = verified_posting_key_from_url(url)
        jobs = [
            posting('a', official_job_url=url, verified_posting_key=key),
            posting('b', source='lensa', official_job_url=url, verified_posting_key=key),
        ]
        self.assertEqual(len(prepare_job_display(jobs)), 1)

    def test_different_locations_are_same_verified_opening(self):
        rows = [posting('a', location='Denver, CO (Remote)'),
                posting('b', source='lensa', location='Austin, TX (Remote)')]
        self.assertEqual(len(prepare_job_display(rows)), 1)

    def test_different_verified_keys_stay_separate_even_same_fingerprint(self):
        rows = [posting('a', duplicate_fingerprint='same'),
                posting('b', duplicate_fingerprint='same',
                        verified_posting_key='greenhouse:456',
                        official_job_url=URL.replace('123', '456'))]
        self.assertEqual(len(prepare_job_display(rows)), 2)

    def test_no_trusted_key_keeps_source_specific_fallback(self):
        rows = [posting('a', verified_posting_key=None, duplicate_fingerprint='same'),
                posting('b', source='lensa', verified_posting_key=None,
                        duplicate_fingerprint='same')]
        self.assertEqual(len(prepare_job_display(rows)), 2)
        self.assertEqual(ui_duplicate_key(rows[0]),
                         ('fingerprint', 'linkedin', 'same', ''))

    def test_legacy_same_source_fingerprint_still_collapses(self):
        rows = [posting('a', verified_posting_key=None, duplicate_fingerprint='same'),
                posting('b', verified_posting_key=None, duplicate_fingerprint='same')]
        self.assertEqual(len(prepare_job_display(rows)), 1)

    def test_untrusted_or_unrecognized_provenance_cannot_collapse(self):
        for change in ({'verified_posting_key_trust': 'untrusted'},
                       {'verified_posting_key_trust': 'conflict'},
                       {'verified_posting_key_source': 'search'},
                       {'verified_posting_key_source': None}):
            with self.subTest(change=change):
                a, b = posting('a', **change), posting('b', source='lensa', **change)
                self.assertIsNone(trusted_display_posting_key(a))
                self.assertEqual(len(prepare_job_display([a,b])), 2)

    def test_third_party_official_url_cannot_prove_duplicate(self):
        for url in ('https://www.builtinsf.com/job/data-engineer/123',
                    'https://lensa.com/jobs/data-engineer/123'):
            with self.subTest(url=url):
                rows = [posting('a', official_job_url=url),
                        posting('b', source='lensa', official_job_url=url)]
                self.assertIsNone(trusted_display_posting_key(rows[0]))
                self.assertEqual(len(prepare_job_display(rows)), 2)

    def test_generic_careers_pages_cannot_prove_duplicate(self):
        for url in ('https://example.com/careers/',
                    'https://job-boards.greenhouse.io/example',
                    'https://jobs.lever.co/example'):
            with self.subTest(url=url):
                rows = [posting('a', official_job_url=url),
                        posting('b', source='lensa', official_job_url=url)]
                self.assertIsNone(trusted_display_posting_key(rows[0]))
                self.assertEqual(len(prepare_job_display(rows)), 2)

    def test_trusted_specific_ats_urls_still_collapse(self):
        for url in (
            URL,
            'https://jobs.lever.co/example/123e4567-e89b-12d3-a456-426614174000',
            'https://jobs.ashbyhq.com/example/123e4567-e89b-12d3-a456-426614174000',
            'https://example.wd1.myworkdayjobs.com/en-US/careers/job/US/Engineer_REQ123',
        ):
            key = verified_posting_key_from_url(url)
            with self.subTest(url=url):
                self.assertTrue(key)
                rows = [posting('a', official_job_url=url, verified_posting_key=key),
                        posting('b', source='lensa', official_job_url=url,
                                verified_posting_key=key)]
                self.assertEqual(trusted_display_posting_key(rows[0]), key)
                self.assertEqual(len(prepare_job_display(rows)), 1)

    def test_verification_status_and_exact_url_key_agreement_required(self):
        for change in ({'official_url_status': 'INSUFFICIENT_IDENTITY'},
                       {'official_url_status': None},
                       {'official_job_url': URL.replace('123', '456')},
                       {'official_job_url': None},
                       {'official_job_url': 'https://[malformed/jobs/123'}):
            with self.subTest(change=change):
                rows = [posting('a', **change), posting('b', source='lensa', **change)]
                self.assertIsNone(trusted_display_posting_key(rows[0]))
                self.assertEqual(len(prepare_job_display(rows)), 2)

    def test_boulder_90_92_88_prefers_newest_jd_not_max_score(self):
        rows = [posting('denver', ai_score=90,
                        description_updated_at=NOW-timedelta(minutes=1)),
                posting('austin', ai_score=92,
                        description_updated_at=NOW-timedelta(minutes=2)),
                posting('lensa', source='lensa', ai_score=88,
                        description_updated_at=NOW-timedelta(days=1))]
        # Ensure all are currently scored for their respective descriptions.
        for row in rows: row['ai_scored_at'] = NOW
        self.assertEqual(prepare_job_display(rows)[0]['record_key'], 'denver')
        rows[0]['ai_score'], rows[1]['ai_score'], rows[2]['ai_score'] = 1, 100, 99
        rows[0]['recommendation'], rows[0]['fit_priority_label'] = 'skip', 'Low'
        self.assertEqual(prepare_job_display(rows)[0]['record_key'], 'denver')

    def test_jd_freshness_precedes_scoring_freshness(self):
        a = posting('a', description_updated_at=NOW-timedelta(minutes=10),
                    ai_scored_at=NOW-timedelta(minutes=5))
        b = posting('b', description_updated_at=NOW-timedelta(minutes=20),
                    ai_scored_at=NOW)
        self.assertEqual(prepare_job_display([b,a])[0]['record_key'], 'a')

    def test_latest_actual_status_event_including_explicit_new_wins(self):
        removed = posting('removed', application_status='removed',
                          application_status_updated_at=NOW-timedelta(minutes=1))
        fresh_new = posting('new', application_status='new',
                            application_status_updated_at=NOW)
        self.assertEqual(prepare_job_display([removed,fresh_new])[0]['record_key'], 'new')
        fresh_new['application_status_updated_at'] = None
        self.assertEqual(prepare_job_display([fresh_new,removed])[0]['record_key'], 'removed')
        removed['application_status_updated_at'] = None
        self.assertEqual(prepare_job_display([fresh_new,removed])[0]['record_key'], 'removed')

    def test_equal_status_timestamps_keep_handled_history(self):
        applied = posting('applied', application_status='applied',
                          application_status_updated_at=NOW)
        new = posting('new', application_status_updated_at=NOW)
        self.assertEqual(prepare_job_display([new,applied])[0]['record_key'], 'applied')

    def test_quality_provenance_and_assessment_presence(self):
        pairs = [
            (posting('preferred'), posting('other', description_state='PARTIAL_JD')),
            (posting('preferred', description_source='greenhouse'),
             posting('other', description_source='search')),
            (posting('preferred', description_source='source_verified'),
             posting('other', description_source='verified_reuse')),
            (posting('preferred', description_source='verified_reuse'),
             posting('other', description_source='unrecognized')),
            (posting('preferred'), posting('other', ai_score=None)),
        ]
        for preferred, other in pairs:
            with self.subTest(other=other['description_source']):
                self.assertEqual(prepare_job_display([other,preferred])[0]['record_key'],
                                 'preferred')

    def test_keep_entire_selected_assessment_and_hash_no_field_merge(self):
        chosen = posting('chosen', ai_score=80, description_hash='new-hash',
                         description_updated_at=NOW-timedelta(minutes=5),
                         ai_scored_at=NOW, actionability_status='UNKNOWN')
        old = posting('other', ai_score=99, description_hash='old-hash',
                      description_updated_at=NOW-timedelta(days=1),
                      ai_scored_at=NOW, actionability_status='ACTIVE')
        displayed = prepare_job_display([old,chosen])
        self.assertEqual(len(displayed), 1)
        self.assertIs(displayed[0], chosen)
        self.assertEqual(displayed[0]['ai_score'], 80)
        self.assertEqual(displayed[0]['description_hash'], 'new-hash')
        self.assertEqual(displayed[0]['matching_strengths'], ['representative chosen'])
        self.assertEqual(displayed[0]['actionability_status'], 'UNKNOWN')

    def test_record_key_breaks_ties_independent_of_input_order(self):
        rows = [posting('c'), posting('b'), posting('a')]
        for perm in itertools.permutations(rows):
            self.assertEqual(prepare_job_display(list(perm))[0]['record_key'], 'a')

    def test_same_url_without_key_never_borrows_verified_proof(self):
        rows = [posting('trusted', source='lensa', duplicate_fingerprint='same'),
                posting('unverified', source='lensa', duplicate_fingerprint='same',
                        verified_posting_key=None, verified_posting_key_source=None,
                        verified_posting_key_trust=None)]
        self.assertEqual(len(prepare_job_display(rows)), 2)
        self.assertIsNone(trusted_display_posting_key(rows[1]))

    def test_counts_and_pagination_use_openings(self):
        rows = []
        for opening in range(30):
            key = 'greenhouse:'+str(1000+opening)
            url = URL.replace('123', str(1000+opening))
            for source in ('linkedin','lensa'):
                rows.append(posting(str(opening)+source, source=source,
                                    verified_posting_key=key, official_job_url=url))
        displayed = prepare_job_display(rows)
        self.assertEqual(len(rows), 60)
        self.assertEqual(len(displayed), 30)
        metrics = reviewable_job_metrics(displayed)
        self.assertEqual(metrics['reviewable_jobs'], 30)
        self.assertEqual(metrics['full_jd'], 30)
        self.assertEqual(metrics['ai_scored'], 30)
        self.assertEqual(count_job_review_buckets(displayed)['needs_review'], 30)
        page = paginate_items(displayed, 2, 25)
        self.assertEqual((page.total_items,page.total_pages,len(page.items)), (30,2,5))

    def test_each_review_bucket_counts_collapsed_openings(self):
        from src.actionability import Settings
        config = Settings(country='United States')
        rows = []
        for index, kind in enumerate(('active', 'unknown', 'skip')):
            key = 'greenhouse:'+str(8000+index)
            url = URL.replace('123',str(8000+index))
            for source in ('linkedin','lensa'):
                row = posting(kind+source, source=source,
                              verified_posting_key=key, official_job_url=url)
                if kind == 'active':
                    row.update(actionability_status='ACTIVE',
                               actionability_checked_at=NOW.isoformat(),
                               actionability_posting_key=key,
                               actionability_official_url=url,
                               actionability_candidate_context=config.context)
                elif kind == 'skip':
                    row.update(ai_score=49, recommendation='skip',
                               fit_priority_label='Low')
                rows.append(row)
        displayed = prepare_job_display(rows)
        self.assertEqual(len(displayed),3)
        with patch('src.actionability.settings',return_value=config), patch(
                'src.actionability.now_utc',return_value=NOW):
            self.assertEqual(count_job_review_buckets(displayed),
                             {'recommended':1,'needs_review':1,'low_fit':1})

    def test_identity_fields_and_raw_rows_are_not_modified(self):
        rows = [posting('a'), posting('b', source='lensa')]
        before = copy.deepcopy(rows)
        self.assertNotEqual(exact_posting_identity('linkedin','a',URL,'a'),
                            exact_posting_identity('lensa','b',URL,'b'))
        prepare_job_display(rows)
        self.assertEqual(rows, before)


if __name__ == '__main__':
    unittest.main()
