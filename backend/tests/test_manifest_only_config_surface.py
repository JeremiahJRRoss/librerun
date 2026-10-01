"""A container agent's LLM steps are configurable (blueprint S4a, D13).

`config_meta()` is the AGENT's settings surface — the fields it invents
for itself — and a container agent has no way to implement a Python
method, so `ContainerAgent` inherits `None` for it. Gating the whole
config page on that meant the reference container declared an LLM step
whose provider and model were editable nowhere, while the gateway's own
`step_not_configured` told the admin to go to that page (Codex P1).

The steps are not the agent's surface: they are the manifest's
declaration and this tenant's rows, both of which the chassis holds.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agents.protocol import AgentConfigMeta, AgentProtocol
from app.agents.manifest import AgentManifest


class _ManifestOnlyAgent(AgentProtocol):
    """What a container looks like to the chassis: no Python surface."""

    agent_id = "probe-container"
    display_name = "Probe"
    description = "manifest only"

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}


class _AgentWithSettings(_ManifestOnlyAgent):
    agent_id = "probe-settings"

    def config_meta(self) -> AgentConfigMeta:
        return AgentConfigMeta(settings=[])


def manifest_with_steps(agent_id: str, steps: list[dict]) -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "id": agent_id,
            "name": "Probe",
            "runtime": "container",
            "container": {"url": "http://probe:8090"},
            "input_schema": "input_schema.json",
            "phases": [{"name": "work"}],
            "output": {"mode": "structured"},
            "capabilities": ["llm"],
            "llm": {"steps": steps},
        }
    )


def manifest_without_steps(agent_id: str) -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "id": agent_id,
            "name": "Probe",
            "runtime": "container",
            "container": {"url": "http://probe:8090"},
            "input_schema": "input_schema.json",
            "phases": [{"name": "work"}],
            "output": {"mode": "structured"},
        }
    )


@pytest.fixture
def client(monkeypatch):
    """The real agents router on a bare app — the same shape
    ``test_agents_router.py`` uses, so no lifespan and no real database;
    what is under test is the routing decision, not the app's boot."""
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import FastAPI, HTTPException, status

    from app.database import get_db
    from app.middleware import get_current_user, require_admin
    from app.routers import agents as agents_router

    state: dict = {}

    monkeypatch.setattr(agents_router, "get_agent", lambda a: state.get("agent"))
    monkeypatch.setattr(agents_router, "get_manifest", lambda a: state.get("manifest"))

    async def _no_overrides(db, tenant_id, agent_id):
        return {}

    monkeypatch.setattr(agents_router.step_configs, "overrides_for", _no_overrides)

    class _User:
        id = "11111111-1111-1111-1111-111111111111"
        tenant_id = "22222222-2222-2222-2222-222222222222"
        email = "admin@librerun.example"
        role = "admin"

    user = _User()
    app = FastAPI()
    app.include_router(agents_router.router)

    async def _user_override():
        return user

    fake_db = AsyncMock()
    fake_db.add = MagicMock()
    fake_db.flush = AsyncMock()

    async def _db_override():
        yield fake_db

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[require_admin] = _user_override
    app.dependency_overrides[get_db] = _db_override

    yield TestClient(app), state


STEP = {
    "id": "think",
    "label": "Think",
    "provider": "openai",
    "model": "gpt-4o",
    "temperature": 0.0,
}


def test_a_manifest_only_agents_steps_are_served(client):
    """The finding: 404 before the steps were ever rendered."""
    http, state = client
    state["agent"] = _ManifestOnlyAgent()
    state["manifest"] = manifest_with_steps("probe-container", [STEP])

    response = http.get("/agents/probe-container/config")

    assert response.status_code == 200
    body = response.json()
    assert [s["step_id"] for s in body["steps"]] == ["think"]
    assert body["steps"][0]["model"] == "gpt-4o"


def test_it_gets_the_chassis_defaults_for_meta(client):
    """No agent surface means no settings section — and the provider list
    and editable fields the chassis knows, so the page can render."""
    http, state = client
    state["agent"] = _ManifestOnlyAgent()
    state["manifest"] = manifest_with_steps("probe-container", [STEP])

    body = http.get("/agents/probe-container/config").json()

    assert body["meta"]["settings"] == []
    assert body["meta"]["supported_providers"]
    assert body["meta"]["step_editable_fields"]
    assert body["meta"]["deprecated"] is False
    assert body["settings"] == []


