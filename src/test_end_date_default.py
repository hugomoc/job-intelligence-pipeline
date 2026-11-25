"""Jobs-tab End date follows the latest completed ingestion run.

Offline: pure session-state logic, a temporary database for run history, and a
whole-app AppTest with repository/network boundaries mocked.
"""
import copy
import inspect
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from src import database
from src import scoring_run_repository as runs
from src.ui.date_filters import (
    END_DATE_RUN_MARKER_KEY, apply_latest_ingestion_end_date, end_date_for_run, local_run_date,
    pacific_date,
)

KEY = 'email_end_date_dropdown_v1'
D = date(2026, 11, 1)
OPTIONS = [date(2026, 11, d) for d in (18, 20, 22, 24)]


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


RUN_A = ('run-a', utc(2026, 11, 22, 23, 30))   # Nov 22 15:30 PST
RUN_B = ('run-b', utc(2026, 11, 24, 19, 22))   # Nov 24 11:22 PST


class SessionLogicTests(unittest.TestCase):
    def test_startup_initializes_end_date_from_latest_completed_run(self):
        state = {}
        self.assertTrue(apply_latest_ingestion_end_date(state, key=KEY, available_dates=OPTIONS, latest_run=RUN_B))
        self.assertEqual(state[KEY], date(2026, 11, 24))
        self.assertEqual(state[END_DATE_RUN_MARKER_KEY], {'run_id': 'run-b', 'applied': date(2026, 11, 24)})

    def test_new_completed_run_advances_end_date(self):
        state = {}
        apply_latest_ingestion_end_date(state, key=KEY, available_dates=OPTIONS, latest_run=RUN_A)
        self.assertEqual(state[KEY], date(2026, 11, 22))
        self.assertTrue(apply_latest_ingestion_end_date(state, key=KEY, available_dates=OPTIONS, latest_run=RUN_B))
        self.assertEqual(state[KEY], date(2026, 11, 24))

    def test_new_run_advances_even_after_an_earlier_manual_choice(self):
        state = {}
        apply_latest_ingestion_end_date(state, key=KEY, available_dates=OPTIONS, latest_run=RUN_A)
        state[KEY] = date(2026, 11, 18)  # user choice during run A
        apply_latest_ingestion_end_date(state, key=KEY, available_dates=OPTIONS, latest_run=RUN_B)
        self.assertEqual(state[KEY], date(2026, 11, 24))

    def test_page_reruns_and_manual_overrides_are_preserved(self):
        state = {}
        apply_latest_ingestion_end_date(state, key=KEY, available_dates=OPTIONS, latest_run=RUN_B)
        state[KEY] = date(2026, 11, 20)  # manual selection
        for _ in range(5):  # ordinary filtering/pagination reruns, same run
            self.assertFalse(apply_latest_ingestion_end_date(state, key=KEY, available_dates=OPTIONS,
                                                             latest_run=RUN_B))
        self.assertEqual(state[KEY], date(2026, 11, 20))

    def test_no_reliable_run_keeps_existing_fallback(self):
        for latest in (None, ('run-x', None), ('run-y', '2026-11-24')):
            with self.subTest(latest=latest):
                state = {}
                self.assertFalse(apply_latest_ingestion_end_date(state, key=KEY, available_dates=OPTIONS,
                                                                 latest_run=latest))
                self.assertEqual(state, {})
        self.assertFalse(apply_latest_ingestion_end_date({}, key=KEY, available_dates=[], latest_run=RUN_B))

    def test_end_date_is_always_a_selectable_option(self):
        sparse = [date(2026, 11, 20), date(2026, 11, 22), date(2026, 11, 25)]
        self.assertEqual(end_date_for_run(sparse, date(2026, 11, 24)), date(2026, 11, 22))
        self.assertIsNone(end_date_for_run(sparse, date(2026, 11, 19)))  # nothing on/before: fallback
        state = {}
        apply_latest_ingestion_end_date(state, key=KEY, available_dates=sparse, latest_run=RUN_B)
        self.assertEqual(state[KEY], date(2026, 11, 22))
        self.assertIn(state[KEY], sparse)

    def test_snapped_date_catches_up_when_the_run_date_appears_unless_user_changed_it(self):
        sparse = [date(2026, 11, 20), date(2026, 11, 22)]
        state = {}
        apply_latest_ingestion_end_date(state, key=KEY, available_dates=sparse, latest_run=RUN_B)
        self.assertEqual(state[KEY], date(2026, 11, 22))
        reloaded = sparse + [date(2026, 11, 24)]  # snapshot refreshed with the run's jobs
        self.assertTrue(apply_latest_ingestion_end_date(state, key=KEY, available_dates=reloaded, latest_run=RUN_B))
        self.assertEqual(state[KEY], date(2026, 11, 24))
        manual = {}
        apply_latest_ingestion_end_date(manual, key=KEY, available_dates=sparse, latest_run=RUN_B)
        manual[KEY] = date(2026, 11, 20)
        apply_latest_ingestion_end_date(manual, key=KEY, available_dates=reloaded, latest_run=RUN_B)
        self.assertEqual(manual[KEY], date(2026, 11, 20))

    def test_timezone_boundaries_use_america_los_angeles(self):
        cases = {
            utc(2026, 11, 25, 7, 59): date(2026, 11, 24),   # 23:59 PST
            utc(2026, 11, 25, 8, 0): date(2026, 11, 25),    # 00:00 PST
            utc(2026, 7, 1, 6, 59): date(2026, 6, 30),      # 23:59 PDT
            utc(2026, 7, 1, 7, 0): date(2026, 7, 1),        # 00:00 PDT
            utc(2026, 11, 1, 7, 30): date(2026, 11, 1),     # 00:30 PDT, DST-end day
            datetime(2026, 11, 25, 7, 30): date(2026, 11, 24),  # naive = stored UTC
        }
        for completed_at, expected in cases.items():
            with self.subTest(completed_at=completed_at):
                self.assertEqual(local_run_date(completed_at), expected)
        # A late-evening Pacific run is not dated by its UTC calendar day.
        state = {}
        options = [date(2026, 11, 24), date(2026, 11, 25)]
        apply_latest_ingestion_end_date(state, key=KEY, available_dates=options,
                                        latest_run=('late', utc(2026, 11, 25, 5, 0)))
        self.assertEqual(state[KEY], date(2026, 11, 24))


