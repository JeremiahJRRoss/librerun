"""Tests for the agent protocol, registry, discovery, and HTTP router.

Built on a minimal FastAPI app that mounts only the agents router and
overrides the auth + DB dependencies — same pattern as
``test_logging_middleware.py``. No real DB or Redis is required.
"""
from __future__ import annotations

import sys
import textwrap
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException, status
from starlette.testclient import TestClient

from app.agents import registry
from app.agents.protocol import (
    AgentConfigMeta,
    AgentInput,
    AgentProtocol,
    AnalysisResult,
    ConfigField,
    InvestigationResult,
    StepProgress,
)
from app.database import get_db
from app.middleware import get_current_user, require_admin
from app.routers import agents as agents_router


# ---------- fixtures ----------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate each test from the module-level registry."""
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


class _FakeUser:
    def __init__(self, role: str = "admin"):
        self.id = uuid.uuid4()
        self.tenant_id = uuid.uuid4()
        self.email = f"{role}@example.com"
        self.role = role


def _build_app(*, admin: bool = True) -> tuple[FastAPI, TestClient, _FakeUser, AsyncMock]:
    """Return a FastAPI app with the agents router mounted and auth/db stubbed.

    ``admin=False`` simulates a non-admin user — ``require_admin`` is wired
    to raise 403, matching the production ``middleware.require_admin``
    behavior.
    """
    app = FastAPI()
    app.include_router(agents_router.router)

    user = _FakeUser(role="admin" if admin else "viewer")

    async def _user_override():
        return user

    async def _admin_override():
        if user.role != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
        return user

    fake_db = AsyncMock()
    fake_db.add = MagicMock()
    fake_db.flush = AsyncMock()

    async def _db_override():
        yield fake_db

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[require_admin] = _admin_override
    app.dependency_overrides[get_db] = _db_override

    return app, TestClient(app), user, fake_db


def _manifest_with_steps():
    """The configured agent's manifest — the one declaration of the steps
    an admin may edit (blueprint S4a)."""
    from app.agents.manifest import AgentManifest

    return AgentManifest.model_validate(
        {
            "id": "configured",
            "name": "Configured Agent",
            "runtime": "python-package",
            "phases": [{"name": "analyze"}, {"name": "investigate", "approval": True}],
            "output": {"mode": "structured"},
            "capabilities": ["llm"],
            "llm": {
                "steps": [
                    {
                        "id": "analyze",
                        "label": "Main analysis step",
                        "provider": "openai",
                        "model": "gpt-4o",
                        "temperature": 0.0,
                        "max_tokens": 2000,
                        "timeout_seconds": 30,
                    }
                ]
            },
        }
    )


# ---------- stub agents ----------


class _NoConfigAgent(AgentProtocol):
    agent_id = "no-config"
    display_name = "No Config Agent"
    description = "An agent that doesn't expose a config surface"

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {"q": {"type": "string"}}}

    async def analyze(self, inp, on_progress):  # pragma: no cover
        return AnalysisResult(display={}, structured={})

    async def investigate(self, inp, on_progress):  # pragma: no cover
        return InvestigationResult(status="complete")


class _ConfiguredAgent(AgentProtocol):
    agent_id = "configured"
    display_name = "Configured Agent"
    description = "Has config surface + settings + steps"

    def __init__(self):
        self._settings = {"depth": "advanced", "top_k": 5}

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {"q": {"type": "string"}}}

    async def analyze(self, inp, on_progress):  # pragma: no cover
        return AnalysisResult(display={}, structured={})

    async def investigate(self, inp, on_progress):  # pragma: no cover
        return InvestigationResult(status="complete")

    def config_meta(self) -> AgentConfigMeta:
        return AgentConfigMeta(
            supported_providers=["openai", "anthropic"],
            step_editable_fields=["temperature", "max_tokens"],
            settings=[
                ConfigField(
                    key="depth",
                    label="Search depth",
                    field_type="enum",
                    enum_options=["basic", "advanced"],
                    default="advanced",
                ),
                ConfigField(
                    key="top_k",
                    label="Top K",
                    field_type="int",
                    default=5,
                ),
            ],
        )

    def get_settings(self) -> dict:
        return dict(self._settings)

    def update_settings(self, updates: dict) -> None:
        self._settings.update(updates)