def test_a_step_the_admin_must_choose_is_served_too(client):
    """The case the whole feature is for: a manifest that declares the
    step and leaves the model to the tenant."""
    http, state = client
    state["agent"] = _ManifestOnlyAgent()
    state["manifest"] = manifest_with_steps("probe-container", [{"id": "think"}])

    body = http.get("/agents/probe-container/config").json()

    assert body["steps"][0]["provider"] is None
    assert body["steps"][0]["model"] is None


def test_saving_steps_for_a_manifest_only_agent_is_accepted(client, monkeypatch):
    from app.routers import agents as agents_router

    http, state = client
    state["agent"] = _ManifestOnlyAgent()
    state["manifest"] = manifest_with_steps("probe-container", [STEP])
    seen: dict = {}

    async def _apply(db, tenant_id, agent_id, manifest, updates, user_id):
        seen["updates"] = updates
        return ["think"]

    async def _audit(*args, **kwargs):
        return None

    monkeypatch.setattr(agents_router.step_configs, "apply_updates", _apply)
    monkeypatch.setattr(agents_router, "log_audit", _audit)

    response = http.put(
        "/agents/probe-container/config/steps",
        json=[{"step_id": "think", "provider": "anthropic", "model": "claude-sonnet-4-6"}],
    )

    assert response.status_code == 204
    assert seen["updates"][0]["model"] == "claude-sonnet-4-6"


def test_an_agent_with_no_steps_and_no_surface_is_still_404(client):
    """The gate still exists; it just stopped being the wrong one."""
    http, state = client
    state["agent"] = _ManifestOnlyAgent()
    state["manifest"] = manifest_without_steps("probe-container")

    assert http.get("/agents/probe-container/config").status_code == 404


def test_settings_writes_still_need_an_agent_surface(client):
    """An agent that declares no settings — no ``settings[]``, and no
    deprecated ``config_meta()`` settings — has nowhere to put them, so
    accepting the write would be worse than refusing it (K5a: 404 with
    no settings)."""
    http, state = client
    state["agent"] = _ManifestOnlyAgent()
    state["manifest"] = manifest_with_steps("probe-container", [STEP])

    response = http.put(
        "/agents/probe-container/config/settings",
        json=[{"key": "anything", "value": 1}],
    )

    assert response.status_code == 404


def test_the_listing_links_a_manifest_only_agent_to_its_page(client, monkeypatch):
    """Fixing the ROUTE is not fixing the feature: the admin index still
    computed `has_config` from `config_meta()`, so a container agent's
    card said "No admin config" and linked nowhere — to a page that had
    just started working (Codex P1). A gate and the link to what it
    gates are one decision."""
    from app.routers import agents as agents_router

    http, state = client
    agent = _ManifestOnlyAgent()
    state["manifest"] = manifest_with_steps("probe-container", [STEP])
    monkeypatch.setattr(agents_router, "list_agents", lambda: [agent])

    rows = http.get("/agents").json()

    assert [r["agent_id"] for r in rows] == ["probe-container"]
    assert rows[0]["has_config"] is True


def test_the_listing_still_says_no_for_an_agent_with_nothing(client, monkeypatch):
    from app.routers import agents as agents_router

    http, state = client
    agent = _ManifestOnlyAgent()
    state["manifest"] = manifest_without_steps("probe-container")
    monkeypatch.setattr(agents_router, "list_agents", lambda: [agent])

    assert http.get("/agents").json()[0]["has_config"] is False


def test_the_listing_and_the_route_agree(client, monkeypatch):
    """One predicate, so a card that links always reaches a page and a
    card that does not never hides one."""
    from app.routers import agents as agents_router

    http, state = client
    agent = _ManifestOnlyAgent()
    monkeypatch.setattr(agents_router, "list_agents", lambda: [agent])
    state["agent"] = agent

    for manifest, expected in (
        (manifest_with_steps("probe-container", [STEP]), 200),
        (manifest_without_steps("probe-container"), 404),
    ):
        state["manifest"] = manifest
        listed = http.get("/agents").json()[0]["has_config"]
        served = http.get("/agents/probe-container/config").status_code

        assert listed is (expected == 200)
        assert served == expected


def test_an_agent_with_its_own_surface_still_gets_it(client):
    """The legacy path is unchanged: an agent that defines settings has
    them served and writable."""
    http, state = client
    state["agent"] = _AgentWithSettings()
    state["manifest"] = manifest_with_steps("probe-settings", [STEP])

    body = http.get("/agents/probe-settings/config").json()

    assert body["meta"]["settings"] == []
    assert [s["step_id"] for s in body["steps"]] == ["think"]
