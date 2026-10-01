"""An agent's tool secrets: the policy over K6's store (K8a; D20, D32, D35).

Against real PostgreSQL and K6's dict ``FakeRedis`` (the fixtures are
``test_secrets_service``'s), these hold:

* the order a run reads a value in — this tenant's row, every tenant's
  default, then, for an in-process agent alone, the environment — with a
  row no configured key opens falling through, never answering;
* the environment is read under the upper-cased name only with the
  fallback asked for, never for a reserved name, and never under
  ``MIN_CHARS``: a placeholder there is unset, logged by name, and kept
  out of the scrub set (``test_a_short_fallback_is_not_scrubbed``);
* a write checks the declaration, the scope and the length, in that
  order, writes nothing on a refusal, and stores the value stripped;
* ``last_used_at`` is stamped at most once an hour per row per process,
  and the fallback stamps nothing;
* the admin API's view names every declared name and never a value.
"""
from __future__ import annotations

import contextlib
import logging
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from app import secrets_keyring as keyring
from app.agents.manifest import AgentManifest
from app.services import run_boundary
from app.services import secrets_service
from app.services import tool_secrets_service as svc
from tests.test_secrets_service import db, fernet_key, store  # noqa: F401  (fixtures)

AGENT = "tool-secrets-probe"
NAME = "search_key"
TENANT_VALUE = "tenant-search-value-6c1f"
DEFAULT_VALUE = "default-search-value-93ab"
ENV_VALUE = "environment-search-value-0d2e"


def _manifest(runtime: str = "python-package") -> AgentManifest:
    fields = {
        "id": AGENT,
        "name": "Tool secrets probe",
        "runtime": runtime,
        "phases": [{"name": "work"}],
        "output": {"mode": "structured"},
        "secrets": [NAME, "vector_key"],
    }
    if runtime == "container":
        fields |= {"container": {"url": "http://probe:8090"}, "input_schema": "input_schema.json"}
    return AgentManifest.model_validate(fields)


@pytest.fixture(autouse=True)
def _sessions_are_the_tests(db, monkeypatch):  # noqa: F811
    """Every ``async_session()`` the service opens yields this test's
    session, so it reads the rows the test wrote."""
    import app.database

    @contextlib.asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(app.database, "async_session", _session)
    monkeypatch.delenv(NAME.upper(), raising=False)
    svc.forget_stamps()
    yield
    svc.forget_stamps()


async def _tenant(db) -> uuid.UUID:  # noqa: F811
    tenant_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'T', :s)"),
        {"id": tenant_id, "s": f"tool-secrets-{tenant_id.hex[:12]}"},
    )
    return tenant_id


async def _set(db, scope: str, tenant_id, value: str, name: str = NAME):  # noqa: F811
    """A write as a route makes it: the write, then (committed) the notice
    that drops what any process cached."""
    write = await svc.set_value(db, _manifest(), scope, tenant_id, name, value, user_id=None)
    await secrets_service.notify_change(svc.owner(scope, tenant_id, AGENT), name)
    return write


# -- the order ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tenants_row_then_the_default_then_the_environment(db, store, monkeypatch):  # noqa: F811
    tenant = await _tenant(db)
    monkeypatch.setenv(NAME.upper(), ENV_VALUE)

    found = await svc.resolve(tenant, AGENT, NAME, env_fallback=True)
    assert (found.value, found.source, found.row) == (ENV_VALUE, "environment", None)

    await _set(db, "agent", tenant, DEFAULT_VALUE)
    found = await svc.resolve(tenant, AGENT, NAME, env_fallback=True)
    assert (found.value, found.source) == (DEFAULT_VALUE, "agent")
    assert found.row == secrets_service.Owner("agent", None, AGENT)

    await _set(db, "tenant", tenant, TENANT_VALUE)
    found = await svc.resolve(tenant, AGENT, NAME, env_fallback=True)
    assert (found.value, found.source) == (TENANT_VALUE, "tenant")
    assert found.row == secrets_service.Owner("tenant", tenant, AGENT)

    # Another tenant has no row of its own: the default serves it.
    other = await _tenant(db)
    assert (await svc.resolve(other, AGENT, NAME, env_fallback=True)).value == DEFAULT_VALUE


