"""Deterministic orchestration checks; no network or production DB writes."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from src import drain_enrichment_backlog as drain
from src import enrich_jobs, database
from src.enrichment import official_job_resolver as resolver
from src.enrichment.job_description import JobDescriptionResult


def job(key, *, attempts=0, state='NEEDS_ENRICHMENT', previous=None):
    return {'record_key': key, 'title': 'Data Engineer', 'company_name': 'Example',
            'attempt_count': attempts, 'previous_status': previous,
            'description_state': state, 'needs_description_enrichment': state != 'FULL_JD',
            'needs_official_resolution': state != 'FULL_JD'}


def result(status='no_description', updated=False, official=None):
    return SimpleNamespace(stored_status=status, updated=updated,
                           stored_error=None, identity_validation=None, official_resolution=official,
                           result=SimpleNamespace(http_status=200, word_count=0))


class DrainTests(unittest.TestCase):
    def setUp(self):
        metadata = patch.object(drain, 'prepare_verified_candidate_metadata_before_queue',
                                return_value={'offline_reused': 0})
        metadata.start()
        self.addCleanup(metadata.stop)

    def run_drain(self, inventory, **options):
        attempted = set()
        saved = []
        connection = SimpleNamespace()
        connection.execute = lambda query, params: SimpleNamespace(
            fetchone=lambda: (1,) if params[0] in attempted else None)
        manager = unittest.mock.MagicMock()
        manager.__enter__.return_value = connection

        def loader():
            return [dict(j, attempt_count=1, previous_status='no_description')
                    if j['record_key'] in attempted else dict(j) for j in inventory]

        def processor(j, client):
            attempted.add(j['record_key'])
            saved.append(j['record_key'])
            return result()

        processor = options.pop('processor', processor)
        with patch.object(drain, 'get_connection', return_value=manager):
            report = drain.drain(None, loader=loader, processor=processor,
                                 emit=lambda text: None, sleeper=lambda seconds: None,
                                 **options)
            again = drain.drain(None, loader=loader, processor=processor,
                                emit=lambda text: None, sleeper=lambda seconds: None)
        return report, again, saved

    def test_reload_chunks_skip_failed_and_full_jds_and_resume(self):
        inventory = [job('a'), job('failed', attempts=2, previous='blocked'),
                     job('full', state='FULL_JD'), job('b'), job('c')]
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            report, again, saved = self.run_drain(inventory, batch_size=2, checkpoint=path)
            # Second invocation overwrites its own report only when explicitly requested.
            persisted = json.loads(path.read_text())
        self.assertEqual(saved, ['a', 'b', 'c'])
        self.assertEqual(report['batches'], 2)
        self.assertEqual(report['remaining_never_attempted_needing_jd'], 0)
        self.assertEqual(report['remaining']['previously_attempted'], 4)
        self.assertEqual(again['processed'], 0)
        self.assertEqual(persisted['processed'], 3)

    def test_max_jobs(self):
        report, _, _ = self.run_drain([job('a'), job('b'), job('c')], batch_size=2, max_jobs=1)
        self.assertEqual(report['processed'], 1)
        self.assertEqual(report['remaining_never_attempted_needing_jd'], 2)
        self.assertEqual(report['stop_reason'], 'max_jobs reached')

    def test_rate_limit_guard_consecutive_and_rolling(self):
        guard = drain.RateLimitGuard()
        for _ in range(5):
            guard.observe(SimpleNamespace(status_code=429))
        self.assertTrue(guard.stopped)
        guard = drain.RateLimitGuard()
        for _ in range(10):
            guard.observe(SimpleNamespace(status_code=429))
            guard.observe(SimpleNamespace(status_code=200))
        self.assertTrue(guard.stopped)
        self.assertEqual(guard.total, 10)

    def test_rate_limit_stops_after_saving_completed_attempt(self):
        guard = drain.RateLimitGuard()
        saved = []
        queue = [job(str(i)) for i in range(10)]
        connection = unittest.mock.MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchone.return_value = None
        def loader():
            return [j for j in queue if j['record_key'] not in saved]
        def processor(j, client):
            guard.observe(SimpleNamespace(status_code=429))
            saved.append(j['record_key'])
            return result(status='blocked')
        with patch.object(drain, 'get_connection', return_value=connection):
            report = drain.drain(None, loader=loader, processor=processor, guard=guard,
                                 emit=lambda text: None, sleeper=lambda seconds: None)
        self.assertEqual(report['processed'], 5)
        self.assertEqual(report['remaining_never_attempted_needing_jd'], 5)
        self.assertEqual(report['stop_reason'], 'excessive rate limiting')

    def test_search_circuit_defers_sixth_job_but_source_ats_and_drain_continue(self):
        circuit = resolver.PublicSearchCircuit()
        source_result = JobDescriptionResult(
            requested_url='https://example.com/job', final_url=None,
            status='no_description', http_status=200, extraction_method=None,
            description='', word_count=0, error_message=None)
        inventory = [dict(job(str(i)), apply_url='https://example.com/job',
                          source='linkedin', description='') for i in range(6)]
        saved = []
        manager = unittest.mock.MagicMock()
        manager.__enter__.return_value.execute.return_value.fetchone.return_value = None
        def loader():
            return [j for j in inventory if j['record_key'] not in saved]
        def processor(j, client):
            outcome = drain.process_one(j, client, search_circuit=circuit)
            saved.append(j['record_key'])
            return outcome
        with patch.object(drain, 'get_connection', return_value=manager), \
             patch.object(enrich_jobs, 'fetch_job_description', return_value=source_result) as source, \
             patch.object(enrich_jobs, 'verify_known_official_candidate', return_value=SimpleNamespace(official_resolution=None)), \
             patch.object(resolver, 'discover_ats_candidates', return_value=[]) as ats, \
             patch.object(resolver, 'search_candidates', side_effect=TimeoutError('search timeout')) as search, \
             patch.object(drain, 'save_enrichment_attempt') as persist:
            report = drain.drain(None, loader=loader, processor=processor, search_circuit=circuit,
                                 emit=lambda text: None, sleeper=lambda seconds: None)
        self.assertEqual(search.call_count, 5)
        self.assertEqual(source.call_count, 6)
        self.assertEqual(ats.call_count, 6)
        self.assertEqual(report['processed'], 6)
        self.assertIsNone(report['stop_reason'])
        self.assertTrue(circuit.open)
        self.assertEqual(circuit.summary(), {'opened': True, 'calls_attempted': 5, 'errors': 5, 'jobs_deferred': 1})
        self.assertEqual(persist.call_args.kwargs['official_resolution'].status, resolver.OFFICIAL_SEARCH_DEFERRED)

    def test_deferred_status_is_persisted_and_fresh_retry_circuit_searches(self):
        with TemporaryDirectory() as directory, \
             patch.object(database, 'DATA_DIR', Path(directory)), \
             patch.object(database, 'DATABASE_PATH', Path(directory) / 'jobs.duckdb'):
            enrich_jobs.initialize_enrichment_tables()
            with database.get_connection() as c:
                c.execute('CREATE TABLE job_matches(record_key VARCHAR,match_score INTEGER,is_recommended BOOLEAN,needs_review BOOLEAN)')
                c.execute('CREATE TABLE resume_job_scores(record_key VARCHAR,overall_score INTEGER,description_complete BOOLEAN,scored_at TIMESTAMPTZ)')
            circuit = resolver.PublicSearchCircuit()
            circuit.open = True
            j = dict(job('deferred'), apply_url='https://example.com/job',
                     source='linkedin', description='')
            with database.get_connection() as c:
                c.execute("INSERT INTO raw_jobs(record_key,job_fingerprint,source,title,company_name,apply_url) VALUES ('deferred','fp','linkedin','Data Engineer','Example','https://example.com/job')")
            source_result = JobDescriptionResult(
                requested_url=j['apply_url'], final_url=None, status='no_description',
                http_status=200, extraction_method=None, description='', word_count=0, error_message=None)
            with patch.object(enrich_jobs, 'fetch_job_description', return_value=source_result), \
                 patch.object(enrich_jobs, 'verify_known_official_candidate', return_value=SimpleNamespace(official_resolution=None)), \
                 patch.object(resolver, 'discover_ats_candidates', return_value=[]), \
                 patch.object(resolver, 'search_candidates', return_value=[]) as search:
                drain.process_one(j, None, search_circuit=circuit)
                search.assert_not_called()
                with database.get_connection() as c:
                    stored = c.execute('SELECT attempt_count,official_url_status FROM job_enrichment_attempts').fetchone()
                self.assertEqual(stored, (1, resolver.OFFICIAL_SEARCH_DEFERRED))
                self.assertEqual(drain.load_queue(), [])
                retry = drain.load_queue(retry_official_search=True)
                self.assertEqual(len(retry), 1)
                drain.process_one(retry[0], None, search_circuit=resolver.PublicSearchCircuit())
                self.assertEqual(search.call_count, 1)
                self.assertEqual(drain.load_queue(retry_official_search=True), [])

    def test_first_pass_queue_does_not_hide_new_records_behind_attempted_or_fuzzy_representative(self):
        with TemporaryDirectory() as directory, \
             patch.object(database, 'DATA_DIR', Path(directory)), \
             patch.object(database, 'DATABASE_PATH', Path(directory) / 'jobs.duckdb'):
            enrich_jobs.initialize_enrichment_tables()
            with database.get_connection() as c:
                c.execute('CREATE TABLE job_matches(record_key VARCHAR,match_score INTEGER,is_recommended BOOLEAN,needs_review BOOLEAN)')
                c.execute('CREATE TABLE resume_job_scores(record_key VARCHAR,overall_score INTEGER,description_complete BOOLEAN,scored_at TIMESTAMPTZ)')
                for key in ['old', 'new-a', 'new-b']:
                    c.execute("INSERT INTO raw_jobs(record_key,job_fingerprint,source,title,company_name,apply_url) VALUES (?, 'same-fuzzy-fingerprint','linkedin','Data Engineer','Example', ?)",
                              [key, 'https://www.linkedin.com/jobs/view/' + key])
                c.execute("INSERT INTO job_enrichment_attempts(record_key,source,requested_url,status,description_word_count,official_url_status,official_url_source) VALUES ('old','linkedin','https://example.com/job','blocked',0,'SEARCH_DEFERRED','search')")
            first_pass = drain.load_queue()
            self.assertEqual({j['record_key'] for j in first_pass}, {'new-a', 'new-b'})
            retry = drain.load_queue(retry_official_search=True)
            self.assertEqual({j['record_key'] for j in retry}, {'old'})
            self.assertTrue(all(j['attempt_count'] is None for j in first_pass))

    def test_trusted_reuse_avoids_fetch_and_does_not_double_save(self):
        with patch.object(drain, 'prepare_verified_candidate_metadata_before_queue', return_value={'offline_reused': 1}), \
             patch.object(drain, 'existing_description_result', return_value=SimpleNamespace(word_count=100)), \
             patch.object(drain, 'process_enrichment_job') as fetch, \
             patch.object(drain, 'save_enrichment_attempt') as save:
            outcome = drain.process_one(job('a'), None)
        self.assertTrue(outcome.updated)
        fetch.assert_not_called()
        save.assert_not_called()

    def test_same_fuzzy_fingerprint_does_not_propagate_failure(self):
        inventory = [dict(job('a'), duplicate_fingerprint='similar'),
                     dict(job('b'), duplicate_fingerprint='similar')]
        report, _, saved = self.run_drain(inventory)
        self.assertEqual(saved, ['a', 'b'])
        self.assertEqual(report['processed'], 2)

    def test_processing_failure_is_persisted_as_real_failure(self):
        failed = result(status='fetch_error')
        with patch.object(drain, 'process_enrichment_job', side_effect=RuntimeError('offline')), \
             patch.object(drain, 'failed_processing_result', return_value=failed), \
             patch.object(drain, 'save_enrichment_attempt') as save:
            self.assertIs(drain.process_one(job('a'), None), failed)
            self.assertEqual(save.call_args.kwargs['status'], 'fetch_error')

    def test_persistence_failure_is_not_swallowed(self):
        with patch.object(drain, 'process_enrichment_job', return_value=result()), \
             patch.object(drain, 'save_enrichment_attempt', side_effect=RuntimeError('disk')):
            with self.assertRaisesRegex(RuntimeError, 'disk'):
                drain.process_one(job('a'), None)

    def test_external_attempt_is_skipped_before_fetch(self):
        calls = 0
        def loader():
            nonlocal calls
            calls += 1
            return [job('a')] if calls == 1 else []
        connection = unittest.mock.MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchone.return_value = (1,)
        processor = unittest.mock.Mock()
        with patch.object(drain, 'get_connection', return_value=connection):
            report = drain.drain(None, loader=loader, processor=processor, emit=lambda text: None)
        processor.assert_not_called()
        self.assertEqual(report['remaining_never_attempted_needing_jd'], 0)


if __name__ == '__main__':
    unittest.main()