# ---------- protocol / registry unit tests ----------


def test_protocol_defaults():
    """An agent with only the required overrides still reports sane defaults."""
    a = _NoConfigAgent()
    assert a.config_meta() is None
    assert a.get_settings() == {}
    assert a.review_schema() is None
    assert a.feedback_sections() == ["overall"]
    assert a.report_template_path() is None


def test_register_and_lookup_roundtrip():
    agent = _NoConfigAgent()
    registry.register(agent)
    assert registry.get_agent("no-config") is agent
    assert agent in registry.list_agents()


def test_register_duplicate_overwrites_and_warns(caplog):
    first = _NoConfigAgent()
    second = _NoConfigAgent()
    registry.register(first)
    with caplog.at_level("WARNING"):
        registry.register(second)
    assert registry.get_agent("no-config") is second
    assert any("agent_register_duplicate" in rec.message for rec in caplog.records)


def test_get_agent_returns_none_for_unknown():
    assert registry.get_agent("nope") is None


# ---------- discover_agents ----------


def test_discover_agents_missing_directory_returns_zero(tmp_path):
    result = registry.discover_agents(tmp_path / "does-not-exist")
    assert result == 0


def test_discover_agents_empty_directory_returns_zero(tmp_path):
    empty = tmp_path / "empty_agents"
    empty.mkdir()
    assert registry.discover_agents(empty) == 0