@pytest.mark.asyncio
async def test_without_the_fallback_the_environment_is_never_read(db, store, monkeypatch):  # noqa: F811
    """A container's reads (K8-12): rows alone."""
    tenant = await _tenant(db)
    monkeypatch.setenv(NAME.upper(), ENV_VALUE)
    assert await svc.resolve(tenant, AGENT, NAME, env_fallback=False) is None


@pytest.mark.asyncio
async def test_a_row_no_key_opens_falls_through(db, store, monkeypatch):  # noqa: F811
    """A tenant row sealed under a key that is gone does not answer: the
    default serves, as the environment would with no default."""
    tenant = await _tenant(db)
    await _set(db, "tenant", tenant, TENANT_VALUE)
    store.use_key(fernet_key())  # the key that sealed the tenant row is gone
    await _set(db, "agent", tenant, DEFAULT_VALUE)

    found = await svc.resolve(tenant, AGENT, NAME, env_fallback=True)
    assert (found.value, found.source) == (DEFAULT_VALUE, "agent")


@pytest.mark.asyncio
async def test_a_reserved_name_is_never_read_from_the_environment(db, store, monkeypatch):  # noqa: F811
    tenant = await _tenant(db)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-the-gateways-key-4f1a")
    monkeypatch.setenv("LIBRERUN_DEMO", "true-and-long-enough")
    assert await svc.resolve(tenant, AGENT, "openai_api_key", env_fallback=True) is None
    assert await svc.resolve(tenant, AGENT, "librerun_demo", env_fallback=True) is None


@pytest.mark.asyncio
async def test_a_short_fallback_is_not_scrubbed(db, store, monkeypatch, caplog):  # noqa: F811
    """A placeholder in an operator's ``.env`` — ``none``, ``test`` — is
    treated as unset and logged by name: in the scrub set it would rewrite
    every match in a report."""
    tenant = await _tenant(db)
    monkeypatch.setenv(NAME.upper(), "none")
    caplog.set_level(logging.WARNING)

    assert await svc.resolve(tenant, AGENT, NAME, env_fallback=True) is None
    assert "tool_secret_fallback_too_short" in caplog.text and "SEARCH_KEY" in caplog.text
    assert "=none" not in caplog.text and "'none'" not in caplog.text

    values = await svc.scrub_values(tenant, AGENT, [NAME], env_fallback=True)
    assert values == []
    with run_boundary.scrubbing(values):
        assert run_boundary.redact_text("none of this is secret") == "none of this is secret"


# -- writes -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_write_checks_the_name_the_scope_and_the_length(db, store):  # noqa: F811
    tenant = await _tenant(db)
    manifest = _manifest()
    with pytest.raises(svc.SecretNotDeclared):
        await svc.set_value(db, manifest, "tenant", tenant, "undeclared", "x" * 20, user_id=None)
    with pytest.raises(ValueError, match="'tenant' or 'agent'"):
        await svc.set_value(db, manifest, "platform", tenant, NAME, "x" * 20, user_id=None)
    for value in ("short", "   seven  ", "x" * (svc.MAX_CHARS + 1), 12345678, None):
        with pytest.raises(svc.SecretValueInvalid) as raised:
            await svc.set_value(db, manifest, "tenant", tenant, NAME, value, user_id=None)
        assert str(value) not in str(raised.value) or value is None
    rows = (await db.execute(text("SELECT count(*) FROM secrets WHERE agent_id = :a"), {"a": AGENT})).scalar()
    assert rows == 0

    write = await _set(db, "tenant", tenant, f"  {TENANT_VALUE}\n")
    assert write.action == "set"
    assert write.fingerprint == keyring.fingerprint(store.key, TENANT_VALUE)
    assert (await svc.resolve(tenant, AGENT, NAME, env_fallback=False)).value == TENANT_VALUE
    assert (await _set(db, "tenant", tenant, TENANT_VALUE + "-2")).action == "replace"


@pytest.mark.asyncio
async def test_clear_removes_the_row_and_the_next_source_serves(db, store):  # noqa: F811
    tenant = await _tenant(db)
    await _set(db, "agent", tenant, DEFAULT_VALUE)
    await _set(db, "tenant", tenant, TENANT_VALUE)
    assert await svc.clear_value(db, _manifest(), "tenant", tenant, NAME) is True
    await secrets_service.notify_change(svc.owner("tenant", tenant, AGENT), NAME)
    assert (await svc.resolve(tenant, AGENT, NAME, env_fallback=False)).value == DEFAULT_VALUE
    assert await svc.clear_value(db, _manifest(), "tenant", tenant, NAME) is False


