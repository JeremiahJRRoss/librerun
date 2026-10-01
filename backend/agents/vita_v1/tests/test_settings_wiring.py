"""The demo agent's three settings reach the run (K5b, D18, D31).

``agent.yaml`` declares ``tavily_search_depth``, ``pinecone_top_k`` and
``max_retries_per_step``. Each phase reads this tenant's values once,
through ``caps.config.settings()``, clamps the two integers, hands them to
the search and LLM services, and stamps what it used on the step spans'
``input.value``. The overlay they used to live in is retired and nothing
reads it: an installation that still holds one is told so once, when the
agent loads.

These drive the real phases — the real steps, normalizers, orchestrator
and report — with the capabilities answered the way the keyless demo
answers them: the model replies with each step's fixture, the internal KB
is not configured, and the Tavily request is recorded instead of sent.
The config double answers through ``agent_settings_service`` — the code
the real façade answers through — so a tenant's value is held to its
declaration exactly as a run's is.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from agents.vita_v1 import agent as vita_agent_mod
from agents.vita_v1 import llm_service
from agents.vita_v1.agent import VitaAgent
from agents.vita_v1.stub_llm import fixture_for
from app.agents.manifest import load_manifest
from app.agents.protocol import AgentInput
from app.capabilities import LlmError
from app.services import agent_settings_service
from app.services import orchestrator as orch_mod

from .test_agent_phase1_wiring import _FakeAudit, _FakeRedis, _FakeRun, _FakeSession

VITA_DIR = Path(vita_agent_mod.__file__).resolve().parent

# What the code hard-coded before K5b: search_service.py sent "advanced"
# and top_k=10, and call_with_retry's max_retries defaulted to 2.
TODAYS_VALUES = {
    "tavily_search_depth": "advanced",
    "pinecone_top_k": 10,
    "max_retries_per_step": 2,
}

# One value each that is not the default, and the step whose span shows
# it. The retry count is read by every LLM step; the one that fails here
# is the first of the analyze phase.
EDITED = {
    "tavily_search_depth": ("basic", "step_search_public_resources"),
    "pinecone_top_k": (7, "step_search_internal_kb"),
    "max_retries_per_step": (4, "step_validate_and_classify_inputs"),
}

# Not shaped like a Tavily key, on purpose: nothing here checks its form.
TAVILY_KEY = "the settings test's search key"


class _Gateway:
    """The ``llm`` capability as the keyless gateway answers it: each
    step's own fixture comes back as the model's reply. ``failing`` names
    a step answered ``503`` every time — the class ``call_with_retry``
    retries — and ``calls`` counts every attempt per step."""

    def __init__(self, failing: str | None = None):
        self.failing = failing
        self.calls: dict[str, int] = {}

    async def complete(self, step, messages, **kwargs):  # noqa: ARG002
        self.calls[step] = self.calls.get(step, 0) + 1
        if step == self.failing:
            raise LlmError(503, "provider_unavailable", "the provider did not answer")
        return {
            "model": "stub",
            "choices": [
                {"message": {"role": "assistant", "content": json.dumps(fixture_for(step))}}
            ],
        }


class _Kb:
    """No internal KB, as in the keyless demo: the search is asked and
    finds nothing, so the step reports itself skipped. ``top_k`` records
    what each search asked for."""

    def __init__(self):
        self.top_k: list[int] = []

    def available(self) -> bool:
        return False

    async def search(self, queries, top_k=10):  # noqa: ARG002
        self.top_k.append(top_k)
        return []

    def stamp(self, documents) -> int:
        return len(documents)


class _Secrets:
    async def get(self, name: str) -> str:
        assert name == "tavily_api_key"
        return TAVILY_KEY


class _Config:
    """This tenant's rows, answered as the façade answers them."""

    def __init__(self, rows: dict):
        self._manifest = load_manifest(VITA_DIR)
        self._rows = dict(rows)
        self.reads = 0

    async def settings(self) -> dict:
        self.reads += 1
        return agent_settings_service.effective_values(self._manifest, self._rows)


