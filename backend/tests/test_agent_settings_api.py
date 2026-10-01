"""The settings surface of ``/agents/{id}/config`` (K5a, L32).

``GET`` serves the manifest's ``settings[]`` as ``meta.settings`` and this
TENANT's effective values as ``settings``; ``PUT …/config/settings`` takes
``[{key, value}]``. Any agent declaring ``settings[]`` gets the surface —
a container with no Python at all included — and an agent still on the
deprecated ``config_meta()`` path is served in the same shapes, marked
``meta.deprecated``, with a log line on every request until v1.2.

Real PostgreSQL, the real agents router behind ``httpx.ASGITransport``
and the real ``require_admin``, over a principal seeded for the test:
what is under test is what lands in ``agent_settings`` and in the audit
log, and a fake session would record calls rather than rows. The lists
the handlers build are non-empty on purpose — the element-coverage gate
needs a test that enters them.
"""
from __future__ import annotations

import logging
import os
import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.agents import registry
from app.agents.manifest import AgentManifest
from app.agents.protocol import AgentConfigMeta, AgentProtocol, ConfigField
from app.database import get_db
from app.middleware import get_current_user
from app.models import User
from app.routers import agents as agents_router
from app.services import agent_settings_service as svc

REQUIRE_DB = os.environ.get("LIBRERUN_REQUIRE_DB", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

AGENT = "probe-settings"


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


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


async def admin_of_a_new_tenant(db) -> User:
    tenant_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, :n, :s)"),
        {"id": tenant_id, "n": "Probe", "s": f"probe-{tenant_id.hex[:12]}"},
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


def client(db, user: User) -> AsyncClient:
    """The agents router with only the session and the principal supplied:
    ``require_admin`` runs for real."""
    app = FastAPI()
    app.include_router(agents_router.router)

    async def _db_dep():
        yield db

    async def _user_dep():
        return user

    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


class _ManifestOnly(AgentProtocol):
    """What a container looks like to the chassis: no Python surface."""

    agent_id = AGENT
    display_name = "Probe"
    description = "declares settings[] and nothing else"

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}


SETTINGS = [
    {
        "key": "depth",
        "label": "Search depth",
        "type": "enum",
        "options": ["basic", "advanced"],
        "default": "advanced",
        "description": "How far to look.",
    },
    {"key": "limit", "label": "Limit", "type": "int", "default": 3},
    {"key": "strict", "type": "bool", "default": False},
]


def container_manifest(settings=None) -> AgentManifest:
    """A container that declares settings and no LLM steps and grants
    nothing: its Settings tab is the only reason it has a page."""
    return AgentManifest.model_validate(
        {
            "id": AGENT,
            "name": "Probe",
            "runtime": "container",
            "container": {"url": "http://probe:8090"},
            "input_schema": "input_schema.json",
            "phases": [{"name": "work"}],
            "output": {"mode": "structured"},
            "settings": SETTINGS if settings is None else settings,
        }
    )


async def audit_rows(db, tenant_id) -> list[dict]:
    result = await db.execute(
        text(
            "SELECT detail FROM activity_audit_log "
            "WHERE tenant_id = :t AND action_type = 'config_change' ORDER BY created_at"
        ),
        {"t": tenant_id},
    )
    return [row[0] for row in result.all()]


@pytest.mark.asyncio
async def test_get_config_serves_this_tenants_settings(db):
    registry.register(_ManifestOnly(), container_manifest())
    admin = await admin_of_a_new_tenant(db)
    other = await admin_of_a_new_tenant(db)
    await svc.apply_updates(
        db, admin.tenant_id, AGENT, container_manifest(),
        [{"key": "limit", "value": 7}], admin.id,
    )
    await svc.apply_updates(
        db, other.tenant_id, AGENT, container_manifest(),
        [{"key": "depth", "value": "basic"}], other.id,
    )

    async with client(db, admin) as http:
        response = await http.get(f"/agents/{AGENT}/config")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"]["deprecated"] is False
    assert body["meta"]["settings"] == [
        {"key": "depth", "label": "Search depth", "type": "enum", "default": "advanced",
         "options": ["basic", "advanced"], "description": "How far to look."},
        {"key": "limit", "label": "Limit", "type": "int", "default": 3,
         "options": None, "description": ""},
        {"key": "strict", "label": "strict", "type": "bool", "default": False,
         "options": None, "description": ""},
    ]
    # This tenant's 7 and nothing of the other tenant's "basic".
    assert body["settings"] == [
        {"key": "depth", "label": "Search depth", "type": "enum", "value": "advanced",
         "default": "advanced", "overridden": False},
        {"key": "limit", "label": "Limit", "type": "int", "value": 7,
         "default": 3, "overridden": True},
        {"key": "strict", "label": "strict", "type": "bool", "value": False,
         "default": False, "overridden": False},
    ]
    assert body["steps"] == []


