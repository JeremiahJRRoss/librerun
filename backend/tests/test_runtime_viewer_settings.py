"""Runtime trace-viewer settings (admin UI) and override-only caching.

Three things under test, each in both directions per the house rule:

1. The three ``trace_viewer*`` registry specs validate — bad presets, bad
   base URLs and templates without ``{trace_id}`` are rejected with
   ``ValueError`` (the admin PUT maps that to HTTP 400), good values pass.
2. ``get_setting`` caches **overrides only**: a computed default is never
   frozen into Redis, so live changes to ``env_settings`` (config reloads,
   test fixtures) are observed immediately, while a DB override still
   spares the DB round-trip via the cache.
3. ``effective_trace_url`` prefers the runtime override, falls back to the
   environment configuration when the settings machinery is unreachable
   (Redis down), and never lets a convenience link raise into a run page.

No real Redis or Postgres: the service's ``get_redis`` is patched with a
dict fake and the DB with a minimal async stand-in, so this file keeps the
suite runnable without services.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.config import settings as env_settings
from app.models import AppSetting
from app.observability import trace_viewer
from app.services import app_settings_service as svc


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)


class FakeDB:
    """The subset of AsyncSession the settings service touches."""

    def __init__(self) -> None:
        self.rows: dict[str, AppSetting] = {}

    async def get(self, model, key):
        assert model is AppSetting
        return self.rows.get(key)

    def add(self, row) -> None:
        self.rows[row.key] = row

    async def flush(self) -> None:
        pass

    async def delete(self, row) -> None:
        self.rows.pop(row.key, None)


class _AdminUser:
    id = None


@pytest.fixture
def fake_backend():
    """Patch the service's ``get_redis`` with a dict-backed fake.

    ``get_redis`` is an async function, so ``patch.object`` swaps in an
    ``AsyncMock`` — ``return_value`` (not a coroutine-returning
    ``side_effect``) is what makes ``await get_redis()`` yield the fake.
    """
    redis = FakeRedis()
    with patch.object(svc, "get_redis", return_value=redis):
        yield FakeDB(), redis


# ---- 1. Spec validation, both directions ----------------------------------

@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("trace_viewer", "grafana"),
        ("trace_viewer_base_url", "ftp://viewer.internal"),
        ("trace_viewer_base_url", "localhost:16686"),
        ("trace_viewer_url_template", "https://viewer.example/latest"),
    ],
)
def test_viewer_specs_reject_invalid_values(key, bad):
    with pytest.raises(ValueError):
        svc.get_spec(key).coerce(bad)


@pytest.mark.parametrize(
    ("key", "good", "expected"),
    [
        ("trace_viewer", "Phoenix", "phoenix"),  # normalized to lowercase
        ("trace_viewer", "off", "off"),
        ("trace_viewer_base_url", "https://jaeger.corp.example", "https://jaeger.corp.example"),
        ("trace_viewer_base_url", "", ""),
        ("trace_viewer_url_template", "{base}/t/{trace_id}", "{base}/t/{trace_id}"),
        ("trace_viewer_url_template", "", ""),
    ],
)
def test_viewer_specs_accept_valid_values(key, good, expected):
    assert svc.get_spec(key).coerce(good) == expected


# ---- 2. Override-only caching ----------------------------------------------

@pytest.mark.asyncio
async def test_defaults_are_never_frozen_into_the_cache(fake_backend, monkeypatch):
    db, redis = fake_backend
    monkeypatch.setattr(env_settings, "TRACE_VIEWER", "jaeger")
    assert await svc.get_setting(db, "trace_viewer") == "jaeger"
    # The env changes (config reload, fixture swap). A cached default would
    # return "jaeger" until the TTL expired; the sentinel must not.
    monkeypatch.setattr(env_settings, "TRACE_VIEWER", "phoenix")
    assert await svc.get_setting(db, "trace_viewer") == "phoenix"


@pytest.mark.asyncio
async def test_override_wins_and_is_cached(fake_backend, monkeypatch):
    db, redis = fake_backend
    monkeypatch.setattr(env_settings, "TRACE_VIEWER", "jaeger")
    await svc.set_setting(db, "trace_viewer", "tempo", _AdminUser())
    assert await svc.get_setting(db, "trace_viewer") == "tempo"
    # Cached now: even with the DB row gone behind the cache's back, the
    # override is served from Redis.
    db.rows.clear()
    assert await svc.get_setting(db, "trace_viewer") == "tempo"


@pytest.mark.asyncio
async def test_reset_returns_to_live_default(fake_backend, monkeypatch):
    db, redis = fake_backend
    monkeypatch.setattr(env_settings, "TRACE_VIEWER", "jaeger")
    await svc.set_setting(db, "trace_viewer", "off", _AdminUser())
    assert await svc.get_setting(db, "trace_viewer") == "off"
    await svc.reset_setting(db, "trace_viewer")
    assert await svc.get_setting(db, "trace_viewer") == "jaeger"


# ---- 3. effective_trace_url -------------------------------------------------

TRACE = "a" * 32


@pytest.mark.asyncio
async def test_effective_url_honors_runtime_override(fake_backend, monkeypatch):
    db, redis = fake_backend
    monkeypatch.setattr(env_settings, "TRACE_VIEWER", "jaeger")
    monkeypatch.setattr(env_settings, "TRACE_VIEWER_BASE_URL", "http://localhost:16686")
    monkeypatch.setattr(env_settings, "TRACE_VIEWER_URL_TEMPLATE", "")
    # No override: environment default flows through the runtime path.
    assert (
        await trace_viewer.effective_trace_url(db, TRACE)
        == f"http://localhost:16686/trace/{TRACE}"
    )
    # Admin flips the viewer at runtime — the link changes immediately.
    await svc.set_setting(db, "trace_viewer", "phoenix", _AdminUser())
    await svc.set_setting(
        db, "trace_viewer_base_url", "http://localhost:6006", _AdminUser()
    )
    assert (
        await trace_viewer.effective_trace_url(db, TRACE)
        == f"http://localhost:6006/traces/{TRACE}"
    )


@pytest.mark.asyncio
async def test_effective_url_falls_back_to_env_when_settings_unreachable(monkeypatch):
    # The fallback reads the LIVE settings object (app.config.settings, which
    # the conftest session fixture replaces after this module's import), so
    # patch that one — the module-level alias would be stale. Passed before
    # S3 only because the default viewer was jaeger; it is off now.
    import app.config as _cfg

    monkeypatch.setattr(_cfg.settings, "TRACE_VIEWER", "jaeger")
    monkeypatch.setattr(_cfg.settings, "TRACE_VIEWER_BASE_URL", "http://localhost:16686")
    monkeypatch.setattr(_cfg.settings, "TRACE_VIEWER_URL_TEMPLATE", "")

    async def _broken_redis():
        raise ConnectionError("redis is down")

    with patch.object(svc, "get_redis", side_effect=_broken_redis):
        # Redis dead: the runtime path must not raise into the case page —
        # it degrades to exactly the environment-configured link.
        assert (
            await trace_viewer.effective_trace_url(FakeDB(), TRACE)
            == f"http://localhost:16686/trace/{TRACE}"
        )


@pytest.mark.asyncio
async def test_effective_url_still_none_for_missing_or_zero_trace(fake_backend):
    db, _ = fake_backend
    assert await trace_viewer.effective_trace_url(db, None) is None
    assert await trace_viewer.effective_trace_url(db, "0" * 32) is None
