"""Main Jobs view shows every relevant job; verification and scoring stay separate.

Uses the real loader (load_all_jobs on a temporary database), display
preparation, review filters, card states, link selection and the whole
streamlit_app.py under AppTest. Only metrics/backlog/network boundaries are
mocked. Production DuckDB is never opened (fail-closed harness).
"""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src import database
from src.repositories import recommendation_repository as repo
from src.test_job_review_visibility import complete_ai_job
from src.ui import job_visibility as vis
from src.ui.job_links import select_job_open_target
from src.ui.jobs_snapshot import patch_status

NOW = datetime.now(timezone.utc)
JD = ('Responsibilities: design and build reliable SQL and Python data pipelines and dimensional '
      'models. Requirements: 5+ years of data engineering experience with cloud warehouses. ') * 12
GD = 'https://www.glassdoor.com/partner/jobListing.htm?pos=1&guid='


def insert(connection, record_key, source, title, apply_url, *, company='Acme', source_job_id=None,
           description=None, days_ago=1):
    connection.execute(
        """INSERT INTO raw_jobs (record_key, job_fingerprint, source, source_job_id, title, company_name,
               location, description, apply_url, discovered_at, email_date)
           VALUES (?, ?, ?, ?, ?, ?, 'Remote', ?, ?, ?, NULL)""",
        [record_key, record_key, source, source_job_id, title, company, description, apply_url,
         NOW - timedelta(days=days_ago)])


def seed():
    from src.enrich_jobs import initialize_enrichment_tables
    initialize_enrichment_tables()
    with database.get_connection() as c:
        insert(c, 'gd-strong', 'glassdoor', 'Senior Data Engineer', GD + 'a', source_job_id='1001')
        insert(c, 'gd-alias', 'glassdoor', 'Senior Data Engineer', GD + 'b', source_job_id='1001', days_ago=2)
        insert(c, 'gd-fulljd', 'glassdoor', 'Analytics Engineer', GD + 'c', source_job_id='1002', description=JD)
        insert(c, 'bebee-offtarget', 'bebee', 'Registered Nurse', 'https://us.bebee.com/job/9', source_job_id='9')
        insert(c, 'lensa-de', 'lensa', 'Data Engineer', 'https://lensa.com/job/1')
        insert(c, 'li-direct', 'linkedin', 'Data Engineer', 'https://www.linkedin.com/jobs/view/555/',
               source_job_id='555')


def run_db(callback):
    from src.test_recommendation_repository import with_temp_review_database
    with_temp_review_database(callback)


def relevant(jobs):
    return [j for j in jobs if vis.is_visible_for_review_filter(j, vis.REVIEW_FILTER_ALL_RELEVANT)]


class LoaderVisibilityTests(unittest.TestCase):
    def test_relevant_unverified_jobs_from_every_source_are_visible_by_default(self):
        def body():
            seed()
            jobs = repo.load_all_jobs(resume_hash='')
            keys = {j['record_key'] for j in relevant(jobs)}
            # Glassdoor/Lensa unverified jobs and the LinkedIn job are all in the default view.
            self.assertTrue({'li-direct', 'gd-fulljd', 'lensa-de'} <= keys)
            self.assertEqual(len(keys & {'gd-strong', 'gd-alias'}), 1)   # one card per exact posting
            self.assertNotIn('bebee-offtarget', keys)                       # title rules still apply
            for job in jobs:
                self.assertFalse(vis.is_employer_verified(job))
                self.assertTrue(vis.job_card_state(job).startswith('Unverified'))
        run_db(body)

    def test_default_view_is_all_relevant_and_recommended_stays_strict(self):
        self.assertEqual(vis.REVIEW_FILTER_OPTIONS[0], vis.REVIEW_FILTER_ALL_RELEVANT)
        verified = complete_ai_job(record_key='verified-apply')
        unverified = complete_ai_job(record_key='unverified-apply', verified_posting_key=None,
                                     verified_posting_key_trust=None, actionability_status=None)
        self.assertTrue(vis.is_visible_for_review_filter(verified, vis.REVIEW_FILTER_RECOMMENDED))
        self.assertFalse(vis.is_visible_for_review_filter(unverified, vis.REVIEW_FILTER_RECOMMENDED))
        self.assertTrue(vis.is_visible_for_review_filter(unverified, vis.REVIEW_FILTER_ALL_RELEVANT))
        self.assertEqual(vis.job_card_state(verified), vis.JOB_STATE_VERIFIED_SCORED)
        self.assertEqual(vis.job_card_state(unverified), vis.JOB_STATE_UNVERIFIED_SCORED)

    def test_six_card_states(self):
        base = dict(complete_ai_job(), ai_score=None, recommendation=None, candidate_assessment_id=None)
        unverified = dict(base, verified_posting_key=None, verified_posting_key_trust=None)
        cases = {
            vis.JOB_STATE_VERIFIED_NOT_SCORED: dict(base, description_state='FULL_JD'),
            vis.JOB_STATE_VERIFIED_NEEDS_DESCRIPTION: dict(base, description_state='PARTIAL_JD', description=''),
            vis.JOB_STATE_UNVERIFIED_JD_AVAILABLE: dict(unverified, description_state='FULL_JD'),
            vis.JOB_STATE_UNVERIFIED_NEEDS_DESCRIPTION: dict(unverified, description_state='NEEDS_ENRICHMENT',
                                                             description=''),
        }
        for expected, job in cases.items():
            with self.subTest(expected):
                self.assertEqual(vis.job_card_state(job), expected)

    def test_unverified_cards_keep_a_labelled_source_link(self):
        target = select_job_open_target(dict(source='glassdoor', apply_url=GD + 'x',
                                             official_url_status='OFFICIAL_NOT_FOUND'))
        self.assertEqual(target.label, 'Open original source listing — unverified')
        self.assertEqual(target.url, GD + 'x')


