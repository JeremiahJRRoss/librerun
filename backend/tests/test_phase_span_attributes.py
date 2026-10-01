"""OpenInference attributes on agent_runner phase spans.

Two things being verified together:

1. The phase root span carries ``openinference.span.kind=CHAIN`` plus
   ``input.value`` and ``output.value`` so the trace tree shows kind
   badges and input/output previews.
2. ``session.id`` and ``user.id`` from the surrounding ``using_attributes``
   block actually land on the phase span. This is the regression guard
   for the "context-set attributes don't auto-attach to manual spans"
   gotcha — without ``using_attributes`` wrapping
   ``start_as_current_span`` (and not the other way around), the
   ``get_attributes_from_context`` re-merge silently returns nothing and
   Sessions/Users views in the trace viewer don't show phase rows.
"""
from __future__ import annotations

import json
import uuid

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.agents import registry
from app.agents.protocol import AgentInput, AgentProtocol, AnalysisResult, InvestigationResult
from app.services import agent_runner


# Reuse the lightweight in-memory stubs from test_agent_runner — they are
# defined in that module's test file as private helpers, so we import via
# ``from .test_agent_runner import ...``. That keeps the test fixture
# surface in one place and avoids divergence.
from tests.test_agent_runner import (  # noqa: E402
    _FakeRun,
    _FakeRedis,
    _FakeSession,
    _FakeSnapshot,
)


@pytest.fixture
def exporter(monkeypatch):
    provider = TracerProvider()
    exp = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    monkeypatch.setattr(agent_runner, "_tracer", provider.get_tracer("test"))
    yield exp
    provider.shutdown()


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


@pytest.fixture
def patch_runner(monkeypatch):
    """Same fake-session pattern as test_agent_runner."""
    state: dict = {"session": None, "redis": _FakeRedis()}

    def _session_factory():
        return state["session"]

    async def _get_redis():
        return state["redis"]

    def install(session: _FakeSession):
        state["session"] = session

    monkeypatch.setattr(agent_runner, "async_session", _session_factory)
    monkeypatch.setattr(agent_runner, "get_redis", _get_redis)
    return install


def _attrs(exp: InMemorySpanExporter, name: str) -> dict:
    for span in exp.get_finished_spans():
        if span.name == name:
            return dict(span.attributes or {})
    raise AssertionError(
        f"span {name!r} not found in {[s.name for s in exp.get_finished_spans()]}"
    )


class _StubAnalyzer(AgentProtocol):
    agent_id = "vita-v1"
    display_name = "v"
    description = "d"

    async def analyze(self, inp, on_progress):  # noqa: ARG002
        return AnalysisResult(
            display={"refined_problem_statement": "rp"},
            structured={
                "refined_problem": {"refined_problem_statement": "rp"},
                "classified_inputs": {"valid": True},
            },
            status="awaiting_approval",
        )

    async def investigate(self, inp, on_progress):  # noqa: ARG002
        return InvestigationResult(
            status="complete",
            report_html="<p>rendered</p>",
            structured={"resolution_plan": {}, "followup_questions": []},
        )


@pytest.mark.asyncio
async def test_phase1_span_has_kind_input_output_and_session(exporter, patch_runner):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    run = _FakeRun(
        run_id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        user_inputs={"vendor_a_name": "Foo", "vendor_b_name": "Bar"},
    )
    session = _FakeSession(run)
    patch_runner(session)
    registry.register(_StubAnalyzer())

    await agent_runner.start_run(run_id, tenant_id, "vita-v1")

    a = _attrs(exporter, "analyze")
    assert a["openinference.span.kind"] == "CHAIN"
    # Session/user re-merged from using_attributes via OTEL Context.
    # Without the inverted nesting, these would be missing.
    assert a["session.id"] == str(run_id)
    assert a["user.id"] == str(user_id)
    # Input is the case's user_inputs blob, JSON-encoded.
    assert a["input.mime_type"] == "application/json"
    assert json.loads(a["input.value"]) == {
        "vendor_a_name": "Foo",
        "vendor_b_name": "Bar",
    }
    # Output stamped on success.
    assert a["output.mime_type"] == "application/json"
    out = json.loads(a["output.value"])
    assert out["status"] == "awaiting_approval"
    # Existing identifying attrs still present.
    assert a["run_id"] == str(run_id)
    assert a["agent_id"] == "vita-v1"
    assert a["phase"] == "analyze"


@pytest.mark.asyncio
async def test_phase1_edit_span_has_kind_and_user_edits_size(
    exporter, patch_runner
):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    run = _FakeRun(run_id=run_id, tenant_id=tenant_id, user_id=user_id)
    session = _FakeSession(run)
    session.snapshot = _FakeSnapshot(run_id, tenant_id)
    session.snapshot.analysis = {
        "refined_problem": {"refined_problem_statement": "old"},
        "classified_inputs": {"valid": True},
    }
    patch_runner(session)
    registry.register(_StubAnalyzer())

    await agent_runner.rerun_current_phase(run_id, "edited content!", "vita-v1")

    a = _attrs(exporter, "analyze_edit")
    assert a["openinference.span.kind"] == "CHAIN"
    assert a["session.id"] == str(run_id)
    assert a["user.id"] == str(user_id)
    # Free-form edit text is recorded by length, not contents (PII).
    assert a["user_edits.chars"] == len("edited content!")
    assert "output.value" in a


@pytest.mark.asyncio
async def test_phase2_span_has_kind_input_output_and_session(
    exporter, patch_runner
):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    run = _FakeRun(run_id=run_id, tenant_id=tenant_id, user_id=user_id)
    session = _FakeSession(run)
    session.snapshot = _FakeSnapshot(run_id, tenant_id)
    session.snapshot.analysis = {
        "refined_problem": {"refined_problem_statement": "rp"},
        "classified_inputs": {"valid": True},
    }
    patch_runner(session)
    registry.register(_StubAnalyzer())

    await agent_runner.resume_run(run_id, tenant_id, "vita-v1")

    a = _attrs(exporter, "investigate")
    assert a["openinference.span.kind"] == "CHAIN"
    assert a["session.id"] == str(run_id)
    assert a["user.id"] == str(user_id)
    assert a["phase"] == "investigate"
    # Output captures the keys of the structured payload + report size,
    # not the report HTML itself (would be too large + PII-ish).
    out = json.loads(a["output.value"])
    assert out["status"] == "complete"
    assert "structured_keys" in out
    assert out["report_chars"] == len("<p>rendered</p>")