def test_discover_agents_skips_underscore_prefixed(tmp_path):
    """``_template`` and friends are scaffolds, not runnable agents."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    tpl = agents_dir / "_template"
    tpl.mkdir()
    (tpl / "__init__.py").write_text("")
    (tpl / "agent.py").write_text(
        textwrap.dedent(
            """
            from app.agents.protocol import AgentProtocol
            class T(AgentProtocol):
                agent_id = "template"
                display_name = "T"
                description = "nope"
            """
        )
    )
    assert registry.discover_agents(agents_dir) == 0
    assert registry.get_agent("template") is None


def test_discover_agents_loads_valid_package(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents_live"
    agents_dir.mkdir()
    pkg = agents_dir / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "agent.py").write_text(
        textwrap.dedent(
            """
            from app.agents.protocol import AgentProtocol, AnalysisResult, InvestigationResult

            class Demo(AgentProtocol):
                agent_id = "demo"
                display_name = "Demo"
                description = "d"

                def input_schema(self):
                    return {"type": "object"}

                async def analyze(self, inp, on_progress):
                    return AnalysisResult(display={}, structured={})

                async def investigate(self, inp, on_progress):
                    return InvestigationResult(status="complete")
            """
        )
    )
    (pkg / "agent.yaml").write_text(
        textwrap.dedent(
            """
            manifest_version: 1
            id: demo
            name: Demo
            runtime: python-package
            phases:
              - name: analyze
              - name: investigate
                approval: true
            output:
              mode: html_report
            """
        )
    )
    # Avoid leaking the dynamically imported module between tests.
    monkeypatch.setitem(sys.modules, "agents_live", None)
    sys.modules.pop("agents_live", None)

    count = registry.discover_agents(agents_dir)
    assert count == 1
    agent = registry.get_agent("demo")
    assert agent is not None
    assert agent.display_name == "Demo"

    # Clean up the ad-hoc package from sys.modules so later tests don't see it.
    for mod in list(sys.modules):
        if mod == "agents_live" or mod.startswith("agents_live."):
            sys.modules.pop(mod, None)


def test_discover_agents_logs_and_continues_on_import_error(tmp_path, caplog):
    agents_dir = tmp_path / "agents_bad"
    agents_dir.mkdir()
    pkg = agents_dir / "broken"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "agent.py").write_text("raise RuntimeError('boom at import time')\n")
    (pkg / "agent.yaml").write_text(
        textwrap.dedent(
            """
            manifest_version: 1
            id: broken
            name: Broken
            runtime: python-package
            phases:
              - name: analyze
            output:
              mode: html_report
            """
        )
    )

    with caplog.at_level("ERROR"):
        count = registry.discover_agents(agents_dir)

    assert count == 0
    # structlog surfaces exc_info via the rendered event dict (see
    # conftest.py _install_structlog_for_tests). We assert on the
    # message text, which is the full event dict serialized.
    discovery_errors = [r for r in caplog.records if "agent_discovery_failed" in r.message]
    assert discovery_errors, "expected an agent_discovery_failed error log"
    assert any("'exc_info': True" in r.getMessage() for r in discovery_errors)
    assert any("RuntimeError" in r.getMessage() for r in discovery_errors)

    for mod in list(sys.modules):
        if mod == "agents_bad" or mod.startswith("agents_bad."):
            sys.modules.pop(mod, None)


# ---------- router: list / input-schema ----------


def test_list_agents_empty_when_none_registered():
    _, client, _, _ = _build_app()
    resp = client.get("/agents")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_agents_shows_registered():
    registry.register(_NoConfigAgent())
    registry.register(_ConfiguredAgent())
    _, client, _, _ = _build_app()
    resp = client.get("/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    by_id = {a["agent_id"]: a for a in body}
    assert by_id["no-config"]["has_config"] is False
    assert by_id["configured"]["has_config"] is True
    assert by_id["configured"]["display_name"] == "Configured Agent"


def test_get_input_schema_404_when_missing():
    _, client, _, _ = _build_app()
    resp = client.get("/agents/nope/input-schema")
    assert resp.status_code == 404


def test_get_input_schema_returns_agent_schema():
    registry.register(_NoConfigAgent())
    _, client, _, _ = _build_app()
    resp = client.get("/agents/no-config/input-schema")
    assert resp.status_code == 200
    assert resp.json() == {"type": "object", "properties": {"q": {"type": "string"}}}


# ---------- router: config surface ----------


def test_get_config_404_when_agent_missing():
    _, client, _, _ = _build_app()
    resp = client.get("/agents/nope/config")
    assert resp.status_code == 404


def test_get_config_404_when_config_meta_none():
    registry.register(_NoConfigAgent())
    _, client, _, _ = _build_app()
    resp = client.get("/agents/no-config/config")
    assert resp.status_code == 404


def test_get_config_returns_serialized_shape(monkeypatch):
    registry.register(_ConfiguredAgent(), _manifest_with_steps())
    _, client, _, _ = _build_app()

    async def _no_overrides(db, tenant_id, agent_id):
        return {}

    monkeypatch.setattr(agents_router.step_configs, "overrides_for", _no_overrides)
    resp = client.get("/agents/configured/config")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"meta", "steps", "settings"}

    assert body["meta"]["supported_providers"] == ["openai", "anthropic"]
    assert body["meta"]["step_editable_fields"] == ["temperature", "max_tokens"]
    # K5a: the agent's config_meta() settings, served in the manifest's
    # settings[] shape (`field_type` is `type`, `enum_options` `options`)
    # and marked as the deprecated path they are.
    assert len(body["meta"]["settings"]) == 2
    assert body["meta"]["settings"][0]["key"] == "depth"
    assert body["meta"]["settings"][0]["type"] == "enum"
    assert body["meta"]["settings"][0]["options"] == ["basic", "advanced"]
    assert body["meta"]["deprecated"] is True

    # The steps are the MANIFEST's, with this tenant's overrides applied
    # (none here) and the overridden fields named so the admin page can
    # tell a local choice from the agent's default.
    assert len(body["steps"]) == 1
    assert body["steps"][0]["step_id"] == "analyze"
    assert body["steps"][0]["model"] == "gpt-4o"
    assert body["steps"][0]["label"] == "Main analysis step"
    assert body["steps"][0]["overridden"] == []

    assert body["settings"] == [
        {"key": "depth", "label": "Search depth", "type": "enum",
         "value": "advanced", "default": "advanced", "overridden": False},
        {"key": "top_k", "label": "Top K", "type": "int",
         "value": 5, "default": 5, "overridden": False},
    ]


def test_put_steps_404_when_missing():
    _, client, _, _ = _build_app()
    resp = client.put("/agents/nope/config/steps", json=[])
    assert resp.status_code == 404


def test_put_steps_writes_the_tenants_row_and_audits_it(monkeypatch):
    """Blueprint S4a: a step edit is this TENANT's row, not a mutation of
    the process-global agent instance — which is what made a second
    tenant's edit overwrite the first."""
    agent = _ConfiguredAgent()
    registry.register(agent, _manifest_with_steps())
    _, client, user, fake_db = _build_app()
    written: list[dict] = []

    async def _apply(db, tenant_id, agent_id, manifest, updates, user_id):
        written.append(
            {
                "tenant_id": tenant_id,
                "agent_id": agent_id,
                "updates": updates,
                "user_id": user_id,
                "declared": [s.id for s in manifest.llm.steps],
            }
        )
        return [u["step_id"] for u in updates]

    monkeypatch.setattr(agents_router.step_configs, "apply_updates", _apply)

    resp = client.put(
        "/agents/configured/config/steps",
        json=[{"step_id": "analyze", "model": "gpt-4o-mini", "temperature": 0.2}],
    )
    assert resp.status_code == 204

    assert len(written) == 1
    assert written[0]["tenant_id"] == user.tenant_id
    assert written[0]["agent_id"] == "configured"
    assert written[0]["user_id"] == user.id
    assert written[0]["declared"] == ["analyze"]

    # Audit row was added + flushed.
    fake_db.add.assert_called_once()
    entry = fake_db.add.call_args.args[0]
    assert entry.action_type == "config_change"
    assert entry.tenant_id == user.tenant_id
    assert entry.detail["surface"] == "agent_config"
    assert entry.detail["agent_id"] == "configured"
    assert entry.detail["section"] == "steps"
    assert entry.detail["changed_step_ids"] == ["analyze"]
    fake_db.flush.assert_awaited()


