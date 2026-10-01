"""The ``secret`` setting type through the admin API (K6; L31, D14, D33).

``auth.azure_client_secret`` is the registry's first secret. On K4b's
PUT-twice harness — real PostgreSQL, the real platform gate, the real
router behind ``httpx.ASGITransport``, a dict ``FakeRedis`` — these hold
that the API treats it as write-only:

* the list, the PUT and the reset answer ``value`` and ``default_value``
  null and describe the value only by ``secret``: whether the store holds
  a row, where the value comes from and, for a readable row, its
  fingerprint (``test_list_serves_metadata_only`` is also the
  element-coverage test for the list's comprehension);
* the audit detail of a set, a replace and a clear is ``{setting,
  action}`` and never a value;
* a row no configured key opens lists as ``unreadable``, never ``runtime``;
* no log line carries the value, on any path, the unreadable one included;
* every process is told of a write — ``secrets:version`` bumped — only once
  the row and its audit have committed;
* with no store key the PUT answers ``503 secrets_store_unconfigured`` —
  on the REAL app, since the answer is an app-level exception handler.
"""
from __future__ import annotations

import logging
import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app import secrets_keyring as keyring
from app.database import get_db
from app.middleware import get_current_user
from app.models import User
from app.routers import admin as admin_router
from app.services import secrets_service
from tests.test_secrets_service import db, fernet_key, store  # noqa: F401  (fixtures)

KEY = "auth.azure_client_secret"
VALUE = "the-azure-client-secret-3b9e"
REPLACED = "the-replaced-client-secret-81c0"
FROM_ENV = "the-environment-secret-55ad"


@pytest_asyncio.fixture
async def platform_admin(db, monkeypatch) -> User:  # noqa: F811
    """An admin of a platform tenant seeded for this test alone, the slug
    patched through ``app.config`` as ``test_app_settings_put_twice.py``
    does it."""
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
    monkeypatch.setattr(cfg.settings, "AZURE_CLIENT_SECRET", "")
    return await db.get(User, user_id)


def _client(db, user: User) -> AsyncClient:  # noqa: F811
    app = FastAPI()
    app.include_router(admin_router.router)

    async def _db_dep():
        yield db

    async def _user_dep():
        return user

    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _secret_row(listing: list) -> dict:
    (row,) = [row for row in listing if row["key"] == KEY]
    return row


async def _details(db, tenant_id) -> list[dict]:  # noqa: F811
    rows = (
        await db.execute(
            text(
                "SELECT detail FROM activity_audit_log"
                " WHERE tenant_id = :t AND action_type = 'config_change'"
            ),
            {"t": tenant_id},
        )
    ).scalars().all()
    return [row for row in rows if row.get("setting") == KEY]


@pytest.mark.asyncio
async def test_list_serves_metadata_only(db, store, platform_admin, monkeypatch):  # noqa: F811
    import app.config as cfg

    async with _client(db, platform_admin) as client:
        unset = await client.get("/admin/settings")
        assert unset.status_code == 200, unset.text
        row = _secret_row(unset.json())
        assert (row["value"], row["default_value"], row["value_type"]) == (None, None, "secret")
        assert row["is_default"] is True
        assert row["secret"] == {
            "set": False, "source": "unset", "fingerprint": None,
            "updated_at": None, "updated_by": None,
        }
        # Every other row is still served. A plain one carries no secret
        # block; another secret setting (K8a's kb.pinecone_api_key)
        # carries one, and no value.
        others = [r for r in unset.json() if r["key"] != KEY]
        assert {r["key"] for r in others} >= {"auth.azure_client_id", "kb.embed_model"}
        for r in others:
            if r["value_type"] == "secret":
                assert r["secret"] is not None, r
                assert (r["value"], r["default_value"]) == (None, None), r
            else:
                assert r["secret"] is None, r

        monkeypatch.setattr(cfg.settings, "AZURE_CLIENT_SECRET", FROM_ENV)
        from_env = _secret_row((await client.get("/admin/settings")).json())
        assert from_env["secret"]["source"] == "env" and from_env["secret"]["fingerprint"] is None

        put = await client.put(f"/admin/settings/{KEY}", json={"value": VALUE})
        assert put.status_code == 200, put.text
        body = put.json()
        assert (body["value"], body["default_value"], body["is_default"]) == (None, None, False)
        fingerprint = keyring.fingerprint(store.key, VALUE)
        assert body["secret"]["source"] == "runtime" and body["secret"]["set"] is True
        assert body["secret"]["fingerprint"] == fingerprint
        assert body["secret"]["updated_by"] == str(platform_admin.id)

        listed = _secret_row((await client.get("/admin/settings")).json())
        assert listed["secret"]["source"] == "runtime"
        assert listed["secret"]["fingerprint"] == fingerprint
        assert listed["value"] is None and listed["is_default"] is False

        texts = [unset.text, put.text, (await client.get("/admin/settings")).text]
    assert not any(VALUE in t or FROM_ENV in t for t in texts)


