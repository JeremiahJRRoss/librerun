"""The example agent's OWN emissions go through the platform's pipeline
(blueprint S4 Accept; gaps H9, H10, J3).

``test_logging_queue.py`` proves the chassis walks what reaches it. That
is the easier half. The harder half is whether an *agent* — code the
platform did not write, running in the platform's process — can emit
something the platform never sees: a span attribute set on its own
tracer, a ``logging`` line, a ``print()``, a write to ``sys.stderr``, a
rendered traceback, or a log handler of its own attached mid-run.

So the assertions here drive the shipped LangGraph example rather than a
test double. Its ``probe`` node exists for exactly this: switches in the
input that make it behave like the agent you are worried about. A
chassis that stopped walking one of these positions fails here, on real
agent code, not on a mock of it.

Skips without LangGraph, which is an adapter-side dependency the chassis
does not ship.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app import logging_queue
from app.logging_context import log_context

# The queue-only pipeline against a temp JSONL file, and its readers.
from tests.test_logging_queue import (  # noqa: F401  (imported for the fixture)
    FIXTURE_EMAIL,
    _by_event,
    _records,
    queue_log,
)

pytest.importorskip(
    "langgraph",
    reason="langgraph is an adapter-side dependency; the chassis does not ship it",
)

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "agents" / "_examples" / "langgraph_triage"
PLACEHOLDER = "[REDACTED_EMAIL_ADDRESS_1]"


def _example():
    sys.path.insert(0, str(EXAMPLE_DIR.parent))
    from langgraph_triage.agent import TriageState, _triage_graph, probe  # noqa: F401

    return _triage_graph()


def _inputs(**switches) -> dict:
    return {
        "user_inputs": {
            "title": "Checkout is down",
            "service": "checkout-api",
            "description": "Every request fails.",
            "probe": {"text": FIXTURE_EMAIL, **switches},
        }
    }


@pytest.mark.asyncio
async def test_the_examples_own_log_print_stderr_and_traceback_are_walked(queue_log):
    """Everything the example writes reaches the sink with the placeholder.

    One invocation, five emission routes: ``logging.info``, ``print()``,
    a ``sys.stderr`` write, a ``logging.exception()`` traceback, and the
    attempt to attach a handler. The address appears in none of them.
    """
    graph = _example()
    logging_queue.install_stdio_capture(fd_belt=False)
    try:
        with log_context(agent_id="langgraph-triage", run_id="run-probe"):
            state = await graph.ainvoke(
                _inputs(log=True, print=True, stderr=True, exception=True, attach_handler=True)
            )
    finally:
        logging_queue._restore_stdio(logging_queue._STATE.stdio)
        logging_queue._STATE.stdio = None

    # What the agent believes it did.
    assert state["structured"]["probed"] == [
        "log", "print", "stderr", "exception", "attach_handler:refused",
    ]

    records = _records(queue_log)
    blob = json.dumps(records)
    assert FIXTURE_EMAIL not in blob, "the example's own emissions reached the sink unwalked"

    events = _by_event(records)
    line = events[f"probe log line: {PLACEHOLDER}"]
    assert line["librerun_scope"] == "run" and line["agent_id"] == "langgraph-triage"

    out = events[f"probe print: {PLACEHOLDER}"]
    assert out["logger"] == "stdout" and out["run_id"] == "run-probe"

    err = events[f"probe stderr: {PLACEHOLDER}"]
    assert err["logger"] == "stderr" and err["level"] == "warning"

    # The traceback is rendered text, and rendered text is walked too.
    traced = events[f"probe exception line: {PLACEHOLDER}"]
    assert "Traceback" in traced["exception"]
    assert f"ValueError: probe exception: {PLACEHOLDER}" in traced["exception"]

    # The handler the agent tried to own was refused, not quietly accepted.
    assert events["logging_handler_refused"]["logger_name"] == "agents.langgraph_triage"


@pytest.mark.asyncio
async def test_the_examples_handler_is_only_refused_because_the_queue_refuses_it(queue_log):
    """Negative test: the refusal above must come from the chassis.

    With no agent bound the same call is *ignored* rather than refused —
    a library attaching its handler at import must not crash the boot —
    so the example records ``allowed``. If that were also the answer
    inside an invocation, the assertion above would be passing on a
    chassis that had stopped refusing anything.
    """
    graph = _example()
    state = await graph.ainvoke(_inputs(attach_handler=True))
    assert state["structured"]["probed"] == ["attach_handler:allowed"]

    with log_context(agent_id="langgraph-triage", run_id="run-probe-2"):
        bound = await graph.ainvoke(_inputs(attach_handler=True))
    assert bound["structured"]["probed"] == ["attach_handler:refused"]


@pytest.mark.asyncio
async def test_the_examples_own_span_attribute_and_event_are_walked_on_export():
    """The example sets an attribute and adds an event on the live span.

    Its tracer is the process's tracer, so the backend's span processors
    are what stand between the agent and the exporter — the same walk the
    relay applies to a container's export.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult

    from app.observability.walkers import WalkingSpanExporter

    class _Sender:
        """Where the OTLP bytes would go. The walk happens before here."""

        def __init__(self):
            self.requests = []

        def send(self, request):
            self.requests.append(request)
            return SpanExportResult.SUCCESS

    sender = _Sender()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(WalkingSpanExporter(sender)))
    tracer = provider.get_tracer("test")

    graph = _example()
    with tracer.start_as_current_span("phase analyze"):
        await graph.ainvoke(_inputs(span_attribute=True))
    provider.shutdown()

    assert sender.requests, "the phase span was never exported"
    assert FIXTURE_EMAIL.encode() not in b"".join(
        r.SerializeToString() for r in sender.requests
    )
    spans = [
        s
        for r in sender.requests
        for rs in r.resource_spans
        for ss in rs.scope_spans
        for s in ss.spans
    ]
    span = next(s for s in spans if s.name == "phase analyze")
    attributes = {a.key: a.value.string_value for a in span.attributes}
    assert attributes["agent.probe.text"] == PLACEHOLDER
    event = next(e for e in span.events if e.name == "probe")
    assert {a.key: a.value.string_value for a in event.attributes}["text"] == PLACEHOLDER


def test_the_probe_text_is_redacted_at_intake_one_level_down():
    """``probe.text`` is marked ``x-pii`` inside an object.

    Intake redaction walks one object level, which is how deep the form
    renders — so the nested field is redacted before persist like any
    other. A schema change that flattened or deepened it would silently
    stop redacting; this is the assertion that notices.
    """
    from app.services.intake import redact_pii_fields

    schema = json.loads((EXAMPLE_DIR / "input_schema.json").read_text())
    payload = {
        "title": "Checkout is down",
        "service": "checkout-api",
        "description": "Every request fails.",
        "probe": {"text": f"mail me at {FIXTURE_EMAIL}", "log": True},
    }
    redacted, count = redact_pii_fields(schema, payload)
    assert count == 1
    assert redacted["probe"]["text"] == f"mail me at {PLACEHOLDER}"
    assert redacted["probe"]["log"] is True
    assert payload["probe"]["text"].endswith(FIXTURE_EMAIL), "the caller's payload was mutated"


def test_the_probe_is_off_by_default_and_changes_no_output():
    """An example whose diagnostics leaked into ordinary runs would be a
    worse example. Without a ``probe`` object the node adds nothing."""
    from langgraph_triage.agent import probe as probe_node

    assert probe_node({"user_inputs": {"title": "x"}}) == {}
    assert probe_node({"user_inputs": {"title": "x", "probe": {}}}) == {}