@pytest.mark.asyncio
async def test_put_settings_writes_this_tenant_and_audits_changed_keys(db):
    registry.register(_ManifestOnly(), container_manifest())
    admin = await admin_of_a_new_tenant(db)
    other = await admin_of_a_new_tenant(db)

    async with client(db, admin) as http:
        # What the tab posts: every key, the unchanged ones at their
        # defaults — only the two that differ are stored.
        response = await http.put(
            f"/agents/{AGENT}/config/settings",
            json=[
                {"key": "depth", "value": "basic"},
                {"key": "limit", "value": 3},
                {"key": "strict", "value": True},
            ],
        )
        assert response.status_code == 204, response.text
        assert response.content == b""

        # Refusals: undeclared, a value the type refuses, and nothing
        # written by either.
        undeclared = await http.put(
            f"/agents/{AGENT}/config/settings", json=[{"key": "nope", "value": 1}]
        )
        refused = await http.put(
            f"/agents/{AGENT}/config/settings",
            json=[{"key": "limit", "value": 9}, {"key": "strict", "value": 1}],
        )
        # The body is a list: the pre-K5a dict is a 422, not a guess.
        as_a_dict = await http.put(
            f"/agents/{AGENT}/config/settings", json={"depth": "basic"}
        )

    assert undeclared.status_code == 400
    assert undeclared.json()["detail"] == "setting 'nope': this agent declares no such setting"
    assert refused.status_code == 400
    assert refused.json()["detail"] == "setting 'strict': must be true or false, not an integer"
    assert as_a_dict.status_code == 422

    assert await svc.values_for(db, admin.tenant_id, AGENT) == {"depth": "basic", "strict": True}
    assert await svc.values_for(db, other.tenant_id, AGENT) == {}
    audits = await audit_rows(db, admin.tenant_id)
    assert audits == [
        {
            "surface": "agent_config",
            "agent_id": AGENT,
            "section": "settings",
            "changed_keys": ["depth", "strict"],
        }
    ]

    # A second Save that changes one key names that key alone, and null
    # returns a setting to its default.
    async with client(db, admin) as http:
        response = await http.put(
            f"/agents/{AGENT}/config/settings",
            json=[
                {"key": "depth", "value": "basic"},
                {"key": "limit", "value": 3},
                {"key": "strict", "value": None},
            ],
        )
    assert response.status_code == 204, response.text
    assert await svc.values_for(db, admin.tenant_id, AGENT) == {"depth": "basic"}
    # One transaction, so both rows carry one created_at: compared as a set.
    assert sorted(a["changed_keys"] for a in await audit_rows(db, admin.tenant_id)) == [
        ["depth", "strict"],
        ["strict"],
    ]


@pytest.mark.asyncio
async def test_a_manifest_only_container_with_one_setting_has_config(db, monkeypatch):
    """C13's ``has_config`` half: a container with one setting, no LLM
    step and no Python surface is linked from the admin hub and served its
    page — it used to be neither, because the only settings surface was a
    method a container cannot implement."""
    manifest = container_manifest([{"key": "note", "type": "string", "default": "hi"}])
    agent = _ManifestOnly()
    registry.register(agent, manifest)
    admin = await admin_of_a_new_tenant(db)

    async with client(db, admin) as http:
        listed = await http.get("/agents")
        page = await http.get(f"/agents/{AGENT}/config")
        saved = await http.put(
            f"/agents/{AGENT}/config/settings", json=[{"key": "note", "value": "there"}]
        )
        reread = await http.get(f"/agents/{AGENT}/config")

    (row,) = [r for r in listed.json() if r["agent_id"] == AGENT]
    assert row["has_config"] is True
    assert page.status_code == 200
    assert [s["key"] for s in page.json()["meta"]["settings"]] == ["note"]
    assert saved.status_code == 204
    assert reread.json()["settings"] == [
        {"key": "note", "label": "note", "type": "string", "value": "there",
         "default": "hi", "overridden": True}
    ]

    # Take the setting away and nothing is left to configure: 404, and
    # the listing and the route agree.
    registry.register(agent, container_manifest([]))
    async with client(db, admin) as http:
        listed = await http.get("/agents")
        page = await http.get(f"/agents/{AGENT}/config")
        refused = await http.put(
            f"/agents/{AGENT}/config/settings", json=[{"key": "note", "value": "x"}]
        )
    (row,) = [r for r in listed.json() if r["agent_id"] == AGENT]
    assert row["has_config"] is False
    assert page.status_code == 404
    assert refused.status_code == 404