@pytest.mark.asyncio
async def test_audit_detail_has_no_value(db, store, platform_admin):  # noqa: F811
    async with _client(db, platform_admin) as client:
        assert (await client.put(f"/admin/settings/{KEY}", json={"value": VALUE})).status_code == 200
        assert (await client.put(f"/admin/settings/{KEY}", json={"value": REPLACED})).status_code == 200
        cleared = await client.post(f"/admin/settings/reset/{KEY}")
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["secret"]["set"] is False and cleared.json()["value"] is None

    details = await _details(db, platform_admin.tenant_id)
    assert sorted(d["action"] for d in details) == ["clear", "replace", "set"]
    assert all(set(d) == {"setting", "action"} for d in details)
    assert not any(VALUE in str(d) or REPLACED in str(d) for d in details)
    stored = (await db.execute(text("SELECT count(*) FROM secrets WHERE name = :n"), {"n": KEY})).scalar()
    assert stored == 0


@pytest.mark.asyncio
async def test_every_process_is_told_only_after_the_commit(db, store, platform_admin, monkeypatch):  # noqa: F811
    """The version bump comes after the row and its audit commit (the review
    after K7, the class Codex found in K7's routes). Told first, a backend
    process reading in between takes the new version and the old row, and
    serves that row under the new version for the cache's 30 seconds. So
    the session's commit and Redis's INCR are recorded in the order they
    happen, for a set, a replace and a clear, and the value is read back
    through the cache at each step."""
    from app.services import app_settings_service

    order: list[str] = []
    real_commit, real_incr = db.commit, store.redis.incr

    async def commit():
        order.append("commit")
        await real_commit()

    async def incr(key):
        order.append("incr")
        return await real_incr(key)

    monkeypatch.setattr(db, "commit", commit)
    monkeypatch.setattr(store.redis, "incr", incr)
    async with _client(db, platform_admin) as client:
        for value in (VALUE, REPLACED):
            order.clear()
            put = await client.put(f"/admin/settings/{KEY}", json={"value": value})
            assert put.status_code == 200, put.text
            assert "incr" in order and "commit" in order[: order.index("incr")], order
            assert await app_settings_service.get_secret_setting(db, KEY) == value
        order.clear()
        cleared = await client.post(f"/admin/settings/reset/{KEY}")
        assert cleared.status_code == 200, cleared.text
        assert "incr" in order and "commit" in order[: order.index("incr")], order
        assert await app_settings_service.get_secret_setting(db, KEY) == ""


@pytest.mark.asyncio
async def test_the_reset_answer_is_read_before_the_commit(db, store, platform_admin, monkeypatch):  # noqa: F811
    """Codex on #172: read after the commit, a failed read of the answer
    would be a 500 for a clear already durable. The state is read inside
    the transaction — where the row is already gone — then the commit, then
    the notice; and a failing read leaves nothing committed."""
    from app.services import app_settings_service

    order: list[str] = []
    real_commit, real_incr = db.commit, store.redis.incr
    real_state = app_settings_service.get_secret_state

    async def commit():
        order.append("commit")
        await real_commit()

    async def incr(key):
        order.append("incr")
        return await real_incr(key)

    async def state(db_, key):
        order.append("state")
        return await real_state(db_, key)

    monkeypatch.setattr(db, "commit", commit)
    monkeypatch.setattr(store.redis, "incr", incr)
    monkeypatch.setattr(app_settings_service, "get_secret_state", state)
    async with _client(db, platform_admin) as client:
        assert (await client.put(f"/admin/settings/{KEY}", json={"value": VALUE})).status_code == 200
        order.clear()
        cleared = await client.post(f"/admin/settings/reset/{KEY}")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["secret"]["set"] is False
    assert order == ["state", "commit", "incr"], order