class _Caps:
    def __init__(self, case, rows: dict, gateway: _Gateway):
        self.llm = gateway
        self.kb = _Kb()
        self.secrets = _Secrets()
        self.config = _Config(rows)
        self.audit = _FakeAudit()
        self.progress = self
        self._case = case
        self._session = _FakeSession(case)
        self._redis = _FakeRedis()

    def pipeline(self, llm, step_kinds=None):
        from contextlib import asynccontextmanager

        from app.services.orchestrator import PipelineOrchestrator

        @asynccontextmanager
        async def _ctx():
            orch = PipelineOrchestrator(
                self._session, self._redis, llm, step_kinds=step_kinds or {}
            )
            yield self._case, orch
            await self._session.commit()

        return _ctx()


@pytest.fixture
def spans(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(orch_mod, "_tracer", provider.get_tracer("test"))
    yield exporter
    provider.shutdown()


@pytest.fixture
def tavily(monkeypatch):
    """Every Tavily request body, recorded instead of sent."""
    sent: list[dict] = []

    class _Response:
        is_success = True

        def json(self):
            return {"results": []}

    async def _post(self, url, json=None, **kwargs):  # noqa: A002, ARG001
        sent.append(json)
        return _Response()

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    return sent


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """call_with_retry waits 5 s, 10 s, 20 s … between attempts; the count
    is what these read, not the wait."""
    import asyncio

    async def _no_wait(_seconds):
        return None

    monkeypatch.setattr(
        llm_service,
        "asyncio",
        type("_Asyncio", (), {"sleep": staticmethod(_no_wait), "TimeoutError": asyncio.TimeoutError}),
    )


@pytest.fixture
def no_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("LIBRERUN_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


async def _run(phase: str, rows: dict, failing: str | None = None):
    """Run one phase of the demo agent with this tenant's ``rows``."""
    case = _FakeRun()
    gateway = _Gateway(failing)
    caps = _Caps(case, rows, gateway)
    inp = AgentInput(
        run_id=case.id,
        tenant_id=case.tenant_id,
        user_inputs={},
        capabilities=caps,
        prior_analysis={
            "refined_problem": fixture_for("refine_problem_statement"),
            "classified_inputs": fixture_for("validate_and_classify_inputs"),
        },
    )

    async def _no_progress(*_a, **_k):
        return None

    result = await getattr(VitaAgent(), phase)(inp, _no_progress)
    return result, caps, gateway


def _stamped(spans: InMemorySpanExporter, name: str) -> dict:
    for span in spans.get_finished_spans():
        if span.name == name:
            return json.loads(span.attributes["input.value"])
    raise AssertionError(
        f"no span {name!r} in {[s.name for s in spans.get_finished_spans()]}"
    )


def _used(key: str, caps: _Caps, gateway: _Gateway, tavily: list[dict]):
    """What the run did with ``key``: the value at the call it governs."""
    if key == "tavily_search_depth":
        depths = {body["search_depth"] for body in tavily}
        assert len(depths) == 1, f"the Tavily requests disagree: {depths}"
        return depths.pop()
    if key == "pinecone_top_k":
        assert caps.kb.top_k, "the internal-KB search was never asked"
        assert len(set(caps.kb.top_k)) == 1, caps.kb.top_k
        return caps.kb.top_k[0]
    # The first analyze step answered 503 every time: one call, then one
    # more per retry, and then the phase gives up.
    return gateway.calls["validate_and_classify_inputs"] - 1


async def _run_for(key: str, rows: dict):
    """The phase that reads ``key``, run so that the effect shows."""
    if key == "max_retries_per_step":
        result, caps, gateway = await _run(
            "analyze", rows, failing="validate_and_classify_inputs"
        )
        assert result.status == "error"
        return caps, gateway
    result, caps, gateway = await _run("investigate", rows)
    assert result.status == "complete", result.error
    return caps, gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(EDITED))
async def test_each_setting_reaches_the_run(key, spans, tavily, no_overlay):
    """A tenant's edited value is the one the run uses, and the one its
    step span shows — D31: a setting the UI edits changes a run, or it is
    not declared."""
    value, span_name = EDITED[key]
    caps, gateway = await _run_for(key, {key: value})

    assert caps.config.reads == 1, "a phase reads its settings once"
    assert _used(key, caps, gateway, tavily) == value
    assert _stamped(spans, span_name)[key] == value


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(TODAYS_VALUES))
async def test_its_default_is_todays_value(key, spans, tavily, no_overlay):
    """A tenant with no row sees no change: the manifest's default is the
    value the code hard-coded before K5b, and a run with no row uses it."""
    manifest = load_manifest(VITA_DIR)
    (spec,) = [s for s in manifest.settings if s.key == key]
    assert spec.default == TODAYS_VALUES[key]

    caps, gateway = await _run_for(key, {})

    assert _used(key, caps, gateway, tavily) == TODAYS_VALUES[key]
    assert _stamped(spans, EDITED[key][1])[key] == TODAYS_VALUES[key]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,value,clamped",
    [("pinecone_top_k", 500, 50), ("max_retries_per_step", -3, 0)],
)
async def test_an_out_of_range_int_is_clamped(key, value, clamped, spans, tavily, no_overlay):
    """``settings[]`` declares no bounds, so the agent clamps where it
    reads — ``top_k`` to 1–50, as the MCP ``kb_search`` tool does, and the
    retry count to 0–5, since a negative one would call no model at all —
    and the clamped value is the one used and the one stamped."""
    caps, gateway = await _run_for(key, {key: value})

    assert _used(key, caps, gateway, tavily) == clamped
    assert _stamped(spans, EDITED[key][1])[key] == clamped


