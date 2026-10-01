"""Gate T — tenant isolation of agent values (the K blueprint's §3.4, K5a).

One agent's settings, valued by two tenants, must never cross: not in the
admin API (``GET /agents/{id}/config``), not in the façade an in-process
run is handed (``caps.config.settings()``), not in the MCP tool a
container asks (``config_get``). The demo agent's overlay is the defect
this exists to keep from coming back (§1.1, T9): keyed by the agent
alone, one tenant's edit there is every tenant's value.

Since K8a the same holds for an agent's tool secrets: the probe declares
``probe_token``, and each tenant's run reads its own value through the
façade (``caps.secrets.get``) and over MCP (``secret_get``). Their
isolation is K6's store's — its row lookup and its in-process cache key
both name the tenant — and the two doubles at the bottom drop each in
turn and watch the leak.

Real PostgreSQL and Redis. One probe agent, two tenants, two values. The
three surfaces read through ``app.database.async_session`` (the façade and
the MCP tool) or ``get_db`` (the admin API); both yield this test's
session, so every read sees the rows the test wrote, inside a transaction
that is rolled back afterwards.

The negative double is in every run, not only in a record: with the
tenant predicate dropped from ``values_for``, tenant A holding a row and
tenant B none, all three of B's reads return A's value, whatever the row
order. So the three reads are only as isolated as ``values_for``'s
predicate, and the three tests above it are what hold it.
"""
from __future__ import annotations

import contextlib
import json
import os
import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app import capabilities as caps_mod
from app.agents import registry
from app.agents.manifest import AgentManifest
from app.agents.protocol import AgentProtocol
from app.database import get_db
from app.middleware import get_current_user
from app.models import AgentSetting, User
from app.routers import agents as agents_router
from app.routers import mcp as mcp_router
from app.services import agent_settings_service as svc
from tests.test_secrets_service import store  # noqa: F401  (fixture)

REQUIRE_DB = os.environ.get("LIBRERUN_REQUIRE_DB", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

AGENT = "gate-t-probe"
DEFAULT = "the default"


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


class _Probe(AgentProtocol):
    agent_id = AGENT
    display_name = "Gate T probe"
    description = "one setting, valued per tenant"

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}


MANIFEST = AgentManifest.model_validate(
    {
        "id": AGENT,
        "name": "Gate T probe",
        "runtime": "container",
        "container": {"url": "http://gate-t:8090"},
        "input_schema": "input_schema.json",
        "phases": [{"name": "work"}],
        "output": {"mode": "structured"},
        "settings": [{"key": "note", "type": "string", "default": DEFAULT}],
        "secrets": ["probe_token"],
    }
)


@pytest.fixture(autouse=True)
def _probe_agent():
    registry._clear_registry_for_tests()
    registry.register(_Probe(), MANIFEST)
    yield
    registry._clear_registry_for_tests()


@pytest.fixture(autouse=True)
def _sessions_are_the_tests(db, monkeypatch):
    """Every ``async_session()`` the façade opens yields this test's
    session, so the reads under test see its rows."""
    import app.database

    @contextlib.asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(app.database, "async_session", _session)


async def admin_of_a_new_tenant(db) -> User:
    tenant_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, :n, :s)"),
        {"id": tenant_id, "n": "Gate T", "s": f"gate-t-{tenant_id.hex[:12]}"},
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


async def set_note(db, admin: User, value: str) -> None:
    changed = await svc.apply_updates(
        db, admin.tenant_id, AGENT, MANIFEST, [{"key": "note", "value": value}], admin.id
    )
    assert changed == ["note"]


# -- the three surfaces ------------------------------------------------------


async def read_admin_api(db, admin: User):
    app = FastAPI()
    app.include_router(agents_router.router)

    async def _db_dep():
        yield db

    async def _user_dep():
        return admin

    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        response = await http.get(f"/agents/{AGENT}/config")
    assert response.status_code == 200, response.text
    (entry,) = response.json()["settings"]
    return entry["value"]


async def read_facade(admin: User):
    caps = caps_mod.for_run(
        run_id=uuid.uuid4(), tenant_id=admin.tenant_id, agent_id=AGENT, grants=[]
    )
    return (await caps.config.settings())["note"]