class _Deprecated(AgentProtocol):
    """An agent still on the pre-K5a surface: config_meta() settings, one
    value for every tenant, kept by the agent itself."""

    agent_id = AGENT
    display_name = "Deprecated"
    description = "config_meta() settings, no settings[]"

    def __init__(self):
        self.values = {"depth": "basic", "limit": 3}
        self.updates: list[dict] = []

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    def config_meta(self) -> AgentConfigMeta:
        return AgentConfigMeta(
            settings=[
                ConfigField(
                    key="depth",
                    label="Search depth",
                    field_type="enum",
                    default="advanced",
                    enum_options=["basic", "advanced"],
                ),
                ConfigField(key="limit", label="Limit", field_type="int", default=3),
            ]
        )

    def get_settings(self) -> dict:
        return dict(self.values)

    def update_settings(self, updates: dict) -> None:
        self.updates.append(dict(updates))
        self.values.update(updates)


def _deprecations(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if "agent_settings_protocol_deprecated" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_the_deprecated_path_still_serves_and_warns(db, caplog):
    """K5-06: until v1.2 an agent's config_meta() — no agent in the tree
    takes that path since K5b — is served in the new shapes, marked
    deprecated, and every request says so in the log, naming the agent
    and the release that removes the path."""
    agent = _Deprecated()
    registry.register(agent)
    admin = await admin_of_a_new_tenant(db)

    with caplog.at_level(logging.WARNING):
        async with client(db, admin) as http:
            listed = await http.get("/agents")
            response = await http.get(f"/agents/{AGENT}/config")
            got = _deprecations(caplog)
            saved = await http.put(
                f"/agents/{AGENT}/config/settings",
                json=[{"key": "depth", "value": None}, {"key": "limit", "value": 8}],
            )
            refused = await http.put(
                f"/agents/{AGENT}/config/settings", json=[{"key": "limit", "value": True}]
            )
        put = _deprecations(caplog)[len(got):]

    (row,) = [r for r in listed.json() if r["agent_id"] == AGENT]
    assert row["has_config"] is True
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"]["deprecated"] is True
    assert [(s["key"], s["type"], s["options"]) for s in body["meta"]["settings"]] == [
        ("depth", "enum", ["basic", "advanced"]),
        ("limit", "int", None),
    ]
    assert body["settings"] == [
        {"key": "depth", "label": "Search depth", "type": "enum", "value": "basic",
         "default": "advanced", "overridden": True},
        {"key": "limit", "label": "Limit", "type": "int", "value": 3,
         "default": 3, "overridden": False},
    ]
    # One line for the GET, naming the agent, the method and v1.2.
    assert len(got) == 1
    assert AGENT in got[0] and "get_settings" in got[0] and "v1.2" in got[0]

    # The PUT: one update_settings call, null sending the default.
    assert saved.status_code == 204, saved.text
    assert agent.updates == [{"depth": "advanced", "limit": 8}]
    assert refused.status_code == 400
    assert refused.json()["detail"] == "setting 'limit': must be an integer, not a boolean"
    assert len(agent.updates) == 1
    assert len(put) == 2 and all("update_settings" in line and "v1.2" in line for line in put)
    # The refused PUT wrote no audit row; the saved one names both keys.
    assert [a["changed_keys"] for a in await audit_rows(db, admin.tenant_id)] == [
        ["depth", "limit"]
    ]

    # Nothing of it reached the tenant's table: the path is the agent's.
    assert await svc.values_for(db, admin.tenant_id, AGENT) == {}


@pytest.mark.asyncio
async def test_declaring_settings_turns_the_deprecated_path_off(db, caplog):
    """§9: a manifest that declares settings[] is served from it, even if
    the agent still answers config_meta() — never from both."""
    agent = _Deprecated()
    registry.register(agent, container_manifest())
    admin = await admin_of_a_new_tenant(db)

    with caplog.at_level(logging.WARNING):
        async with client(db, admin) as http:
            body = (await http.get(f"/agents/{AGENT}/config")).json()
            saved = await http.put(
                f"/agents/{AGENT}/config/settings", json=[{"key": "strict", "value": True}]
            )

    assert body["meta"]["deprecated"] is False
    assert [s["key"] for s in body["meta"]["settings"]] == ["depth", "limit", "strict"]
    assert saved.status_code == 204
    assert agent.updates == []
    assert await svc.values_for(db, admin.tenant_id, AGENT) == {"strict": True}
    assert _deprecations(caplog) == []
