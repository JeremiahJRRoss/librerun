"""OpenInference attributes on PipelineOrchestrator step spans.

These tests drive the orchestrator with a real ``TracerProvider`` +
``InMemorySpanExporter`` so we can assert on the attributes trace viewers will see
in production. The orchestrator's module-level ``_tracer`` is replaced
with one bound to the test provider — the SDK doesn't expose a way to
override the global provider mid-process safely, so swapping the
module-level handle is the cleanest route.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.services import orchestrator as orch_mod
from app.services.orchestrator import PipelineOrchestrator


@pytest.fixture
def exporter(monkeypatch):
    """A TracerProvider + InMemorySpanExporter wired into the orchestrator."""
    provider = TracerProvider()
    exp = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    monkeypatch.setattr(orch_mod, "_tracer", provider.get_tracer("test"))
    yield exp
    provider.shutdown()


def _attrs(exp: InMemorySpanExporter, name: str) -> dict:
    for span in exp.get_finished_spans():
        if span.name == name:
            return dict(span.attributes or {})
    raise AssertionError(
        f"span {name!r} not found in {[s.name for s in exp.get_finished_spans()]}"
    )


def _make_orchestrator(step_kinds=None):
    fake_db = AsyncMock()
    fake_redis = MagicMock()
    fake_redis.hset = AsyncMock()
    fake_redis.delete = AsyncMock()
    # `progress_write` opens a transaction now, and a bare `MagicMock`
    # answers `pipeline()` with something whose `execute()` is not
    # awaitable. The pipeline is spelled out rather than auto-mocked so
    # that `execute()` returns the LIST redis really replies with.
    fake_pipe = MagicMock()
    fake_pipe.hset = MagicMock(return_value=fake_pipe)
    fake_pipe.expire = MagicMock(return_value=fake_pipe)
    fake_pipe.execute = AsyncMock(return_value=[1, True])
    fake_redis.pipeline = MagicMock(return_value=fake_pipe)
    fake_llm = MagicMock()
    fake_llm.get_step_config = MagicMock(
        return_value={"provider": "openai", "model": "gpt-4o"}
    )
    return PipelineOrchestrator(
        fake_db, fake_redis, fake_llm, step_kinds=step_kinds or {}
    )


@pytest.mark.asyncio
async def test_chain_step_has_kind_input_output(exporter):
    orch = _make_orchestrator({"refine_problem_statement": "CHAIN"})
    run_id = uuid.uuid4()

    async def step():
        return {"refined_problem_statement": "ok", "key_signals": ["x"]}

    await orch.run_step(
        run_id,
        "refine_problem_statement",
        step,
        input_payload={"problem": "broken integration"},
    )

    a = _attrs(exporter, "step_refine_problem_statement")
    assert a["openinference.span.kind"] == "CHAIN"
    assert a["input.mime_type"] == "application/json"
    assert json.loads(a["input.value"]) == {"problem": "broken integration"}
    assert a["output.mime_type"] == "application/json"
    assert json.loads(a["output.value"])["refined_problem_statement"] == "ok"
    assert a["status"] == "ok"
    assert a["provider"] == "openai"
    assert a["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_retriever_step_stamps_its_documents_through_kb_stamp(exporter):
    """Blueprint S2: retrieval documents land on the step span because the
    STEP stamps them through the granted ``kb`` capability's helper — the
    chassis knows the shape of its own search results and nothing about
    an agent's. The span is the orchestrator's; the attributes are the
    step's."""
    from app.capabilities import KbCapability

    orch = _make_orchestrator({"search_internal_kb": "RETRIEVER"})
    # No LLM config for retriever-only steps — orchestrator must swallow the
    # KeyError, which would otherwise bubble out and we'd see no span at all.
    orch.llm.get_step_config = MagicMock(side_effect=KeyError())
    run_id = uuid.uuid4()
    kb = KbCapability(uuid.uuid4())

    async def step():
        results = [
            {"url": "https://example/a", "snippet": "alpha snippet", "relevance_score": 0.9},
            {"url": "https://example/b", "snippet": "beta snippet", "relevance_score": 0.7},
        ]
        assert kb.stamp(results) == 2
        return {"results": results, "skipped": False}

    await orch.run_step(
        run_id, "search_internal_kb", step, input_payload={"queries": ["q1"]}
    )

    a = _attrs(exporter, "step_search_internal_kb")
    assert a["openinference.span.kind"] == "RETRIEVER"
    assert a["retrieval.documents.0.document.id"] == "https://example/a"
    assert a["retrieval.documents.0.document.score"] == pytest.approx(0.9)
    assert a["retrieval.documents.0.document.content"] == "alpha snippet"
    assert a["retrieval.documents.1.document.id"] == "https://example/b"
    # Provider/model not stamped because the step has no LLM config.
    assert "provider" not in a


@pytest.mark.asyncio
async def test_orchestrator_no_longer_stamps_documents_it_did_not_fetch(exporter):
    """The pre-S2 orchestrator sniffed the demo agent's result shapes
    (``results``, ``vendor_a_results``, …) and stamped them itself. A
    retriever step that returns such a shape WITHOUT calling ``kb.stamp``
    now gets no document attributes — the chassis guesses at no agent's
    vocabulary (gap A2)."""
    orch = _make_orchestrator({"search_public_resources": "RETRIEVER"})
    orch.llm.get_step_config = MagicMock(side_effect=KeyError())
    run_id = uuid.uuid4()

    async def step():
        return {
            "results": [{"url": "https://a/1", "snippet": "s1", "relevance_score": 0.8}],
            "vendor_a_results": [{"url": "https://a/2", "snippet": "s2", "relevance_score": 0.6}],
        }

    await orch.run_step(run_id, "search_public_resources", step, input_payload={"q": ["x"]})

    a = _attrs(exporter, "step_search_public_resources")
    assert a["openinference.span.kind"] == "RETRIEVER"
    assert not [k for k in a if k.startswith("retrieval.documents.")], a


@pytest.mark.asyncio
async def test_step_without_input_payload_still_has_kind(exporter):
    """Back-compat: callers that don't pass ``input_payload`` keep working."""
    orch = _make_orchestrator({"x": "CHAIN"})
    run_id = uuid.uuid4()

    async def step():
        return {"ok": True}

    await orch.run_step(run_id, "x", step)

    a = _attrs(exporter, "step_x")
    assert a["openinference.span.kind"] == "CHAIN"
    assert "input.value" not in a
    assert "output.value" in a  # output is always set on success


@pytest.mark.asyncio
async def test_unknown_step_falls_back_to_chain_kind(exporter):
    """Steps not in ``step_kinds`` default to CHAIN — never empty."""
    orch = _make_orchestrator({})  # empty map
    run_id = uuid.uuid4()

    async def step():
        return {}

    await orch.run_step(run_id, "uncategorised", step)

    a = _attrs(exporter, "step_uncategorised")
    assert a["openinference.span.kind"] == "CHAIN"


@pytest.mark.asyncio
async def test_error_path_does_not_set_output_value(exporter):
    """Span status carries the error signal — output.value would be misleading."""
    orch = _make_orchestrator({"x": "CHAIN"})
    run_id = uuid.uuid4()

    async def step():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await orch.run_step(run_id, "x", step, input_payload={"foo": "bar"})

    a = _attrs(exporter, "step_x")
    assert a["openinference.span.kind"] == "CHAIN"
    assert a["status"] == "error"
    assert "output.value" not in a
    # Input is still recorded — useful for debugging the failed step.
    assert a["input.value"] == json.dumps({"foo": "bar"})