async def read_config_get(admin: User):
    """A container's read: a run token minted for this tenant's run, then
    ``config_get`` on the chassis's own MCP endpoint."""
    import redis.asyncio as aioredis

    from app.config import settings

    token = f"gate-t-{uuid.uuid4().hex}"
    record = {
        "run_id": str(uuid.uuid4()),
        "tenant_id": str(admin.tenant_id),
        "agent_id": AGENT,
        "grants": [],
        "state": "active",
    }
    async with aioredis.from_url(
        settings.REDIS_URL.get_secret_value(), decode_responses=True
    ) as redis:
        await redis.set(f"run_token:{token}", json.dumps(record), ex=300)
    try:
        app = FastAPI()
        app.include_router(mcp_router.router, prefix="/api/v1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            response = await http.post(
                "/api/v1/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "config_get", "arguments": {}}},
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        async with aioredis.from_url(
            settings.REDIS_URL.get_secret_value(), decode_responses=True
        ) as redis:
            await redis.delete(f"run_token:{token}")
    body = response.json()
    assert "error" not in body, body
    payload = json.loads(body["result"]["content"][0]["text"])
    (entry,) = payload["settings"]
    assert entry["key"] == "note"
    return entry["value"]


# -- the gate ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_tenant_reads_its_own_value(db):
    """Two tenants edit the same agent setting; each reads its own."""
    a = await admin_of_a_new_tenant(db)
    b = await admin_of_a_new_tenant(db)
    await set_note(db, a, "tenant A's")
    await set_note(db, b, "tenant B's")

    assert await read_admin_api(db, a) == "tenant A's"
    assert await read_admin_api(db, b) == "tenant B's"


@pytest.mark.asyncio
async def test_the_facade_serves_the_runs_tenant(db):
    a = await admin_of_a_new_tenant(db)
    b = await admin_of_a_new_tenant(db)
    c = await admin_of_a_new_tenant(db)
    await set_note(db, a, "tenant A's")
    await set_note(db, b, "tenant B's")

    assert await read_facade(a) == "tenant A's"
    assert await read_facade(b) == "tenant B's"
    # A third tenant that never chose reads the manifest's default.
    assert await read_facade(c) == DEFAULT


@pytest.mark.asyncio
async def test_config_get_serves_the_runs_tenant(db):
    a = await admin_of_a_new_tenant(db)
    b = await admin_of_a_new_tenant(db)
    await set_note(db, a, "tenant A's")
    await set_note(db, b, "tenant B's")

    assert await read_config_get(a) == "tenant A's"
    assert await read_config_get(b) == "tenant B's"


@pytest.mark.asyncio
async def test_gate_t_goes_red_without_the_tenant_predicate(db, monkeypatch):
    """The negative double: ``values_for`` with the tenant predicate
    dropped. Tenant A holds a row and tenant B none, so without the
    predicate B's reads can only return A's value, whatever the row order
    — and all three surfaces do, which is what proves the three tests
    above are holding the predicate rather than passing around it."""
    a = await admin_of_a_new_tenant(db)
    b = await admin_of_a_new_tenant(db)
    await set_note(db, a, "tenant A's")

    # With the real service, B reads the default everywhere.
    assert await read_admin_api(db, b) == DEFAULT
    assert await read_facade(b) == DEFAULT
    assert await read_config_get(b) == DEFAULT

    async def _values_for_without_the_tenant(db, tenant_id, agent_id):
        rows = await db.execute(
            select(AgentSetting.key, AgentSetting.value).where(
                AgentSetting.agent_id == agent_id
            )
        )
        return {key: value for key, value in rows.all()}

    monkeypatch.setattr(svc, "values_for", _values_for_without_the_tenant)

    leaked = {
        "admin API": await read_admin_api(db, b),
        "façade": await read_facade(b),
        "config_get": await read_config_get(b),
    }
    assert leaked == {
        "admin API": "tenant A's",
        "façade": "tenant A's",
        "config_get": "tenant A's",
    }, "a surface did not read through values_for, so Gate T does not hold it"


# -- tool secrets (K8a) ------------------------------------------------------------


async def set_token(db, admin: User, value: str) -> None:
    from app.services import secrets_service
    from app.services import tool_secrets_service as tool_secrets

    await tool_secrets.set_value(
        db, MANIFEST, "tenant", admin.tenant_id, "probe_token", value, user_id=None
    )
    await secrets_service.notify_change(
        tool_secrets.owner("tenant", admin.tenant_id, AGENT), "probe_token"
    )


async def read_token_facade(tenant_id):
    """An in-process read — a string, or the refusal's code."""
    caps = caps_mod.for_run(run_id=uuid.uuid4(), tenant_id=tenant_id, agent_id=AGENT, grants=[])
    try:
        return await caps.secrets.get("probe_token")
    except caps_mod.SecretNotSet as exc:
        return exc.code


async def read_token_mcp(tenant_id):
    """A container's read over ``secret_get`` — a string, or the code."""
    import redis.asyncio as aioredis

    from app.config import settings

    token = f"gate-t-{uuid.uuid4().hex}"
    record = {"run_id": str(uuid.uuid4()), "tenant_id": str(tenant_id), "agent_id": AGENT,
              "grants": [], "state": "active"}
    url = settings.REDIS_URL.get_secret_value()
    async with aioredis.from_url(url, decode_responses=True) as redis:
        await redis.set(f"run_token:{token}", json.dumps(record), ex=300)
    try:
        app = FastAPI()
        app.include_router(mcp_router.router, prefix="/api/v1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            response = await http.post(
                "/api/v1/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "secret_get", "arguments": {"name": "probe_token"}}},
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        async with aioredis.from_url(url, decode_responses=True) as redis:
            await redis.delete(f"run_token:{token}")
    body = response.json()
    if "error" in body:
        return {-32006: "secret_not_set", -32005: "secret_not_declared"}[body["error"]["code"]]
    return json.loads(body["result"]["content"][0]["text"])["value"]


@pytest.fixture
def secrets_store(store, monkeypatch):  # noqa: F811
    """K6's store with a key and its dict counter; a read's stamp is not
    what this gate holds, so it records nothing here."""
    from app.services import tool_secrets_service as tool_secrets

    async def _stamp(row, name):
        return None

    monkeypatch.setattr(tool_secrets, "stamp", _stamp)
    return store


@pytest.mark.asyncio
async def test_each_tenants_run_reads_its_own_tool_secret(db, secrets_store):
    a = await admin_of_a_new_tenant(db)
    b = await admin_of_a_new_tenant(db)
    a_id, b_id = a.tenant_id, b.tenant_id
    await set_token(db, a, "tenant-a-token-value")
    await set_token(db, b, "tenant-b-token-value")

    assert await read_token_facade(a_id) == "tenant-a-token-value"
    assert await read_token_facade(b_id) == "tenant-b-token-value"
    assert await read_token_mcp(a_id) == "tenant-a-token-value"
    assert await read_token_mcp(b_id) == "tenant-b-token-value"


@pytest.mark.asyncio
async def test_gate_t_goes_red_without_the_secrets_tenant_filter(db, secrets_store, monkeypatch):
    """The negative double for the row lookup: K6's owner predicate with
    the tenant dropped. Tenant A holds a row and tenant B none, so without
    it B's reads can only find A's — and both surfaces do."""
    from sqlalchemy import and_

    from app.models import Secret
    from app.services import secrets_service

    a = await admin_of_a_new_tenant(db)
    b = await admin_of_a_new_tenant(db)
    a_id, b_id = a.tenant_id, b.tenant_id
    await set_token(db, a, "tenant-a-token-value")

    assert await read_token_facade(b_id) == "secret_not_set"
    assert await read_token_mcp(b_id) == "secret_not_set"

    def _where_without_the_tenant(owner, name=None):
        clauses = [Secret.scope == owner.scope]
        if owner.agent_id is not None:
            clauses.append(Secret.agent_id == owner.agent_id)
        if name is not None:
            clauses.append(Secret.name == name)
        return and_(*clauses)

    monkeypatch.setattr(secrets_service, "_where", _where_without_the_tenant)
    secrets_service.clear_cache()

    leaked = {"façade": await read_token_facade(b_id), "secret_get": await read_token_mcp(b_id)}
    assert leaked == {"façade": "tenant-a-token-value", "secret_get": "tenant-a-token-value"}, (
        "a surface did not read through the store's owner predicate, so Gate T does not hold it"
    )


@pytest.mark.asyncio
async def test_gate_t_goes_red_when_the_secret_cache_drops_the_tenant(db, secrets_store, monkeypatch):
    """The negative double for the in-process cache: its key with the
    tenant dropped. Tenant A reads its value first, and B's reads — B has
    no row — are then answered from A's entry, on both surfaces."""
    from app.services import secrets_service

    a = await admin_of_a_new_tenant(db)
    b = await admin_of_a_new_tenant(db)
    a_id, b_id = a.tenant_id, b.tenant_id
    await set_token(db, a, "tenant-a-token-value")

    monkeypatch.setattr(
        secrets_service, "_cache_key", lambda owner, name: (owner.scope, owner.agent_id, name)
    )
    secrets_service.clear_cache()
    assert await read_token_facade(a_id) == "tenant-a-token-value"

    leaked = {"façade": await read_token_facade(b_id), "secret_get": await read_token_mcp(b_id)}
    assert leaked == {"façade": "tenant-a-token-value", "secret_get": "tenant-a-token-value"}, (
        "a surface did not read through the store's cache key, so Gate T does not hold it"
    )
