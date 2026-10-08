"""Deterministic orchestration checks; no network or production DB writes."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from src import drain_enrichment_backlog as drain


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
