"""One trace per run across the approval gate (blueprint S4, promise 3).

The runner parents every phase span on the run's persisted root: a gated
run keeps one trace id before and after approval, the resumed phase's
parent is the root span the submission opened (asserted on the exported
spans' parent ids), a rerun sits under the same root, ``trace_id`` is
never changed by a later phase, the final phase's span id is still
persisted for feedback deep links, and a row from before the root
existed — or with a pair that cannot be parsed — gets one minted at its
next phase. Vendor ``tracestate`` persisted with the root shows on the
post-approval phase's context.
"""
from __future__ import annotations

import logging
import uuid

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.agents import registry
from app.agents.protocol import AgentProtocol, AnalysisResult, InvestigationResult
from app.observability import run_trace
from app.services import agent_runner

from tests.test_agent_runner import (  # noqa: E402
    _FakeRun,
    _FakeSession,
    _FakeSnapshot,
    patch_runner,  # noqa: F401  (fixture)
)

UPSTREAM_TRACE = "4bf92f3577b34da6a2ce929d0e0e4736"
UPSTREAM_SPAN = "00f067aa0ba902b7"


@pytest.fixture
def exporter(monkeypatch):
    provider = TracerProvider(sampler=run_trace.RunRootSampler())
    exp = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(agent_runner, "_tracer", tracer)
    monkeypatch.setattr(run_trace, "_tracer", tracer)
    yield exp
    provider.shutdown()


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


def _find_span(exp: InMemorySpanExporter, name: str):
    for span in exp.get_finished_spans():
        if span.name == name:
            return span
    raise AssertionError(
        f"span {name!r} not found in {[s.name for s in exp.get_finished_spans()]}"
    )


class _StubAgent(AgentProtocol):
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
            report_html="<p>r</p>",
            structured={},
        )


def _make_run(**overrides):
    run_id = overrides.pop("run_id", uuid.uuid4())
    tenant_id = overrides.pop("tenant_id", uuid.uuid4())
    user_id = overrides.pop("user_id", uuid.uuid4())
    run = _FakeRun(run_id=run_id, tenant_id=tenant_id, user_id=user_id)
    for k, v in overrides.items():
        setattr(run, k, v)
    return run


def _submit(run, tracestate=None):
    """What ``create_run`` does: open the root, persist its pair."""
    upstream = None
    if tracestate is not None:
        upstream = run_trace.upstream_context(
            {
                "traceparent": f"00-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-01",
                "tracestate": tracestate,
            }
        )
    with run_trace.root_span(
        upstream=upstream,
        run_id=run.id,
        run_number=run.run_number,
        agent_id="vita-v1",
        tenant_id=run.tenant_id,
    ) as root:
        assert run_trace.persist_root(run, root)
    return root.get_span_context()


@pytest.mark.asyncio
async def test_gated_run_is_one_trace_before_and_after_approval(exporter, patch_runner):
    run = _make_run()
    root_sc = _submit(run)
    trace_hex = format(root_sc.trace_id, "032x")
    root_span_hex = format(root_sc.span_id, "016x")
    assert run.trace_id == trace_hex
    # Sampled by the run-plane sampler; the random-trace-id bit rides
    # along because the chassis generated this trace id.
    assert run.root_traceparent == f"00-{trace_hex}-{root_span_hex}-03"

    session = _FakeSession(run)
    patch_runner(session)
    registry.register(_StubAgent())

    await agent_runner.start_run(run.id, run.tenant_id, "vita-v1")
    assert run.status == "awaiting_approval"
    first = _find_span(exporter, "analyze")
    assert format(first.context.trace_id, "032x") == trace_hex
    assert format(first.parent.span_id, "016x") == root_span_hex
    assert first.links == ()

    # The gate: a separate background task, however long later.
    await agent_runner.resume_run(run.id, run.tenant_id, "vita-v1")
    assert run.status == "complete"
    second = _find_span(exporter, "investigate")
    assert format(second.context.trace_id, "032x") == trace_hex
    assert format(second.parent.span_id, "016x") == root_span_hex
    assert second.links == ()

    # The lookup column never changed; the final span id is persisted.
    assert run.trace_id == trace_hex
    assert run.root_traceparent == f"00-{trace_hex}-{root_span_hex}-03"
    assert run.phase2_span_id == format(second.context.span_id, "016x")
    # Exactly one root, ended before either phase started.
    roots = [s for s in exporter.get_finished_spans() if s.name == "run"]
    assert len(roots) == 1
    assert roots[0].end_time <= first.start_time