def insert_run(connection, run_id, workflow, status, started, completed):
    connection.execute(
        "INSERT INTO scoring_runs (run_id, resume_hash, workflow, status, started_at, completed_at, "
        "prompt_version) VALUES (?, 'resume-1', ?, ?, ?, ?, 'p')",
        [run_id, workflow, status, started, completed])


class RunHistoryTests(unittest.TestCase):
    def scenario(self, callback):
        from src.test_recommendation_repository import with_temp_review_database
        with_temp_review_database(callback)

    def test_only_completed_ingestion_runs_count(self):
        def body():
            self.assertIsNone(runs.load_latest_completed_ingestion_run())  # empty history
            runs.initialize_scoring_run_table()
            with database.get_connection() as c:
                insert_run(c, 'ok-old', 'ingest_enrich_score', 'completed', utc(2026, 11, 21, 17), utc(2026, 11, 21, 17, 5))
                insert_run(c, 'ok', 'ingest_enrich_score', 'completed', utc(2026, 11, 22, 23), utc(2026, 11, 22, 23, 30))
                insert_run(c, 'failed', 'ingest_enrich_score', 'failed', utc(2026, 11, 24, 18), utc(2026, 11, 24, 18, 5))
                insert_run(c, 'running', 'ingest_enrich_score', 'running', utc(2026, 11, 24, 19), None)
                insert_run(c, 'score-only', 'score_unscored', 'completed', utc(2026, 11, 24, 20), utc(2026, 11, 24, 20, 9))
                insert_run(c, 'resume', 'score_jobs', 'completed', utc(2026, 11, 24, 21), utc(2026, 11, 24, 21, 9))
            run_id, completed_at = runs.load_latest_completed_ingestion_run()
            self.assertEqual(run_id, 'ok')
            self.assertEqual(local_run_date(completed_at), date(2026, 11, 22))
        self.scenario(body)

    def test_streamlit_refreshes_run_cache_only_after_a_daily_run(self):
        source = Path('streamlit_app.py').read_text()
        branch = source.split('workflow=(\n                    "ingest_enrich_score"', 1)[1][:600]
        self.assertIn('if run_daily_clicked:', branch)
        self.assertIn('st.session_state.pop(LATEST_INGESTION_RUN_CACHE_KEY, None)', branch)