class EligibilityTests(unittest.TestCase):
    def test_assessment_eligibility_needs_a_full_jd_for_unverified_jobs(self):
        unverified_full = dict(source='glassdoor', apply_url=GD, description=JD, raw_description_word_count=len(JD.split()))
        unverified_partial = dict(unverified_full, description='Build dashboards.', raw_description_word_count=2)
        rejected = dict(unverified_full, enrichment_status='resolution_rejected')
        self.assertTrue(vis.is_assessment_eligible_job(unverified_full))
        self.assertFalse(vis.is_assessment_eligible_job(unverified_partial))
        self.assertFalse(vis.is_assessment_eligible_job(rejected))
        self.assertTrue(vis.is_assessment_eligible_job(dict(source='linkedin', apply_url='https://www.linkedin.com/jobs/view/1/')))

    def test_scoring_and_screening_queues_exclude_missing_or_partial_unverified_descriptions(self):
        def body():
            seed()
            scoring = {j['record_key'] for j in repo.load_candidate_jobs(
                resume_hash='resume-1', model_name='m', limit=100, minimum_rule_score=0)}
            screening = {j['record_key'] for j in repo.load_unscreened_job_eligibility_candidates(
                resume_hash='resume-1', limit=100)}
            for queue in (scoring, screening):
                self.assertFalse(queue & {'gd-strong', 'gd-alias', 'lensa-de', 'bebee-offtarget'})
            self.assertNotIn('li-direct', scoring)                  # no JD: never scored
        run_db(body)


class DisplayTests(unittest.TestCase):
    def rows(self, n):
        rows = []
        for i in range(n):
            rows.append(dict(record_key=f'r{i}', exact_posting_key=f'glassdoor|{i}', duplicate_fingerprint=f'f{i}',
                             source='glassdoor', title='Data Engineer', title_classification='STRONG_MATCH',
                             company_name='Acme', apply_url=GD + str(i), application_status='new',
                             discovered_at=NOW - timedelta(hours=i), _snapshot_new_admitted=True))
        return rows

    def test_pagination_and_exact_dedup(self):
        from src.ui.pagination import paginate_items
        rows = self.rows(30) + [dict(self.rows(1)[0], record_key='r0-alias')]
        shown = repo.prepare_job_display(rows)
        self.assertEqual(len(shown), 30)                                   # alias collapsed
        self.assertEqual(len(paginate_items(shown, 1, 25).items), 25)
        self.assertEqual(len(paginate_items(shown, 2, 25).items), 5)

    def test_admission_exclusion_still_hides_irrelevant_jobs(self):
        rows = self.rows(2)
        rows[1]['_snapshot_new_admitted'] = False
        self.assertEqual([j['record_key'] for j in repo.prepare_job_display(rows)], ['r0'])

    def test_strong_titles_first_within_the_same_day_newest_day_first(self):
        rows = self.rows(3)
        rows[0].update(title_classification='POSSIBLE_MATCH', discovered_at=NOW.replace(hour=20, minute=0))
        rows[1].update(title_classification='STRONG_MATCH', discovered_at=NOW.replace(hour=19, minute=0))
        rows[2].update(title_classification='STRONG_MATCH', discovered_at=NOW - timedelta(days=3))
        self.assertEqual([j['record_key'] for j in repo.prepare_job_display(rows)], ['r1', 'r0', 'r2'])


