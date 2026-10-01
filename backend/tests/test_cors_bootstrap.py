"""Regression tests for the import-time CORS bootstrap in ``app.main``.

``_load_cors_origins`` runs under ``asyncio.run`` at module import — a
throwaway event loop closed before uvicorn starts. Any pooled asyncpg
connection created there stays bound to the dead loop; if it survives in
the engine's pool, the first real request fails with "got Future attached
to a different loop" and the connection is then permanently stuck at
"cannot perform operation: another operation is in progress". The guard is
``engine.dispose()`` in the ``finally`` — these tests pin it for both the
success and the DB-unreachable paths.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import main as main_module


def _fake_session_factory():
    @asynccontextmanager
    async def _session():
        yield object()

    return _session


def test_cors_bootstrap_disposes_engine_on_success(monkeypatch):
    from app import database

    # AsyncEngine.dispose is read-only on the instance; swap the whole
    # engine object — _load_cors_origins imports it at call time.
    dispose = AsyncMock()
    monkeypatch.setattr(database, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory())
    monkeypatch.setattr(
        main_module.app_settings_service,
        "get_setting",
        AsyncMock(return_value=["http://example.test"]),
    )

    result = asyncio.run(main_module._load_cors_origins())

    assert result == ["http://example.test"]
    dispose.assert_awaited_once()


def test_cors_bootstrap_disposes_engine_on_failure(monkeypatch):
    from app import database

    # AsyncEngine.dispose is read-only on the instance; swap the whole
    # engine object — _load_cors_origins imports it at call time.
    dispose = AsyncMock()
    monkeypatch.setattr(database, "engine", SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(main_module, "async_session", _fake_session_factory())
    monkeypatch.setattr(
        main_module.app_settings_service,
        "get_setting",
        AsyncMock(side_effect=ConnectionError("db down")),
    )

    result = asyncio.run(main_module._load_cors_origins())

    # Falls back to the .env-derived list...
    assert result == main_module.settings.cors_origins_list
    # ...and still cleans up any connection made on this throwaway loop.
    dispose.assert_awaited_once()
