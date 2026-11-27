"""Optional historical fixture reader and its deterministic, stdlib-only tests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class MissingArchiveFixture(unittest.SkipTest):
    """A specifically requested optional archive file is genuinely absent."""


def read_archive_fixture(path: Path, *, binary: bool = False) -> str | bytes:
    """Skip only an absent file; decoding, permissions and other errors propagate."""
    try:
        return path.read_bytes() if binary else path.read_text()
    except FileNotFoundError as error:
        raise MissingArchiveFixture(f'Missing optional archive fixture: {path}') from error


class ArchiveResponseBodies:
    """Remember fixture read errors even if a resolver handles the exception."""

    def __init__(self):
        self.error = None

    def read(self, path: Path) -> bytes:
        try:
            return read_archive_fixture(path, binary=True)
        except (MissingArchiveFixture, OSError) as error:
            # Never let a later absence replace an actual I/O error.
            if self.error is None or isinstance(self.error, MissingArchiveFixture):
                self.error = error
            raise

    def require_complete(self):
        if self.error is not None:
            raise self.error


class ArchiveFixtureTests(unittest.TestCase):
    def test_missing_parent_and_missing_file_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in (root / 'missing-parent' / 'sample', root / 'missing-file'):
                for binary in (False, True):
                    with self.subTest(path=path, binary=binary):
                        with self.assertRaises(MissingArchiveFixture) as caught:
                            read_archive_fixture(path, binary=binary)
                        self.assertIn(str(path), str(caught.exception))

    def test_present_file_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample'
            path.write_bytes(b'fixture-reader test only')
            self.assertEqual(read_archive_fixture(path), 'fixture-reader test only')
            self.assertEqual(read_archive_fixture(path, binary=True), b'fixture-reader test only')

    def test_non_absence_io_error_propagates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample'
            path.touch()
            with patch.object(Path, 'read_text', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    read_archive_fixture(path)


    def test_swallowed_missing_body_still_skips_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing-body'
            bodies = ArchiveResponseBodies()

            def resolver_with_broad_handler():
                try:
                    bodies.read(path)
                except Exception:
                    return False  # Simulate the expected negative identity result.
                return True

            class Replay(unittest.TestCase):
                def runTest(self):
                    verified = resolver_with_broad_handler()
                    bodies.require_complete()
                    self.assertFalse(verified)

            result = Replay().run(unittest.TestResult())
            self.assertEqual(result.testsRun, 1)
            self.assertEqual(len(result.skipped), 1)
            self.assertIn(str(path), result.skipped[0][1])
            self.assertEqual(result.errors, [])
            self.assertEqual(result.failures, [])


    def test_swallowed_non_absence_body_error_still_fails(self):
        bodies = ArchiveResponseBodies()
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('denied')):
            try:
                bodies.read(Path('unreadable'))
            except Exception:
                pass  # Simulate a resolver converting I/O errors to a result.
        with self.assertRaises(PermissionError):
            bodies.require_complete()


if __name__ == '__main__':
    unittest.main()