class DismissTests(unittest.TestCase):
    def test_mark_removed_on_an_unverified_card_targets_one_posting(self):
        def body():
            seed()
            snapshot = repo.load_all_jobs(resume_hash='', for_session_snapshot=True)
            state = {'jobs_snapshot': snapshot, 'jobs_snapshot_version': 1}
            card = next(j for j in snapshot if j['record_key'] in {'gd-strong', 'gd-alias'})
            change = patch_status(state, card['record_key'], 'removed', repo.update_application_status)
            self.assertEqual(sorted(change.record_keys), ['gd-alias', 'gd-strong'])
            with database.get_connection() as c:
                statuses = repo.automatic_statuses(c)
            self.assertEqual({k for k, v in statuses.items() if v != 'new'}, {'gd-alias', 'gd-strong'})
        run_db(body)


class WholeAppTests(unittest.TestCase):
    def test_jobs_tab_default_view_shows_unverified_glassdoor_card_with_state_and_link(self):
        from streamlit.testing.v1 import AppTest
        from src.ui.daily_workflow_service import ScoringBacklogStatus

        def body():
            seed()
            metrics = repo.reviewable_job_metrics([])
            metrics.update(raw_jobs=0, exact_postings=0, enrichment_attempts={})
            queue = {k: 0 for k in ('fresh_description', 'eligible_deferred_retries', 'employer_verification_pending',
                                    'full_jd_employer_pending', 'fit_recommended_employer_pending', 'cooldown',
                                    'excluded', 'retry_disabled', 'runnable', 'selected')}
            queue.update(pending_employer_by_source={}, pending_employer_all_new_by_source={})
            with patch.object(repo, 'load_pipeline_operation_metrics', return_value=metrics), \
                    patch('src.ui.daily_workflow_service.load_scoring_backlog_status',
                          return_value=ScoringBacklogStatus('resume-1', 0, 0, ())), \
                    patch('src.ui.daily_workflow_service.load_daily_enrichment_plan', return_value=([], queue)), \
                    patch('src.actionability.refresh_daily', side_effect=AssertionError('no network')), \
                    patch('src.ui.candidate_identity.candidates', return_value=[]), \
                    patch('src.ui.candidate_identity.cache_token', return_value=None):
                app = AppTest.from_string(Path('streamlit_app.py').read_text(), default_timeout=60)
                app.session_state['show_saved_jobs'] = True
                app.run()
                self.assertFalse(app.exception, str(app.exception))
                review = next(s for s in app.selectbox if s.label == 'Review view')
                self.assertEqual(review.value, vis.REVIEW_FILTER_ALL_RELEVANT)
                def cards():  # Jobs tab card titles only (other tabs have their own headings)
                    return {h.value for h in app.tabs[0].subheader} - {'Jobs'}
                text = ' '.join(m.value for m in app.tabs[0].markdown) + ' '.join(c.value for c in app.tabs[0].caption)
                self.assertTrue({'Senior Data Engineer', 'Analytics Engineer', 'Data Engineer'} <= cards())
                self.assertIn('Unverified · Needs description', text)
                self.assertIn('Unverified · JD available', text)
                self.assertNotIn('Registered Nurse', cards())
                self.assertIn('Missing (no job description stored)', text)
                # Source filter applies to unverified jobs like any other.
                next(s for s in app.selectbox if s.label == 'Source').set_value('glassdoor').run()
                self.assertEqual(cards(), {'Senior Data Engineer', 'Analytics Engineer'})
                # Recommended keeps its strict semantics: no unverified, unscored card.
                review = next(s for s in app.selectbox if s.label == 'Review view')
                review.set_value(vis.REVIEW_FILTER_RECOMMENDED).run()
                self.assertEqual(cards(), set())
        run_db(body)


if __name__ == '__main__':
    unittest.main()
