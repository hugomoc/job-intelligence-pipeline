"""Database path selection and test isolation for src.database.

Import-time behaviour is checked in fresh interpreters so a cached module cannot
mask it. No test opens the default application database.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import database

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT = REPO_ROOT / 'data' / 'jobs.duckdb'
ENV, FORBID = 'JOB_INTELLIGENCE_DB_PATH', 'JOB_INTELLIGENCE_FORBID_DEFAULT_DB'


def run_python(code, **env_changes):
    environment = {k: v for k, v in os.environ.items() if k not in (ENV, FORBID)}
    environment.update({k: v for k, v in env_changes.items() if v is not None})
    environment['PYTHONPATH'] = str(REPO_ROOT)
    return subprocess.run([sys.executable, '-c', code], cwd=REPO_ROOT, env=environment,
                          capture_output=True, text=True, timeout=120)


class PathSelectionTests(unittest.TestCase):
    def test_default_path_is_unchanged_without_override(self):
        out = run_python('from src import database as d; print(d.DATABASE_PATH); print(d.DEFAULT_DATABASE_PATH)')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), [str(DEFAULT), str(DEFAULT)])

    def test_blank_override_means_default(self):
        out = run_python('from src import database as d; print(d.DATABASE_PATH)', **{ENV: '   '})
        self.assertEqual(out.stdout.strip(), str(DEFAULT), out.stderr)

    def test_explicit_override_is_used_for_connections_and_cached_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'nested' / 'jobs.duckdb'
            code = ('from src import database as d\n'
                    'from src.database import DATABASE_PATH as cached\n'
                    'with d.get_connection() as c: c.execute("create table t(x int)")\n'
                    'print(d.DATABASE_PATH); print(cached)')
            out = run_python(code, **{ENV: str(target), FORBID: '1'})
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.split(), [str(target), str(target)])
            self.assertTrue(target.exists())

    def test_module_that_cached_the_path_at_import_sees_the_override(self):
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / 'jobs.duckdb')
            out = run_python('import src.drain_enrichment_backlog as m; print(m.DATABASE_PATH)',
                             **{ENV: target, FORBID: '1'})
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), target)


class FailClosedTests(unittest.TestCase):
    def test_forbid_flag_refuses_default_database_before_connecting(self):
        code = ('from src import database as d\n'
                'import duckdb\n'
                'duckdb.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("connected"))\n'
                'try:\n    d.get_connection()\nexcept RuntimeError as e:\n    print("REFUSED", e)\n')
        out = run_python(code, **{FORBID: '1'})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith('REFUSED'), out.stdout + out.stderr)

    def test_forbid_flag_catches_default_path_restored_or_spelled_differently(self):
        with patch.dict(os.environ, {FORBID: '1'}):
            for path in (DEFAULT, REPO_ROOT / 'data' / '..' / 'data' / 'jobs.duckdb'):
                with self.subTest(path=path), patch.object(database, 'DATABASE_PATH', path), \
                        patch.object(database.duckdb, 'connect', side_effect=AssertionError('connected')):
                    with self.assertRaises(RuntimeError):
                        database.get_connection()

    def test_temporary_paths_still_work_with_the_flag(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {FORBID: '1'}), \
                patch.object(database, 'DATABASE_PATH', Path(directory) / 'jobs.duckdb'):
            with database.get_connection() as connection:
                self.assertEqual(connection.execute('select 1').fetchone(), (1,))


@unittest.skipUnless(os.environ.get(FORBID) == '1', 'only meaningful under pytest or the CI runner')
class HarnessTests(unittest.TestCase):
    def test_harness_database_is_disposable(self):
        self.assertNotEqual(Path(database.DATABASE_PATH).resolve(), DEFAULT.resolve())
        self.assertTrue(os.environ.get(ENV))


class CiRunnerTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(REPO_ROOT / 'scripts'))
        self.addCleanup(sys.path.remove, str(REPO_ROOT / 'scripts'))
        import run_ci_tests
        self.runner = run_ci_tests

    def test_runner_supplies_disposable_path_and_fail_closed_flag(self):
        env = self.runner.isolated_database_environment({}, '/tmp/scratch')
        self.assertEqual(env[ENV], str(Path('/tmp/scratch') / 'jobs.duckdb'))
        self.assertEqual(env[FORBID], '1')

    def test_runner_keeps_explicit_ci_override(self):
        env = self.runner.isolated_database_environment({ENV: '/tmp/job-intelligence-ci.duckdb'}, '/tmp/s')
        self.assertEqual(env[ENV], '/tmp/job-intelligence-ci.duckdb')
        self.assertEqual(env[FORBID], '1')


if __name__ == '__main__':
    unittest.main()
