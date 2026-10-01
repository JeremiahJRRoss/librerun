"""The façade's ``secrets`` member (K8a; D20, L28, L31, K8-03, K8-10).

``caps.secrets.get(name)`` answers a name the manifest declares with this
tenant's value — the tenant's row, else every tenant's default, else, when
the runner allows it for an in-process agent, the backend's environment —
and refuses anything else by name: ``SecretNotDeclared``, ``SecretNotSet``.
It needs no grant, logs neither the name nor the value, stamps the row that
served, and adds the value to the run's scrub set as it hands it over.

Real PostgreSQL with K6's fixtures; the façade's own session is the test's.
"""
from __future__ import annotations

import contextlib
import logging
import uuid

import pytest
from sqlalchemy import text

from app import capabilities as caps_mod
from app.agents import registry
from app.agents.manifest import AgentManifest
from app.agents.protocol import AgentProtocol
from app.services import run_boundary, secrets_service
from app.services import tool_secrets_service as tool_secrets
from tests.test_secrets_service import db, fernet_key, store  # noqa: F401  (fixtures)

AGENT = "capability-secrets-probe"
VALUE = "the-tenants-search-value-2f7c"
ENV_VALUE = "the-environments-search-value-81d0"


class _Probe(AgentProtocol):
    agent_id = AGENT
    display_name = "probe"
    description = "d"


MANIFEST = AgentManifest.model_validate(
    {
        "id": AGENT,
        "name": "probe",
        "runtime": "python-package",
        "phases": [{"name": "work"}],
        "output": {"mode": "structured"},
        "secrets": ["search_key"],
    }
)


@pytest.fixture(autouse=True)
def _probe_and_sessions(db, monkeypatch):  # noqa: F811
    import app.database

    @contextlib.asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(app.database, "async_session", _session)
    monkeypatch.delenv("SEARCH_KEY", raising=False)
    monkeypatch.delenv("OTHER_KEY", raising=False)
    registry._clear_registry_for_tests()
    registry.register(_Probe(), MANIFEST)
    tool_secrets.forget_stamps()
    yield
    registry._clear_registry_for_tests()
    tool_secrets.forget_stamps()


async def _tenant(db) -> uuid.UUID:  # noqa: F811
    tenant_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'T', :s)"),
        {"id": tenant_id, "s": f"caps-secrets-{tenant_id.hex[:12]}"},
    )
    return tenant_id


def _caps(tenant_id, *, fallback: bool = False):
    return caps_mod.for_run(
        run_id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_id=AGENT,
        grants=[],
        secrets_env_fallback=fallback,
    )


async def _set(db, tenant_id, name: str = "search_key", value: str = VALUE):  # noqa: F811
    await tool_secrets.set_value(db, MANIFEST, "tenant", tenant_id, name, value, user_id=None)
    await secrets_service.notify_change(tool_secrets.owner("tenant", tenant_id, AGENT), name)


def test_it_needs_no_grant_and_is_no_capability():
    caps = _caps(uuid.uuid4())
    assert "secrets" not in caps_mod.KNOWN_CAPABILITIES
    assert isinstance(caps.secrets, caps_mod.SecretsCapability)
    assert "secrets" not in caps._members


@pytest.mark.asyncio
async def test_a_declared_value_is_delivered_stamped_and_scrubbed(db, store):  # noqa: F811
    tenant = await _tenant(db)
    await _set(db, tenant)

    with run_boundary.scrubbing(()):
        assert await _caps(tenant).secrets.get("search_key") == VALUE
        assert run_boundary.redact_text(f"used {VALUE}") == "used [REDACTED_SECRET]"
    stamped = (
        await db.execute(
            text("SELECT last_used_at FROM secrets WHERE agent_id = :a AND scope = 'tenant'"),
            {"a": AGENT},
        )
    ).scalar()
    assert stamped is not None


@pytest.mark.asyncio
async def test_an_undeclared_name_is_refused_even_with_a_row(db, store):  # noqa: F811
    """The declaration is the whole list: a row a platform admin wrote
    under a name the manifest does not (or no longer) declare is not read."""
    tenant = await _tenant(db)
    await secrets_service.set_secret(
        db, tool_secrets.owner("tenant", tenant, AGENT), "other_key", VALUE, user_id=None
    )
    with pytest.raises(caps_mod.SecretNotDeclared) as raised:
        await _caps(tenant).secrets.get("other_key")
    assert raised.value.name == "other_key" and VALUE not in str(raised.value)


@pytest.mark.asyncio
async def test_a_declared_name_with_no_value_is_not_set(db, store, monkeypatch):  # noqa: F811
    tenant = await _tenant(db)
    with pytest.raises(caps_mod.SecretNotSet):
        await _caps(tenant).secrets.get("search_key")

    # The environment serves only where the runner allows it.
    monkeypatch.setenv("SEARCH_KEY", ENV_VALUE)
    with pytest.raises(caps_mod.SecretNotSet):
        await _caps(tenant).secrets.get("search_key")
    assert await _caps(tenant, fallback=True).secrets.get("search_key") == ENV_VALUE


@pytest.mark.asyncio
async def test_neither_the_name_nor_the_value_is_logged(db, store, caplog):  # noqa: F811
    tenant = await _tenant(db)
    await _set(db, tenant)
    caplog.set_level(logging.DEBUG)
    await _caps(tenant).secrets.get("search_key")
    with pytest.raises(caps_mod.SecretNotDeclared):
        await _caps(tenant).secrets.get("other_key")
    assert VALUE not in caplog.text
    assert "search_key" not in caplog.text


@pytest.mark.asyncio
async def test_the_errors_are_key_errors_an_agent_can_catch(db, store):  # noqa: F811
    tenant = await _tenant(db)
    for name in ("search_key", "other_key"):
        with pytest.raises(KeyError):
            await _caps(tenant).secrets.get(name)