class WholeAppTests(unittest.TestCase):
    """Real streamlit_app.py reruns; repository and network boundaries mocked."""

    def test_end_date_initializes_survives_reruns_and_advances_after_new_run(self):
        from src.test_recommendation_repository import with_temp_review_database
        with_temp_review_database(self._app_scenario)

    def _app_scenario(self):
        from streamlit.testing.v1 import AppTest
        from src.repositories import recommendation_repository as repo
        from src.test_job_review_visibility import complete_ai_job
        from src.test_status_performance import DESC
        from src.ui.daily_workflow_service import ScoringBacklogStatus
        today = datetime.now(timezone.utc)
        jobs = []
        for (key, days_ago), number in zip((('a', 4), ('b', 2), ('c', 0)), (101, 102, 103)):
            url = f'https://job-boards.greenhouse.io/example/jobs/{number}'  # distinct postings
            jobs.append(complete_ai_job(
                apply_url=url, official_job_url=url, verified_posting_key=f'greenhouse:{number}',
                actionability_posting_key=f'greenhouse:{number}', actionability_official_url=url,
                record_key=key, source='linkedin', company_name='Example Co', location='Remote',
                title='Senior Analytics Engineer', ai_score=90, recommendation='apply',
                canonical_job_key=key, exact_posting_key=key, duplicate_fingerprint=key,
                sent_at=today - timedelta(days=days_ago), description=DESC, matching_strengths=[],
                hard_requirements_missing=[], preferred_qualifications_missing=[], risk_factors=[],
                title_fit=90, skills_fit=90, experience_fit=90, seniority_fit=90, industry_fit=90,
                location_fit=90))
        dates = sorted({pacific_date(j['sent_at']) for j in jobs})  # app dates are Pacific
        runs.initialize_scoring_run_table()
        first_run_at = datetime.combine(dates[1], datetime.min.time(), timezone.utc) + timedelta(hours=20)
        with database.get_connection() as c:  # 20:00 UTC is midday Pacific: same calendar day
            insert_run(c, 'run-1', 'ingest_enrich_score', 'completed', first_run_at, first_run_at)
        queue_stats = {name: 0 for name in (
            'fresh_description', 'eligible_deferred_retries', 'employer_verification_pending',
            'full_jd_employer_pending', 'fit_recommended_employer_pending', 'cooldown',
            'excluded', 'retry_disabled', 'runnable', 'selected')}
        queue_stats.update(pending_employer_by_source={}, pending_employer_all_new_by_source={})
        metrics = repo.reviewable_job_metrics(jobs)
        metrics.update(raw_jobs=3, exact_postings=3, enrichment_attempts={})
        with patch.object(repo, 'load_all_jobs', Mock(side_effect=lambda **kw: copy.deepcopy(jobs))), \
                patch.object(repo, 'load_pipeline_operation_metrics', return_value=metrics), \
                patch('src.ui.daily_workflow_service.load_scoring_backlog_status',
                      return_value=ScoringBacklogStatus('resume-1', 0, 0, ())), \
                patch('src.ui.daily_workflow_service.load_daily_enrichment_plan', return_value=([], queue_stats)), \
                patch('src.actionability.refresh_daily', side_effect=AssertionError('no network')), \
                patch('src.ui.candidate_identity.candidates', return_value=[]), \
                patch('src.ui.candidate_identity.cache_token', return_value=None), \
                patch('src.scoring_run_repository.load_latest_scoring_run', return_value=None):
            app = AppTest.from_string(Path('streamlit_app.py').read_text(), default_timeout=60)
            app.session_state['show_saved_jobs'] = True
            app.session_state['resume_hash'] = 'resume-1'
            app.run()
            self.assertFalse(app.exception, str(app.exception))
            end = lambda: next(s for s in app.selectbox if s.label == 'End date')
            start = lambda: next(s for s in app.selectbox if s.label == 'Start date')
            self.assertEqual(end().value, dates[1])        # startup: run-1's date, not the newest job
            self.assertEqual(start().value, dates[0])      # Start date behaviour unchanged
            self.assertFalse(app.warning and any('Session State' in w.value for w in app.warning))
            end().set_value(dates[0]).run()                # manual override
            next(s for s in app.selectbox if s.label == 'Status').set_value('All').run()  # ordinary rerun
            self.assertEqual(end().value, dates[0])
            # A new completed daily run is persisted; the Run daily path drops the cache.
            second_run_at = datetime.combine(dates[2], datetime.min.time(), timezone.utc) + timedelta(hours=20)
            with database.get_connection() as c:
                insert_run(c, 'run-failed', 'ingest_enrich_score', 'failed', second_run_at, second_run_at)
            app.run()
            self.assertEqual(end().value, dates[0])        # a failed run never advances it
            with database.get_connection() as c:
                insert_run(c, 'run-2', 'ingest_enrich_score', 'completed', second_run_at, second_run_at)
            app.run()
            self.assertEqual(end().value, dates[0])        # plain rerun: cached, no re-query
            del app.session_state['latest_completed_ingestion_run_cache']  # what Run daily does
            app.run()
            self.assertFalse(app.exception, str(app.exception))
            self.assertEqual(end().value, dates[2])
            self.assertEqual(start().value, dates[0])


