"""Tests for ``render_embedded`` after Phase 6 (no legacy fallback).

The runner populates ``RunSnapshot.report_html`` during phase 2.
``render_embedded`` returns it as-is and raises if it's missing.
"""
from __future__ import annotations

import uuid

import pytest

from app.services import report_service


class _FakeSnapshot:
    def __init__(self, report_html: str | None) -> None:
        self.report_html = report_html


class _FakeSession:
    def __init__(self, snapshot: _FakeSnapshot | None) -> None:
        self._snapshot = snapshot

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        class _R:
            def __init__(self, value):
                self._value = value

            def scalar_one_or_none(self):
                return self._value

        return _R(self._snapshot)


@pytest.mark.asyncio
async def test_render_embedded_returns_cached_report_html(monkeypatch):
    snapshot = _FakeSnapshot("<p>cached fragment</p>")
    monkeypatch.setattr(report_service, "async_session", lambda: _FakeSession(snapshot))
    out = await report_service.render_embedded(uuid.uuid4())
    assert out == "<p>cached fragment</p>"


@pytest.mark.asyncio
async def test_render_embedded_raises_when_snapshot_missing(monkeypatch):
    monkeypatch.setattr(report_service, "async_session", lambda: _FakeSession(None))
    with pytest.raises(RuntimeError, match="No rendered report"):
        await report_service.render_embedded(uuid.uuid4())


@pytest.mark.asyncio
async def test_render_embedded_raises_when_report_html_missing(monkeypatch):
    snapshot = _FakeSnapshot(None)
    monkeypatch.setattr(report_service, "async_session", lambda: _FakeSession(snapshot))
    with pytest.raises(RuntimeError, match="No rendered report"):
        await report_service.render_embedded(uuid.uuid4())
