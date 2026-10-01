"""``caps.config`` — the run's own configuration, under no grant (K5a, K5-05).

The façade knew no agent (§1.1, T19): ``for_run()`` took a tenant and a
grant list, so nothing on it could read the agent's own configuration,
and every member it had sat behind ``__getattr__``'s grant check. A
setting is the agent's own data — its manifest declares it — so reading
one is no platform capability: ``config`` is a plain attribute, outside
the members the check guards and outside ``KNOWN_CAPABILITIES``, and
``for_run()`` requires the ``agent_id`` whose configuration it reads.

The database reads are stubbed here; ``test_gate_t.py`` holds them to
PostgreSQL and to the run's tenant.
"""
from __future__ import annotations

import contextlib
import uuid

import pytest

from app import capabilities as caps_mod
from app.agents import registry
from app.agents.manifest import AgentManifest
from app.agents.protocol import AgentProtocol, AnalysisResult
from app.capabilities import KNOWN_CAPABILITIES, CapabilityNotGranted, ConfigCapability
from app.services import agent_runner, agent_settings_service
from tests.test_agent_runner import (  # noqa: F401  (fixture)
    _FakeRun,
    _FakeSession,
    patch_runner,
)

AGENT = "probe-config"


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


SETTINGS = (
    {"key": "note", "type": "string", "default": "the default"},
    {"key": "limit", "type": "int", "default": 3},
)


def manifest(capabilities=(), settings=SETTINGS) -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "id": AGENT,
            "name": "Probe",
            "runtime": "python-package",
            "phases": [{"name": "analyze"}],
            "output": {"mode": "structured"},
            "capabilities": list(capabilities),
            "settings": list(settings),
        }
    )


def facade(grants, *, tenant_id=None):
    return caps_mod.for_run(
        run_id=uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        agent_id=AGENT,
        grants=grants,
    )


def test_config_needs_no_grant():
    caps = facade([])

    assert isinstance(caps.config, ConfigCapability)
    # Every capability still sits behind its grant...
    with pytest.raises(CapabilityNotGranted):
        caps.kb
    with pytest.raises(CapabilityNotGranted):
        caps.llm
    # ...and config is none of them: not a member __getattr__ guards, not
    # a name a manifest could grant, not reported as granted.
    assert "config" not in caps.__dict__["_members"]
    assert "config" not in KNOWN_CAPABILITIES
    assert caps.granted("config") is False
    assert caps.agent_id == AGENT


def test_for_run_requires_the_agent():
    """A façade that knew no agent could only answer with another's."""
    with pytest.raises(TypeError, match="agent_id"):
        caps_mod.for_run(run_id=uuid.uuid4(), tenant_id=uuid.uuid4(), grants=[])


@pytest.fixture
def stubbed_reads(monkeypatch):
    """The two reads ``config`` makes, recorded, over a stored dict the
    test can change between calls."""
    import app.database
    from app.services import agent_settings_service, agent_step_config_service

    state = {"stored": {}, "reads": []}

    @contextlib.asynccontextmanager
    async def _session():
        yield object()

    async def _values_for(db, tenant_id, agent_id):
        state["reads"].append(("settings", tenant_id, agent_id))
        return dict(state["stored"])

    async def _overrides_for(db, tenant_id, agent_id):
        state["reads"].append(("steps", tenant_id, agent_id))
        return {}

    monkeypatch.setattr(app.database, "async_session", _session)
    monkeypatch.setattr(agent_settings_service, "values_for", _values_for)
    monkeypatch.setattr(agent_step_config_service, "overrides_for", _overrides_for)
    return state


@pytest.mark.asyncio
async def test_settings_are_read_at_call_time_for_the_runs_agent_and_tenant(stubbed_reads):
    registry.register(_Recorder(), manifest())
    tenant_id = uuid.uuid4()
    caps = facade([], tenant_id=tenant_id)

    assert await caps.config.settings() == {"note": "the default", "limit": 3}
    # An admin saves between two reads: the second read sees it.
    stubbed_reads["stored"] = {"limit": 9}
    assert await caps.config.settings() == {"note": "the default", "limit": 9}
    assert await caps.config.steps() == []
    assert stubbed_reads["reads"] == [
        ("settings", tenant_id, AGENT),
        ("settings", tenant_id, AGENT),
        ("steps", tenant_id, AGENT),
    ]


@pytest.mark.asyncio
async def test_a_run_that_changes_what_it_read_changes_no_default(stubbed_reads):
    """A ``string_list`` default is a list on the manifest the registry
    holds for the life of the worker: what ``settings()`` hands a run is
    the run's to change, and the default every later run reads is not."""
    declared = manifest(
        settings=[{"key": "tags", "type": "string_list", "default": ["a"]}]
    )
    registry.register(_Recorder(), declared)
    caps = facade([])

    # Each way a run is handed the default: no row, a row the declaration
    # no longer accepts, and a row equal to the default.
    for stored in ({}, {"tags": 5}, {"tags": ["a"]}):
        stubbed_reads["stored"] = stored
        (await caps.config.settings())["tags"].append("the run's own")

    assert declared.setting("tags").default == ["a"]
    assert await caps.config.settings() == {"tags": ["a"]}
    # The admin page's copy of the default is its own as well.
    (entry,) = agent_settings_service.effective(declared, {})
    entry["default"].append("the page's own")
    assert declared.setting("tags").default == ["a"]


class _Recorder(AgentProtocol):
    agent_id = AGENT
    display_name = "Probe"
    description = "records the façade it is handed"

    def __init__(self):
        self.seen = []

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def analyze(self, inp, on_progress):
        self.seen.append(inp.capabilities)
        return AnalysisResult(
            display={},
            structured={"settings": await inp.capabilities.config.settings()},
        )


@pytest.mark.asyncio
async def test_the_runner_hands_an_agent_its_own_config_with_no_grant(
    patch_runner, stubbed_reads  # noqa: F811
):
    """The runner builds the façade with the run's agent (``manifest.id``)
    and tenant, and an agent that grants nothing reads its settings."""
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(run_id=run_id, tenant_id=tenant_id)
    session = _FakeSession(run)
    patch_runner(session)
    agent = _Recorder()
    registry.register(agent, manifest(capabilities=()))

    await agent_runner.start_run(run_id, tenant_id, AGENT)

    assert run.status == "complete", run.status
    (caps,) = agent.seen
    assert (caps.agent_id, caps.tenant_id) == (AGENT, tenant_id)
    assert session.snapshot.structured_data == {
        "settings": {"note": "the default", "limit": 3}
    }
    assert stubbed_reads["reads"] == [("settings", tenant_id, AGENT)]
