"""The gateway's suite runs against a REAL PostgreSQL and a REAL Redis.

Both are what the code under test is *about*: the key table's partial
unique indexes decide whether a rotation is atomic, and the run-token
record is a Redis document the chassis writes. A double for either
would prove something about the double.

Without them the modules skip — unless ``LIBRERUN_REQUIRE_DB=1``, which
turns the skip into a failure so the CI job cannot pass by running
nothing.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

_ROOT = Path(__file__).resolve().parents[3]
for path in (_ROOT / "services" / "gateway", _ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

REQUIRE_DB = os.environ.get("LIBRERUN_REQUIRE_DB", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


def _skip_or_fail(reason: str):
    if REQUIRE_DB:
        pytest.fail("LIBRERUN_REQUIRE_DB is set, so this may not skip: " + reason)
    pytest.skip(reason)


@pytest_asyncio.fixture
async def session():
    """A gateway DB session in a transaction that is always rolled back."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from gateway.config import settings

    engine = create_async_engine(settings.DATABASE_URL.get_secret_value())
    try:
        async with engine.connect() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        await_reason = f"no database at DATABASE_URL ({type(exc).__name__}: {exc})"
        _skip_or_fail(await_reason)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        db = AsyncSession(bind=connection)
        try:
            yield db
        finally:
            await db.close()
            await transaction.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def redis_client():
    import redis.asyncio as aioredis

    from gateway.config import settings

    client = aioredis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:
        await client.aclose()
        _skip_or_fail(f"no redis at REDIS_URL ({type(exc).__name__}: {exc})")
    written: list[str] = []
    try:
        yield (client, written)
    finally:
        if written:
            await client.delete(*written)
        await client.aclose()


@pytest.fixture
def agent_id() -> str:
    """A fresh id per test, so rows from one never authorize another."""
    return f"probe-{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------
# Fixtures that install what a real deployment would have installed: a
# manifest snapshot the backend wrote at discovery, an agent key the
# gateway registered at boot, and a run token the chassis minted for one
# invocation.
# --------------------------------------------------------------------------


def manifest_of(agent_id: str, **overrides) -> dict:
    """A validated manifest, produced by the CHASSIS validator rather than
    hand-written — the gateway's authority is what the chassis accepted,
    so a test that invented its own shape would be testing a fiction."""
    from app.agents.manifest import AgentManifest

    base = {
        "id": agent_id,
        "name": "Probe",
        "runtime": "container",
        "container": {"url": "http://probe:8090"},
        "input_schema": "input_schema.json",
        "phases": [{"name": "work"}],
        "output": {"mode": "structured"},
        "capabilities": ["llm"],
        "llm": {
            "steps": [
                {
                    "id": "think",
                    "provider": "openai",
                    "model": "gpt-4o",
                    "temperature": 0.0,
                    "max_tokens": 100,
                    "timeout_seconds": 30,
                }
            ]
        },
    }
    base.update(overrides)
    return AgentManifest.model_validate(base).model_dump(mode="json")


@pytest.fixture
def manifest():
    """``manifest_of`` as a fixture — the backend's suite also ships a
    ``tests`` package, so a test that imported this module by name would
    get that one."""
    return manifest_of


@pytest_asyncio.fixture
async def installed(session, agent_id):
    """Install an agent the way a deployment does, and hand back the
    levers a test needs: its id, its key, and a way to re-install it with
    a different manifest."""
    import json

    from sqlalchemy import text

    from gateway import keys

    async def install(manifest: dict | None = None, *, absent: bool = False) -> None:
        payload = manifest if manifest is not None else manifest_of(agent_id)
        await session.execute(
            text(
                "INSERT INTO agent_manifests "
                "(agent_id, manifest, sha256, source, absent_at) "
                "VALUES (:a, CAST(:m AS JSONB), :s, 'directory', "
                " CASE WHEN :absent THEN NOW() END) "
                "ON CONFLICT (agent_id) DO UPDATE SET "
                " manifest = EXCLUDED.manifest, absent_at = EXCLUDED.absent_at"
            ),
            {
                "a": agent_id,
                "m": json.dumps(payload),
                "s": "0" * 64,
                "absent": absent,
            },
        )

    key_value = keys.mint_key()
    await install()
    await keys.reconcile_env_keys(session, {keys.env_name_for(agent_id): key_value})

    class Installed:
        id = agent_id
        key = key_value
        reinstall = staticmethod(install)

    return Installed()


@pytest_asyncio.fixture
async def run_token(redis_client, installed):
    """Mint a run token the way ``app/agents/container.py`` mints one."""
    import json
    import secrets

    client, written = redis_client

    async def mint(**overrides) -> str:
        token = secrets.token_urlsafe(24)
        record = {
            "run_id": str(uuid.uuid4()),
            "tenant_id": str(uuid.uuid4()),
            "agent_id": installed.id,
            "grants": ["llm"],
            "user_id": str(uuid.uuid4()),
            "run_number": "RUN-1000",
            "deadline_seconds": 300,
            "trace_id": f"{secrets.randbits(128):032x}",
            "traceparent": None,
            "state": "active",
        }
        record.update(overrides)
        if record.get("traceparent") is None and record.get("trace_id"):
            record["traceparent"] = (
                f"00-{record['trace_id']}-{secrets.randbits(64):016x}-01"
            )
        key = f"run_token:{token}"
        await client.set(key, json.dumps(record), ex=300)
        written.append(key)
        return token

    return mint


@pytest_asyncio.fixture
async def client(session):
    """The real gateway app, with the transactional session bound in so
    every row a request writes is rolled back with the test."""
    from httpx import ASGITransport, AsyncClient

    from gateway import db as gateway_db
    from gateway.main import app

    async def _session_override():
        yield session

    app.dependency_overrides[gateway_db.get_db] = _session_override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gateway"
        ) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _keyless_is_opt_in():
    """The suite measures the same thing whatever the shell carries.

    Tests that want keyless mode set it themselves — `monkeypatch.
    setattr(steps.settings, "LIBRERUN_STUB_LLM", True)` — so the default
    has to be off. It was read from the ambient environment instead, and
    a shell that happened to export `LIBRERUN_STUB_LLM=true` (the
    backend suite is run that way: it needs a stubbed gateway) silently
    routed every step to the stub. Six `test_steps` cases then failed,
    asserting `openai/...` and getting `stub/...`, in a suite that
    passes cleanly on its own.

    That is a check answering a question nobody asked. Pinned off here,
    the opt-in monkeypatches still work and the ambient value cannot
    change what the tests measure.
    """
    from gateway import steps

    previous = steps.settings.LIBRERUN_STUB_LLM
    steps.settings.LIBRERUN_STUB_LLM = False
    try:
        yield
    finally:
        steps.settings.LIBRERUN_STUB_LLM = previous
