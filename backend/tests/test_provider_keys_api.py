"""Provider keys from the admin UI: the backend's half (K7; L33, D16, K7-03,
K7-07, K7-16).

On the REAL ``app.main.app`` — the 503 is its exception handler — with real
PostgreSQL in a rolled-back transaction and a dict ``FakeRedis``. The
backend stores a blob it cannot open and relays what the gateway reports;
these hold that:

* the list reads the gateway's row, marks ``pending`` a row the gateway has
  not processed, and serves the public key — never a key
  (``test_list_reads_a_reported_provider`` is also the element-coverage
  test for the list's comprehension);
* anything but one 3072-bit block answers ``400 plaintext_refused`` and
  writes nothing — a raw key above all;
* with no public key the POST answers ``503 secrets_store_unconfigured``;
* a set, a replace and a clear are audited ``{surface, provider, action}``,
  and the blob is in no audit detail and no response.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.database import get_db
from app.middleware import get_current_user
from app.models import User
from app.services import provider_keys_service
from tests.test_secret_setting_api import platform_admin  # noqa: F401  (fixture)
from tests.test_secrets_service import FakeRedis, db, fernet_key, store  # noqa: F401  (fixtures)

# Any SubjectPublicKeyInfo PEM will do: the backend only relays it.
PEM = "-----BEGIN PUBLIC KEY-----\nMIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEA\n-----END PUBLIC KEY-----\n"
RAW_KEY = "sk-proj-a-raw-openai-key-never-to-be-stored-0001"
SET_AT = datetime(2026, 9, 29, 1, 2, 3, 456789, tzinfo=timezone.utc)


@pytest.fixture
def versions(monkeypatch) -> FakeRedis:
    redis = FakeRedis()

    async def _redis():
        return redis

    monkeypatch.setattr(provider_keys_service, "get_redis", _redis)
    return redis


async def _report(db, *, pem: str | None = PEM, entries: list[dict] | None = None) -> None:  # noqa: F811
    await db.execute(
        text(
            "INSERT INTO gateway_status (id, version, stub, providers, public_key_pem) "
            "VALUES (1, '1.0.0', false, CAST(:p AS JSONB), :pem) ON CONFLICT (id) DO UPDATE "
            "SET version = EXCLUDED.version, stub = EXCLUDED.stub, providers = EXCLUDED.providers, "
            "public_key_pem = EXCLUDED.public_key_pem"
        ),
        {"p": json.dumps(entries or []), "pem": pem},
    )


async def _unreported(db) -> None:  # noqa: F811
    await db.execute(text("DELETE FROM gateway_status"))


async def _row(db, name: str):  # noqa: F811
    return (
        await db.execute(
            text(
                "SELECT ciphertext, key_id, fingerprint, updated_by FROM secrets "
                "WHERE scope = 'gateway' AND name = :n"
            ),
            {"n": f"provider.{name}"},
        )
    ).first()


async def _details(db, tenant_id) -> list[dict]:  # noqa: F811
    rows = (
        await db.execute(
            text(
                "SELECT detail FROM activity_audit_log WHERE tenant_id = :t "
                "AND action_type = 'config_change'"
            ),
            {"t": tenant_id},
        )
    ).scalars().all()
    return [row for row in rows if row.get("surface") == "provider_keys"]


def _client(db, user: User) -> AsyncClient:  # noqa: F811
    from app.main import app as chassis_app

    async def _db_dep():
        yield db

    async def _user_dep():
        return user

    chassis_app.dependency_overrides[get_db] = _db_dep
    chassis_app.dependency_overrides[get_current_user] = _user_dep
    return AsyncClient(transport=ASGITransport(app=chassis_app), base_url="http://testserver")


@pytest.fixture(autouse=True)
def _release_the_app():
    yield
    from app.main import app as chassis_app

    chassis_app.dependency_overrides.pop(get_db, None)
    chassis_app.dependency_overrides.pop(get_current_user, None)


def _blob() -> tuple[bytes, str]:
    blob = os.urandom(provider_keys_service.BLOB_BYTES)
    return blob, base64.b64encode(blob).decode("ascii")


@pytest.mark.asyncio
async def test_list_reads_a_reported_provider(db, store, versions, platform_admin):  # noqa: F811
    """The gateway's row, as the page needs it: anthropic's stored key in
    effect (the gateway saw the row as it stands), openai from gateway.env,
    and google pending — a blob the gateway has not reported on yet."""
    await db.execute(
        text(
            "INSERT INTO secrets (scope, name, ciphertext, key_id, fingerprint, updated_at) "
            "VALUES ('gateway', 'provider.anthropic', :c, 'a1b2c3d4e5f60718', 'f00dfacecafe', :at)"
        ),
        {"c": b"gAAAAA-a-fernet-token-the-backend-cannot-open", "at": SET_AT},
    )
    blob, _ = _blob()
    await db.execute(
        text(
            "INSERT INTO secrets (scope, name, ciphertext, key_id, fingerprint) "
            "VALUES ('gateway', 'provider.google', :c, 'sealed', :f)"
        ),
        {"c": blob, "f": hashlib.sha256(blob).hexdigest()[:12]},
    )
    set_by = str(platform_admin.id)
    await _report(db, entries=[
        {"name": "openai", "aliases": ["openai", "azure"], "source": "env", "fingerprint": None,
         "set_by": None, "set_at": None, "row": None, "reason": None},
        {"name": "anthropic", "aliases": ["anthropic"], "source": "runtime",
         "fingerprint": "f00dfacecafe", "set_by": set_by, "set_at": SET_AT.isoformat(),
         "row": "runtime", "reason": None},
        {"name": "google", "aliases": ["gemini", "google", "vertex_ai"], "source": "unset",
         "fingerprint": None, "set_by": None, "set_at": None, "row": None, "reason": None},
    ])

    async with _client(db, platform_admin) as client:
        answer = await client.get("/api/v1/admin/providers")

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["reported"], body["stub"], body["gateway_version"]) == (True, False, "1.0.0")
    assert body["public_key_pem"] == PEM
    by_name = {entry["name"]: entry for entry in body["providers"]}
    assert list(by_name) == ["openai", "anthropic", "google"]
    assert (by_name["openai"]["source"], by_name["openai"]["row"]) == ("env", None)
    anthropic = by_name["anthropic"]
    assert (anthropic["source"], anthropic["row"], anthropic["fingerprint"]) == (
        "runtime", "runtime", "f00dfacecafe"
    )
    assert anthropic["set_by"] == set_by
    assert by_name["google"]["row"] == "pending"
    assert by_name["google"]["aliases"] == ["gemini", "google", "vertex_ai"]
    assert "gAAAAA" not in answer.text


@pytest.mark.asyncio
async def test_an_unreported_gateway_lists_every_name_and_no_key(db, store, versions, platform_admin):  # noqa: F811
    await _unreported(db)
    async with _client(db, platform_admin) as client:
        answer = await client.get("/api/v1/admin/providers")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["reported"], body["public_key_pem"]) == (False, None)
    assert [entry["name"] for entry in body["providers"]] == ["openai", "anthropic", "google"]


@pytest.mark.asyncio
async def test_plaintext_refused(db, store, versions, platform_admin):  # noqa: F811
    """A raw key — and anything else that is not one 3072-bit OAEP block —
    answers 400 plaintext_refused and writes no row (K7-16): a Google-shaped
    key strict-decodes too, so only the length tells a blob from a key."""
    await _report(db)
    google_shaped = "AIzaSyDaGmWKa4JsXZHjGw7ISLn3namBGewQeA12"
    short, long_ = (
        base64.b64encode(os.urandom(provider_keys_service.BLOB_BYTES + delta)).decode()
        for delta in (-1, 1)
    )
    async with _client(db, platform_admin) as client:
        for sealed in (RAW_KEY, google_shaped, short, long_, "", "not base64 at all!"):
            answer = await client.post("/api/v1/admin/providers/openai/key", json={"sealed": sealed})
            assert answer.status_code == 400, (sealed[:12], answer.text)
            assert answer.json()["code"] == "plaintext_refused"
            assert sealed not in answer.text or not sealed
    assert await _row(db, "openai") is None
    assert versions.store == {}, "a refused write bumped the version"


@pytest.mark.asyncio
async def test_no_public_key_answers_503(db, store, versions, platform_admin):  # noqa: F811
    """K7-07: the gateway's store key is blank (a row with no public key),
    or the gateway has never reported: nothing to seal to."""
    _, sealed = _blob()
    async with _client(db, platform_admin) as client:
        for arrange in (lambda: _report(db, pem=None), lambda: _unreported(db)):
            await arrange()
            answer = await client.post("/api/v1/admin/providers/openai/key", json={"sealed": sealed})
            assert answer.status_code == 503, answer.text
            assert answer.json() == {
                "detail": provider_keys_service.GatewayStoreUnconfigured.detail,
                "code": "secrets_store_unconfigured",
            }
    assert await _row(db, "openai") is None


@pytest.mark.asyncio
async def test_unknown_provider_answers_404(db, store, versions, platform_admin):  # noqa: F811
    await _report(db)
    _, sealed = _blob()
    async with _client(db, platform_admin) as client:
        posted = await client.post("/api/v1/admin/providers/bedrock/key", json={"sealed": sealed})
        deleted = await client.delete("/api/v1/admin/providers/bedrock/key")
    for answer in (posted, deleted):
        assert answer.status_code == 404, answer.text
        assert answer.json()["code"] == "unknown_provider"


@pytest.mark.asyncio
async def test_set_and_clear_are_audited_without_the_value(db, store, versions, platform_admin):  # noqa: F811
    """202 {name, row: pending}: the blob is stored as it came, sealed, its
    own id the fingerprint; every change bumps the version the gateway
    watches; the audit says who did what to which provider, and nothing
    of the blob."""
    await _report(db)
    first, first_b64 = _blob()
    second, second_b64 = _blob()
    async with _client(db, platform_admin) as client:
        set_ = await client.post("/api/v1/admin/providers/openai/key", json={"sealed": first_b64})
        assert set_.status_code == 202, set_.text
        assert set_.json() == {"name": "openai", "row": "pending"}
        row = await _row(db, "openai")
        assert (bytes(row.ciphertext), row.key_id) == (first, "sealed")
        assert row.fingerprint == hashlib.sha256(first).hexdigest()[:12]
        assert row.updated_by == platform_admin.id

        listed = await client.get("/api/v1/admin/providers")
        assert {e["name"]: e["row"] for e in listed.json()["providers"]}["openai"] == "pending"

        replaced = await client.post("/api/v1/admin/providers/openai/key", json={"sealed": second_b64})
        assert replaced.status_code == 202, replaced.text
        assert bytes((await _row(db, "openai")).ciphertext) == second

        cleared = await client.delete("/api/v1/admin/providers/openai/key")
        assert cleared.status_code == 204, cleared.text
        assert await _row(db, "openai") is None

    details = await _details(db, platform_admin.tenant_id)
    assert [d["action"] for d in details] == ["set", "replace", "clear"]
    assert all(d == {"surface": "provider_keys", "provider": "openai", "action": d["action"]} for d in details)
    everything = json.dumps(details) + set_.text + replaced.text
    for b64 in (first_b64, second_b64):
        assert b64 not in everything
    assert versions.store.get("secrets:version") == "3"


@pytest.mark.asyncio
async def test_the_gateway_is_told_only_after_the_commit(db, store, versions, platform_admin, monkeypatch):  # noqa: F811
    """The version bump that wakes the gateway comes after the row commits
    (Codex on #170). Told first, a gateway polling in between reads the new
    version and the old row, holds the version as seen, and keeps the old
    key — or a cleared one — until its 30-second full reload. So the
    session's commit and Redis's INCR are recorded in the order they happen,
    for a set and for a clear."""
    await _report(db)
    order: list[str] = []
    real_commit, real_incr = db.commit, versions.incr

    async def commit():
        order.append("commit")
        await real_commit()

    async def incr(key):
        order.append("incr")
        return await real_incr(key)

    monkeypatch.setattr(db, "commit", commit)
    monkeypatch.setattr(versions, "incr", incr)
    _, blob_b64 = _blob()
    async with _client(db, platform_admin) as client:
        posted = await client.post("/api/v1/admin/providers/openai/key", json={"sealed": blob_b64})
        assert posted.status_code == 202, posted.text
        assert "incr" in order and "commit" in order[: order.index("incr")], order
        order.clear()
        cleared = await client.delete("/api/v1/admin/providers/openai/key")
        assert cleared.status_code == 204, cleared.text
        assert "incr" in order and "commit" in order[: order.index("incr")], order


@pytest.mark.asyncio
async def test_a_tenant_admin_may_not_touch_provider_keys(db, store, versions):  # noqa: F811
    """The provider keys are the deployment's: platform admins only."""
    tenant_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'T', :s)"),
        {"id": tenant_id, "s": f"tenant-{tenant_id.hex[:12]}"},
    )
    user_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, auth_provider, role)"
            " VALUES (:id, :t, :e, 'credentials', 'admin')"
        ),
        {"id": user_id, "t": tenant_id, "e": f"{user_id.hex[:10]}@example.com"},
    )
    tenant_admin = await db.get(User, user_id)
    await _report(db)
    _, sealed = _blob()
    async with _client(db, tenant_admin) as client:
        answers = [
            await client.get("/api/v1/admin/providers"),
            await client.post("/api/v1/admin/providers/openai/key", json={"sealed": sealed}),
            await client.delete("/api/v1/admin/providers/openai/key"),
        ]
    assert [a.status_code for a in answers] == [403, 403, 403]
    assert await _row(db, "openai") is None
