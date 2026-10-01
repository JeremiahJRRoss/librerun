"""The MCP ``secret_get`` tool (K8a; D20, K8-05, K8-12).

A container asks for one of its declared tool secrets by name and is
answered from the store's rows alone — this tenant's, else every tenant's
default — never from the backend's environment, which is the chassis's and
not the container's (K8-12). A name the manifest does not declare answers
``-32005 secret_not_declared`` and a declared one with no value ``-32006
secret_not_set``, both ahead of the generic ``-32602``. The writes a
container makes over MCP — ``audit_log``, ``run_store_set`` — are scrubbed
of the run's values.

Real PostgreSQL (K6's fixtures) and the real Redis the token registry and
the run store use; the router is driven in the test's own event loop, as
Gate T drives it, so the façade's session is the test's.
"""
from __future__ import annotations

import contextlib
import json
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.agents import registry
from app.agents.manifest import AgentManifest
from app.agents.protocol import AgentProtocol
from app.routers import mcp as mcp_router
from app.services import secrets_service
from app.services import tool_secrets_service as tool_secrets
from tests.test_secrets_service import db, fernet_key, store  # noqa: F401  (fixtures)

AGENT = "secret-get-probe"
VALUE = "the-containers-search-value-7d3a"
ENV_VALUE = "the-backends-own-environment-value-1c9e"


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
        "runtime": "container",
        "container": {"url": "http://probe:8090"},
        "input_schema": "input_schema.json",
        "phases": [{"name": "work"}],
        "output": {"mode": "structured"},
        "capabilities": ["audit", "run_store"],
        "secrets": ["search_key"],
    }
)


@pytest.fixture(autouse=True)
def _probe_and_sessions(db, monkeypatch):  # noqa: F811
    """Each ``async_session()`` is a session of its own, as in the app, on
    the test's connection and inside its transaction: a savepoint for each
    ``begin`` or commit, all rolled back with the test."""
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
    monkeypatch.delenv("SEARCH_KEY", raising=False)
    registry._clear_registry_for_tests()
    registry.register(_Probe(), MANIFEST)
    tool_secrets.forget_stamps()
    yield
    registry._clear_registry_for_tests()


async def _tenant(db) -> uuid.UUID:  # noqa: F811
    tenant_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'T', :s)"),
        {"id": tenant_id, "s": f"secret-get-{tenant_id.hex[:12]}"},
    )
    return tenant_id


@contextlib.asynccontextmanager
async def _token(tenant_id, grants=()):
    import redis.asyncio as aioredis

    from app.config import settings

    token = f"secret-get-{uuid.uuid4().hex}"
    run_id = str(uuid.uuid4())
    record = {
        "run_id": run_id,
        "tenant_id": str(tenant_id),
        "agent_id": AGENT,
        "grants": list(grants),
        "state": "active",
    }
    url = settings.REDIS_URL.get_secret_value()
    async with aioredis.from_url(url, decode_responses=True) as redis:
        await redis.set(f"run_token:{token}", json.dumps(record), ex=300)
    try:
        yield token, run_id
    finally:
        async with aioredis.from_url(url, decode_responses=True) as redis:
            await redis.delete(f"run_token:{token}", f"run:{run_id}:kv")


async def _call(token: str, tool: str, arguments: dict) -> dict:
    app = FastAPI()
    app.include_router(mcp_router.router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        response = await http.post(
            "/api/v1/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": tool, "arguments": arguments}},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200, response.text
    return response.json()


def _result(body: dict) -> dict:
    assert "error" not in body, body
    return json.loads(body["result"]["content"][0]["text"])


async def _set(db, tenant_id, scope="tenant", value=VALUE):  # noqa: F811
    await tool_secrets.set_value(db, MANIFEST, scope, tenant_id, "search_key", value, user_id=None)
    await secrets_service.notify_change(tool_secrets.owner(scope, tenant_id, AGENT), "search_key")


@pytest.mark.asyncio
async def test_secret_get_answers_this_tenants_value_with_no_grant(db, store):  # noqa: F811
    tenant = await _tenant(db)
    await _set(db, tenant, "agent", "every-tenants-default-value")
    async with _token(tenant) as (token, _):
        assert _result(await _call(token, "secret_get", {"name": "search_key"})) == {
            "value": "every-tenants-default-value"
        }
    await _set(db, tenant)
    async with _token(tenant) as (token, _):
        assert _result(await _call(token, "secret_get", {"name": "search_key"})) == {"value": VALUE}


