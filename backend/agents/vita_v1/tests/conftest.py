"""Pytest fixtures for the vita_v1 agent test tree.

Mirrors ``backend/tests/conftest.py`` so tests under this directory get
the same structlog-into-caplog bridge regardless of pytest collection
order (running ``pytest`` from ``backend/`` discovers ``agents/``
before ``tests/`` alphabetically, so we can't rely on the shell
conftest's autouse session fixture running first).

When the shell's legacy test directory is removed in Phase 6 this
file becomes the single source for vita_v1 test fixtures.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Decided at import, before any test module is collected (see
# backend/tests/conftest.py): the queue-only logging pipeline consults no
# handler list, so caplog would see nothing; the suites run the
# synchronous pipeline and test the queue explicitly.
os.environ.setdefault("LOG_QUEUE_ONLY", "false")


@pytest.fixture(scope="session", autouse=True)
def _install_structlog_for_tests():
    tmp_path = Path(tempfile.mkdtemp(prefix="vita-agent-test-logs-"))
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


@pytest.fixture(autouse=True)
def clear_contextvars_between_tests():
    import structlog

    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