@pytest.mark.asyncio
async def test_scrub_values_are_every_declared_value_longest_first(db, store, monkeypatch):  # noqa: F811
    tenant = await _tenant(db)
    await _set(db, "tenant", tenant, "short-value-1")
    await _set(db, "agent", tenant, "short-value-1", name="vector_key")
    monkeypatch.setenv("UNDECLARED_KEY", "never-in-the-set-1234")

    values = await svc.scrub_values(
        tenant, AGENT, [NAME, "vector_key", "undeclared_key"], env_fallback=False
    )
    assert values == ["short-value-1"]  # de-duplicated; the environment not read

    await _set(db, "agent", tenant, "a-much-longer-short-value-1", name="vector_key")
    values = await svc.scrub_values(tenant, AGENT, [NAME, "vector_key"], env_fallback=False)
    assert values == ["a-much-longer-short-value-1", "short-value-1"]


# -- last_used_at ---------------------------------------------------------------------


async def _last_used(db, scope: str):  # noqa: F811
    return (
        await db.execute(
            text("SELECT last_used_at FROM secrets WHERE agent_id = :a AND scope = :s"),
            {"a": AGENT, "s": scope},
        )
    ).scalar()


@pytest.mark.asyncio
async def test_last_used_at_is_stamped_at_most_hourly(db, store, monkeypatch):  # noqa: F811
    tenant = await _tenant(db)
    await _set(db, "tenant", tenant, TENANT_VALUE)
    row = svc.owner("tenant", tenant, AGENT)
    assert await _last_used(db, "tenant") is None

    await svc.stamp(row, NAME)
    first = await _last_used(db, "tenant")
    assert first is not None

    # Within the hour, this process does not write again.
    await db.execute(text("UPDATE secrets SET last_used_at = NULL WHERE agent_id = :a"), {"a": AGENT})
    await svc.stamp(row, NAME)
    assert await _last_used(db, "tenant") is None

    # An hour later it does.
    clock = [svc.time.monotonic() + svc.STAMP_SECONDS + 1]
    monkeypatch.setattr(svc.time, "monotonic", lambda: clock[0])
    await svc.stamp(row, NAME)
    assert await _last_used(db, "tenant") is not None


@pytest.mark.asyncio
async def test_a_failed_stamp_is_dropped(db, store, monkeypatch, caplog):  # noqa: F811
    import app.database

    tenant = await _tenant(db)

    @contextlib.asynccontextmanager
    async def _broken():
        raise ConnectionError("no database")
        yield  # pragma: no cover

    monkeypatch.setattr(app.database, "async_session", _broken)
    caplog.set_level(logging.WARNING)
    await svc.stamp(svc.owner("tenant", tenant, AGENT), NAME)  # does not raise
    assert "tool_secret_stamp_failed" in caplog.text


# -- the admin API's view ------------------------------------------------------------


@pytest.mark.asyncio
async def test_states_describe_every_name_and_never_a_value(db, store, monkeypatch):  # noqa: F811
    tenant = await _tenant(db)
    monkeypatch.setenv("VECTOR_KEY", ENV_VALUE)
    await _set(db, "agent", tenant, DEFAULT_VALUE)

    states = await svc.states(db, _manifest(), tenant)
    assert [s.name for s in states] == [NAME, "vector_key"]
    search, vector = states
    assert (search.effective, search.tenant.set, search.agent.set) == ("agent", False, True)
    assert search.agent.fingerprint == keyring.fingerprint(store.key, DEFAULT_VALUE)
    assert search.environment is False
    assert (vector.effective, vector.environment) == ("environment", True)
    assert DEFAULT_VALUE not in repr(states) and ENV_VALUE not in repr(states)

    # A container's environment is its own: never asked, never reported.
    container = await svc.states(db, _manifest("container"), tenant)
    assert [s.environment for s in container] == [None, None]
    assert [s.effective for s in container] == ["agent", "unset"]

    # A row no configured key opens is set, has no fingerprint, and serves
    # nothing: the next source is effective.
    store.use_key(fernet_key())
    (search, _) = await svc.states(db, _manifest(), tenant)
    assert (search.agent.set, search.agent.fingerprint, search.effective) == (True, None, "unset")
