"""Shared pytest fixtures.

Configures logging against a temporary file so tests don't scribble on
the real ``./data/logs/backend.jsonl``. Tests that read logged output
use the ``log_file`` fixture; tests that only rely on ``caplog`` can
ignore it.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Decided at import, before any test module is collected: a test module
# that imports ``app.main`` at collection time runs ``configure_logging``
# right there, and the queue-only pipeline (blueprint S4) consults no
# handler list — pytest's caplog would see nothing for the whole session.
# The suite runs the synchronous pipeline and drives the queue explicitly
# (tests/test_logging_queue.py).
os.environ.setdefault("LOG_QUEUE_ONLY", "false")

# The reference echo agent runs on the agent SDK (blueprint S4), imported
# from its source tree in this repository; the SDK's stdout/stderr
# capture stays off in the test process (pytest's streams are pytest's).
_SDK_SRC = Path(__file__).resolve().parents[2] / "sdk" / "python" / "librerun-agent" / "src"
if str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))
os.environ.setdefault("LIBRERUN_AGENT_CAPTURE_STDIO", "0")


@pytest.fixture(scope="session", autouse=True)
def _install_structlog_for_tests():
    """Install our structlog stack so ``caplog`` captures structured events
    from ``structlog.get_logger(...).error(...)`` calls across the whole
    suite. Without this, tests that don't use the ``log_file`` fixture see
    structlog's default (non-stdlib) config and nothing reaches caplog.
    """
    tmp_path = Path(tempfile.mkdtemp(prefix="vita-test-logs-"))
    os.environ.setdefault("LOG_FILE_PATH", str(tmp_path / "backend.jsonl"))
    os.environ.setdefault("LOG_STDERR_ENABLED", "false")
    from app.config import Settings

    new_settings = Settings()
    import app.config as cfg
    cfg.settings = new_settings
    import app.logging_config as lc
    lc.settings = new_settings
    lc._CONFIGURED = False
    lc.configure_logging()
    yield


@pytest.fixture
def log_file(tmp_path, monkeypatch) -> Path:
    """Point logging at a temp file, (re)install logging, and return the path.

    Use with the ``logging_configured`` fixture when you need to drive
    actual file writes. Resets the structlog-configured flag so each test
    gets its own handler set.
    """
    path = tmp_path / "backend.jsonl"
    monkeypatch.setenv("LOG_FILE_PATH", str(path))
    monkeypatch.setenv("LOG_STDERR_ENABLED", "false")
    # Re-import settings so the env change is picked up.
    from app.config import Settings
    from app import logging_config as _lc

    new_settings = Settings()
    monkeypatch.setattr("app.config.settings", new_settings)
    monkeypatch.setattr(_lc, "settings", new_settings)
    monkeypatch.setattr(_lc, "_CONFIGURED", False)

    # Force reconfigure with fresh handlers targeting the tmp file.
    _lc.configure_logging()
    yield path

    # After test: detach file handler so tmp_path can be cleaned up cleanly.
    import logging as _logging

    root = _logging.getLogger()
    for h in list(root.handlers):
        try:
            h.close()
        except Exception:
            pass
        root.removeHandler(h)
    monkeypatch.setattr(_lc, "_CONFIGURED", False)


@pytest.fixture(autouse=True)
def clear_contextvars_between_tests():
    """Don't let request/run contextvars bleed across tests."""
    import structlog

    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