def _jobs_tab_nodes():
    import ast
    module = ast.parse(Path('streamlit_app.py').read_text())
    tab = next(n for n in module.body if isinstance(n, ast.With)
               and any(isinstance(i.context_expr, ast.Name) and i.context_expr.id == 'jobs_tab' for i in n.items))
    return ast, tab


def run_app_date_filter(jobs, start_date, end_date):
    """Execute the Jobs tab's actual Start/End filter statement."""
    ast, tab = _jobs_tab_nodes()
    node = next(n for n in tab.body if isinstance(n, ast.If) and ast.unparse(n.test) == 'start_date and end_date')
    scope = dict(filtered_jobs=list(jobs), start_date=start_date, end_date=end_date, pacific_date=pacific_date)
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'streamlit_app.py', 'exec'), scope)
    return [job['record_key'] for job in scope['filtered_jobs']]


def run_app_date_options(jobs):
    """Execute the Jobs tab's actual job_dates statement (Start/End options source)."""
    ast, tab = _jobs_tab_nodes()
    node = next(n for n in tab.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'job_dates' for t in n.targets))
    scope = dict(all_jobs=list(jobs), pacific_date=pacific_date)
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'streamlit_app.py', 'exec'), scope)
    return sorted(set(scope['job_dates']))


class PacificJobDateTests(unittest.TestCase):
    def test_conversion_keeps_utc_storage_and_handles_dst(self):
        cases = {
            utc(2026, 11, 25, 7, 59): date(2026, 11, 24),          # 23:59 PST
            utc(2026, 11, 25, 8, 0): date(2026, 11, 25),             # 00:00 PST
            datetime(2026, 11, 25, 3, 0): date(2026, 11, 24),        # naive = stored UTC
            utc(2026, 3, 8, 9, 59): date(2026, 3, 8),                # 01:59 PST, DST starts
            utc(2026, 3, 8, 10, 0): date(2026, 3, 8),                # 03:00 PDT
            utc(2026, 3, 9, 6, 59): date(2026, 3, 8),                # 23:59 PDT
            utc(2026, 3, 9, 7, 0): date(2026, 3, 9),                 # 00:00 PDT
            utc(2026, 11, 1, 8, 30): date(2026, 11, 1),              # 01:30 PDT, DST ends
            utc(2026, 11, 1, 9, 30): date(2026, 11, 1),              # 01:30 PST (repeated hour)
            utc(2026, 11, 2, 7, 59): date(2026, 11, 1),              # 23:59 PST
            utc(2026, 11, 2, 8, 0): date(2026, 11, 2),               # 00:00 PST
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(pacific_date(value), expected)
        self.assertEqual(pacific_date(date(2026, 11, 24)), date(2026, 11, 24))       # already a calendar date
        self.assertEqual(pacific_date('2026-11-24'), date(2026, 11, 24))
        self.assertEqual(pacific_date('2026-11-25T03:00:00+00:00'), date(2026, 11, 24))
        self.assertEqual(pacific_date('2026-11-25 03:00:00'), date(2026, 11, 24))    # naive string = UTC
        for empty in (None, '', '   ', 'not a date'):
            self.assertIsNone(pacific_date(empty))

    def test_filter_includes_entire_pacific_end_day_and_excludes_next_day(self):
        jobs = [
            dict(record_key='before-start', sent_at=datetime(2026, 11, 24, 7, 59, 59, tzinfo=timezone.utc)),            # Nov 23 23:59:59 PST
            dict(record_key='start-of-day', sent_at=utc(2026, 11, 24, 8, 0)),                  # Nov 24 00:00 PST
            dict(record_key='evening-utc-next-day', sent_at=utc(2026, 11, 25, 3, 0)),          # Nov 24 19:00 PST
            dict(record_key='last-microsecond', sent_at=datetime(2026, 11, 25, 7, 59, 59, 999999,
                                                                 tzinfo=timezone.utc)),     # Nov 24 23:59:59.999999
            dict(record_key='next-day', sent_at=utc(2026, 11, 25, 8, 0)),                      # Nov 25 00:00 PST
            dict(record_key='no-date', sent_at=None),
        ]
        day = date(2026, 11, 24)
        self.assertEqual(run_app_date_filter(jobs, day, day),
                         ['start-of-day', 'evening-utc-next-day', 'last-microsecond'])
        self.assertEqual(run_app_date_options(jobs), [date(2026, 11, 23), day, date(2026, 11, 25)])

    def test_filter_across_dst_transitions(self):
        jobs = [dict(record_key='pdt-last', sent_at=utc(2026, 11, 1, 6, 59)),     # Oct 31 23:59 PDT
                dict(record_key='repeat-pdt', sent_at=utc(2026, 11, 1, 8, 30)),   # Nov 1 01:30 PDT
                dict(record_key='repeat-pst', sent_at=utc(2026, 11, 1, 9, 30)),   # Nov 1 01:30 PST
                dict(record_key='pst-last', sent_at=utc(2026, 11, 2, 7, 59))]     # Nov 1 23:59 PST
        self.assertEqual(run_app_date_filter(jobs, date(2026, 11, 1), date(2026, 11, 1)),
                         ['repeat-pdt', 'repeat-pst', 'pst-last'])
        spring = [dict(record_key='pst', sent_at=utc(2026, 3, 8, 9, 59)), dict(record_key='pdt', sent_at=utc(2026, 3, 8, 10, 0)),
                  dict(record_key='pdt-last', sent_at=utc(2026, 3, 9, 6, 59)), dict(record_key='next', sent_at=utc(2026, 3, 9, 7, 0))]
        self.assertEqual(run_app_date_filter(spring, date(2026, 3, 8), date(2026, 3, 8)), ['pst', 'pdt', 'pdt-last'])

    def test_run_based_end_date_now_includes_an_evening_runs_own_jobs(self):
        # 8:13 PM PST run on Nov 18 (04:13 UTC Nov 19); an email at 7 PM PST has UTC date Nov 19.
        run = ('evening', utc(2026, 11, 19, 4, 13))
        jobs = [dict(record_key='morning', sent_at=utc(2026, 11, 18, 17, 0)),
                dict(record_key='evening-email', sent_at=utc(2026, 11, 19, 3, 0))]
        options = run_app_date_options(jobs)
        self.assertEqual(options, [date(2026, 11, 18)])           # both are Nov 18 in Pacific
        state = {}
        apply_latest_ingestion_end_date(state, key=KEY, available_dates=options, latest_run=run)
        self.assertEqual(run_app_date_filter(jobs, options[0], state[KEY]), ['morning', 'evening-email'])


if __name__ == '__main__':
    unittest.main()
