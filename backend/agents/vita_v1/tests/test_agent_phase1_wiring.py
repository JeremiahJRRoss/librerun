"""End-to-end smoke for ``VitaAgent.analyze``.

The step modules are patched so the LLM never runs; we only verify that
the agent assembles ``AnalysisResult`` correctly for both the happy and
rejected paths, including the drift bookkeeping convention (structured
``_drifts`` key) and the blocked-request audit write.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.vita_v1 import agent as vita_agent_mod
from agents.vita_v1.agent import VitaAgent
from agents.vita_v1.normalizers import SchemaDriftReport
from app.agents.protocol import AgentInput


class _FakePipeline:
    """`progress_write` writes the field and the key's expiry as one
    transaction, so a double that models only `hset` no longer stands in
    for redis. Applies what it is queued and records the ttl."""

    def __init__(self, parent: "_FakeRedis") -> None:
        self._parent = parent
        self._queued: list = []

    def hset(self, key, field, value):
        self._queued.append(("hset", key, field, value))
        return self

    def expire(self, key, ttl):
        self._queued.append(("expire", key, ttl))
        return self

    async def execute(self) -> list:
        replies = []
        for command in self._queued:
            if command[0] == "hset":
                _, key, field, value = command
                self._parent.hashes.setdefault(key, {})[field] = value
                replies.append(1)
            else:
                _, key, ttl = command
                self._parent.ttls[key] = ttl
                replies.append(True)
        self._queued = []
        return replies


class _FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}

    def pipeline(self, transaction: bool = False) -> _FakePipeline:
        assert transaction, "run_hash_write must ask for a transaction"
        return _FakePipeline(self)

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def delete(self, *keys):
        for key in keys:
            self.hashes.pop(key, None)


class _FakeRun:
    def __init__(self):
        self.id = uuid.uuid4()
        self.tenant_id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.run_number = "C-1"
        self.vendor_a_name = "A"
        self.vendor_a_product = None
        self.vendor_a_feature = None
        self.vendor_a_observation = None
        self.vendor_b_name = "B"
        self.vendor_b_product = None
        self.vendor_b_feature = None
        self.vendor_b_observation = None
        self.logs_a = None
        self.logs_b = None
        self.use_case = "u"
        self.problem_statement = "p"
        self.impact_statement = None
        self.severity = None


class _FakeSession:
    def __init__(self, case: _FakeRun):
        self.case = case
        self.added = []
        self.commits = 0
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, key):
        if key == self.case.id:
            return self.case
        return None

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rolled_back = True

    def begin_nested(self):
        session = self

        class _Ctx:
            async def __aenter__(self_inner):
                return session

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


class _FakeKb:
    """Internal-KB double: available() False keeps phase 1 off the wire."""

    def available(self) -> bool:
        return False

    async def search(self, queries, top_k=10):  # noqa: ARG002
        return []


class _FakeAudit:
    """B13: the agent audits through the capability, not the session."""

    def __init__(self):
        self.drift_batches: list[list] = []
        self.rows: list[tuple[str, dict]] = []

    async def log_schema_drift(self, reports):
        self.drift_batches.append(list(reports))

    async def log(self, action_type, detail):
        # Same signature as the real capability (S4): attribution is the
        # chassis's, never an argument.
        self.rows.append((action_type, detail))


class _FakeConfig:
    """The run's configuration (K5b): each phase reads its settings here
    once, and a tenant with no row reads the manifest's defaults — built
    by the service the real façade answers from, not copied."""

    def __init__(self):
        from app.agents.manifest import load_manifest
        from app.services import agent_settings_service

        manifest = load_manifest(Path(vita_agent_mod.__file__).resolve().parent)
        self.values = agent_settings_service.effective_values(manifest, {})
        self.reads = 0

    async def settings(self):
        self.reads += 1
        return dict(self.values)


class _FakeCaps:
    """Capability façade double: a REAL PipelineOrchestrator wired onto
    the fake session/redis (that wiring is what these tests exercise),
    plus a recording audit capability."""

    def __init__(self, case, session, redis):
        from app.capabilities import LlmCapability

        self.audit = _FakeAudit()
        # B13: VITA reaches kb/llm through the GRANTED façade, so the
        # double has to expose them the way the real one does.
        self.llm = LlmCapability()
        self.kb = _FakeKb()
        # K5b: the settings, which need no grant, as on the real façade.
        self.config = _FakeConfig()
        self._run = case
        self._session = session
        self._redis = redis
        self.progress = self

    def pipeline(self, llm, step_kinds=None):
        from contextlib import asynccontextmanager

        from app.services.orchestrator import PipelineOrchestrator

        case, session, redis = self._run, self._session, self._redis

        @asynccontextmanager
        async def _ctx():
            orch = PipelineOrchestrator(
                session, redis, llm, step_kinds=step_kinds or {}
            )
            yield case, orch
            await session.commit()

        return _ctx()


@pytest.fixture
def patch_vita(monkeypatch):
    case = _FakeRun()
    session = _FakeSession(case)
    redis = _FakeRedis()
    caps = _FakeCaps(case, session, redis)
    return case, session, redis, caps


@pytest.mark.asyncio
async def test_vita_analyze_happy_path(monkeypatch, patch_vita):
    case, session, _redis, caps = patch_vita

    # Intercept the step modules by patching what VitaAgent.analyze imports.
    import agents.vita_v1.steps.step_0_validate as s0
    import agents.vita_v1.steps.step_2_refine as s2
    import agents.vita_v1.llm_service as llm_mod

    fake_llm = SimpleNamespace(
        get_step_config=lambda step_id: {"provider": "openai", "model": "gpt-4o"}
    )
    monkeypatch.setattr(llm_mod, "for_run", lambda *_a, **_k: fake_llm)

    async def _s0(llm, c):  # noqa: ARG001
        return (
            {"valid": True, "scope": "vendor_interop"},
            [SchemaDriftReport("validate_and_classify_inputs", "openai", "gpt-4o", "none")],
        )

    async def _s2(llm, c, classified, customer_edit=None):  # noqa: ARG001
        return (
            {
                "refined_problem_statement": "Canonical refined",
                "key_signals": ["sig1"],
                "suspected_root_causes": ["cause1"],
                "research_focus_areas": ["area1"],
            },
            [],
        )

    monkeypatch.setattr(s0, "run", _s0)
    monkeypatch.setattr(s2, "run", _s2)

    agent = VitaAgent()
    inp = AgentInput(
        run_id=case.id,
        tenant_id=case.tenant_id,
        user_inputs={"vendor_a_name": "A"},
        capabilities=caps,
    )
    result = await agent.analyze(inp, on_progress=AsyncMock())

    assert result.status == "awaiting_approval"
    # K5b: the phase reads its settings once, when it starts.
    assert caps.config.reads == 1
    assert result.structured["refined_problem"]["refined_problem_statement"] == "Canonical refined"
    assert result.structured["classified_inputs"]["valid"] is True
    # One drift from step 0.
    assert len(result.structured["_drifts"]) == 1
    assert session.commits >= 1


@pytest.mark.asyncio
async def test_vita_analyze_rejection_path(monkeypatch, patch_vita):
    case, session, _redis, caps = patch_vita

    import agents.vita_v1.steps.step_0_validate as s0
    import agents.vita_v1.llm_service as llm_mod

    fake_llm = SimpleNamespace(
        get_step_config=lambda step_id: {"provider": "openai", "model": "gpt-4o"}
    )
    monkeypatch.setattr(llm_mod, "for_run", lambda *_a, **_k: fake_llm)

    async def _s0(llm, c):  # noqa: ARG001
        return (
            {
                "valid": False,
                "rejection_reason": "out_of_scope",
            },
            [],
        )

    monkeypatch.setattr(s0, "run", _s0)

    agent = VitaAgent()
    inp = AgentInput(
        run_id=case.id,
        tenant_id=case.tenant_id,
        user_inputs={"vendor_a_name": "A"},
        capabilities=caps,
    )
    result = await agent.analyze(inp, on_progress=AsyncMock())

    assert result.status == "blocked"
    assert result.structured["classified_inputs"]["valid"] is False
    assert result.display["rejected"] is True
    # The agent writes a blocked_request audit row via the capability.
    assert any(action == "blocked_request" for action, _ in caps.audit.rows)