@pytest.mark.asyncio
async def test_a_row_under_a_lost_key_is_reported(db, store, platform_admin):  # noqa: F811
    async with _client(db, platform_admin) as client:
        assert (await client.put(f"/admin/settings/{KEY}", json={"value": VALUE})).status_code == 200
        store.use_key(fernet_key())  # the key that sealed it is gone

        lost = _secret_row((await client.get("/admin/settings")).json())
        assert lost["secret"]["source"] == "unreadable", lost
        assert lost["secret"]["set"] is True and lost["secret"]["fingerprint"] is None
        assert lost["is_default"] is False

        # Replace seals it under the current key: runtime again.
        replaced = await client.put(f"/admin/settings/{KEY}", json={"value": REPLACED})
        assert replaced.status_code == 200
        assert replaced.json()["secret"]["source"] == "runtime"
        again = _secret_row((await client.get("/admin/settings")).json())
        assert again["secret"]["fingerprint"] == keyring.fingerprint(
            secrets_service.configured_keys()[0], REPLACED
        )


@pytest.mark.asyncio
async def test_no_log_line_has_the_value(db, store, platform_admin, caplog):  # noqa: F811
    from app.services import app_settings_service

    caplog.set_level(logging.DEBUG)
    async with _client(db, platform_admin) as client:
        await client.put(f"/admin/settings/{KEY}", json={"value": VALUE})
        await client.put(f"/admin/settings/{KEY}", json={"value": REPLACED})
        await client.get("/admin/settings")
        await client.put(f"/admin/settings/{KEY}", json={"value": "   "})
        assert await app_settings_service.get_secret_setting(db, KEY) == REPLACED
        store.use_key(fernet_key())
        assert await app_settings_service.get_secret_setting(db, KEY) == ""
        await client.post(f"/admin/settings/reset/{KEY}")

    # The unreadable path ran and logged — without the value.
    assert "secret_unreadable" in caplog.text
    for record in caplog.records:
        rendered = record.getMessage() + repr(record.args) + repr(getattr(record, "__dict__", {}))
        assert VALUE not in rendered and REPLACED not in rendered, record.getMessage()


@pytest.mark.asyncio
async def test_a_refused_value_names_the_setting_and_never_the_value(db, store, platform_admin):  # noqa: F811
    async with _client(db, platform_admin) as client:
        for value, reason in (
            ("   ", "blank"),
            ("x" * 4097 + VALUE, "longer than 4096"),
            (12345, "takes a string"),
        ):
            answer = await client.put(f"/admin/settings/{KEY}", json={"value": value})
            assert answer.status_code == 400, answer.text
            assert KEY in answer.json()["detail"] and reason in answer.json()["detail"]
            assert VALUE not in answer.text
    assert (await db.execute(text("SELECT count(*) FROM secrets"))).scalar() == 0


@pytest.mark.asyncio
async def test_blank_key_answers_503(db, store, platform_admin):  # noqa: F811
    """On the REAL ``app.main.app``: the 503 is its exception handler, so a
    harness that re-registered the routes would test a wiring nobody
    ships."""
    from app.main import app as chassis_app

    store.use_key("")

    async def _db_dep():
        yield db

    async def _user_dep():
        return platform_admin

    chassis_app.dependency_overrides[get_db] = _db_dep
    chassis_app.dependency_overrides[get_current_user] = _user_dep
    try:
        async with AsyncClient(
            transport=ASGITransport(app=chassis_app), base_url="http://testserver"
        ) as client:
            answer = await client.put(f"/api/v1/admin/settings/{KEY}", json={"value": VALUE})
    finally:
        chassis_app.dependency_overrides.pop(get_db, None)
        chassis_app.dependency_overrides.pop(get_current_user, None)

    assert answer.status_code == 503, answer.text
    assert answer.json() == {
        "detail": secrets_service.SecretsStoreUnconfigured.detail,
        "code": "secrets_store_unconfigured",
    }
    assert VALUE not in answer.text
