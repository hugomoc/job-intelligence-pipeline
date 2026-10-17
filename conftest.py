"""Offline pytest configuration; the legacy runner owns the test inventory."""

from __future__ import annotations

import ast
import ipaddress
import os
from pathlib import Path
import socket
import tempfile

import pytest


REPO_ROOT = Path(__file__).resolve().parent
os.environ.update(
    PYTHONPATH=str(REPO_ROOT),
    PYTHONDONTWRITEBYTECODE="1",
    RUN_LIVE_EMAIL_TESTS="0",
    SERPER_API_KEY="",
    TAVILY_API_KEY="",
    PUBLIC_SEARCH_PROVIDER="duckduckgo",
    PYTHON_DOTENV_DISABLED="1",
    DBT_SEND_ANONYMOUS_USAGE_STATS="false",
    DO_NOT_TRACK="1",
)


def pytest_addoption(parser):
    parser.addoption(
        "--ignore-manual-only", action="store_true",
        help="Exclude MANUAL_ONLY files declared by scripts/run_ci_tests.py",
    )


def pytest_configure(config):
    if config.getoption("ignore_manual_only"):
        tree = ast.parse((REPO_ROOT / "scripts/run_ci_tests.py").read_text())
        manual_only = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "MANUAL_ONLY"
                    for target in node.targets)
        )
        config.option.ignore = list(config.option.ignore or [])
        config.option.ignore.extend(str(REPO_ROOT / path) for path in manual_only)

    # Set this before collection imports src.database and caches its path.
    directory = tempfile.TemporaryDirectory(prefix="job-pytest-session-")
    config.add_cleanup(directory.cleanup)
    previous = os.environ.get("JOB_INTELLIGENCE_DB_PATH")
    os.environ["JOB_INTELLIGENCE_DB_PATH"] = str(Path(directory.name) / "jobs.db")

    def restore_database_path():
        if previous is None:
            os.environ.pop("JOB_INTELLIGENCE_DB_PATH", None)
        else:
            os.environ["JOB_INTELLIGENCE_DB_PATH"] = previous

    config.add_cleanup(restore_database_path)


def _localhost(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host is None or str(host).lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def block_external_sockets(monkeypatch):
    """Reject external connections, even if a test catches the initial failure."""
    attempts = []
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    create_connection = socket.create_connection
    getaddrinfo = socket.getaddrinfo

    def check(host):
        if not _localhost(host):
            attempts.append(str(host))
            pytest.fail(f"External socket connection blocked: {host}")

    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            check(address[0])
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            check(address[0])
        return connect_ex(sock, address)

    def guarded_create_connection(address, *args, **kwargs):
        check(address[0])
        return create_connection(address, *args, **kwargs)

    def guarded_getaddrinfo(host, *args, **kwargs):
        check(host)
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield
    if attempts:
        pytest.fail("External socket attempts: " + ", ".join(attempts))