def _overlay_lines(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if "vita_legacy_settings_overlay" in r.getMessage()
    ]


@pytest.mark.asyncio
async def test_a_legacy_overlay_is_named_once_at_load(tmp_path, monkeypatch, caplog, spans, tavily):
    """D18: the overlay's values are not carried over. An installation
    that still holds one is told so when the agent loads — one line,
    naming the file and the Settings tab — and a run neither reads the
    file nor says it again."""
    state = tmp_path / "state"
    overlay = state / "agents" / "vita_v1" / "config.json"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(json.dumps({"search": {"tavily": {"search_depth": "basic"}}}))
    monkeypatch.setenv("LIBRERUN_STATE_DIR", str(state))

    with caplog.at_level(logging.WARNING):
        agent = VitaAgent()  # what the registry does once per discovery
        loaded = _overlay_lines(caplog)
        caplog.clear()
        case = _FakeRun()
        caps = _Caps(case, {}, _Gateway())
        inp = AgentInput(
            run_id=case.id,
            tenant_id=case.tenant_id,
            user_inputs={},
            capabilities=caps,
            prior_analysis={
                "refined_problem": fixture_for("refine_problem_statement"),
                "classified_inputs": fixture_for("validate_and_classify_inputs"),
            },
        )

        async def _no_progress(*_a, **_k):
            return None

        result = await agent.investigate(inp, _no_progress)
        during_run = _overlay_lines(caplog)

    assert len(loaded) == 1, loaded
    assert str(overlay) in loaded[0]
    assert "Settings tab" in loaded[0]
    assert during_run == []
    # The overlay said "basic"; the run searched with the default.
    assert result.status == "complete", result.error
    assert {body["search_depth"] for body in tavily} == {"advanced"}


def test_no_overlay_logs_nothing(no_overlay, caplog):
    """No file, no line: a fresh installation's log says nothing about a
    store it never had."""
    with caplog.at_level(logging.DEBUG):
        VitaAgent()
    assert _overlay_lines(caplog) == []
    assert not no_overlay.exists()
