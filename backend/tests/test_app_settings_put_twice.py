"""H17: the settings PUT answers 200 on every write, not only the first.

``AppSetting.updated_at`` carries ``onupdate=func.now()``, a SQL
expression the database evaluates. Under the mapper's default
``eager_defaults="auto"`` SQLAlchemy fetches server-generated values on
INSERT (by RETURNING) and on nothing else, so the first write of a key
worked and every later one expired ``updated_at`` in the flush. The
handler then read it to build its response (``admin.py``'s
``updated_at=row.updated_at``), the ORM tried to lazy-load it, and a
lazy load in async code raises ``MissingGreenlet``: a 500 on the second
save of any setting. ``eager_defaults=True`` makes the UPDATE return
the value too, so the handler reads a loaded attribute and nothing
changes in the handler or in ``set_setting``.

No test PUT a setting before this one. The settings tests run on fakes
that expire nothing (``test_handlers_with_rows.py``), so the defect was
invisible to them by construction. This one takes real PostgreSQL, the
real platform gate (the slug patched the way
``test_platform_admin_gate.py`` does, to a non-default value), the real
router behind ``httpx.ASGITransport`` and a dict ``FakeRedis`` for the
cache. The two values differ on purpose: an unchanged value emits no
UPDATE and would pass on the broken model.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import get_db
from app.middleware import get_current_user
from app.models import User
from app.routers import admin as admin_router

REQUIRE_DB = os.environ.get("LIBRERUN_REQUIRE_DB", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


@pytest_asyncio.fixture
async def db():
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import settings

    engine = create_async_engine(settings.DATABASE_URL.get_secret_value())
    try:
        async with engine.connect() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        reason = f"no database at DATABASE_URL ({type(exc).__name__}: {exc})"
        if REQUIRE_DB:
            pytest.fail("LIBRERUN_REQUIRE_DB is set, so this may not skip: " + reason)
        pytest.skip(reason)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(bind=connection)
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


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


@pytest_asyncio.fixture
async def platform_admin(db, monkeypatch) -> User:
    """An admin of a platform tenant seeded for this test alone.

    The slug is patched through ``app.config`` (which the gate re-resolves
    per call) and is not the default, so a gate reading some other
    settings object could not pass by coincidence.
    """
    import app.config as cfg

    tenant_id = uuid.uuid4()
    slug = f"platform-{tenant_id.hex[:12]}"
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, :n, :s)"),
        {"id": tenant_id, "n": "Platform", "s": slug},
    )
    user_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, auth_provider, role)"
            " VALUES (:id, :t, :e, 'credentials', 'admin')"
        ),
        {"id": user_id, "t": tenant_id, "e": f"{user_id.hex[:10]}@example.com"},
    )
    monkeypatch.setattr(cfg.settings, "PLATFORM_TENANT_SLUG", slug)
    return await db.get(User, user_id)


def _client(db, user: User) -> AsyncClient:
    """The admin router with only the session and the principal supplied:
    ``require_admin`` and ``require_platform_admin`` run for real."""
    app = FastAPI()
    app.include_router(admin_router.router)

    async def _db_dep():
        yield db

    async def _user_dep():
        return user

    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest.mark.asyncio
async def test_the_same_key_put_twice_answers_200_both_times(db, platform_admin):
    fake = FakeRedis()

    async def _fake_redis():
        return fake

    with patch("app.services.app_settings_service.get_redis", _fake_redis):
        async with _client(db, platform_admin) as client:
            first = await client.put(
                "/admin/settings/max_upload_size_mb", json={"value": 60}
            )
            assert first.status_code == 200, first.text
            second = await client.put(
                "/admin/settings/max_upload_size_mb", json={"value": 70}
            )
            assert second.status_code == 200, second.text

    assert first.json()["value"] == 60 and second.json()["value"] == 70
    # One transaction, so PostgreSQL's now() is the same instant for both
    # writes: equal is right, earlier would be wrong.
    before = datetime.fromisoformat(first.json()["updated_at"])
    after = datetime.fromisoformat(second.json()["updated_at"])
    assert after >= before
    assert second.json()["updated_by"] == str(platform_admin.id)