def test_put_settings_applies_update_and_writes_audit():
    agent = _ConfiguredAgent()
    registry.register(agent)
    _, client, _, fake_db = _build_app()

    resp = client.put(
        "/agents/configured/config/settings",
        json=[{"key": "depth", "value": "basic"}, {"key": "top_k", "value": 12}],
    )
    assert resp.status_code == 204
    assert agent.get_settings() == {"depth": "basic", "top_k": 12}

    entry = fake_db.add.call_args.args[0]
    assert entry.action_type == "config_change"
    assert entry.detail["section"] == "settings"
    assert set(entry.detail["changed_keys"]) == {"depth", "top_k"}


def test_put_steps_refuses_an_override_the_manifest_would_reject():
    """The real service, not a stub: an unusable value must come back as
    a 400 rather than reach the table, where the gateway would give it
    precedence over the manifest and fail every invocation of the step
    until somebody found the row (Codex P2)."""
    registry.register(_ConfiguredAgent(), _manifest_with_steps())
    _, client, _, fake_db = _build_app()

    resp = client.put(
        "/agents/configured/config/steps",
        json=[{"step_id": "analyze", "max_tokens": 0}],
    )

    assert resp.status_code == 400
    assert "max_tokens" in resp.json()["detail"]
    # Nothing was written, and no audit row claims a change.
    fake_db.add.assert_not_called()


def test_put_steps_requires_admin():
    registry.register(_ConfiguredAgent())
    _, client, _, _ = _build_app(admin=False)
    resp = client.put(
        "/agents/configured/config/steps",
        json=[{"step_id": "analyze", "temperature": 0.1}],
    )
    assert resp.status_code == 403


def test_put_settings_requires_admin():
    registry.register(_ConfiguredAgent())
    _, client, _, _ = _build_app(admin=False)
    resp = client.put(
        "/agents/configured/config/settings",
        json={"depth": "basic"},
    )
    assert resp.status_code == 403


# ---------- protocol dataclass smoke ----------


def test_agent_input_and_progress_dataclasses_round_trip():
    inp = AgentInput(
        run_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_inputs={"x": 1},
    )
    assert inp.prior_analysis is None
    assert inp.user_edits is None

    p = StepProgress(step_id="step_0", status="running", detail="loading")
    assert p.status == "running"