@pytest.mark.asyncio
async def test_vendor_tracestate_survives_the_gate_onto_the_resumed_phase(
    exporter, patch_runner
):
    run = _make_run()
    _submit(run, tracestate="vendor=abc,rojo=1")
    assert run.root_tracestate == "vendor=abc,rojo=1"
    assert run.trace_id == UPSTREAM_TRACE
    session = _FakeSession(run)
    session.snapshot = _FakeSnapshot(run.id, run.tenant_id)
    session.snapshot.analysis = {"refined_problem": {}, "classified_inputs": {}}
    session.run = run
    run.current_phase = "analyze"
    patch_runner(session)
    registry.register(_StubAgent())

    await agent_runner.resume_run(run.id, run.tenant_id, "vita-v1")

    span = _find_span(exporter, "investigate")
    assert format(span.context.trace_id, "032x") == UPSTREAM_TRACE
    assert span.context.trace_state.to_header() == "vendor=abc,rojo=1"
    root = _find_span(exporter, "run")
    assert format(root.parent.span_id, "016x") == UPSTREAM_SPAN


@pytest.mark.asyncio
async def test_rerun_sits_under_the_same_root(exporter, patch_runner):
    run = _make_run(current_phase="analyze")
    root_sc = _submit(run)
    session = _FakeSession(run)
    session.snapshot = _FakeSnapshot(run.id, run.tenant_id)
    session.snapshot.analysis = {
        "refined_problem": {"refined_problem_statement": "old"},
        "classified_inputs": {"valid": True},
    }
    patch_runner(session)
    registry.register(_StubAgent())

    await agent_runner.rerun_current_phase(run.id, "edited!", "vita-v1")

    span = _find_span(exporter, "analyze_edit")
    assert span.context.trace_id == root_sc.trace_id
    assert span.parent.span_id == root_sc.span_id
    assert span.links == ()
    assert run.trace_id == format(root_sc.trace_id, "032x")


@pytest.mark.asyncio
async def test_a_row_from_before_the_root_gets_one_minted_at_its_next_phase(
    exporter, patch_runner, caplog
):
    """A run parked across the upgrade: no pair, an old-style trace id."""
    run = _make_run(current_phase="analyze", trace_id="a" * 32)
    session = _FakeSession(run)
    session.snapshot = _FakeSnapshot(run.id, run.tenant_id)
    session.snapshot.analysis = {"refined_problem": {}, "classified_inputs": {}}
    patch_runner(session)
    registry.register(_StubAgent())

    with caplog.at_level(logging.INFO):
        await agent_runner.resume_run(run.id, run.tenant_id, "vita-v1")

    root = _find_span(exporter, "run")
    span = _find_span(exporter, "investigate")
    assert root.parent is None
    assert root.attributes["librerun.run.root_minted_late"] is True
    assert span.parent.span_id == root.context.span_id
    assert run.trace_id == format(root.context.trace_id, "032x")
    assert run.root_traceparent.startswith(f"00-{run.trace_id}-")
    assert "run_root_minted_late" in caplog.text
    # The minted root belongs to the run plane: it was opened inside the
    # runner's bound context, as the enricher would stamp it.
    assert "run_trace_id_changed" not in caplog.text


@pytest.mark.asyncio
async def test_an_unparseable_persisted_pair_is_replaced_not_fatal(
    exporter, patch_runner, caplog
):
    run = _make_run(
        current_phase="analyze",
        root_traceparent="not-a-traceparent",
        root_tracestate="also-not",
    )
    session = _FakeSession(run)
    session.snapshot = _FakeSnapshot(run.id, run.tenant_id)
    session.snapshot.analysis = {"refined_problem": {}, "classified_inputs": {}}
    patch_runner(session)
    registry.register(_StubAgent())

    with caplog.at_level(logging.WARNING):
        await agent_runner.resume_run(run.id, run.tenant_id, "vita-v1")

    assert run.status == "complete"
    assert "run_root_unparseable" in caplog.text
    root = _find_span(exporter, "run")
    span = _find_span(exporter, "investigate")
    assert span.parent.span_id == root.context.span_id
    assert run_trace.parse_traceparent(run.root_traceparent) is not None
    assert run.root_tracestate is None


@pytest.mark.asyncio
async def test_tracing_off_means_no_pair_and_no_error(patch_runner, monkeypatch):
    from opentelemetry import trace as otel_trace

    monkeypatch.setattr(run_trace, "_tracer", otel_trace.NoOpTracer())
    monkeypatch.setattr(agent_runner, "_tracer", otel_trace.NoOpTracer())
    run = _make_run()
    session = _FakeSession(run)
    patch_runner(session)
    registry.register(_StubAgent())

    await agent_runner.start_run(run.id, run.tenant_id, "vita-v1")

    assert run.status == "awaiting_approval"
    assert run.root_traceparent is None
    assert run.trace_id is None
    assert run.phase2_span_id is None
