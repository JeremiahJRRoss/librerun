"""An agent's tool secrets through the admin API (K8a; K8-08, D32, L31).

``GET /agents/{id}/secrets`` describes every name the manifest declares —
this tenant's row, every tenant's default and, for an in-process agent,
the environment's fallback — and never a value. ``PUT`` and ``DELETE
…/secrets/{scope}/{name}`` set and clear a row: a tenant admin this
tenant's (``tenant``), a platform admin every tenant's default (``agent``).
The body is read by hand, so no refusal echoes it; each write is audited
without the value, and every process is told of it only once the row and
its audit have committed.

Real PostgreSQL (K6's fixtures, K6's platform admin), the real router
behind ``httpx.ASGITransport``, K6's dict ``FakeRedis``. The list's
comprehension is evaluated with a declared name, which is what the
element-coverage gate asks of it.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app import secrets_keyring as keyring
from app.agents import registry
from app.agents.manifest import AgentManifest
from app.agents.protocol import AgentProtocol
from app.database import get_db
from app.middleware import get_current_user
from app.models import User
from app.routers import agents as agents_router
from app.services import secrets_service
from app.services import tool_secrets_service as tool_secrets
from tests.test_secret_setting_api import platform_admin  # noqa: F401  (fixture)
from tests.test_secrets_service import db, fernet_key, store  # noqa: F401  (fixtures)

AGENT = "tool-secrets-api-probe"
NAME = "search_key"
VALUE = "the-tenants-search-value-4e02"
DEFAULT = "every-tenants-default-value-b71c"


class _Probe(AgentProtocol):
    agent_id = AGENT
    display_name = "probe"
    description = "d"

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}


MANIFEST = AgentManifest.model_validate(
    {
        "id": AGENT,
        "name": "probe",
        "runtime": "python-package",
        "phases": [{"name": "work"}],
        "output": {"mode": "structured"},
        "secrets": [NAME, "vector_key"],
    }
)


@pytest.fixture(autouse=True)
def _probe_and_sessions(db, monkeypatch):  # noqa: F811
    """The probe registered; each ``async_session()`` a session of its own
    on the test's connection, as the app opens one."""
    import app.database
    from sqlalchemy.ext.asyncio import AsyncSession

    @contextlib.asynccontextmanager
    async def _session():
        session = AsyncSession(
            bind=db.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr(app.database, "async_session", _session)
    monkeypatch.delenv(NAME.upper(), raising=False)
    monkeypatch.delenv("VECTOR_KEY", raising=False)
    registry._clear_registry_for_tests()
    registry.register(_Probe(), MANIFEST)
    yield
    registry._clear_registry_for_tests()


@pytest_asyncio.fixture
async def tenant_admin(db, platform_admin) -> User:  # noqa: F811
    """An admin of an ordinary tenant (the platform admin's tenant is the
    platform, K4b)."""
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
    return await db.get(User, user_id)


def _client(db, user: User) -> AsyncClient:  # noqa: F811
    app = FastAPI()
    app.include_router(agents_router.router)

    async def _db_dep():
        yield db

    async def _user_dep():
        return user

    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _url(scope: str = "tenant", name: str = NAME, agent: str = AGENT) -> str:
    return f"/agents/{agent}/secrets/{scope}/{name}"


async def _audits(db, tenant_id) -> list[dict]:  # noqa: F811
    rows = (
        await db.execute(
            text(
                "SELECT detail FROM activity_audit_log"
                " WHERE tenant_id = :t AND action_type = 'config_change' ORDER BY created_at"
            ),
            {"t": tenant_id},
        )
    ).scalars().all()
    return [row for row in rows if row.get("surface") == "agent_secrets"]


@pytest.mark.asyncio
async def test_the_list_describes_every_declared_name_and_never_a_value(
    db, store, platform_admin, tenant_admin, monkeypatch  # noqa: F811
):
    monkeypatch.setenv("VECTOR_KEY", "the-environments-vector-value")
    async with _client(db, platform_admin) as client:
        assert (await client.put(_url("agent"), json={"value": DEFAULT})).status_code == 200
    async with _client(db, tenant_admin) as client:
        assert (await client.put(_url(), json={"value": VALUE})).status_code == 200
        listed = await client.get(f"/agents/{AGENT}/secrets")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert (body["agent_id"], body["runtime"]) == (AGENT, "python-package")
    search, vector = body["secrets"]
    assert search["name"] == NAME and search["effective"] == "tenant"
    assert search["tenant"]["set"] and search["tenant"]["fingerprint"] == keyring.fingerprint(
        store.key, VALUE
    )
    assert search["tenant"]["updated_by"] == str(tenant_admin.id)
    assert search["agent"]["set"] and search["agent"]["fingerprint"] == keyring.fingerprint(
        store.key, DEFAULT
    )
    # Who set every tenant's default is the platform admin's to see.
    assert search["agent"]["updated_by"] is None
    assert search["environment"] == {
        "set": False, "fingerprint": None, "updated_at": None, "updated_by": None,
        "last_used_at": None,
    }
    assert vector["effective"] == "environment" and vector["environment"]["set"] is True
    assert VALUE not in listed.text and DEFAULT not in listed.text
    assert "the-environments-vector-value" not in listed.text

    async with _client(db, platform_admin) as client:
        mine = (await client.get(f"/agents/{AGENT}/secrets")).json()
    assert mine["secrets"][0]["agent"]["updated_by"] == str(platform_admin.id)


@pytest.mark.asyncio
async def test_put_answers_the_names_state(db, store, tenant_admin):  # noqa: F811
    async with _client(db, tenant_admin) as client:
        put = await client.put(_url(), json={"value": f"  {VALUE}\n"})
    assert put.status_code == 200, put.text
    state = put.json()
    assert state["name"] == NAME and state["effective"] == "tenant"
    assert state["tenant"]["fingerprint"] == keyring.fingerprint(store.key, VALUE)  # stripped
    assert VALUE not in put.text


@pytest.mark.asyncio
async def test_a_tenant_admin_may_not_set_every_tenants_default(db, store, tenant_admin):  # noqa: F811
    async with _client(db, tenant_admin) as client:
        for answer in (
            await client.put(_url("agent"), json={"value": DEFAULT}),
            await client.delete(_url("agent")),
        ):
            assert answer.status_code == 403, answer.text
            assert answer.json()["code"] == "platform_admin_only"
    stored = (await db.execute(text("SELECT count(*) FROM secrets WHERE agent_id = :a"), {"a": AGENT})).scalar()
    assert stored == 0


@pytest.mark.asyncio
async def test_the_refusals_name_the_problem_and_never_the_value(db, store, tenant_admin):  # noqa: F811
    async with _client(db, tenant_admin) as client:
        unknown = await client.put(_url(agent="no-such-agent"), json={"value": VALUE})
        assert unknown.status_code == 404
        undeclared = await client.put(_url(name="other_key"), json={"value": VALUE})
        assert (undeclared.status_code, undeclared.json()["code"]) == (404, "secret_not_declared")
        scope = await client.put(_url("platform"), json={"value": VALUE})
        assert (scope.status_code, scope.json()["code"]) == (422, "secret_scope_invalid")
        for body in (
            b"not json at all " + VALUE.encode(),
            b'"' + VALUE.encode() + b'"',
            b"{}",
            b'{"value": "' + VALUE.encode() + b'", "extra": 1}',
            b'{"value": "short"}',
            b'{"value": 12345678}',
        ):
            answer = await client.put(
                _url(), content=body, headers={"Content-Type": "application/json"}
            )
            assert answer.status_code == 400, (body, answer.text)
            assert answer.json()["code"] == "secret_value_invalid"
            assert VALUE not in answer.text and "short" not in answer.text
    stored = (await db.execute(text("SELECT count(*) FROM secrets WHERE agent_id = :a"), {"a": AGENT})).scalar()
    assert stored == 0


@pytest.mark.asyncio
async def test_put_and_delete_are_audited_without_the_value(db, store, tenant_admin):  # noqa: F811
    # Each request is held to the one action it adds: every row shares the
    # test transaction's now(), so ORDER BY created_at leaves their order to
    # the planner (CI once read them reversed).
    async def actions() -> list[str]:
        return sorted(d["action"] for d in await _audits(db, tenant_admin.tenant_id))

    async with _client(db, tenant_admin) as client:
        for call, body, code, action in (
            (client.put, {"json": {"value": VALUE}}, 200, "set"),
            (client.put, {"json": {"value": VALUE + "-2"}}, 200, "replace"),
            (client.delete, {}, 204, "clear"),
            (client.delete, {}, 204, "clear"),  # idempotent
        ):
            before = await actions()
            assert (await call(_url(), **body)).status_code == code
            assert await actions() == sorted([*before, action]), action
    details = await _audits(db, tenant_admin.tenant_id)
    assert len(details) == 4
    assert all(
        d == {"surface": "agent_secrets", "agent_id": AGENT, "name": NAME, "scope": "tenant",
              "action": d["action"]}
        for d in details
    )
    assert not any(VALUE in str(d) for d in details)
    left = (await db.execute(text("SELECT count(*) FROM secrets WHERE agent_id = :a"), {"a": AGENT})).scalar()
    assert left == 0


@pytest.mark.asyncio
async def test_a_write_is_announced_only_after_the_commit(db, store, tenant_admin, monkeypatch):  # noqa: F811
    """The version bump comes after the row and its audit commit (the
    review after K7): told first, a process reading in between would keep
    the old value for the cache's 30 seconds. The session's commit and
    Redis's INCR are recorded in the order they happen, for a set and a
    clear."""
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
    async with _client(db, tenant_admin) as client:
        assert (await client.put(_url(), json={"value": VALUE})).status_code == 200
        assert "incr" in order and "commit" in order[: order.index("incr")], order
        order.clear()
        assert (await client.delete(_url())).status_code == 204
        assert "incr" in order and "commit" in order[: order.index("incr")], order


@pytest.mark.asyncio
async def test_the_config_surface_counts_a_declared_secret(db, store, tenant_admin):  # noqa: F811
    """An agent that declares only tool secrets has a page to link to, and
    the config GET says what they are (the Secrets tab reads the rest)."""
    async with _client(db, tenant_admin) as client:
        config = await client.get(f"/agents/{AGENT}/config")
        listing = await client.get("/agents")
    assert config.status_code == 200, config.text
    assert config.json()["meta"]["secrets"] == [NAME, "vector_key"]
    (row,) = [a for a in listing.json() if a["agent_id"] == AGENT]
    assert row["has_config"] is True


@pytest.mark.asyncio
async def test_a_blank_store_key_answers_503_on_the_real_app(db, store, tenant_admin):  # noqa: F811
    """K6's refusal, from the app-level handler every writer shares."""
    from app.main import app as chassis_app

    store.use_key("")

    async def _db_dep():
        yield db

    async def _user_dep():
        return tenant_admin

    chassis_app.dependency_overrides[get_db] = _db_dep
    chassis_app.dependency_overrides[get_current_user] = _user_dep
    try:
        async with AsyncClient(
            transport=ASGITransport(app=chassis_app), base_url="http://testserver"
        ) as client:
            answer = await client.put(f"/api/v1{_url()}", json={"value": VALUE})
    finally:
        chassis_app.dependency_overrides.pop(get_db, None)
        chassis_app.dependency_overrides.pop(get_current_user, None)
    assert answer.status_code == 503, answer.text
    assert answer.json()["code"] == "secrets_store_unconfigured"
    assert VALUE not in answer.text