@pytest.mark.asyncio
async def test_undeclared_is_32005(db, store):  # noqa: F811
    """Ahead of the generic ``-32602``, which would otherwise catch it: both
    refusals are ``KeyError``s."""
    tenant = await _tenant(db)
    async with _token(tenant) as (token, _):
        body = await _call(token, "secret_get", {"name": "other_key"})
    assert body["error"]["code"] == -32005, body
    assert "other_key" in body["error"]["message"]


@pytest.mark.asyncio
async def test_declared_but_unset_is_32006(db, store):  # noqa: F811
    tenant = await _tenant(db)
    async with _token(tenant) as (token, _):
        body = await _call(token, "secret_get", {"name": "search_key"})
    assert body["error"]["code"] == -32006, body


@pytest.mark.asyncio
async def test_mcp_never_reads_the_environment(db, store, monkeypatch):  # noqa: F811
    """K8-12: the backend's environment is the chassis's, not the
    container's. With the variable set and no row, the answer is still
    "not set" — and the value is nowhere in the response."""
    tenant = await _tenant(db)
    monkeypatch.setenv("SEARCH_KEY", ENV_VALUE)
    async with _token(tenant) as (token, _):
        body = await _call(token, "secret_get", {"name": "search_key"})
    assert body["error"]["code"] == -32006, body
    assert ENV_VALUE not in json.dumps(body)


@pytest.mark.asyncio
async def test_bad_arguments_are_refused(db, store):  # noqa: F811
    tenant = await _tenant(db)
    async with _token(tenant) as (token, _):
        for arguments in ({}, {"name": 7}, {"name": ""}, {"name": "search_key", "x": 1}):
            body = await _call(token, "secret_get", arguments)
            assert body["error"]["code"] == -32602, (arguments, body)


@pytest.mark.asyncio
async def test_the_writes_a_container_makes_are_scrubbed(db, store):  # noqa: F811
    """The value a container was handed does not come back into an audit
    row or the run store through MCP."""
    import redis.asyncio as aioredis

    from app.config import settings

    tenant = await _tenant(db)
    await _set(db, tenant)
    async with _token(tenant, grants=["audit", "run_store"]) as (token, run_id):
        body = await _call(
            token, "audit_log", {"action_type": "blocked_request", "detail": {"k": f"got {VALUE}"}}
        )
        assert _result(body) == {"ok": True}
        body = await _call(token, "run_store_set", {"key": "last", "value": {"v": VALUE}})
        assert _result(body)["ok"] is True
        async with aioredis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True) as r:
            stored = await r.hget(f"run:{run_id}:kv", "last")
    assert VALUE not in stored and "[REDACTED_SECRET]" in stored
    detail = (
        await db.execute(
            text("SELECT detail::text FROM activity_audit_log WHERE tenant_id = :t"),
            {"t": tenant},
        )
    ).scalar()
    assert detail is not None and VALUE not in detail and "[REDACTED_SECRET]" in detail


@pytest.mark.asyncio
@pytest.mark.parametrize("then", ["replaced", "cleared"])
async def test_a_value_handed_over_stays_scrubbed_once_the_row_moves_on(db, store, then):  # noqa: F811
    """Codex on #173: a container reads a value, an admin replaces or
    clears the row, and the container then writes the value it was handed.
    The rows no longer hold it, so re-resolving alone would let it through;
    the run's set in this process keeps it out of both writes."""
    import redis.asyncio as aioredis

    from app.config import settings
    from app.services import run_boundary

    tenant = await _tenant(db)
    await _set(db, tenant)
    async with _token(tenant, grants=["audit", "run_store"]) as (token, run_id):
        try:
            assert _result(await _call(token, "secret_get", {"name": "search_key"})) == {"value": VALUE}
            if then == "replaced":
                await _set(db, tenant, value="the-replacement-value-2e7f")
            else:
                await tool_secrets.clear_value(db, MANIFEST, "tenant", tenant, "search_key")
                await secrets_service.notify_change(
                    tool_secrets.owner("tenant", tenant, AGENT), "search_key"
                )
            body = await _call(
                token, "audit_log", {"action_type": "blocked_request", "detail": {"k": f"old {VALUE}"}}
            )
            assert _result(body) == {"ok": True}
            body = await _call(token, "run_store_set", {"key": "last", "value": {"v": VALUE}})
            assert _result(body)["ok"] is True
            async with aioredis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True) as r:
                stored = await r.hget(f"run:{run_id}:kv", "last")
        finally:
            run_boundary.forget_run(run_id)
    assert VALUE not in stored and "[REDACTED_SECRET]" in stored
    detail = (
        await db.execute(
            text("SELECT detail::text FROM activity_audit_log WHERE tenant_id = :t"),
            {"t": tenant},
        )
    ).scalar()
    assert detail is not None and VALUE not in detail and "[REDACTED_SECRET]" in detail
