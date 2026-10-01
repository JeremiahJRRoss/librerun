"""The adapter conformance battery, exercised framework-agnostically (B16).

Every check in here runs against a hand-written ``AgentProtocol`` stub —
**no LangGraph, no adapter, no optional dependency of any kind** — so a
plain chassis test install exercises all of them. That is deliberate and
was itself a review finding: the whole battery originally lived in one
module that began with ``pytest.importorskip("langgraph")``, and since
LangGraph is not a chassis requirement, a normal ``pip install -r
requirements.txt && pytest`` silently skipped every one of these
invariants. A conformance suite that disappears when an optional package
is missing is exactly the kind of check-that-does-not-check this batch
exists to catch.

The LangGraph-specific half — the example agent, the adapter, reducer
channels, checkpointers — lives in ``test_adapter_kit_langgraph.py``,
which may skip.

Each test below is a regression for a defect that reached review: the
battery is tested against deliberately broken agents, because a
conformance suite that only ever sees passing input proves nothing about
itself — the same lesson the name-purity guardrail taught at B15a.
"""
from __future__ import annotations

from typing import Any

import pytest

from adapter_kit import Scenario, run_battery
from app.agents.protocol import AgentInput, AgentProtocol, AnalysisResult


@pytest.fixture(scope="module", autouse=True)
def _tracing_initialized():
    """Initialize tracing for this module.

    The kit deliberately refuses to install a tracer provider — that
    would claim the process-global one and could leave a real
    application exporter ignored. Setting it up is the caller's job,
    and here the caller is this test module.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())


@pytest.mark.asyncio
async def test_battery_rejects_an_agent_that_never_reports_progress():
    """A silent agent must fail — this is the check that would otherwise
    pass vacuously for every adapter that forgets to stream."""

    class SilentAgent(AgentProtocol):
        agent_id = "silent"
        display_name = "Silent"
        description = "Reports nothing while it works"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress) -> AnalysisResult:
            return AnalysisResult(display={}, structured={"done": True})

    result = await run_battery(SilentAgent(), Scenario("empty", {}), require_traces=False)
    assert not result.passed
    assert any("no progress was streamed" in f for f in result.failures)


@pytest.mark.asyncio
async def test_battery_rejects_output_the_run_page_could_not_render():
    """Neither structured output nor report HTML means a blank results view."""

    class EmptyOutputAgent(AgentProtocol):
        agent_id = "empty-output"
        display_name = "Empty"
        description = "Finishes with nothing to show"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            from app.agents.protocol import InvestigationResult, StepProgress

            await on_progress(StepProgress(step_id="work", status="complete"))
            return InvestigationResult(status="complete")

    result = await run_battery(
        EmptyOutputAgent(), Scenario("empty", {}), require_traces=False
    )
    assert not result.passed
    # Wording changed when the check was widened to reject an empty dict
    # as well as None; the behaviour under test is unchanged.
    assert any("nothing to render" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_battery_rejects_a_scenario_the_agent_could_not_accept():
    """The scenario is validated against the agent's OWN schema, so a demo
    that the intake form would reject cannot be shipped as passing."""

    class StrictAgent(AgentProtocol):
        agent_id = "strict"
        display_name = "Strict"
        description = "Requires a field the scenario omits"

        def input_schema(self) -> dict:
            return {
                "type": "object",
                "required": ["must_have"],
                "properties": {"must_have": {"type": "string"}},
            }

        async def analyze(self, inp: AgentInput, on_progress):
            from app.agents.protocol import StepProgress

            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={})

    result = await run_battery(
        StrictAgent(), Scenario("missing-field", {}), require_traces=False
    )
    assert not result.passed
    assert any("input_schema()" in f for f in result.failures)


# --------------------------------------------------------------------
# Regressions for findings raised in review on this batch. Every one was
# verified to fail against the code as it stood AND to fail for the
# defect rather than incidentally — one of them first failed on a
# missing import, which would have made it a regression test for the
# wrong thing.
# --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_phase_must_stream_progress_not_just_one():
    """P2: a silent later phase must fail even when an earlier one reported.

    The aggregate check passed as soon as ANY phase emitted, so an agent
    whose second phase runs silently for a minute looked conformant
    while the run page sat blank for that minute.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    class ChattyThenSilentAgent(AgentProtocol):
        agent_id = "chatty-then-silent"
        display_name = "Chatty then silent"
        description = "Reports during phase one, then goes quiet"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="phase-one-work", status="complete"))
            return AnalysisResult(display={}, structured={"ok": True})

        async def investigate(self, inp: AgentInput, on_progress):
            return InvestigationResult(status="complete", structured={"ok": True})

    result = await run_battery(
        ChattyThenSilentAgent(),
        Scenario("two-phase", {}),
        phases=["analyze", "investigate"],
        require_traces=False,
    )
    assert not result.passed
    assert any(
        "'investigate' completed without streaming any progress" in f
        for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_span_capture_leaves_no_live_exporter_behind():
    """P2: the in-memory exporter must stop retaining spans after the run.

    In a booted process the battery attaches a processor it cannot
    detach; if the exporter stays live it collects every span the
    process finishes thereafter — an unbounded leak from a one-off
    conformance run.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    from adapter_kit import _span_capture

    provider = TracerProvider()
    captured_exporter = None
    with _span_capture() as exporter:
        captured_exporter = exporter
    assert captured_exporter is not None

    # Anything finished after the battery must not be retained.
    tracer = trace.get_tracer("after-battery")
    with tracer.start_as_current_span("later-span"):
        pass
    assert captured_exporter.get_finished_spans() == (), (
        "the exporter is still collecting spans after the battery ended"
    )


@pytest.mark.asyncio
async def test_unrelated_concurrent_spans_do_not_satisfy_the_trace_check():
    """P2: only THIS run's spans count.

    In a booted process the exporter also sees spans from concurrent
    requests and background jobs. A bare "were any spans emitted?" check
    would pass for an adapter that emits none, whenever unrelated work
    happened to finish during its phase. Here the agent emits progress
    but no spans, while unrelated work finishes in its own trace — the
    battery must still fail the trace check.
    """
    from opentelemetry import context as otel_context
    from opentelemetry import trace

    from app.agents.protocol import StepProgress

    class UntracedAgent(AgentProtocol):
        agent_id = "untraced"
        display_name = "Untraced"
        description = "Works, reports progress, opens no spans of its own"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            # Unrelated work finishing mid-phase, in its own trace:
            # attaching an empty context makes this a new root.
            token = otel_context.attach(otel_context.Context())
            try:
                with trace.get_tracer("some.other.subsystem").start_as_current_span(
                    "unrelated-background-work"
                ):
                    pass
            finally:
                otel_context.detach(token)
            return AnalysisResult(display={}, structured={"done": True})

    result = await run_battery(UntracedAgent(), Scenario("untraced", {}))
    assert not result.passed, (
        "an adapter that emitted no spans passed the trace check on the back "
        f"of unrelated concurrent work: {result.summary()}"
    )
    assert any("no spans were emitted" in f for f in result.failures), result.failures
    assert "unrelated-background-work" not in result.span_names


@pytest.mark.asyncio
async def test_kit_never_claims_the_global_tracer_provider():
    """P1: with no SDK provider the kit reports blindness, it does not install one.

    Installing one would claim the process-global provider; OTEL refuses
    later overrides, so an application initializing telemetry afterwards
    would have its real exporter ignored for the life of the process.
    """
    from opentelemetry import trace

    from adapter_kit import _span_capture

    class _NoOpProvider:
        def get_tracer(self, *a, **k):
            return trace.NoOpTracer()

    real = trace.get_tracer_provider
    trace.get_tracer_provider = lambda: _NoOpProvider()  # type: ignore[assignment]
    try:
        with _span_capture() as exporter:
            assert exporter is None, "the kit installed a provider it should not have"
    finally:
        trace.get_tracer_provider = real  # type: ignore[assignment]

    # And the real global is untouched by that attempt.
    from opentelemetry.sdk.trace import TracerProvider

    assert isinstance(trace.get_tracer_provider(), TracerProvider)


@pytest.mark.asyncio
async def test_status_outside_the_runner_vocabulary_fails():
    """P2: only the runner's success statuses pass.

    Checking for the literal "error" let anything else through —
    "failed", "cancelled" — while the runner marks the case failed for
    all of them. The vocabulary is imported from the runner so the two
    cannot drift.
    """
    from app.agents.protocol import InvestigationResult, StepProgress
    from app.services.agent_runner import _SUCCESS_STATUSES

    class OddStatusAgent(AgentProtocol):
        agent_id = "odd-status"
        display_name = "Odd status"
        description = "Finishes with a status the runner calls a failure"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return InvestigationResult(status="failed", structured={"x": 1})

    result = await run_battery(
        OddStatusAgent(), Scenario("odd", {}), require_traces=False
    )
    assert not result.passed
    assert any("'failed'" in f and "failed run" in f for f in result.failures), (
        result.failures
    )
    # And the vocabulary really is the runner's, not a local copy.
    assert _SUCCESS_STATUSES == frozenset({"awaiting_approval", "complete"})


@pytest.mark.asyncio
async def test_non_dict_input_schema_fails():
    """P2: a non-dict schema is a failure, not "nothing to validate".

    The chassis hands input_schema() to Draft202012Validator, so an
    agent returning None or a list cannot accept any submission.
    """
    from app.agents.protocol import StepProgress

    class NoSchemaAgent(AgentProtocol):
        agent_id = "no-schema"
        display_name = "No schema"
        description = "Returns None from input_schema()"

        def input_schema(self):
            return None

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={})

    result = await run_battery(
        NoSchemaAgent(), Scenario("no-schema", {}), require_traces=False
    )
    assert not result.passed
    assert any("not a dict" in f and "input_schema()" in f for f in result.failures), (
        result.failures
    )


@pytest.mark.asyncio
async def test_prior_analysis_carries_from_any_result_type():
    """P2: the next phase must see what the chassis would give it.

    The runner sets snap.analysis from every non-final phase's
    structured output regardless of result TYPE, so a middle phase
    returning an InvestigationResult still becomes prior_analysis. The
    battery previously carried it only for AnalysisResult, exercising
    different inputs from production.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    seen: dict[str, object] = {}

    class ThreePhaseAgent(AgentProtocol):
        agent_id = "three-phase"
        display_name = "Three phase"
        description = "Middle phase returns an InvestigationResult"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def run_phase(self, phase_name, inp, on_progress):
            seen[phase_name] = inp.prior_analysis
            await on_progress(StepProgress(step_id=phase_name, status="complete"))
            if phase_name == "middle":
                return InvestigationResult(
                    status="complete", structured={"from": "middle"}
                )
            return AnalysisResult(display={}, structured={"from": phase_name})

    result = await run_battery(
        ThreePhaseAgent(),
        Scenario("three", {}),
        phases=["first", "middle", "last"],
        require_traces=False,
    )
    assert result.passed, result.summary()
    assert seen["middle"] == {"from": "first"}
    assert seen["last"] == {"from": "middle"}, (
        "the middle phase's InvestigationResult did not become the next "
        f"phase's prior_analysis: {seen['last']}"
    )


@pytest.mark.asyncio
async def test_scenario_validated_with_the_chassis_draft():
    """P2: validate the way intake does, not the way $schema asks.

    `jsonschema.validate` honours a schema's own $schema declaration, so
    a draft-07 schema would have 2020-12 keywords ignored here and
    enforced by the production intake endpoint. The battery delegates to
    the chassis validator, which pins Draft 2020-12.
    """
    from app.agents.protocol import StepProgress

    class Draft7Agent(AgentProtocol):
        agent_id = "draft7"
        display_name = "Draft 7"
        description = "Declares draft-07 but uses a 2020-12 keyword"

        def input_schema(self) -> dict:
            return {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "contains": {"type": "string"},
                        "minContains": 2,  # ignored by draft-07, enforced by 2020-12
                    }
                },
            }

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={})

    result = await run_battery(
        Draft7Agent(),
        Scenario("one-tag", {"tags": ["only-one"]}),
        require_traces=False,
    )
    assert not result.passed, (
        "a scenario the production intake endpoint would reject passed the "
        f"battery: {result.summary()}"
    )
    assert any("input_schema()" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_truthy_non_dict_structured_is_reported_not_raised():
    """P2 regression: the battery must REPORT this, never raise.

    The prior-analysis coercion `dict(structured or {})` raises for a
    truthy non-mapping like "oops", which would abort run_battery before
    the check meant to catch it — the caller getting an exception
    instead of a BatteryResult listing what is wrong.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    class StringOutputAgent(AgentProtocol):
        agent_id = "string-output"
        display_name = "String output"
        description = "Returns a string where a dict belongs"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return InvestigationResult(status="complete", structured="oops")

    result = await run_battery(
        StringOutputAgent(), Scenario("string", {}), require_traces=False
    )
    assert not result.passed
    # Wording changed when the battery began replaying the runner's
    # STEPS — reads AND the operation at each position. Production
    # raises at this value's own step, so the failure names that step
    # and its line rather than describing the value. Same verdict.
    assert any(
        "agent_runner.py:493" in f and "result.structured" in f
        for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_non_string_report_html_fails():
    """P2: report_html must be a string when present.

    Truthiness alone accepted report_html=1, which the runner persists
    to a Text column and measures with len() — failing at persistence
    rather than at conformance.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    class BadReportAgent(AgentProtocol):
        agent_id = "bad-report"
        display_name = "Bad report"
        description = "Returns a number as report HTML"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return InvestigationResult(status="complete", report_html=1, structured=None)

    result = await run_battery(
        BadReportAgent(), Scenario("bad-report", {}), require_traces=False
    )
    assert not result.passed
    # Wording changed when the battery began replaying the runner's
    # STEPS — reads AND the operation at each position. Production
    # raises at this value's own step, so the failure names that step
    # and its line rather than describing the value. Same verdict.
    assert any(
        "agent_runner.py:519" in f and "report_html" in f
        for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_every_phase_must_emit_spans_not_just_one():
    """P2: a later uninstrumented phase must fail even when an earlier one traced.

    The aggregate span check was satisfied by the first phase that
    emitted anything, so an agent that traces its cheap opening phase and
    runs the expensive one blind read as conformant — with the trace gap
    sitting exactly where someone debugging would look.
    """
    from opentelemetry import trace

    from app.agents.protocol import InvestigationResult, StepProgress

    class TracedThenBlindAgent(AgentProtocol):
        agent_id = "traced-then-blind"
        display_name = "Traced then blind"
        description = "Opens a span in phase one, none in phase two"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            with trace.get_tracer("phase-one").start_as_current_span("phase-one-span"):
                await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={"ok": True})

        async def investigate(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="more-work", status="complete"))
            return InvestigationResult(status="complete", structured={"ok": True})

    result = await run_battery(
        TracedThenBlindAgent(),
        Scenario("two-phase", {}),
        phases=["analyze", "investigate"],
    )
    assert not result.passed, (
        "an agent that stopped tracing after its first phase passed: "
        f"{result.summary()}"
    )
    assert any(
        "'investigate' completed without emitting any spans" in f
        for f in result.failures
    ), result.failures
    # The aggregate check alone would have been satisfied by phase one.
    assert "phase-one-span" in result.span_names


@pytest.mark.asyncio
async def test_battery_redacts_the_scenario_like_intake_does():
    """P2: the agent must see what production would give it.

    `POST /runs` redacts every `x-pii` string field before the agent
    runs, so an adapter certified on raw input has been exercised with
    input no real run produces. An SSN is used because stage-1 regex
    catches it without loading Presidio's model.
    """
    from app.agents.protocol import StepProgress

    seen: dict[str, Any] = {}

    class PiiAgent(AgentProtocol):
        agent_id = "pii-agent"
        display_name = "PII"
        description = "Records the logs field it was handed"

        def input_schema(self) -> dict:
            return {
                "type": "object",
                "properties": {
                    "logs": {"type": "string", "x-pii": True},
                    "note": {"type": "string"},
                },
            }

        async def analyze(self, inp: AgentInput, on_progress):
            seen.update(inp.user_inputs)
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={"ok": True})

    raw = {"logs": "customer SSN is 123-45-6789", "note": "not marked x-pii: 123-45-6789"}
    result = await run_battery(
        PiiAgent(), Scenario("pii", raw), require_traces=False
    )
    assert result.passed, result.summary()

    assert "123-45-6789" not in seen["logs"], (
        f"the battery handed the agent unredacted PII: {seen['logs']!r}"
    )
    assert "REDACTED" in seen["logs"], seen["logs"]
    assert result.redactions >= 1

    # Only x-pii fields are touched — the battery redacts the way intake
    # does, it does not invent a stricter rule of its own.
    assert seen["note"] == raw["note"]

    # And the caller's scenario is not mutated on the way through.
    assert raw["logs"] == "customer SSN is 123-45-6789"


@pytest.mark.asyncio
async def test_redaction_failure_stops_before_the_agent_runs():
    """P1: a failed redaction must abort, not merely be recorded.

    Production raises before persisting the run or scheduling the
    runner, so the agent never sees the body. Recording the failure and
    continuing would hand raw `x-pii` values to an adapter that may
    forward them to an external model — a leak, not a failed check.
    """
    from unittest.mock import patch

    from app.agents.protocol import StepProgress
    from app.services import intake as intake_mod

    ran = {"phases": 0}

    class PiiAgent(AgentProtocol):
        agent_id = "pii-abort"
        display_name = "PII abort"
        description = "Must never be invoked when redaction failed"

        def input_schema(self) -> dict:
            return {"type": "object", "properties": {"logs": {"type": "string", "x-pii": True}}}

        async def analyze(self, inp: AgentInput, on_progress):
            ran["phases"] += 1
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={})

    def boom(schema, payload):
        raise RuntimeError("presidio exploded")

    with patch.object(intake_mod, "redact_pii_fields", side_effect=boom):
        import adapter_kit

        with patch.object(adapter_kit, "_chassis_redact", side_effect=boom):
            result = await run_battery(
                PiiAgent(),
                Scenario("pii", {"logs": "customer SSN is 123-45-6789"}),
                require_traces=False,
            )

    assert not result.passed
    assert any("redaction failed" in f for f in result.failures), result.failures
    assert ran["phases"] == 0, "the agent ran with unredacted input"
    assert result.phases_run == []


@pytest.mark.asyncio
async def test_nested_non_json_output_fails_the_battery():
    """P1: top-level dict-ness is not enough for a JSONB write.

    An adapter returning `{"ids": {1, 2}}` or a bare UUID nested in an
    otherwise ordinary dict passed every shape check and then failed the
    run at COMMIT, with the work already done. The battery now runs the
    same encode persistence does.
    """
    from uuid import uuid4 as _uuid4

    from app.agents.protocol import StepProgress

    def _agent_returning(payload):
        class NestedAgent(AgentProtocol):
            agent_id = "nested"
            display_name = "Nested"
            description = "Returns a dict with an unencodable value inside"

            def input_schema(self) -> dict:
                return {"type": "object"}

            async def analyze(self, inp: AgentInput, on_progress):
                await on_progress(StepProgress(step_id="work", status="complete"))
                return AnalysisResult(display={}, structured=payload)

        return NestedAgent()

    # A set, a UUID, and a NaN: all fine in Python, none storable in JSONB.
    for payload, needle in (
        ({"ids": {1, 2}}, "structured.ids"),
        ({"nested": {"who": _uuid4()}}, "structured.nested.who"),
        ({"scores": [1.0, float("nan")]}, "structured.scores[1]"),
    ):
        result = await run_battery(
            _agent_returning(payload), Scenario("nested", {}), require_traces=False
        )
        assert not result.passed, f"{payload!r} passed the battery"
        assert any("cannot persist" in f and needle in f for f in result.failures), (
            f"{payload!r} -> {result.failures}"
        )

    # And an ordinary nested dict still passes — the check must not be a
    # blanket refusal of nesting.
    ok = await run_battery(
        _agent_returning({"nested": {"fine": [1, "two", None, 3.5]}}),
        Scenario("nested-ok", {}),
        require_traces=False,
    )
    assert ok.passed, ok.summary()


@pytest.mark.asyncio
async def test_battery_stops_after_an_unsuccessful_phase():
    """P2: the runner returns on a failed phase, so the battery must too.

    Continuing would run the next phase's model calls and capability
    side effects — with their costs — down a path the real run cannot
    take, then report observations from it as if they meant something.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    ran: list[str] = []

    class FailsFirstAgent(AgentProtocol):
        agent_id = "fails-first"
        display_name = "Fails first"
        description = "First phase reports a status the runner calls a failure"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def run_phase(self, phase_name, inp, on_progress):
            ran.append(phase_name)
            await on_progress(StepProgress(step_id=phase_name, status="complete"))
            if phase_name == "analyze":
                return AnalysisResult(display={}, structured={}, status="failed")
            return InvestigationResult(status="complete", structured={"ok": True})

    result = await run_battery(
        FailsFirstAgent(),
        Scenario("fails-first", {}),
        phases=["analyze", "investigate"],
        require_traces=False,
    )
    assert not result.passed
    assert any("failed run" in f for f in result.failures), result.failures

    # The failing phase was still validated...
    assert result.phases_run == ["analyze"]
    # ...and the next one never started.
    assert ran == ["analyze"], f"the battery ran past a failed phase: {ran}"


@pytest.mark.asyncio
async def test_battery_stops_when_output_cannot_be_processed():
    """P2: a successful STATUS with unusable output must also stop.

    The runner's first act is `dict(result.structured or {})`
    (agent_runner.py:493), which raises for a truthy non-mapping — so
    `status="complete", structured="oops"` never reaches the next phase
    in production, however healthy its status looks.
    """
    from app.agents.protocol import StepProgress

    ran: list[str] = []

    class BadOutputThenMoreAgent(AgentProtocol):
        agent_id = "bad-output-then-more"
        display_name = "Bad output then more"
        description = "First phase succeeds with output the runner cannot coerce"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def run_phase(self, phase_name, inp, on_progress):
            ran.append(phase_name)
            await on_progress(StepProgress(step_id=phase_name, status="complete"))
            if phase_name == "analyze":
                return AnalysisResult(display={}, structured="oops")
            return AnalysisResult(display={}, structured={"ok": True})

    result = await run_battery(
        BadOutputThenMoreAgent(),
        Scenario("bad-output", {}),
        phases=["analyze", "investigate"],
        require_traces=False,
    )
    assert not result.passed
    # Wording changed when the battery began replaying the runner's
    # STEPS — reads AND the operation at each position. Production
    # raises at this value's own step, so the failure names that step
    # and its line rather than describing the value. Same verdict.
    assert any(
        "agent_runner.py:493" in f and "result.structured" in f
        for f in result.failures
    ), result.failures
    assert ran == ["analyze"], f"the battery ran past unusable output: {ran}"


@pytest.mark.asyncio
async def test_scenario_rejected_by_intake_never_reaches_the_agent():
    """P2: production returns 422 before scheduling a run.

    A scenario that fails the agent's own schema can never become a real
    run, so invoking the agent on it spends model calls and capability
    side effects on input production would have refused.
    """
    from app.agents.protocol import StepProgress

    ran = {"n": 0}

    class StrictAgent(AgentProtocol):
        agent_id = "strict-abort"
        display_name = "Strict abort"
        description = "Requires a field the scenario omits"

        def input_schema(self) -> dict:
            return {
                "type": "object",
                "required": ["must_have"],
                "properties": {"must_have": {"type": "string"}},
            }

        async def analyze(self, inp: AgentInput, on_progress):
            ran["n"] += 1
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={})

    result = await run_battery(
        StrictAgent(), Scenario("missing-field", {}), require_traces=False
    )
    assert not result.passed
    assert any("input_schema()" in f for f in result.failures), result.failures
    assert ran["n"] == 0, "the agent ran on input intake would have rejected"
    assert result.phases_run == []


@pytest.mark.asyncio
async def test_mixed_mapping_keys_fail_even_though_json_dumps_accepts_them():
    """P2: `json.dumps` is lenient exactly where the chassis is strict.

    `{"summary": "ok", 1: "item"}` encodes fine — json coerces the int
    key to "1" — so an encode-first check returns success. The runner
    then calls `sorted(structured.keys())` at agent_runner.py:517 and
    raises `TypeError: '<' not supported between instances of 'int' and
    'str'`. The walker existed to catch this and was unreachable because
    the encode succeeded first.
    """
    import json as _json

    from app.agents.protocol import StepProgress

    class MixedKeyAgent(AgentProtocol):
        agent_id = "mixed-keys"
        display_name = "Mixed keys"
        description = "Returns a dict with both string and integer keys"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={"summary": "ok", 1: "item"})

    payload = {"summary": "ok", 1: "item"}
    # The premise: encoding really does succeed, so this cannot be caught
    # by trying to serialize.
    assert _json.dumps(payload) == '{"summary": "ok", "1": "item"}'
    with pytest.raises(TypeError):
        sorted(payload.keys())

    result = await run_battery(
        MixedKeyAgent(), Scenario("mixed", {}), require_traces=False
    )
    assert not result.passed
    # Wording changed when the walk was corrected to mirror the runner's
    # two operations separately (sorted() at the top level, json.dumps at
    # every depth); the behaviour under test — mixed top-level keys must
    # fail — is unchanged and still asserted above via the premise.
    # Asserted on the LINE, not the exact expression text: the sort
    # is now replayed at :517 where the runner performs it, and the
    # replay quotes the runner literally — `sorted(list(...))` —
    # while this check previously matched the walk's paraphrase.
    # Same defect, same verdict, more faithful position; the line
    # number is the part worth pinning.
    assert any("agent_runner.py:517" in f and "sorted" in f for f in result.failures), (
        result.failures
    )


@pytest.mark.asyncio
async def test_declared_output_mode_decides_which_field_must_be_filled():
    """P2: the manifest picks the field the run page reads.

    An agent declaring `output.mode: structured` that returns only
    `report_html` produces a COMPLETE run whose results view renders
    "No structured result available." — the HTML is never fetched.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    def _agent(structured, report_html):
        class ModeAgent(AgentProtocol):
            agent_id = "mode-agent"
            display_name = "Mode"
            description = "Fills one output field or the other"

            def input_schema(self) -> dict:
                return {"type": "object"}

            async def analyze(self, inp: AgentInput, on_progress):
                await on_progress(StepProgress(step_id="work", status="complete"))
                return InvestigationResult(
                    status="complete", structured=structured, report_html=report_html
                )

        return ModeAgent()

    # Declares structured, returns only HTML: the run page ignores it.
    wrong = await run_battery(
        _agent({}, "<p>a full report</p>"),
        Scenario("mode", {}),
        require_traces=False,
        output_mode="structured",
    )
    assert not wrong.passed
    assert any("output.mode=structured" in f for f in wrong.failures), wrong.failures

    # The mirror image.
    wrong_html = await run_battery(
        _agent({"finding": "x"}, None),
        Scenario("mode", {}),
        require_traces=False,
        output_mode="html_report",
    )
    assert not wrong_html.passed
    assert any("output.mode=html_report" in f for f in wrong_html.failures)

    # Each mode's correct shape passes.
    for structured, html, mode in (
        ({"finding": "x"}, None, "structured"),
        ({}, "<p>report</p>", "html_report"),
    ):
        ok = await run_battery(
            _agent(structured, html),
            Scenario("mode", {}),
            require_traces=False,
            output_mode=mode,
        )
        assert ok.passed, ok.summary()

    # An unknown mode is a caller error, not a silent no-op — otherwise a
    # typo would disable the check it was meant to enable.
    with pytest.raises(ValueError):
        await run_battery(
            _agent({"a": 1}, None), Scenario("mode", {}), output_mode="structrued"
        )


def test_output_modes_match_the_manifest():
    """The kit's mode vocabulary must be the manifest's, not a guess."""
    import typing

    from adapter_kit import _OUTPUT_MODES
    from app.agents.manifest import OutputSpec

    declared = set(typing.get_args(OutputSpec.model_fields["mode"].annotation))
    assert _OUTPUT_MODES == declared, (
        f"kit knows {_OUTPUT_MODES}, manifest declares {declared}"
    )


@pytest.mark.asyncio
async def test_empty_final_analysis_result_fails_too():
    """P2: the renderability check must cover BOTH result classes.

    It lived inside the InvestigationResult branch, so a final
    `AnalysisResult(display={}, structured={})` recorded nothing — and
    an AnalysisResult has no report field to provide even the weaker
    "something renderable" fallback. The runner marks the phase complete
    and stores `{}`, leaving the results view with no answer.

    Pointed out immediately after I lifted the output-mode check out of
    that same branch *because* a final phase may return either type.
    """
    from app.agents.protocol import StepProgress

    class EmptyAnalysisAgent(AgentProtocol):
        agent_id = "empty-analysis"
        display_name = "Empty analysis"
        description = "Final phase returns an empty AnalysisResult"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={})

    result = await run_battery(
        EmptyAnalysisAgent(), Scenario("empty-analysis", {}), require_traces=False
    )
    assert not result.passed
    assert any("nothing to render" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_non_string_progress_detail_fails():
    """P2: `detail` is declared `str | None` and the chassis serializes it.

    `_on_progress` json.dumps() the progress record into Redis, so a
    UUID detail raises TypeError and aborts the phase mid-run. The
    battery recorded the event and dropped the detail, making the one
    field that breaks production the one field it never looked at.
    """
    from uuid import uuid4 as _uuid4

    from app.agents.protocol import StepProgress

    class BadDetailAgent(AgentProtocol):
        agent_id = "bad-detail"
        display_name = "Bad detail"
        description = "Emits a progress detail the chassis cannot serialize"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(
                StepProgress(step_id="work", status="complete", detail=_uuid4())
            )
            return AnalysisResult(display={}, structured={"ok": True})

    result = await run_battery(
        BadDetailAgent(), Scenario("bad-detail", {}), require_traces=False
    )
    assert not result.passed
    assert any("progress detail" in f for f in result.failures), result.failures

    # A string detail is fine, so the check discriminates.
    class GoodDetailAgent(BadDetailAgent):
        agent_id = "good-detail"

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(
                StepProgress(step_id="work", status="complete", detail="halfway")
            )
            return AnalysisResult(display={}, structured={"ok": True})

    ok = await run_battery(
        GoodDetailAgent(), Scenario("good-detail", {}), require_traces=False
    )
    assert ok.passed, ok.summary()


@pytest.mark.asyncio
async def test_unserializable_progress_aborts_the_phase_like_production():
    """P2: recording the failure is not enough — production RAISES here.

    The chassis' write happens inside the callback, so an unencodable
    detail raises within the agent's own `await on_progress(...)`,
    killing the phase and everything after it. A battery that noted the
    problem and returned normally would keep running phases production
    never reaches.
    """
    from uuid import uuid4 as _uuid4

    from app.agents.protocol import InvestigationResult, StepProgress

    ran: list[str] = []

    class BadDetailTwoPhaseAgent(AgentProtocol):
        agent_id = "bad-detail-2"
        display_name = "Bad detail two-phase"
        description = "Emits an unserializable detail in phase one"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def run_phase(self, phase_name, inp, on_progress):
            ran.append(phase_name)
            await on_progress(
                StepProgress(
                    step_id=f"{phase_name}:work",
                    status="complete",
                    detail=_uuid4() if phase_name == "analyze" else None,
                )
            )
            return InvestigationResult(status="complete", structured={"ok": True})

    result = await run_battery(
        BadDetailTwoPhaseAgent(),
        Scenario("bad-detail-2", {}),
        phases=["analyze", "investigate"],
        require_traces=False,
    )
    assert not result.passed
    assert any("progress detail" in f for f in result.failures), result.failures
    assert ran == ["analyze"], f"the battery ran past an aborted phase: {ran}"

    # A non-string detail that IS serializable violates the declared type
    # but does not kill the run, so it is reported without aborting —
    # the battery must not be stricter than production about which
    # failures stop execution.
    class IntDetailAgent(AgentProtocol):
        agent_id = "int-detail"
        display_name = "Int detail"
        description = "Detail is an int: wrong type, still serializable"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete", detail=5))
            return AnalysisResult(display={}, structured={"ok": True})

    soft = await run_battery(
        IntDetailAgent(), Scenario("int-detail", {}), require_traces=False
    )
    assert not soft.passed
    assert soft.phases_run == ["analyze"], soft.summary()


@pytest.mark.asyncio
async def test_non_string_step_id_fails():
    """P2: step_id is a Redis hash FIELD.

    redis-py encodes bytes/str/int/float and refuses anything else —
    verified against a live Redis: `DataError: Invalid input of type:
    'UUID'`. The battery never looked at the type.
    """
    from uuid import uuid4 as _uuid4

    from app.agents.protocol import StepProgress

    class UuidStepAgent(AgentProtocol):
        agent_id = "uuid-step"
        display_name = "UUID step"
        description = "Uses a UUID as its step id"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id=_uuid4(), status="complete"))
            return AnalysisResult(display={}, structured={"ok": True})

    result = await run_battery(
        UuidStepAgent(), Scenario("uuid-step", {}), require_traces=False
    )
    assert not result.passed
    assert any("step_id" in f and "not a string" in f for f in result.failures), (
        result.failures
    )


@pytest.mark.asyncio
async def test_drifts_only_output_is_not_an_answer():
    """P2: check what the chassis PERSISTS, not what the agent returned.

    The runner strips `_drifts` before storing (agent_runner.py:494), so
    `{"_drifts": [...]}` is truthy in the battery and lands as `{}` in
    the database — a complete run with nothing to render. The stripping
    already existed for `prior` and was simply not carried across to the
    output checks.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    class DriftsOnlyAgent(AgentProtocol):
        agent_id = "drifts-only"
        display_name = "Drifts only"
        description = "Returns nothing but runner-internal drift data"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return InvestigationResult(
                status="complete", structured={"_drifts": [{"step": "x"}]}
            )

    for mode in (None, "structured"):
        result = await run_battery(
            DriftsOnlyAgent(),
            Scenario("drifts", {}),
            require_traces=False,
            output_mode=mode,
        )
        assert not result.passed, f"output_mode={mode!r}: {result.summary()}"
        # And the recorded output is what a user would actually see.
        assert result.outputs["analyze"] == {}, result.outputs


@pytest.mark.asyncio
async def test_dict_convertible_output_is_carried_forward_like_production():
    """P2: the battery must not be STRICTER than the chassis.

    `dict()` succeeds for more than dicts — a list of pairs, a custom
    Mapping — and the runner carries whatever it produced into the next
    phase. Substituting `{}` gave later phases different
    `prior_analysis` than the real run: the same divergence this battery
    exists to catch, committed by the battery.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    seen: dict[str, Any] = {}

    class PairsAgent(AgentProtocol):
        agent_id = "pairs"
        display_name = "Pairs"
        description = "Returns a list of pairs, which dict() accepts"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def run_phase(self, phase_name, inp, on_progress):
            seen[phase_name] = inp.prior_analysis
            await on_progress(StepProgress(step_id=f"{phase_name}:w", status="complete"))
            if phase_name == "analyze":
                return AnalysisResult(display={}, structured=[("summary", "ok")])
            return InvestigationResult(status="complete", structured={"done": True})

    result = await run_battery(
        PairsAgent(),
        Scenario("pairs", {}),
        phases=["analyze", "investigate"],
        require_traces=False,
    )
    # Still a conformance failure — the declared type is a dict...
    assert not result.passed
    assert any("not a dict" in f for f in result.failures), result.failures

    # ...but the run continues exactly as production would, carrying the
    # COERCED value forward rather than an empty dict.
    assert result.phases_run == ["analyze", "investigate"]
    assert seen["investigate"] == {"summary": "ok"}, seen


@pytest.mark.asyncio
async def test_non_final_report_html_does_not_abort():
    """P2: fatal only where the field is consumed.

    The runner reads `report_html` under `if is_final:` alone; a
    non-final phase stores `snap.analysis` and advances without touching
    it. Aborting made the battery stricter than production and skipped
    later phases the real run performs.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    ran: list[str] = []

    class MidReportAgent(AgentProtocol):
        agent_id = "mid-report"
        display_name = "Mid report"
        description = "Non-final phase sets a non-string report_html"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def run_phase(self, phase_name, inp, on_progress):
            ran.append(phase_name)
            await on_progress(StepProgress(step_id=f"{phase_name}:w", status="complete"))
            if phase_name == "analyze":
                return InvestigationResult(
                    status="complete", structured={"ok": True}, report_html=1
                )
            return InvestigationResult(status="complete", structured={"done": True})

    result = await run_battery(
        MidReportAgent(),
        Scenario("mid-report", {}),
        phases=["analyze", "investigate"],
        require_traces=False,
    )
    assert not result.passed
    assert any("report_html is int" in f for f in result.failures), result.failures
    # Reported, but not fatal: production would have run both phases.
    assert ran == ["analyze", "investigate"], ran

    # On the FINAL phase the same value IS fatal, because that is where
    # the runner persists it and measures its length.
    class FinalReportAgent(MidReportAgent):
        agent_id = "final-report"

        async def run_phase(self, phase_name, inp, on_progress):
            await on_progress(StepProgress(step_id=f"{phase_name}:w", status="complete"))
            return InvestigationResult(
                status="complete", structured={"ok": True}, report_html=1
            )

    final = await run_battery(
        FinalReportAgent(), Scenario("final-report", {}), require_traces=False
    )
    assert not final.passed
    # The FINAL phase reaches `len(result.report_html or "")`
    # (agent_runner.py:519) and raises there, so the battery names that
    # step. The non-final half above still reports the type, because
    # production never touches the field on a non-final phase — which is
    # the asymmetry this test exists for.
    assert any(
        "agent_runner.py:519" in f and "report_html" in f for f in final.failures
    ), final.failures


@pytest.mark.asyncio
async def test_bool_step_id_aborts_like_redis_would():
    """P2: `bool` is an `int` subclass, and redis-py rejects it anyway.

    Verified against a live Redis: `True` raises `DataError: Invalid
    input of type: 'bool'` while `7`, `1.5`, `b"x"` and `"x"` are all
    accepted. So the int subclassing that makes `True` look encodable
    here is exactly the trap.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    ran: list[str] = []

    class BoolStepAgent(AgentProtocol):
        agent_id = "bool-step"
        display_name = "Bool step"
        description = "Uses True as a step id"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def run_phase(self, phase_name, inp, on_progress):
            ran.append(phase_name)
            await on_progress(StepProgress(step_id=True, status="complete"))
            return InvestigationResult(status="complete", structured={"ok": True})

    result = await run_battery(
        BoolStepAgent(),
        Scenario("bool-step", {}),
        phases=["analyze", "investigate"],
        require_traces=False,
    )
    assert not result.passed
    assert ran == ["analyze"], f"the battery ran past a phase Redis would kill: {ran}"

    # An int step id violates the declared type but Redis accepts it, so
    # it is reported WITHOUT aborting — the two thresholds differ.
    class IntStepAgent(BoolStepAgent):
        agent_id = "int-step"

        async def run_phase(self, phase_name, inp, on_progress):
            ran.append(phase_name)
            await on_progress(StepProgress(step_id=7, status="complete"))
            return InvestigationResult(status="complete", structured={"ok": True})

    ran.clear()
    soft = await run_battery(
        IntStepAgent(),
        Scenario("int-step", {}),
        phases=["analyze", "investigate"],
        require_traces=False,
    )
    assert not soft.passed
    assert any("step_id" in f and "not a string" in f for f in soft.failures)
    assert ran == ["analyze", "investigate"], ran


@pytest.mark.asyncio
async def test_duck_typed_result_reports_but_still_advances():
    """P2: the runner never `isinstance`-checks — it uses `getattr`.

    A custom object with a good `status` and coercible `structured` is
    persisted and advanced exactly like a declared result, so marking it
    fatal made the battery stricter than the chassis. Worse, `prior` was
    computed only inside the isinstance branch, so the next phase
    received the PREVIOUS phase's analysis.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    class DuckResult:
        status = "complete"
        structured = {"from": "duck"}
        report_html = None
        display = {}

    seen: dict[str, Any] = {}

    class DuckAgent(AgentProtocol):
        agent_id = "duck"
        display_name = "Duck"
        description = "Returns a duck-typed result object"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def run_phase(self, phase_name, inp, on_progress):
            seen[phase_name] = inp.prior_analysis
            await on_progress(StepProgress(step_id=f"{phase_name}:w", status="complete"))
            if phase_name == "analyze":
                return DuckResult()
            return InvestigationResult(status="complete", structured={"done": True})

    result = await run_battery(
        DuckAgent(),
        Scenario("duck", {}),
        phases=["analyze", "investigate"],
        require_traces=False,
    )
    assert not result.passed
    assert any("expected" in f and "AnalysisResult" in f for f in result.failures)

    # Reported — but the run continues, and the next phase gets THIS
    # phase's output, not the one before it.
    assert result.phases_run == ["analyze", "investigate"]
    assert seen["investigate"] == {"from": "duck"}, seen


@pytest.mark.asyncio
async def test_unhashable_status_is_reported_not_raised():
    """P2: the battery's own check must not be the thing that breaks.

    `p.status in VALID_STATUSES` raises `TypeError: unhashable type` for
    `["complete"]`, aborting the phase from inside the check. Production
    `json.dumps`es the value happily and carries on.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    ran: list[str] = []

    class ListStatusAgent(AgentProtocol):
        agent_id = "list-status"
        display_name = "List status"
        description = "Emits an unhashable progress status"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def run_phase(self, phase_name, inp, on_progress):
            ran.append(phase_name)
            await on_progress(
                StepProgress(step_id=f"{phase_name}:w", status=["complete"])
            )
            return InvestigationResult(status="complete", structured={"ok": True})

    result = await run_battery(
        ListStatusAgent(),
        Scenario("list-status", {}),
        phases=["analyze", "investigate"],
        require_traces=False,
    )
    assert not result.passed
    assert any("not one of the chassis vocabulary" in f for f in result.failures)
    # Production would run both phases, so the battery must too.
    assert ran == ["analyze", "investigate"], ran


@pytest.mark.asyncio
async def test_input_schema_exception_is_reported_not_propagated():
    """P2: intake catches ANY exception from input_schema() and answers 400.

    The battery caught only NotImplementedError, so a config-driven
    ValueError escaped `run_battery` — handing the caller a traceback
    instead of a BatteryResult saying the agent cannot accept input.
    """

    class BrokenSchemaAgent(AgentProtocol):
        agent_id = "broken-schema"
        display_name = "Broken schema"
        description = "input_schema() blows up"

        def input_schema(self) -> dict:
            raise ValueError("schema file missing")

        async def analyze(self, inp: AgentInput, on_progress):
            raise AssertionError("must never run")

    result = await run_battery(
        BrokenSchemaAgent(), Scenario("broken", {}), require_traces=False
    )
    assert not result.passed
    assert any("ValueError" in f and "schema file missing" in f for f in result.failures)
    assert result.phases_run == []


@pytest.mark.asyncio
async def test_identity_failure_reports_but_still_runs_the_agent():
    """Self-caught: not every failure means production would refuse.

    The intake abort was first written as `if result.failures: return`,
    which also stopped for a missing `description` — a real conformance
    failure, but one the chassis runs anyway. That would have hidden an
    author's progress and tracing results behind a cosmetic complaint.
    """
    from app.agents.protocol import StepProgress

    class NamelessAgent(AgentProtocol):
        agent_id = "nameless"
        display_name = "Nameless"
        description = ""  # the only thing wrong with it

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={"ok": True})

    result = await run_battery(
        NamelessAgent(), Scenario("nameless", {}), require_traces=False
    )
    assert not result.passed
    assert any("does not set description" in f for f in result.failures)

    # ...but the agent still ran, so its other results are real.
    assert result.phases_run == ["analyze"]
    assert result.progress == [("work", "complete")]


@pytest.mark.asyncio
async def test_empty_structured_output_fails():
    """P2: an empty dict is as empty as None to a reader.

    The runner marks such a run complete and ResultsView.tsx renders
    "No structured result available." — a finished run with no answer,
    which the battery must not certify.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    class EmptyDictAgent(AgentProtocol):
        agent_id = "empty-dict"
        display_name = "Empty dict"
        description = "Completes with an empty structured result"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return InvestigationResult(status="complete", structured={}, report_html=None)

    result = await run_battery(
        EmptyDictAgent(), Scenario("empty-dict", {}), require_traces=False
    )
    assert not result.passed
    assert any("nothing to render" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_non_string_identity_fields_are_rejected():
    """P2: emptiness was only the first threshold; type is the second.

    `AgentProtocol` declares all three identity fields as `str`. For
    `agent_id` production enforces it — discovery compares
    `instance.agent_id` against the manifest's validated string id and
    refuses to register on mismatch (registry.py:315) — so a non-string
    id is an agent the picker never offers. The battery was certifying
    an adapter production would not expose at all.
    """
    from app.agents.protocol import StepProgress

    class NumericIdentityAgent(AgentProtocol):
        agent_id = 123  # truthy, so the old `if not value` check passed
        display_name = 456
        description = 789

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={"ok": True})

    result = await run_battery(
        NumericIdentityAgent(), Scenario("numeric-identity", {}), require_traces=False
    )
    assert not result.passed
    for attr in ("agent_id", "display_name", "description"):
        assert any(
            f"agent {attr} is int, not a string" in f for f in result.failures
        ), f"{attr} not reported: {result.failures}"
    # The agent_id message must name the production consequence, not
    # merely the declared type — that is what makes it actionable.
    assert any("never be offered" in f for f in result.failures), result.failures
    # Identity failures report but do not block: the author still learns
    # whether their progress and tracing work.
    assert result.phases_run == ["analyze"]


@pytest.mark.asyncio
async def test_unhashable_result_status_is_reported_not_raised():
    """P2: the same guard the progress check needed, at the result sites.

    `getattr(out, "status", None) in RUNNER_SUCCESS_STATUSES` raises
    `TypeError` for `["complete"]`. Production raises there too
    (agent_runner.py:495), so the result is genuinely fatal — but the
    battery must fail it by REPORTING, not by letting the exception
    escape `run_battery` and handing the caller a traceback instead of
    a `BatteryResult`.
    """
    from app.agents.protocol import StepProgress

    class UnhashableStatusAgent(AgentProtocol):
        agent_id = "unhashable-status"
        display_name = "Unhashable status"
        description = "Returns a list as its status"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            out = AnalysisResult(display={}, structured={"ok": True})
            object.__setattr__(out, "status", ["complete"])
            return out

    # The assertion that matters is that this call RETURNS at all.
    result = await run_battery(
        UnhashableStatusAgent(), Scenario("unhashable", {}), require_traces=False
    )
    assert not result.passed
    # Wording generalized when the guard widened from `except TypeError`
    # to `except Exception` — `__hash__` is the agent's code and may
    # raise anything, so the message now names the actual exception
    # rather than asserting it was an unhashable type. The behaviour
    # under test — reported, not raised — is unchanged.
    assert any(
        "status the runner cannot test" in f and "TypeError: unhashable type" in f
        for f in result.failures
    ), result.failures
    assert any("agent_runner.py:495" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_non_string_report_html_fails_for_any_result_type():
    """P2: the last class-gated check, and the same miss twice.

    The runner duck-types `report_html` for EVERY result type — under
    `if is_final:` it does `len(getattr(result, "report_html", None) or
    "")` (agent_runner.py:517-519) — so an `AnalysisResult` subclass
    carrying `report_html=1` reaches that `len()` and errors the run
    with the work already done. The check used to live inside the
    `InvestigationResult` branch, and the generic renderability check
    below it only tests truthiness, which `1` satisfies.
    """
    from app.agents.protocol import StepProgress

    from dataclasses import dataclass

    @dataclass
    class ReportingAnalysis(AnalysisResult):
        report_html: Any = None

    class NumericReportAgent(AgentProtocol):
        agent_id = "numeric-report"
        display_name = "Numeric report"
        description = "AnalysisResult subclass whose report_html is an int"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return ReportingAnalysis(display={}, structured={"ok": True}, report_html=1)

    result = await run_battery(
        NumericReportAgent(),
        Scenario("numeric-report", {}),
        require_traces=False,
        output_mode="html_report",
    )
    assert not result.passed, (
        "an int report_html passed the battery, but the runner calls len() on "
        f"it and errors the run: {result.summary()}"
    )
    # Wording changed when the battery began replaying the runner's
    # STEPS — its reads AND the operation at each position — rather than
    # its reads alone. Production reaches this value's step and raises
    # there, so the failure now names the step and its line instead of
    # describing the value. Same verdict, exact location.
    assert any(
        "agent_runner.py:519" in f and "report_html" in f for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_none_input_schema_blocks_intake_even_with_an_identity_failure():
    """P2: `result.failures` is not the question "did the lookup fail?".

    The non-dict branch was guarded by `schema is not None or not
    result.failures`, using the global failure list as a stand-in for
    "the lookup already reported itself". Those diverge the moment an
    unrelated identity failure exists: with `description=""` AND
    `input_schema()` returning None, both disjuncts are false, so the
    battery reported neither the invalid schema nor blocked intake — it
    redacted and ran the agent. Production hands that None to
    Draft202012Validator and fails before scheduling anything, so the
    battery was paying for model calls and capability side effects on
    input that can never reach an agent.
    """
    from app.agents.protocol import StepProgress

    ran = {"n": 0}

    class NoSchemaAndNamelessAgent(AgentProtocol):
        agent_id = "none-schema"
        display_name = "None schema"
        description = ""  # the unrelated identity failure that hid the bug

        def input_schema(self):
            return None  # RETURNED, not raised

        async def analyze(self, inp: AgentInput, on_progress):
            ran["n"] += 1
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={"ok": True})

    result = await run_battery(
        NoSchemaAndNamelessAgent(), Scenario("none-schema", {}), require_traces=False
    )
    assert not result.passed
    assert any(
        "not a dict" in f and "input_schema()" in f for f in result.failures
    ), result.failures
    assert ran["n"] == 0, "the agent ran on input production would have rejected"
    assert result.phases_run == []


@pytest.mark.asyncio
async def test_nested_non_string_keys_pass_because_production_survives_them():
    """P2: the two-thresholds rule, inverted — the battery was stricter.

    The runner sorts the TOP-LEVEL keys only (agent_runner.py:517);
    `json.dumps` reaches every depth and coerces str/int/float/bool/None
    keys. So `{"items": {1: "ok"}}` persists fine as
    `{"items": {"1": "ok"}}`. The walk rejected any non-string key at any
    depth, failing a production-compatible adapter AND setting
    phase_fatal — so its later phases were never exercised either.
    """
    from app.agents.protocol import StepProgress

    class NestedIntKeyAgent(AgentProtocol):
        agent_id = "nested-int-key"
        display_name = "Nested int key"
        description = "Returns an int-keyed nested mapping, as production allows"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={"items": {1: "ok"}})

    result = await run_battery(
        NestedIntKeyAgent(), Scenario("nested-int", {}), require_traces=False
    )
    assert result.passed, (
        "the battery failed output the chassis stores without complaint: "
        f"{result.summary()}"
    )


@pytest.mark.asyncio
async def test_uniform_non_string_top_level_keys_also_pass():
    """`sorted([1, 2])` is fine — only MIXED key types make it raise.

    Pins the other half of the same correction: the top-level check must
    mirror `sorted()`, not "are these all strings".
    """
    from app.agents.protocol import StepProgress

    class IntKeyAgent(AgentProtocol):
        agent_id = "int-keys"
        display_name = "Int keys"
        description = "Top-level keys are all ints, which sort fine"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={1: "a", 2: "b"})

    result = await run_battery(
        IntKeyAgent(), Scenario("int-keys", {}), require_traces=False
    )
    assert result.passed, result.summary()


@pytest.mark.asyncio
async def test_unencodable_nested_key_still_fails():
    """A tuple key is refused by json.dumps at any depth — still fatal."""
    from app.agents.protocol import StepProgress

    class TupleKeyAgent(AgentProtocol):
        agent_id = "tuple-key"
        display_name = "Tuple key"
        description = "Nested key json.dumps cannot encode at all"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={"items": {(1, 2): "ok"}})

    result = await run_battery(
        TupleKeyAgent(), Scenario("tuple-key", {}), require_traces=False
    )
    assert not result.passed
    assert any("json.dumps accepts only" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_colliding_keys_are_reported_but_do_not_abort():
    """Self-caught while measuring: JSON overwrites rather than raising.

    `{1: "x", "1": "y"}` round-trips to `{"1": "y"}` — the run completes
    and a value the agent produced is simply gone. Production survives
    it, so this reports without setting phase_fatal.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    ran: list[str] = []

    class CollidingKeyAgent(AgentProtocol):
        agent_id = "colliding-keys"
        display_name = "Colliding keys"
        description = "Two keys that encode to the same JSON string"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            ran.append("analyze")
            await on_progress(StepProgress(step_id="a", status="complete"))
            return AnalysisResult(display={}, structured={"n": {1: "x", "1": "y"}})

        async def investigate(self, inp: AgentInput, on_progress):
            ran.append("investigate")
            await on_progress(StepProgress(step_id="b", status="complete"))
            return InvestigationResult(status="complete", structured={"ok": True})

    result = await run_battery(
        CollidingKeyAgent(),
        Scenario("collide", {}),
        phases=["analyze", "investigate"],
        require_traces=False,
    )
    assert not result.passed
    assert any("collide once encoded" in f for f in result.failures), result.failures
    # The whole point of report-not-abort: the later phase still ran.
    assert ran == ["analyze", "investigate"], ran


def test_json_key_names_match_what_the_encoder_writes():
    """The collision walk must name keys the way `json.dumps` does.

    `str()` and JSON disagree on the non-finite floats — `str(nan)` is
    `'nan'`, JSON writes `NaN` — and on int subclasses, where `str()`
    gives an enum's member name while json writes its value. A collision
    check built on `str()` therefore sees two distinct names for keys
    production merges into one.
    """
    import json as _json
    from enum import IntEnum

    from adapter_kit import _json_key_name

    class Code(IntEnum):
        OK = 200

    for key in (
        float("nan"),
        float("inf"),
        float("-inf"),
        1.5,
        10,
        True,
        False,
        None,
        "plain",
        Code.OK,
    ):
        # The encoder is the authority: dump a one-key object and read
        # back the name it chose, rather than asserting a literal.
        written = next(iter(_json.loads(_json.dumps({key: 0}))))
        assert _json_key_name(key) == written, (
            f"{key!r}: named {_json_key_name(key)!r}, encoder writes {written!r}"
        )


@pytest.mark.asyncio
async def test_nan_key_colliding_with_its_json_spelling_is_reported():
    """P2 regression: `{nan: "x", "NaN": "y"}` loses a value in production.

    `json.dumps` writes both as `"NaN"`, so the stored object keeps only
    the last. The collision walk missed it because it derived the name
    with `str()`, which spells it `'nan'`.
    """
    from app.agents.protocol import StepProgress

    class NanKeyAgent(AgentProtocol):
        agent_id = "nan-key"
        display_name = "NaN key"
        description = "A float('nan') key alongside the string 'NaN'"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(
                display={}, structured={"n": {float("nan"): "x", "NaN": "y"}}
            )

    result = await run_battery(
        NanKeyAgent(), Scenario("nan-key", {}), require_traces=False
    )
    assert not result.passed, (
        "output that round-trips to a single key passed: " + result.summary()
    )
    assert any("collide once encoded" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_mixed_keys_on_a_NON_final_phase_do_not_abort_the_run():
    """P2: ordering is a final-phase concern; encodability is not.

    `sorted(list(structured.keys()))` sits inside `if is_final:`
    (agent_runner.py:517). A non-final result is assigned to
    `snap.analysis` and committed WITHOUT sorting, so mixed top-level
    keys there are production-compatible. Failing them marked the phase
    fatal and truncated a run production completes.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    ran: list[str] = []

    class MixedMidPhaseAgent(AgentProtocol):
        agent_id = "mixed-mid-phase"
        display_name = "Mixed mid-phase"
        description = "Mixed top-level keys on a phase that is not the last"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            ran.append("analyze")
            await on_progress(StepProgress(step_id="a", status="complete"))
            return AnalysisResult(display={}, structured={1: "numeric", "summary": "ok"})

        async def investigate(self, inp: AgentInput, on_progress):
            ran.append("investigate")
            await on_progress(StepProgress(step_id="b", status="complete"))
            return InvestigationResult(status="complete", structured={"done": True})

    result = await run_battery(
        MixedMidPhaseAgent(),
        Scenario("mixed-mid", {}),
        phases=["analyze", "investigate"],
        require_traces=False,
    )
    assert ran == ["analyze", "investigate"], (
        f"the battery truncated a run production completes: {result.summary()}"
    )
    assert result.passed, result.summary()


@pytest.mark.asyncio
async def test_mixed_keys_on_the_FINAL_phase_still_fail():
    """The other half: the final phase IS sorted, so it must still fail."""
    from app.agents.protocol import StepProgress

    class MixedFinalAgent(AgentProtocol):
        agent_id = "mixed-final"
        display_name = "Mixed final"
        description = "Mixed top-level keys on the final phase"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="a", status="complete"))
            return AnalysisResult(display={}, structured={1: "numeric", "summary": "ok"})

    result = await run_battery(
        MixedFinalAgent(), Scenario("mixed-final", {}), require_traces=False
    )
    assert not result.passed
    # Asserted on the LINE, not the exact expression text: the sort
    # is now replayed at :517 where the runner performs it, and the
    # replay quotes the runner literally — `sorted(list(...))` —
    # while this check previously matched the walk's paraphrase.
    # Same defect, same verdict, more faithful position; the line
    # number is the part worth pinning.
    assert any("agent_runner.py:517" in f and "sorted" in f for f in result.failures), (
        result.failures
    )


@pytest.mark.asyncio
async def test_repeated_battery_runs_attach_only_one_span_processor():
    """P2: undetachable processors must not accumulate per run.

    OTEL cannot remove a span processor, so attaching a fresh one each
    run left the provider calling `on_end` on a growing pile of dead
    processors for every span the process finishes thereafter. One
    reusable processor per provider, switched on and off, instead.
    """
    from opentelemetry.sdk.trace import TracerProvider

    from adapter_kit import _CaptureProcessor, _span_capture

    provider = TracerProvider()

    def capture_count() -> int:
        return sum(
            isinstance(p, _CaptureProcessor)
            for p in provider._active_span_processor._span_processors
        )

    from opentelemetry import trace

    previous = trace.get_tracer_provider()
    try:
        trace._TRACER_PROVIDER = provider  # bypass the one-shot set guard
        for _ in range(5):
            with _span_capture():
                pass
        assert capture_count() == 1, (
            f"{capture_count()} capture processors attached after 5 runs"
        )
    finally:
        trace._TRACER_PROVIDER = previous


@pytest.mark.asyncio
async def test_capture_stops_collecting_once_the_run_ends():
    """The switch must actually switch off, not merely stop being read."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    from adapter_kit import _span_capture

    provider = TracerProvider()
    previous = trace.get_tracer_provider()
    try:
        trace._TRACER_PROVIDER = provider
        with _span_capture() as capturer:
            pass
        with provider.get_tracer("after").start_as_current_span("later"):
            pass
        assert capturer.get_finished_spans() == (), (
            "still collecting after the run ended"
        )
    finally:
        trace._TRACER_PROVIDER = previous


@pytest.mark.asyncio
async def test_a_span_left_running_by_an_earlier_phase_does_not_certify_a_later_one():
    """P2: per-phase spans are attributed by START time, not a count delta.

    Every phase shares the battery root's trace id, and only FINISHED
    spans reach the processor. So a span phase one leaves running
    finishes during phase two, lifts the count, and certifies a phase
    that emitted nothing of its own. Reproduced before the fix:
    `passed=True` with `span_names=['phase-one-real-work',
    'phase-one-async-work']` — both phase one's.
    """
    from opentelemetry import trace

    from app.agents.protocol import InvestigationResult, StepProgress

    held: dict = {}

    class LeakyAgent(AgentProtocol):
        agent_id = "leaky-span"
        display_name = "Leaky span"
        description = "Phase one leaves a span running into phase two"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="a", status="complete"))
            tracer = trace.get_tracer("phase-one")
            with tracer.start_as_current_span("phase-one-real-work"):
                pass
            # Started here, deliberately not ended until the next phase.
            held["span"] = tracer.start_span("phase-one-async-work")
            return AnalysisResult(display={}, structured={"ok": True})

        async def investigate(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="b", status="complete"))
            held["span"].end()  # phase one's leftover finishes in phase two
            # phase two emits no spans of its own
            return InvestigationResult(status="complete", structured={"done": True})

    result = await run_battery(
        LeakyAgent(), Scenario("leaky", {}), phases=["analyze", "investigate"]
    )
    assert not result.passed, (
        "phase two was certified by a span phase one emitted: " + result.summary()
    )
    assert any(
        "'investigate' completed without emitting any spans" in f
        for f in result.failures
    ), result.failures
    # Phase one genuinely did emit a span of its own, so it must not be
    # blamed as well — the fix has to be attribution, not a blanket
    # tightening.
    assert not any("'analyze' completed without emitting" in f for f in result.failures), (
        result.failures
    )


@pytest.mark.asyncio
async def test_a_deferred_span_opened_by_an_earlier_phase_does_not_certify_a_later_one():
    """P2: wall-clock attribution was still the wrong question.

    Start-time attribution closed "phase one's span FINISHES during
    phase two" but not "phase one's deferred task OPENS its span during
    phase two" — the timestamp lands squarely in phase two's window.
    Reproduced before the fix: `passed=True` with
    `span_names=['phase-one-real-work', 'phase-one-deferred']`, both
    phase one's.

    Context answers it where time cannot: asyncio copies the ambient
    context when a task is created, so the deferred span is parented to
    phase one's span however late it opens.
    """
    import asyncio

    from opentelemetry import trace

    from app.agents.protocol import InvestigationResult, StepProgress

    gate: dict = {}

    class DeferredSpanAgent(AgentProtocol):
        agent_id = "deferred-span"
        display_name = "Deferred span"
        description = "Phase one opens a span during phase two"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="a", status="complete"))
            tracer = trace.get_tracer("phase-one")
            with tracer.start_as_current_span("phase-one-real-work"):
                pass

            async def deferred():
                await gate["go"].wait()
                with tracer.start_as_current_span("phase-one-deferred"):
                    pass

            gate["go"] = asyncio.Event()
            gate["task"] = asyncio.create_task(deferred())
            return AnalysisResult(display={}, structured={"ok": True})

        async def investigate(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="b", status="complete"))
            gate["go"].set()
            await gate["task"]  # phase one's span opens AND closes in here
            # phase two emits nothing of its own
            return InvestigationResult(status="complete", structured={"done": True})

    result = await run_battery(
        DeferredSpanAgent(), Scenario("deferred", {}), phases=["analyze", "investigate"]
    )
    assert not result.passed, (
        "phase two was certified by a span phase one deferred: " + result.summary()
    )
    assert any(
        "'investigate' completed without emitting any spans" in f
        for f in result.failures
    ), result.failures
    assert not any("'analyze' completed without emitting" in f for f in result.failures), (
        result.failures
    )


@pytest.mark.asyncio
async def test_the_batterys_own_phase_spans_do_not_satisfy_the_trace_check():
    """The scaffolding must not certify the thing it scaffolds.

    Each phase now runs inside `adapter_kit:phase:<name>`. If that span
    counted as instrumentation, every phase would pass its own trace
    check for free — a check that cannot fail.
    """
    from app.agents.protocol import StepProgress

    class UntracedAgent(AgentProtocol):
        agent_id = "untraced"
        display_name = "Untraced"
        description = "Streams progress but emits no spans at all"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={"ok": True})

    result = await run_battery(UntracedAgent(), Scenario("untraced", {}))
    assert not result.passed, (
        "the battery's own phase span satisfied the trace check: " + result.summary()
    )
    assert any(
        "completed without emitting any spans" in f for f in result.failures
    ), result.failures
    assert result.span_names == [], (
        f"scaffolding spans leaked into the reported span list: {result.span_names}"
    )


def test_pg_text_problem_matches_what_postgres_actually_rejects():
    """P2: `json.dumps` succeeding is not "the database can store it".

    Measured against a live PostgreSQL 16 (the full sweep, not just the
    reported character): NUL and unpaired surrogates are rejected by
    both jsonb and Text; ordinary control characters, DEL, astral
    codepoints and newlines are all fine. This pins that shape so the
    check neither loosens nor over-tightens.
    """
    import json as _json

    from adapter_kit import _pg_text_problem

    rejected = [chr(0), "\ud800", "ok\x00then", "lead\ud83d"]
    accepted = [chr(1), chr(7), chr(127), "\U0001F600", "\n", "", "plain"]

    for text in rejected:
        assert _pg_text_problem(text) is not None, f"{text!r} should be rejected"
        # The premise: Python encodes it happily, which is why the
        # battery cannot lean on json.dumps alone.
        _json.dumps({"v": text}, allow_nan=False)

    for text in accepted:
        assert _pg_text_problem(text) is None, f"{text!r} should be accepted"


@pytest.mark.asyncio
async def test_nul_in_structured_output_is_rejected():
    """The chassis commits this into JSONB and PostgreSQL refuses it."""
    from app.agents.protocol import StepProgress

    class NulAgent(AgentProtocol):
        agent_id = "nul-output"
        display_name = "NUL output"
        description = "Structured output containing a NUL character"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={"text": chr(0)})

    result = await run_battery(
        NulAgent(), Scenario("nul", {}), require_traces=False
    )
    assert not result.passed
    assert any("NUL character" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_nul_in_a_structured_key_is_rejected():
    """Keys go into the same JSONB document as the values."""
    from app.agents.protocol import StepProgress

    class NulKeyAgent(AgentProtocol):
        agent_id = "nul-key"
        display_name = "NUL key"
        description = "A structured key containing a NUL character"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={"bad" + chr(0): "v"})

    result = await run_battery(
        NulKeyAgent(), Scenario("nul-key", {}), require_traces=False
    )
    assert not result.passed
    assert any("NUL character" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_nul_in_report_html_is_rejected_too():
    """Self-caught by sweeping: the Text column rejects the same characters.

    The finding named `structured`/JSONB. `report_html` is a Text
    column and PostgreSQL refuses NUL there as well, so fixing only the
    reported field would have left the identical defect one attribute
    over.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    class NulReportAgent(AgentProtocol):
        agent_id = "nul-report"
        display_name = "NUL report"
        description = "report_html containing a NUL character"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return InvestigationResult(
                status="complete", report_html="<p>ok" + chr(0) + "</p>"
            )

    result = await run_battery(
        NulReportAgent(),
        Scenario("nul-report", {}),
        require_traces=False,
        output_mode="html_report",
    )
    assert not result.passed
    assert any(
        "report_html" in f and "NUL character" in f for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_nul_below_the_recursion_cutoff_is_still_rejected():
    """P2: the depth backstop answered a different question.

    Past `_MAX_WALK_DEPTH` the walk fell back to a whole-subtree
    `json.dumps`, which succeeds for both NUL and unpaired surrogates —
    so output nested deeply enough passed the battery and failed at
    COMMIT. The cutoff exists to bound recursion on self-referential
    state, not to stop checking.
    """
    from app.agents.protocol import StepProgress

    from adapter_kit import _MAX_WALK_DEPTH

    # Bury the NUL well past the cutoff.
    deep: Any = {"text": chr(0)}
    for _ in range(_MAX_WALK_DEPTH + 5):
        deep = {"nested": deep}

    class DeepNulAgent(AgentProtocol):
        agent_id = "deep-nul"
        display_name = "Deep NUL"
        description = "A NUL buried below the recursion cutoff"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured=deep)

    result = await run_battery(
        DeepNulAgent(), Scenario("deep-nul", {}), require_traces=False
    )
    assert not result.passed, (
        "a NUL below the cutoff passed the battery: " + result.summary()
    )
    assert any("NUL character" in f for f in result.failures), result.failures


def test_deep_pg_scan_terminates_on_a_self_referential_structure():
    """The cutoff's whole point: state that references itself.

    The replacement scan is iterative with an id()-keyed visited set, so
    a cycle must terminate rather than spin — otherwise the fix for the
    finding would reintroduce the hang the cutoff was guarding against.
    """
    from adapter_kit import _pg_text_problem_deep

    cyclic: dict = {"name": "fine"}
    cyclic["self"] = cyclic

    assert _pg_text_problem_deep(cyclic, "structured") is None

    bad: dict = {"text": chr(0)}
    bad["self"] = bad
    assert _pg_text_problem_deep(bad, "structured") is not None


@pytest.mark.asyncio
async def test_key_collision_below_the_recursion_cutoff_is_still_reported():
    """P2: the collision walk gave up where `_json_problem` accepts.

    `_json_problem` passes a deep subtree, so returning None past the
    cutoff certified output whose keys production still collapses —
    losing a value. Same defect the PostgreSQL check had one function
    above, in the same guard.
    """
    from app.agents.protocol import StepProgress

    from adapter_kit import _MAX_WALK_DEPTH

    deep: Any = {1: "first", "1": "second"}
    for _ in range(_MAX_WALK_DEPTH + 5):
        deep = {"nested": deep}

    class DeepCollisionAgent(AgentProtocol):
        agent_id = "deep-collision"
        display_name = "Deep collision"
        description = "Colliding keys buried below the recursion cutoff"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured=deep)

    result = await run_battery(
        DeepCollisionAgent(), Scenario("deep-collision", {}), require_traces=False
    )
    assert not result.passed, (
        "a collision below the cutoff passed: " + result.summary()
    )
    assert any("collide once encoded" in f for f in result.failures), result.failures


def test_deep_collision_scan_terminates_on_a_self_referential_structure():
    """The cutoff's purpose must survive its replacement."""
    from adapter_kit import _json_lossy_deep

    cyclic: dict = {"name": "fine"}
    cyclic["self"] = cyclic
    assert _json_lossy_deep(cyclic, "structured") is None


@pytest.mark.asyncio
async def test_memoryview_step_id_is_reported_but_does_not_abort():
    """P2: redis-py accepts a memoryview hash field; the battery did not.

    Measured against a live Redis: bytes, bytearray, memoryview, str,
    int and float are all accepted as hash fields; bool, None, UUID and
    list are rejected. The stand-in raise used a narrower allow-list, so
    it truncated the run along a path production survives — the
    over-strict direction again.

    The declared-type violation is still reported: `StepProgress.step_id`
    is `str`, and an adapter emitting a memoryview should hear about it.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    ran: list[str] = []

    class MemoryviewAgent(AgentProtocol):
        agent_id = "memoryview-step"
        display_name = "Memoryview step"
        description = "Emits a memoryview step_id, which Redis accepts"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            ran.append("analyze")
            await on_progress(
                StepProgress(step_id=memoryview(b"work"), status="complete")
            )
            return AnalysisResult(display={}, structured={"ok": True})

        async def investigate(self, inp: AgentInput, on_progress):
            ran.append("investigate")
            await on_progress(StepProgress(step_id="second", status="complete"))
            return InvestigationResult(status="complete", structured={"done": True})

    result = await run_battery(
        MemoryviewAgent(),
        Scenario("memoryview", {}),
        phases=["analyze", "investigate"],
        require_traces=False,
    )
    # Production stores it and carries on, so the battery must too.
    assert ran == ["analyze", "investigate"], (
        f"the battery truncated a run production completes: {result.summary()}"
    )
    # But the declared type is still violated, and still reported.
    assert not result.passed
    assert any("not a string" in f and "step_id" in f for f in result.failures), (
        result.failures
    )


@pytest.mark.asyncio
async def test_oversized_integer_key_is_reported_not_raised():
    """P2: the battery crashed while describing the failure.

    An oversized int key passes the key-TYPE check and then raises in
    every rendering path — `str`, `repr`, `!r`, plain interpolation —
    so `run_battery` escaped with a traceback instead of returning a
    failed `BatteryResult`. Production refuses the key too (json.dumps
    raises), so it is a real failure; the battery just has to be able
    to say so.
    """
    from app.agents.protocol import StepProgress

    class BigKeyAgent(AgentProtocol):
        agent_id = "big-key"
        display_name = "Big key"
        description = "Structured output keyed by an oversized integer"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={10**5000: "v"})

    # The assertion that matters is that this RETURNS.
    result = await run_battery(
        BigKeyAgent(), Scenario("big-key", {}), require_traces=False
    )
    assert not result.passed
    assert any("cannot be encoded" in f for f in result.failures), result.failures


def test_safe_repr_never_raises_on_values_repr_cannot_render():
    """The shared repair point, exercised directly.

    Seventeen diagnostics in this module interpolate an offending
    value; two were reported. This is the function they all now go
    through.
    """
    from adapter_kit import _path_of, _safe_repr

    big = 10**5000
    assert _safe_repr(big).startswith("<int 16610 bits, 0x")
    assert _safe_repr("ordinary") == "'ordinary'"
    assert _safe_repr(42) == "42"
    assert _path_of("structured", "name") == "structured.name"
    assert _path_of("structured", big).startswith("structured.<int 16610 bits,")


# --- rendering a value runs the AGENT'S code -------------------------
#
# The round above made the diagnostics survive an oversized integer, and
# stopped there: every guard caught `ValueError` only, because that is
# the exception CPython's digit cap raises. But `repr`, `str` and
# `type(x).__name__` are all overridable, and everything they run on here
# came from the agent under test. A `__repr__` that raises `RuntimeError`
# — or returns a non-string, which makes the builtin raise `TypeError` —
# walked straight through the guard that was supposed to be the fix.
#
# So these exercise the guards against arbitrary exceptions rather than
# against the one that was reported.


class _HostileRepr:
    """A value that refuses to be rendered. Nothing exotic: an object
    whose ``__repr__`` touches a lazily-loaded attribute that is not
    there yet does exactly this."""

    def __repr__(self):
        raise RuntimeError("repr is not available")

    def __str__(self):
        raise RuntimeError("str is not available")


class _NonStringRepr:
    """``__repr__`` returning something that is not a string, which makes
    the ``repr`` BUILTIN raise ``TypeError`` — a different exception
    from a different layer, and equally fatal."""

    def __repr__(self):
        return 42

    def __str__(self):
        return 42


def test_safe_renderers_survive_any_exception_not_just_value_error():
    """The three guards, exercised on values whose own code fights back."""
    from adapter_kit import _exc_text, _safe_repr, _safe_str, _safe_type_name

    hostile = _HostileRepr()
    assert _safe_repr(hostile) == "<unrepresentable _HostileRepr>"
    assert _safe_str(hostile) == "<unrenderable _HostileRepr>"

    # `repr`/`str` returning a non-string raises TypeError in the builtin.
    wrong_type = _NonStringRepr()
    assert _safe_repr(wrong_type) == "<unrepresentable _NonStringRepr>"
    assert _safe_str(wrong_type) == "<unrenderable _NonStringRepr>"

    # The oversized-int behaviour the previous round added is unchanged.
    assert _safe_repr(10**5000).startswith("<int 16610 bits, 0x")
    assert _safe_str(10**5000).startswith("<int 16610 bits, 0x")
    assert _safe_str("plain") == "plain"

    # An exception whose MESSAGE cannot render — the agent picks both the
    # type and the args, so both halves of the diagnostic go through the
    # guards.
    assert _exc_text(RuntimeError(10**5000)).startswith("RuntimeError: <int 16610 bits,")
    assert _exc_text(ValueError(hostile)) == "ValueError: <unrepresentable _HostileRepr>"
    assert _exc_text(RuntimeError("plain")) == "RuntimeError: plain"

    # And the last assumption every fallback above makes.
    assert _safe_type_name(hostile) == "_HostileRepr"

    class _NoName(type):
        @property
        def __name__(cls):  # noqa: N805 - deliberately hostile
            raise RuntimeError("no name for you")

    class _Nameless(metaclass=_NoName):
        pass

    assert _safe_type_name(_Nameless()) == "?"


@pytest.mark.asyncio
async def test_phase_exception_with_an_unrenderable_message_is_reported():
    """`raise RuntimeError(10**5000)` inside a phase.

    The battery catches the exception and interpolates it as `{exc}`,
    which calls `str(exc)` — and `str` of an oversized int raises the
    same `ValueError` the last round was about. So the crash-report was
    the crash, one layer up from where it was fixed.
    """
    from app.agents.protocol import StepProgress

    class ExplodingAgent(AgentProtocol):
        agent_id = "exploding"
        display_name = "Exploding"
        description = "Raises an exception that cannot be printed"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            raise RuntimeError(10**5000)

    # The assertion that matters is that this RETURNS rather than
    # propagating the agent's exception to the battery's caller.
    result = await run_battery(
        ExplodingAgent(), Scenario("boom", {}), require_traces=False
    )
    assert not result.passed
    assert any(
        "raised RuntimeError" in f and "16610 bits" in f for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_unrenderable_progress_step_id_is_reported_not_raised():
    """A value the battery renders UNCONDITIONALLY, on every event.

    A non-string `step_id` is recorded as `_safe_repr(p.step_id)` for
    each progress callback — before any check decides whether it is a
    problem — so a hostile `__repr__` here killed the battery while the
    agent was still running.

    (A hostile value in `structured` does NOT exercise this: json's
    "not JSON serializable" message is built from the class name and
    never calls `repr`, so that path was safe all along. Worth stating,
    because it was the first thing this test tried.)
    """
    from app.agents.protocol import StepProgress

    class HostileStepAgent(AgentProtocol):
        agent_id = "hostile-step"
        display_name = "Hostile step"
        description = "Reports progress under an unrenderable step id"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id=_HostileRepr(), status="complete"))
            return AnalysisResult(display={}, structured={"ok": True})

    result = await run_battery(
        HostileStepAgent(), Scenario("hostile-step", {}), require_traces=False
    )
    assert not result.passed
    assert any(
        "<unrepresentable _HostileRepr>" in f and "is _HostileRepr" in f
        for f in result.failures
    ), result.failures
    # And it was recorded, not merely rejected.
    assert result.progress == [("<unrepresentable _HostileRepr>", "complete")]


@pytest.mark.asyncio
async def test_unrenderable_mapping_key_is_reported_not_raised():
    """The same, on the key side — where the diagnostic has no choice but
    to render the key, since the path itself is built from it."""
    from app.agents.protocol import StepProgress

    class HostileKeyAgent(AgentProtocol):
        agent_id = "hostile-key"
        display_name = "Hostile key"
        description = "Structured output keyed by an unrenderable object"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={_HostileRepr(): "v"})

    result = await run_battery(
        HostileKeyAgent(), Scenario("hostile-key", {}), require_traces=False
    )
    assert not result.passed
    assert any(
        "<unrepresentable _HostileRepr>" in f and "_HostileRepr" in f
        for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_report_html_whose_truth_value_raises_is_reported_not_raised():
    """`report_html` reaches the emptiness check as whatever the agent set.

    The type check above it records a failure and sets `phase_fatal`,
    which is not READ until after these lines — so `if not rendered`
    truth-tests the agent's object, and `__bool__` is the agent's code
    too. Rendering was the reported class; this is the same value one
    line earlier.
    """
    from app.agents.protocol import StepProgress

    class UnBoolable:
        def __bool__(self):
            raise RuntimeError("cannot be truth-tested")

        def __repr__(self):
            raise RuntimeError("cannot be rendered either")

    class HostileReportAgent(AgentProtocol):
        agent_id = "hostile-report"
        display_name = "Hostile report"
        description = "report_html that refuses to be truth-tested"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            out = AnalysisResult(display={}, structured={"ok": True})
            object.__setattr__(out, "report_html", UnBoolable())
            return out

    # `output_mode` is what routes `report_html` into the emptiness
    # check — without a declared mode the `structured` branch
    # short-circuits before `__bool__` is ever called, so a test that
    # omits it passes against the unfixed code and proves nothing.
    result = await run_battery(
        HostileReportAgent(),
        Scenario("hostile-report", {}),
        require_traces=False,
        output_mode="html_report",
    )
    assert not result.passed
    # Wording changed when the battery began replaying the runner's
    # STEPS — its reads AND the operation at each position — rather than
    # its reads alone. Production reaches this value's step and raises
    # there, so the failure now names the step and its line instead of
    # describing the value. Same verdict, exact location.
    assert any(
        "agent_runner.py:519" in f and "report_html" in f for f in result.failures
    ), result.failures


class _HostileStr(str):
    """A `str` subclass owning every method the battery calls on text.

    `encode` included — which makes it unusable as a Redis hash field,
    so it belongs in the tests that assert a FAILURE.
    """

    def encode(self, *a, **k):
        raise RuntimeError("encode is not available")

    def __contains__(self, item):
        raise RuntimeError("contains is not available")

    def __format__(self, spec):
        raise RuntimeError("format is not available")


class _QuirkyStr(str):
    """A `str` subclass production can carry end to end.

    Overrides what the SANITIZER calls (`in`, `format`) but not
    `encode`, so redis-py handles it and the value really is storable.
    This is the case where normalizing is the right answer — and
    keeping it separate from `_HostileStr` is the point: the previous
    round used one class for both and asserted the battery passed an
    agent whose progress write would have died.
    """

    def __contains__(self, item):
        raise RuntimeError("contains is not available")

    def __format__(self, spec):
        raise RuntimeError("format is not available")


@pytest.mark.asyncio
async def test_storable_str_subclass_output_is_normalized_and_passes():
    """Reported against the adapter; the battery has the same hole.

    `isinstance(x, str)` licences `in` and `.encode(...)` inside
    `_pg_text_problem` — both of which a `str` subclass owns. This value
    is genuinely storable (json reads the buffer, PostgreSQL takes the
    bytes, redis-py encodes it), so a battery that dies here fails an
    agent production would have run.
    """
    from app.agents.protocol import StepProgress

    class SubclassAgent(AgentProtocol):
        agent_id = "subclass-out"
        display_name = "Subclass"
        description = "Returns a str subclass as output and as step id"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(
                StepProgress(step_id=_QuirkyStr("work"), status="complete")
            )
            return AnalysisResult(
                display={}, structured={_QuirkyStr("key"): _QuirkyStr("value")}
            )

    result = await run_battery(
        SubclassAgent(), Scenario("subclass", {}), require_traces=False
    )
    assert result.passed, result.summary()
    # Recorded as an exact `str`, so everything downstream that formats,
    # compares or sorts it is running `str`'s code and not the agent's.
    assert result.progress == [("work", "complete")]
    assert type(result.progress[0][0]) is str


@pytest.mark.asyncio
async def test_step_id_subclass_redis_cannot_encode_is_a_failure():
    """P2, and it is the previous round's fix aimed at the wrong target.

    Normalizing the copy the battery RECORDS changes nothing about what
    production sends: `redis.hset(..., p.step_id, ...)`
    (agent_runner.py:207-209) hands over the agent's own object, and
    redis-py's encoder calls `value.encode(encoding, errors)` on it. So
    a `str` subclass whose `encode` raises kills the phase — while the
    battery, having normalized its private copy, said PASS.

    Measured, and the measurement is the finding: the SYNC redis client
    accepts this object, because `redis[hiredis]` packs commands in C
    and never calls `.encode`. The chassis is async, and the async
    client goes through the Python encoder and raises. Probing the
    convenient client would have "confirmed" there was no bug.
    """
    from app.agents.protocol import StepProgress

    class BadStepAgent(AgentProtocol):
        agent_id = "subclass-step"
        display_name = "Subclass step"
        description = "Reports progress under an unencodable str subclass"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(
                StepProgress(step_id=_HostileStr("work"), status="complete")
            )
            return AnalysisResult(display={}, structured={"ok": True})

    result = await run_battery(
        BadStepAgent(), Scenario("subclass-step", {}), require_traces=False
    )
    assert not result.passed
    assert any(
        "cannot be encoded for Redis" in f and "aborts the phase" in f
        for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_pseudo_string_report_html_is_a_type_failure_not_normalized_text():
    """P2: coercion must not launder a type failure into a pass.

    An object whose `__class__` property returns `str` passes
    `isinstance` and is not a string. Substituting its `repr` made the
    text check answer "storable" while the ORIGINAL object stayed in
    the result — so the battery blessed a `report_html` that production
    then calls `len(... or "")` on (agent_runner.py:518) and dies on.
    A failed coercion has to stay a failure.
    """
    from app.agents.protocol import StepProgress

    class NotReallyAString:
        @property
        def __class__(self):
            return str

        def __repr__(self):
            return "<p>looks like a report</p>"

    class PseudoReportAgent(AgentProtocol):
        agent_id = "pseudo-report"
        display_name = "Pseudo report"
        description = "report_html that only claims to be a string"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            out = AnalysisResult(display={}, structured={"ok": True})
            object.__setattr__(out, "report_html", NotReallyAString())
            return out

    # The premise: production's own expression raises on this object.
    with pytest.raises(TypeError):
        len(NotReallyAString() or "")

    result = await run_battery(
        PseudoReportAgent(),
        Scenario("pseudo-report", {}),
        require_traces=False,
        output_mode="html_report",
    )
    assert not result.passed
    # Wording changed when the battery began replaying the runner's
    # STEPS — its reads AND the operation at each position — rather than
    # its reads alone. Production reaches this value's step and raises
    # there, so the failure now names the step and its line instead of
    # describing the value. Same verdict, exact location.
    assert any(
        "agent_runner.py:519" in f and "report_html" in f for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_str_subclass_carrying_a_nul_is_still_reported():
    """Normalizing must not cost the check it was blocking.

    The danger of "coerce it and move on" is silently disabling the
    inspection: the subclass is normalized so the battery CAN look, and
    what it then finds must still be reported.
    """
    from app.agents.protocol import StepProgress

    class BadSubclassAgent(AgentProtocol):
        agent_id = "subclass-nul"
        display_name = "Subclass NUL"
        description = "A str subclass whose text PostgreSQL cannot hold"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(
                display={}, structured={"answer": _HostileStr("bad\x00text")}
            )

    result = await run_battery(
        BadSubclassAgent(), Scenario("subclass-nul", {}), require_traces=False
    )
    assert not result.passed
    assert any("NUL" in f for f in result.failures), result.failures


class _OneArgEncodeStr(str):
    """A `str` subclass whose `encode` takes the signature people write.

    `def encode(self, encoding="utf-8")` is a perfectly ordinary
    override — and redis-py calls `value.encode(self.encoding,
    self.encoding_errors)`, with TWO positional arguments. So this
    object satisfies a one-argument probe and raises `TypeError` on the
    real call.
    """

    def encode(self, encoding="utf-8"):
        return str.encode(self, encoding)


@pytest.mark.asyncio
async def test_redis_emulation_uses_the_real_two_argument_call():
    """P2: an emulation written from a DESCRIPTION drifts from it.

    The previous round's check called `p.step_id.encode("utf-8")` —
    one argument — directly beneath a comment correctly quoting
    redis-py's two-argument `value.encode(encoding, errors)`. A
    subclass accepting one and refusing two therefore passed the
    battery and aborted the real phase.

    Measured against live async Redis:

        v.encode("utf-8")            -> b'work'
        v.encode("utf-8", "strict")  -> TypeError
        await redis.hset(..., v, ..) -> TypeError

    The fix is not "pass another argument" — it is to call redis-py's
    own `Encoder` instead of a hand-written imitation, the same rule
    this module already follows for the chassis' validator and redactor.
    """
    from app.agents.protocol import StepProgress

    v = _OneArgEncodeStr("work")
    # The premise, on record: a one-argument probe says this is fine.
    assert v.encode("utf-8") == b"work"
    with pytest.raises(TypeError):
        v.encode("utf-8", "strict")

    class OneArgStepAgent(AgentProtocol):
        agent_id = "one-arg-step"
        display_name = "One arg"
        description = "step_id whose encode refuses redis-py's signature"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id=v, status="complete"))
            return AnalysisResult(display={}, structured={"ok": True})

    result = await run_battery(
        OneArgStepAgent(), Scenario("one-arg", {}), require_traces=False
    )
    assert not result.passed
    assert any("cannot be encoded for Redis" in f for f in result.failures), result.failures


def test_the_emulation_is_redis_pys_own_encoder_not_a_copy():
    """The allow-list and the encoder must agree with redis-py itself.

    Asserted against the real `Encoder` rather than against a restated
    table, so a redis-py change that alters what is storable shows up
    here as a failure instead of as a battery that certifies a value
    production refuses.
    """
    from adapter_kit import _redis_encode

    # Accepted by redis-py, so accepted here.
    for ok in ("plain", 7, 1.5, b"raw", bytearray(b"raw")):
        _redis_encode(ok)

    # Refused by redis-py, so refused here — `bool` is the trap, since
    # `isinstance(True, int)` makes it look storable.
    for bad in (True, False, object(), None, ["list"]):
        with pytest.raises(Exception):
            _redis_encode(bad)


@pytest.mark.asyncio
async def test_repr_returning_a_str_subclass_does_not_escape_the_battery():
    """P2: `_safe_repr` normalized its FALLBACK but not its success path.

    `repr()` guarantees a `str` INSTANCE, which includes a subclass —
    so an agent whose `__repr__` returns one gets that object handed
    back by `_safe_repr`, and the very next thing every caller does is
    drop it into an f-string, running the subclass's `__format__`.
    `_json_problem` does exactly that for an unsupported mapping key,
    so `run_battery` escaped instead of returning a failed result.
    """
    from app.agents.protocol import StepProgress

    class ReprLiar:
        """An unsupported mapping key whose repr is a hostile subclass."""

        def __repr__(self):
            return _HostileStr("looks fine")

    # The premise, measured: `repr()` does not normalize.
    assert type(repr(ReprLiar())) is _HostileStr
    with pytest.raises(RuntimeError):
        f"{repr(ReprLiar())}"

    class ReprLiarAgent(AgentProtocol):
        agent_id = "repr-liar"
        display_name = "Repr liar"
        description = "Keys its output by an object with a hostile repr"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={ReprLiar(): "v"})

    # The assertion that matters is that this RETURNS.
    result = await run_battery(
        ReprLiarAgent(), Scenario("repr-liar", {}), require_traces=False
    )
    assert not result.passed
    assert any("json.dumps" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_unrenderable_result_error_is_reported_not_raised():
    """P2: `result.error` is diagnostic detail PRODUCTION NEVER READS.

    The runner records the failed status and exits; it never touches
    `.error`. So the battery was the only thing dying on it — a pure
    false negative with no production counterpart. The old line managed
    three unguarded operations at once: `getattr` (a raising property
    propagates), truthiness (`__bool__` is the agent's), and bare
    interpolation (`str()` raises on `10**5000`).
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    class BadErrorAgent(AgentProtocol):
        agent_id = "bad-error"
        display_name = "Bad error"
        description = "Fails with an error value that cannot be rendered"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return InvestigationResult(
                status="error", error=10**5000, structured={"partial": True}
            )

    result = await run_battery(
        BadErrorAgent(), Scenario("bad-error", {}), require_traces=False
    )
    assert not result.passed
    # The status failure is still reported, and the unrenderable detail
    # is described rather than dropped.
    assert any("16610 bits" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_result_attributes_that_raise_on_read_are_reported():
    """A result attribute is the AGENT's to define, so it may be a
    property that raises — and `getattr(..., None)` only swallows
    `AttributeError`.

    Production consumes `status`, `structured` and `report_html`, so a
    raising property is a genuine failure there too; it must be REPORTED
    (not silently coerced to `None`, which would pass), and the battery
    must still return a `BatteryResult`.
    """
    from app.agents.protocol import StepProgress

    class ExplodingResult(AnalysisResult):
        # The setter is a no-op so the dataclass `__init__` can still
        # build the object; only READING the field raises, which is the
        # shape of a lazily-computed property that fails.
        @property
        def structured(self):
            raise RuntimeError("structured is not available")

        @structured.setter
        def structured(self, value):
            pass

    class ExplodingAttrAgent(AgentProtocol):
        agent_id = "exploding-attr"
        display_name = "Exploding attr"
        description = "Result whose structured property raises on read"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            out = ExplodingResult(display={}, structured={})
            return out

    result = await run_battery(
        ExplodingAttrAgent(), Scenario("exploding-attr", {}), require_traces=False
    )
    assert not result.passed
    # Wording changed when the battery began replaying the runner's
    # STEPS — its reads AND the operation at each position — rather than
    # its reads alone. Production reaches this value's step and raises
    # there, so the failure now names the step and its line instead of
    # describing the value. Same verdict, exact location.
    assert any(
        "agent_runner.py:493" in f and "result.structured" in f
        for f in result.failures
    ), result.failures


def _counting_agent(agent_id, fail_on, **result_kw):
    from app.agents.protocol import StepProgress

    class CountingAgent(AgentProtocol):
        display_name = "Counting"
        description = "Result whose attribute reads are counted"

        def __init__(self):
            self.agent_id = agent_id

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return _make_counting(fail_on, **result_kw)

    return CountingAgent()


def _make_counting(fail_on, **kw):
    reads: dict = {}

    class Counted(AnalysisResult):
        @property
        def status(self):
            return _tick("status", kw.get("status", "complete"))

        @status.setter
        def status(self, v):
            pass

        @property
        def report_html(self):
            return _tick("report_html", kw.get("report_html"))

        @report_html.setter
        def report_html(self, v):
            pass

    def _tick(name, value):
        n = reads.get(name, 0) + 1
        reads[name] = n
        if n == fail_on.get(name):
            raise RuntimeError(f"{name} raised on read #{n}")
        return value

    out = Counted(display={}, structured=kw.get("structured", {"ok": True}))
    return out


@pytest.mark.asyncio
async def test_status_is_read_as_many_times_as_the_runner_reads_it():
    """P2: a property is not a value.

    The runner reads `result.status` THREE times — the success test
    (agent_runner.py:495), the span attribute (:516/:529) and the
    completion log (:537). A stateful property answering "complete"
    once and raising later errors the run at that third access, while a
    battery caching one read reports PASS.
    """
    agent = _counting_agent("counting-status", {"status": 3})
    result = await run_battery(
        agent, Scenario("counting", {}), require_traces=False
    )
    assert not result.passed, result.summary()
    # Wording changed when the battery began replaying the runner's
    # STEPS — its reads AND the operation at each position — rather than
    # its reads alone. Production reaches this value's step and raises
    # there, so the failure now names the step and its line instead of
    # describing the value. Same verdict, exact location.
    assert any(
        "agent_runner.py:537" in f and "result.status" in f and "three times" in f
        for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_final_report_html_is_read_twice_like_the_runner():
    """P2: the final phase persists `report_html` and then measures it.

    Two separate reads (agent_runner.py:499 and :518-519). A property
    that returns valid text once and raises on the second errors the run
    after the work is done — and a single cached read certified it.
    """
    agent = _counting_agent(
        "counting-report", {"report_html": 2}, report_html="<p>fine</p>"
    )
    result = await run_battery(
        agent,
        Scenario("counting", {}),
        require_traces=False,
        output_mode="html_report",
    )
    assert not result.passed, result.summary()
    # Wording changed when the battery began replaying the runner's
    # STEPS — its reads AND the operation at each position — rather than
    # its reads alone. Production reaches this value's step and raises
    # there, so the failure now names the step and its line instead of
    # describing the value. Same verdict, exact location.
    assert any(
        "agent_runner.py:519" in f and "result.report_html" in f
        and "two separate reads" in f
        for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_non_final_report_html_failure_is_not_fatal():
    """P2: the mirror image — the battery must not be STRICTER either.

    Both `report_html` accesses sit inside the runner's `if is_final:`
    branches, so a non-final phase never touches the property and
    production advances. Making that read fatal invented a failure
    production does not have AND truncated the remaining phases, which
    is the more expensive half of the mistake.
    """
    from app.agents.protocol import StepProgress

    class ExplodingReport(AnalysisResult):
        @property
        def report_html(self):
            raise RuntimeError("report_html is not available")

        @report_html.setter
        def report_html(self, v):
            pass

    class TwoPhaseAgent(AgentProtocol):
        agent_id = "two-phase-report"
        display_name = "Two phase"
        description = "Non-final phase whose report_html property raises"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def run_phase(self, phase, inp, on_progress):
            await on_progress(StepProgress(step_id=f"{phase}:work", status="complete"))
            if phase == "analyze":
                return ExplodingReport(display={}, structured={"first": True})
            return AnalysisResult(display={}, structured={"second": True})

    result = await run_battery(
        TwoPhaseAgent(),
        Scenario("two-phase", {}),
        phases=["analyze", "investigate"],
        require_traces=False,
    )
    # The second phase MUST have run — that is the regression.
    assert result.phases_run == ["analyze", "investigate"], result.summary()
    # And the unreadable attribute is still reported, just not fatally.
    assert any(
        "result.report_html could not be read" in f and "survives" in f
        for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_identity_property_that_raises_is_reported_not_raised():
    """P2: an invalid agent is exactly what this API must DIAGNOSE.

    `agent_id` was read unguarded twice — once for `BatteryResult` and
    again in the identity loop — so a raising property escaped
    `run_battery`, handing the caller a traceback instead of the failed
    result they asked for.
    """

    class NoIdentityAgent(AgentProtocol):
        display_name = "No identity"
        description = "agent_id property raises"

        @property
        def agent_id(self):
            raise RuntimeError("agent_id is not available")

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            from app.agents.protocol import StepProgress

            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={"ok": True})

    # The assertion that matters is that this RETURNS.
    result = await run_battery(
        NoIdentityAgent(), Scenario("no-identity", {}), require_traces=False
    )
    assert not result.passed
    assert any("agent_id could not be read" in f for f in result.failures), result.failures
    assert result.agent_id == "<unset>"


@pytest.mark.asyncio
async def test_the_first_read_decides_the_verdict_like_the_runner():
    """P2: counting the reads was only half the emulation.

    The runner's OUTCOME hangs on the FIRST access — `ok = getattr(
    result, "status", None) in _SUCCESS_STATUSES` (agent_runner.py:495)
    — while reads two and three feed the span attribute and the
    completion log. A `status` answering `"error"` once and
    `"complete"` twice therefore errors the run in production, and a
    battery keeping the LAST value reported PASS.
    """
    from app.agents.protocol import StepProgress

    answers = iter(["error", "complete", "complete"])

    class DriftingStatus(AnalysisResult):
        @property
        def status(self):
            return next(answers, "complete")

        @status.setter
        def status(self, v):
            pass

    class DriftingAgent(AgentProtocol):
        agent_id = "drifting-status"
        display_name = "Drifting"
        description = "status answers error once, then complete"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return DriftingStatus(display={}, structured={"ok": True})

    result = await run_battery(
        DriftingAgent(), Scenario("drifting", {}), require_traces=False
    )
    assert not result.passed, result.summary()
    # The verdict follows read #1 — the one production acts on.
    assert any(
        "returned status 'error'" in f for f in result.failures
    ), result.failures
    # And the divergence between reads is surfaced in its own right.
    assert any(
        "changed between the runner's repeated reads" in f for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_status_whose_hash_raises_is_reported_not_raised():
    """P2: `except TypeError` was the reported exception, not the class.

    `__hash__` is the agent's code and may raise anything. This guard is
    called OUTSIDE the phase exception handler, so a miss escapes
    `run_battery` entirely — the caller gets a traceback instead of the
    failed result they asked for.
    """
    from app.agents.protocol import StepProgress

    class HostileHash:
        def __hash__(self):
            raise RuntimeError("hash is not available")

        def __repr__(self):
            return "<HostileHash>"

    class HostileHashAgent(AgentProtocol):
        agent_id = "hostile-hash"
        display_name = "Hostile hash"
        description = "status object whose __hash__ raises"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            out = AnalysisResult(display={}, structured={"ok": True})
            object.__setattr__(out, "status", HostileHash())
            return out

    # The premise: the runner's own expression raises on this value.
    from app.services.agent_runner import _SUCCESS_STATUSES

    with pytest.raises(RuntimeError):
        HostileHash() in _SUCCESS_STATUSES

    # The assertion that matters is that this RETURNS.
    result = await run_battery(
        HostileHashAgent(), Scenario("hostile-hash", {}), require_traces=False
    )
    assert not result.passed
    assert any(
        "status the runner cannot test" in f and "RuntimeError: hash is not available" in f
        for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_the_status_membership_verdict_is_computed_once():
    """P2: two membership tests where production performs one.

    A stateful `__hash__` that raises on the first check and succeeds on
    the second made `phase_fatal` true from call #1 while call #2
    returned no problem — so the phase aborted and `result.failures`
    stayed empty. `passed` is `not failures`, so the battery reported
    **PASS while refusing to run the phase**, which is the worst of both.
    """
    from app.agents.protocol import StepProgress

    class FlakyHash:
        _calls = 0

        def __hash__(self):
            type(self)._calls += 1
            if type(self)._calls == 1:
                raise RuntimeError("hash unavailable on first call")
            return hash("complete")

        def __eq__(self, other):
            return other == "complete"

        def __repr__(self):
            return "<FlakyHash>"

    class FlakyHashAgent(AgentProtocol):
        agent_id = "flaky-hash"
        display_name = "Flaky hash"
        description = "status whose __hash__ raises only the first time"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            out = AnalysisResult(display={}, structured={"ok": True})
            object.__setattr__(out, "status", FlakyHash())
            return out

    result = await run_battery(
        FlakyHashAgent(), Scenario("flaky-hash", {}), require_traces=False
    )
    # Production errors on its single membership test, so the battery
    # must FAIL — and must say why rather than aborting silently.
    assert not result.passed, result.summary()
    assert any(
        "status the runner cannot test" in f for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_reads_are_replayed_in_the_runners_interleaved_order():
    """P2: matching each attribute's COUNT is not matching the runner.

    For a final phase production reads `status`, `report_html`,
    `status`, `report_html`, `status` — interleaved. Grouping them as
    `status × 3` then `report_html × 2` is a different experiment
    whenever the properties share state, and this agent detects the
    difference: `status` raises once `report_html` has been read twice,
    which the runner reaches (its third status read follows both report
    reads) and the grouped version never does.
    """
    from app.agents.protocol import StepProgress

    class SharedState(AnalysisResult):
        _reports = 0

        @property
        def report_html(self):
            type(self)._reports += 1
            return "<p>report</p>"

        @report_html.setter
        def report_html(self, v):
            pass

        @property
        def status(self):
            if type(self)._reports >= 2:
                raise RuntimeError("status after the second report read")
            return "complete"

        @status.setter
        def status(self, v):
            pass

    class SharedStateAgent(AgentProtocol):
        agent_id = "shared-state"
        display_name = "Shared state"
        description = "status and report_html share a counter"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            SharedState._reports = 0
            return SharedState(display={}, structured={"ok": True})

    result = await run_battery(
        SharedStateAgent(),
        Scenario("shared-state", {}),
        require_traces=False,
        output_mode="html_report",
    )
    assert not result.passed, result.summary()
    # Wording changed when the battery began replaying the runner's
    # STEPS — its reads AND the operation at each position — rather than
    # its reads alone. Production reaches this value's step and raises
    # there, so the failure now names the step and its line instead of
    # describing the value. Same verdict, exact location.
    assert any(
        "agent_runner.py:537" in f and "result.status" in f
        for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_identity_that_only_claims_to_be_a_string_is_rejected():
    """P2: `isinstance(agent_id, str)` is not "is a string".

    An object whose `__class__` says `str` passes with no string behind
    it — and discovery compares `instance.agent_id` against the
    manifest's validated string id (registry.py:315), so the agent never
    registers. The battery would have certified something production
    refuses to offer at all.
    """
    from app.agents.protocol import StepProgress

    class PseudoId:
        @property
        def __class__(self):
            return str

        def __repr__(self):
            return "<PseudoId>"

    class PseudoIdAgent(AgentProtocol):
        display_name = "Pseudo id"
        description = "agent_id only claims to be a string"
        agent_id = PseudoId()

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={"ok": True})

    # The premise: it passes `isinstance` and fails an equality compare
    # against the manifest's real string.
    assert isinstance(PseudoIdAgent.agent_id, str)
    assert PseudoIdAgent.agent_id != "pseudo-id"

    result = await run_battery(
        PseudoIdAgent(), Scenario("pseudo-id", {}), require_traces=False
    )
    assert not result.passed
    assert any(
        "agent agent_id is" in f and "not a string" in f for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_the_membership_test_runs_before_report_html_is_read():
    """P2: replaying reads in order is not replaying the runner.

    Production performs the membership test at `agent_runner.py:495`
    BEFORE it reads `report_html` at `:499`. A `__hash__` with a side
    effect that the report property observes therefore leaves the two in
    a different state than a replay that fetches every attribute first
    and tests afterwards.
    """
    from app.agents.protocol import StepProgress

    class HashThenReport(AnalysisResult):
        _hashed = False

        @property
        def status(self):
            return _TouchyStatus()

        @status.setter
        def status(self, v):
            pass

        @property
        def report_html(self):
            if type(self)._hashed:
                raise RuntimeError("report read after the status was hashed")
            return "<p>report</p>"

        @report_html.setter
        def report_html(self, v):
            pass

    class _TouchyStatus(str):
        def __hash__(self):
            HashThenReport._hashed = True
            return hash("complete")

        def __eq__(self, other):
            return other == "complete"

    class OrderedAgent(AgentProtocol):
        agent_id = "ordered-ops"
        display_name = "Ordered ops"
        description = "hashing the status changes what report_html does"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            HashThenReport._hashed = False
            return HashThenReport(display={}, structured={"ok": True})

    result = await run_battery(
        OrderedAgent(),
        Scenario("ordered", {}),
        require_traces=False,
        output_mode="html_report",
    )
    # Production hashes at :495 and then reads report_html at :499, which
    # raises. The battery must reach the same state and fail.
    assert not result.passed, result.summary()
    assert any(
        "agent_runner.py:499" in f and "report_html" in f for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_the_drifts_pop_is_replayed_where_the_runner_does_it():
    """P2: `structured.pop("_drifts", None)` is a runner step too.

    It sits at `agent_runner.py:494` — between the `dict()` coercion at
    :493 and the status membership test at :495. The battery performed
    it later, outside the ordered replay, which is wrong twice over: a
    key whose `__eq__` raises escaped `run_battery` from that line, and
    a key whose `__eq__` MUTATES shared state was observed in the wrong
    order by everything downstream.
    """
    from app.agents.protocol import StepProgress

    class CollidingKey:
        """Hashes to `"_drifts"` so `pop` must compare it, then refuses."""

        def __hash__(self):
            return hash("_drifts")

        def __eq__(self, other):
            raise RuntimeError("equality is not available")

        def __repr__(self):
            return "<CollidingKey>"

    class CollidingKeyAgent(AgentProtocol):
        agent_id = "colliding-key"
        display_name = "Colliding key"
        description = "structured key that collides with _drifts and refuses =="

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={CollidingKey(): "v"})

    # The premise: the runner's own line raises on this dict.
    with pytest.raises(RuntimeError):
        {CollidingKey(): "v"}.pop("_drifts", None)

    # The assertion that matters is that this RETURNS.
    result = await run_battery(
        CollidingKeyAgent(), Scenario("colliding", {}), require_traces=False
    )
    assert not result.passed
    assert any(
        "agent_runner.py:494" in f and "_drifts" in f for f in result.failures
    ), result.failures


@pytest.mark.asyncio
async def test_output_inspection_survives_agent_code_that_raises_anything():
    """P2: every walk over agent output runs agent code, which can raise anything.

    Review named one branch: ``sorted(value.keys())`` was guarded with
    ``except TypeError``, because TypeError is what an unorderable key
    SET raises — and that was the only failure the guard imagined. But
    sorting runs the keys' own ``__lt__``, and a real ``str`` subclass
    owns that method. The runner reaches the same expression at
    ``agent_runner.py:517``, *after* ``await db.commit()``, so an escape
    is a crash with the result already stored.

    The branch review named was not the whole set, which is why this
    test asserts the set. Two more walks over the same value ran the
    same kind of agent code with no guard at all:

    * ``_json_lossy`` iterates with ``value.items()`` and
      ``enumerate(value)`` — a real ``dict``/``list`` subclass owns both.
    * ``json.dumps`` itself calls into them: a ``dict`` subclass is
      encoded through ``PyMapping_Items``, a ``list`` subclass through
      its ``__iter__``.

    All three were measured escaping ``run_battery`` as a raw
    ``RuntimeError`` — there is no enclosing ``try`` between the call
    site and the function's signature — so the adapter author got a
    traceback out of the battery instead of a failed ``BatteryResult``
    naming their own defect.
    """
    from app.agents.protocol import StepProgress

    def _agent_returning(payload):
        class HostileOutputAgent(AgentProtocol):
            agent_id = "hostile-output"
            display_name = "Hostile output"
            description = "Returns output whose own methods raise"

            def input_schema(self) -> dict:
                return {"type": "object"}

            async def analyze(self, inp: AgentInput, on_progress):
                await on_progress(StepProgress(step_id="work", status="complete"))
                return AnalysisResult(display={}, structured=payload)

        return HostileOutputAgent()

    class RaisingLt(str):
        """A *real* str subclass — isinstance is not being lied to here."""

        def __lt__(self, other):
            raise RuntimeError("comparison is not available")

        def __gt__(self, other):
            raise RuntimeError("comparison is not available")

    class RaisingItems(dict):
        def items(self):
            raise RuntimeError("items is not available")

    class RaisingIter(list):
        def __iter__(self):
            raise RuntimeError("iteration is not available")

    for label, payload in (
        # The branch review named: sorted() reaching a raising __lt__.
        ("__lt__", {RaisingLt("b"): 1, RaisingLt("a"): 2}),
        # Its siblings, which the named fix would not have reached.
        ("items", {"k": RaisingItems({"a": 1})}),
        ("__iter__", {"k": RaisingIter([1, 2])}),
    ):
        try:
            result = await run_battery(
                _agent_returning(payload), Scenario("hostile", {}), require_traces=False
            )
        except Exception as exc:  # pragma: no cover - the defect being fixed
            raise AssertionError(
                f"{label}: run_battery raised {type(exc).__name__}: {exc} "
                f"instead of returning a failed BatteryResult"
            ) from exc

        assert not result.passed, f"{label}: hostile output passed the battery"
        # Not merely "it failed" — it must say the run cannot be
        # persisted, since that is what the chassis does with it.
        # The wording depends on WHERE production dies, which is the
        # point of replaying its steps: the `__lt__` case now fails at
        # the sort (:517) rather than in the persistence walk, because
        # that is where the runner actually sorts. All three phrasings
        # say the same thing — this output cannot complete a real run.
        assert any(
            "cannot persist" in f
            or "could not inspect" in f
            or "agent_runner.py:517" in f
            for f in result.failures
        ), f"{label}: {result.failures}"

    # The reason the __lt__ case is worth naming precisely: the author
    # needs to know it is the runner's own sort, and where.
    lt_result = await run_battery(
        _agent_returning({RaisingLt("b"): 1, RaisingLt("a"): 2}),
        Scenario("hostile-lt", {}),
        require_traces=False,
    )
    assert any(
        "agent_runner.py:517" in f and "sorted" in f
        for f in lt_result.failures
    ), lt_result.failures
    # And the exception TYPE has to survive into the message: a bare
    # RuntimeError("...") renders as just its text under `str`, which
    # reads like a description rather than a failure.
    assert any("RuntimeError" in f for f in lt_result.failures), lt_result.failures

    # A boundary that turns everything into a failure would be worthless.
    # Ordinary output — including the nesting, containers and uniform
    # non-string keys the walk is required to accept — still passes.
    ok = await run_battery(
        _agent_returning(
            {"rows": [{"a": 1}, {"b": [2, 3]}], "n": {1: "x", 2: "y"}, "s": "t"}
        ),
        Scenario("hostile-ok", {}),
        require_traces=False,
    )
    assert ok.passed, ok.summary()


# GitHub Actions loads BOTH extensions from .github/workflows, and the
# choice between them is pure preference. A checker that globs one of
# them is a check that silently stops checking the day somebody uses the
# other — the same shape as every other defect in this file's late
# rounds, one level further out: the guard itself had a blind spot, so
# the tree could go stale underneath a passing test.
_WORKFLOW_SUFFIXES = (".yml", ".yaml")


def _workflow_dir():
    from pathlib import Path

    return Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _workflow_files(directory):
    """Every file GitHub Actions would load as a workflow, both suffixes.

    Not recursive: Actions reads workflows from the top level of
    ``.github/workflows`` only, so a nested file is not a workflow and
    counting it would invent coverage that does not exist.
    """
    files = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix in _WORKFLOW_SUFFIXES
    )
    # The non-empty invariant lives HERE, in the collector, and that is the
    # whole point (Codex round 20). Every guard in this file that walks the
    # real tree is a loop over this list, and a loop over an empty list
    # does nothing and reports success. Enforcing it at each consumer means
    # enumerating the consumers, and every way of doing that is a guess: by
    # source text (a name can appear for other reasons), or by observing the
    # call (locating a sibling path is not censusing). Enforcing it at the
    # single collector needs no enumeration at all — a future guard is
    # covered the moment it calls this, which it must.
    #
    # Only for the REAL directory. Fixtures build their own trees in
    # tmp_path and may legitimately be empty while testing that very case.
    if not files and directory == _workflow_dir():
        raise AssertionError(
            f"no workflow files under {directory}. Whatever asked for this "
            f"census would have looped over nothing and reported success; "
            f"either the path is wrong or CI has been deleted, and neither "
            f"should look like a pass."
        )
    return files


def _load_workflow(path):
    import yaml

    return yaml.safe_load(path.read_text()) or {}


def _workflow_triggers(doc):
    """The ``on:`` block, normalised to ``{event: config}``.

    Two separate traps here, both measured rather than assumed.

    PyYAML resolves the bare key ``on`` to the BOOLEAN ``True`` — YAML
    1.1 treats on/off/yes/no as booleans — so ``doc["on"]`` silently
    misses on every GitHub workflow file.

    And ``on:`` has three legal shapes, not one::

        on: {pull_request: {paths: [...]}}   -> dict
        on: [push, pull_request]             -> list
        on: pull_request                     -> str

    The list and string forms mean "this event, no filters". Code that
    assumes the mapping raises ``AttributeError`` on the other two, so
    the shape is normalised here, once, rather than at each caller.

    ``config`` is ``None`` for an event with no configuration — which is
    NOT the same as the event being absent, and conflating the two is
    exactly what let a missing ``pull_request:`` trigger pass a check
    that existed to require it.
    """
    on = doc.get(True)
    if on is None:
        on = doc.get("on")
    if on is None:
        return {}
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {event: None for event in on}
    return dict(on)


# Probe paths that a "runs on workflow changes" filter has to admit.
#
# The honest limitation first: the property wanted is universal — for
# EVERY possible workflow filename, that path is selected — and no
# finite list of probes proves it. Two probes proved even less than
# that: a filter literally naming those two files passed while any
# differently named workflow went uncovered, which is enumeration
# masquerading as coverage.
#
# So the probes are chosen to defeat the ways a filter is realistically
# wrong rather than to be exhaustive, and they are varied enough that
# only a pattern general over the directory passes all of them:
#
#   * both extensions, since GitHub loads .yml and .yaml alike
#   * a file that exists and files that do not, because the case the
#     guards are FOR is a pull request that ADDS a workflow
#   * names differing in shape — hyphens, digits, an embedded dot, a
#     long name — so a filter enumerating a couple of literals, or one
#     assuming a naming convention, fails at least one probe
#
# A filter could still be written to enumerate all of these, but such a
# filter is self-defeating rather than plausibly mistaken, and the test
# below asserts the specific enumeration that review found.
_SAMPLE_WORKFLOW_PATHS = (
    ".github/workflows/librerun-smoke.yml",
    ".github/workflows/a-workflow-added-by-this-pr.yaml",
    ".github/workflows/new-check.yaml",
    ".github/workflows/a.yml",
    ".github/workflows/release-2.yaml",
    ".github/workflows/deploy.prod.yml",
)


def _glob_matches(pattern, path):
    """GitHub Actions path-filter globbing, as GitHub defines it.

    ``*`` matches any run of characters except ``/``; ``**`` matches any
    run including ``/``; ``?`` matches one non-``/`` character.

    Written as a matcher rather than a prefix comparison because the
    prefix version could only recognise the exact spelling it was
    written against. ``.github/workflows/**`` was accepted and
    ``.github/**`` — which excludes strictly more — was not, so a
    broader exclusion silenced the guards while the check stayed green.
    Measured against a table of real filter spellings, both directions.
    """
    import re

    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        char = pattern[i]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    return re.fullmatch("".join(out), path) is not None


def _filter_selects(patterns, path):
    """Does a whole ORDERED path filter select this path?

    A GitHub filter is a sequence, not a set. Patterns apply in order, a
    leading ``!`` negates, and a later pattern overrides an earlier one,
    so ``["**", "!.github/workflows/**"]`` selects nothing under
    ``.github/workflows`` while ``["!.github/workflows/**", "**"]`` puts
    it back. Testing patterns independently with ``any()`` sees the
    first match and never reaches the exclusion that undoes it.
    """
    selected = False
    for raw in patterns:
        pattern = str(raw)
        negated = pattern.startswith("!")
        if negated:
            pattern = pattern[1:]
        if _glob_matches(pattern, path):
            selected = not negated
    return selected


def _guard_trigger_problem(name, doc):
    """Why this workflow would not run the guards on a workflow change.

    ``None`` when it would. Split out from the test so the rule can be
    driven against every legal ``on:`` shape, including the ones this
    repository does not happen to use — a check whose blind spots are
    only visible in trees that do not exist here is how the last three
    rounds went.
    """
    triggers = _workflow_triggers(doc)

    # Presence, not truthiness. `pull_request:` with an empty body and
    # no `pull_request` key at all both read as None through `.get`,
    # and they mean opposite things: the first runs on every pull
    # request, the second runs on none.
    if "pull_request" not in triggers:
        return (
            f"{name} runs the workflow guards but has no `pull_request` "
            f"trigger at all, so the guards never run on a pull request — "
            f"removing that trigger would silence them completely"
        )

    config = triggers["pull_request"]
    if not isinstance(config, dict):
        # `pull_request:` bare, or the list/string form: every PR.
        return None

    # ANY sample being ignored is a problem: GitHub skips the run when
    # the changed files all match paths-ignore, and a PR that touches
    # one workflow has exactly one changed file.
    ignored = config.get("paths-ignore") or []
    skipped = [p for p in _SAMPLE_WORKFLOW_PATHS if _filter_selects(ignored, p)]
    if skipped:
        return (
            f"{name} runs the workflow guards but paths-ignore {ignored} "
            f"excludes {skipped}, so a PR touching such a file skips them"
        )

    paths = config.get("paths")
    if paths is None:
        return None  # no filter: every pull request.

    # EVERY sample must be selected, not merely one of them. Each sample
    # stands for a possible single-file pull request, and GitHub runs the
    # workflow only if a changed file matches — so a filter like
    # `.github/workflows/*.yml` covers the sample that happens to end
    # `.yml` and silently drops the `.yaml` one. `any()` here would
    # accept exactly the filter that reintroduces the round-42 defect.
    missed = [p for p in _SAMPLE_WORKFLOW_PATHS if not _filter_selects(paths, p)]
    if missed:
        return (
            f"{name} runs the workflow guards but only on {paths}, which "
            f"does not select {missed} — a PR that adds or renames such a "
            f"workflow, or edits the exporter alone, does not run them, so "
            f"the guards miss the very change they exist to catch"
        )
    return None


def test_ci_actually_runs_the_langgraph_half_of_the_battery():
    """P1: the adapter suite existed and no workflow ever ran it.

    ``test_adapter_kit_langgraph.py`` opens with
    ``pytest.importorskip("langgraph")``, and LangGraph is deliberately
    absent from ``backend/requirements.txt`` — it is an adapter-side
    dependency the chassis does not ship. Every workflow installed
    requirements.txt and nothing else, and none invoked that module by
    name, so all 25 of its tests skipped everywhere they were collected.
    The batch's headline deliverable — the adapter and its example —
    could regress and still show a full board of green ticks.

    This test lives in the FRAMEWORK-AGNOSTIC half on purpose, so it runs
    in the job that has no adapter installed. The environment that cannot
    run the adapter suite is exactly the one that should be checking
    somebody else does.

    Reading the workflows rather than trusting a comment is the point:
    the previous state of the tree had a comment in
    chassis-zero-agents.yml accurately describing the gap — "the
    LangGraph half is not run here" — which documented the hole instead
    of closing it. A comment cannot fail CI.
    """
    workflows = _workflow_files(_workflow_dir())
    assert workflows, "no workflows found — the path in this test is wrong"

    suite = "tests/test_adapter_kit_langgraph.py"
    runners = []
    for path in workflows:
        jobs = _load_workflow(path).get("jobs") or {}
        for job_name, job in jobs.items():
            steps = job.get("steps") or []
            commands = "\n".join(s.get("run", "") for s in steps)
            if suite in commands:
                runners.append((path.name, job_name, commands))

    assert runners, (
        f"no workflow job runs {suite} — it is behind "
        f"importorskip('langgraph'), so it skips wherever it is collected "
        f"and the adapter has no CI at all. Workflows checked: "
        f"{[p.name for p in workflows]}"
    )

    for workflow, job_name, commands in runners:
        where = f"{workflow}:{job_name}"
        # Running it is not enough. Without the adapter installed the
        # suite skips and the job still goes green — which is the defect
        # itself, wearing a tick.
        assert "./adapters" in commands, (
            f"{where} runs the adapter suite without installing the "
            f"adapter, so importorskip skips it and the job proves nothing"
        )
        # And the job must NOTICE a skip rather than trust the install.
        # Asserted through the shared rule, not by looking for the word
        # "skipped" in the step text: that matched this job's own INLINE
        # copy of the assertion, so the test went green on the weaker
        # `tests == 0` version it was supposed to be demanding better
        # than. Asserting on a word was asserting on a proxy.
        assert not _unchecked_pytest_runs(_load_workflow(_workflow_dir() / workflow)), (
            f"{where} never checks that the suite ran; a suite behind "
            f"importorskip reports success by disappearing, so the job "
            f"needs scripts/assert_suite_ran.py on its report, not just "
            f"the exit status"
        )


def test_the_workflow_checks_see_dot_yaml_files_too(tmp_path):
    """P2: the derived checks globbed `*.yml`, so a `.yaml` workflow was invisible.

    GitHub Actions loads ``.yml`` and ``.yaml`` alike and the choice is
    pure preference, so a workflow added with the other suffix was
    absent from the "every workflow" set that every derived check in
    this file is built on — the guard-trigger census, the "who runs the
    adapter suite" census, the pytest-reporting census. A guard with a
    blind spot is the failure this whole run of rounds keeps circling:
    it reports on what it looked at as though it had looked at
    everything.

    Driven against a fixture tree rather than the real one, because the
    property has to hold for a repository that HAS such a file, and the
    real repository may not at any given moment. A test that can only
    pass is not evidence.

    (S7a: this used to prove the property through the OTEL exporter's
    sync rule, which was the only consumer that happened to name a
    ``.yaml`` file. That workflow is retired — Groundcover is not a
    supported vendor, JR 2026-09-20 — so the property is asserted
    against ``_workflow_files`` itself, which is what every remaining
    census actually calls.)
    """
    (tmp_path / "alpha.yml").write_text("name: alpha\non: [push]\n")
    assert {p.name for p in _workflow_files(tmp_path)} == {"alpha.yml"}

    # The other suffix. GitHub would load it; a `*.yml` glob would not
    # see it; the collector must.
    (tmp_path / "beta.yaml").write_text("name: beta\non: [push]\n")
    found = {p.name for p in _workflow_files(tmp_path)}
    assert found == {"alpha.yml", "beta.yaml"}, (
        f"a .yaml workflow was not collected ({sorted(found)}) — the "
        f"census is globbing one suffix and calling it the whole tree"
    )

    # Not recursive, and not every file: Actions reads workflows from the
    # top level of .github/workflows only, so a nested file is not a
    # workflow and a README beside them is not one either. A collector
    # that swept them in would report failures for files GitHub never
    # loads, which is the same blind spot wearing the other face.
    (tmp_path / "notes.md").write_text("not a workflow\n")
    nested = tmp_path / "shared"
    nested.mkdir()
    (nested / "gamma.yml").write_text("name: gamma\non: [push]\n")
    assert {p.name for p in _workflow_files(tmp_path)} == {"alpha.yml", "beta.yaml"}

    # And the real tree is collected by the same call, non-empty, so this
    # property is about the census the other tests actually use.
    assert _workflow_files(_workflow_dir()), "no workflow files in the real tree"


def test_the_workflow_guards_run_when_a_workflow_changes():
    """P2: the guards lived behind a path filter that workflow edits do not match.

    These checks run in ``chassis-zero-agents.yml``, whose ``paths:``
    filter was ``backend/**`` plus its own file. A pull request that
    adds a workflow, renames one, or edits one alone matches neither —
    so the derived censuses never run on exactly the change they exist
    to catch, and a workflow slips past every one of them with a green
    board.

    A gate that does not fire on the change it guards is the same
    nothing as a comment.
    """
    guard_suite = "tests/test_adapter_kit.py"

    runners = []
    for path in _workflow_files(_workflow_dir()):
        doc = _load_workflow(path)
        for job in (doc.get("jobs") or {}).values():
            commands = "\n".join(s.get("run", "") for s in job.get("steps") or [])
            if guard_suite in commands:
                runners.append((path, doc))
                break

    assert runners, f"no workflow runs {guard_suite} — the guards never execute"

    for path, doc in runners:
        problem = _guard_trigger_problem(path.name, doc)
        assert problem is None, problem


def test_the_trigger_check_distinguishes_no_filter_from_no_trigger():
    """P2: the trigger check passed a workflow with NO pull_request trigger at all.

    ``triggers.get("pull_request")`` returns ``None`` both for
    ``pull_request:`` with an empty body — every pull request, no
    filters, which is what the guard wants — and for a workflow with no
    ``pull_request`` key whatsoever, which runs the guards on nothing.
    The previous version followed the same "no paths, therefore fine"
    branch for both, so deleting the trigger outright would have
    silenced the guards while this very test stayed green.

    That is the fourth consecutive round of the same shape: the checker
    reported on the case it imagined rather than the cases that exist.
    So this drives every legal ``on:`` form, including the list and
    string ones this repository does not use — where the old code did
    not merely mis-answer, it would have raised ``AttributeError`` on a
    mapping method the value does not have.
    """
    ok = {"paths": ["backend/**", ".github/workflows/**"]}

    # Accepted: the trigger exists and cannot filter workflow edits out.
    for label, on in (
        ("paths include workflows", {"pull_request": ok}),
        ("no paths filter", {"pull_request": {"branches": ["main"]}}),
        ("bare pull_request", {"pull_request": None}),
        ("list form", ["push", "pull_request"]),
        ("string form", "pull_request"),
    ):
        assert _guard_trigger_problem("w.yml", {True: on}) is None, label

    # Rejected: the trigger is absent, so the guards run on no PR at all.
    absent = _guard_trigger_problem("w.yml", {True: {"push": {"branches": ["main"]}}})
    assert absent is not None, (
        "a workflow with no pull_request trigger passed the guard-trigger "
        "check — `.get` cannot tell an absent event from an unconfigured one"
    )
    assert "no `pull_request` trigger" in absent, absent

    # Rejected: a paths filter that workflow edits do not match.
    narrow = _guard_trigger_problem(
        "w.yml", {True: {"pull_request": {"paths": ["backend/**"]}}}
    )
    assert narrow is not None and "only on" in narrow, narrow

    # Rejected: the filter allows workflow edits but paths-ignore takes
    # them straight back out again — the same hole with the opposite key.
    ignored = _guard_trigger_problem(
        "w.yml",
        {True: {"pull_request": {"paths-ignore": [".github/workflows/**"]}}},
    )
    assert ignored is not None and "paths-ignore" in ignored, ignored


def test_the_trigger_check_understands_broader_glob_patterns():
    """P2: the filter check compared prefixes, so a broader pattern slipped past.

    ``paths-ignore: [".github/**"]`` excludes strictly MORE than
    ``.github/workflows/**`` — every workflow file included — but a
    check written as ``startswith(".github/workflows")`` does not
    recognise it, so the guards could be silenced with the test green.

    The same literal-mindedness ran in the opposite direction on the
    positive filter: ``paths: [".github/**"]`` legitimately covers every
    workflow change, and the old ``startswith(...) and endswith("**")``
    pair would have REJECTED it. One blind spot, two costs — a hole and
    a false alarm.

    So both sides go through a real matcher now, and it is checked
    against the sample paths including one that does not exist yet,
    because the case the guards exist for is a PR that ADDS a workflow.
    """
    # The matcher itself, against GitHub's documented semantics: `*`
    # stops at a slash, `**` does not.
    sample = ".github/workflows/librerun-smoke.yml"
    assert _glob_matches(".github/workflows/**", sample)
    assert _glob_matches(".github/**", sample)
    assert _glob_matches("**", sample)
    assert _glob_matches("**/*.yml", sample)
    assert not _glob_matches(".github/*", sample), "single * must not cross a slash"
    assert not _glob_matches("backend/**", sample)

    # A new .yaml workflow — the file a guard-worthy PR actually adds —
    # is selected by the broad patterns and missed by a .yml-specific one.
    sample_yaml = ".github/workflows/a-workflow-added-by-this-pr.yaml"
    assert _filter_selects([".github/**"], sample_yaml)
    assert _filter_selects([".github/workflows/**"], sample_yaml)
    assert not _filter_selects(["docs/**"], sample_yaml)

    # paths-ignore with a BROADER pattern must be caught, not just the
    # one spelling the previous version was written against.
    for ignore in (".github/**", "**", ".github/workflows/**"):
        problem = _guard_trigger_problem(
            "w.yml", {True: {"pull_request": {"paths-ignore": [ignore]}}}
        )
        assert problem is not None and "paths-ignore" in problem, (
            f"paths-ignore {ignore!r} excludes workflow files and was not "
            f"caught — the check is comparing spellings, not matching paths"
        )

    # And the mirror image: a broad positive filter is legitimate and
    # must NOT be reported. Rejecting it would be the same blind spot
    # wearing the opposite sign.
    for allow in (".github/**", "**", ".github/workflows/**"):
        assert _guard_trigger_problem(
            "w.yml", {True: {"pull_request": {"paths": ["backend/**", allow]}}}
        ) is None, allow


def test_the_trigger_check_applies_ordered_negations_and_covers_every_sample():
    """P2: a filter is an ordered sequence, and one sample matching is not enough.

    Two ways the previous version accepted a filter that silences the
    guards, both measured against GitHub's documented semantics.

    **Order.** ``paths: ["**", "!.github/workflows/**"]`` is valid and
    excludes every workflow change — a later pattern overrides an
    earlier one. Testing patterns independently with ``any()`` saw
    ``**`` match and never reached the exclusion that undoes it.

    **Coverage.** ``paths: [".github/workflows/*.yml"]`` selects the
    sample that happens to end ``.yml`` and not the ``.yaml`` one, so
    ``any()`` accepted precisely the filter that reintroduces the
    round-42 defect: a PR adding a ``.yaml`` workflow skips the guards.
    Every sample stands for a possible single-file pull request, and
    GitHub runs the workflow only if a changed file matches, so the
    positive filter has to select them ALL.

    The two directions are deliberately asymmetric — a positive filter
    must select every sample, while an exclusion is a problem if it
    catches any — because that is what the two keywords actually mean.
    """
    yml = ".github/workflows/librerun-smoke.yml"
    yaml_new = ".github/workflows/a-workflow-added-by-this-pr.yaml"

    # Ordered evaluation, both directions of override.
    assert not _filter_selects(["**", "!.github/workflows/**"], yml)
    assert _filter_selects(["!.github/workflows/**", "**"], yml)
    assert _filter_selects(["backend/**", ".github/workflows/**"], yml)
    assert not _filter_selects(["backend/**"], yml)

    # A trailing negation must fail the guard check, not pass it.
    negated = _guard_trigger_problem(
        "w.yml",
        {True: {"pull_request": {"paths": ["**", "!.github/workflows/**"]}}},
    )
    assert negated is not None, (
        "an ordered `!` exclusion of workflow files was accepted — the "
        "check is testing patterns independently instead of applying the "
        "filter as the ordered sequence GitHub applies"
    )

    # ...and a re-inclusion after a negation must pass, or the check is
    # merely refusing anything containing a `!`.
    assert _guard_trigger_problem(
        "w.yml",
        {True: {"pull_request": {"paths": ["!.github/workflows/**", "**"]}}},
    ) is None

    # Coverage: a .yml-only filter leaves the .yaml sample unselected.
    assert _filter_selects([".github/workflows/*.yml"], yml)
    assert not _filter_selects([".github/workflows/*.yml"], yaml_new)
    partial = _guard_trigger_problem(
        "w.yml", {True: {"pull_request": {"paths": [".github/workflows/*.yml"]}}}
    )
    assert partial is not None and ".yaml" in partial, (
        f"a filter covering only .yml workflows was accepted, which is the "
        f"round-42 defect returning through the checker: {partial}"
    )

    # And an ordered exclusion in paths-ignore is caught too.
    ignored = _guard_trigger_problem(
        "w.yml",
        {True: {"pull_request": {"paths-ignore": ["docs/**", ".github/**"]}}},
    )
    assert ignored is not None and "paths-ignore" in ignored, ignored


def test_the_trigger_check_is_not_satisfied_by_enumerating_the_samples():
    """P2: two probe filenames proved coverage of two filenames, nothing more.

    ``paths`` listing exactly the sample paths made
    ``_guard_trigger_problem`` return ``None`` while a pull request
    adding any differently named workflow — ``new-check.yaml``, say —
    would not have run the guards at all. Requiring every sample to
    match fixed the extension asymmetry and still left enumeration
    passing as though it were generality.

    The property wanted is universal and no finite probe set proves it;
    that limitation is stated where the probes are defined rather than
    papered over. What the probes CAN do is fail every filter that is
    wrong in a way anyone would plausibly write it — wrong extension,
    a directory-vs-file mistake, a naming assumption, or the literal
    enumeration below.
    """
    wf = ".github/workflows"

    # The exact shape review found: enumerate the two original samples.
    enumerated = _guard_trigger_problem(
        "w.yml",
        {
            True: {
                "pull_request": {
                    "paths": [
                        f"{wf}/librerun-smoke.yml",
                        f"{wf}/a-workflow-added-by-this-pr.yaml",
                    ]
                }
            }
        },
    )
    assert enumerated is not None, (
        "a filter naming individual workflow files was accepted as covering "
        "workflow changes — enumeration is not generality, and a PR adding "
        "any other workflow would not run the guards"
    )

    # A filter assuming one extension, and one assuming a name prefix:
    # both plausible, both leave real additions uncovered.
    for narrow in ([f"{wf}/*.yml"], [f"{wf}/librerun-*"], [f"{wf}/a*"]):
        problem = _guard_trigger_problem(
            "w.yml", {True: {"pull_request": {"paths": narrow}}}
        )
        assert problem is not None, f"{narrow} was accepted but is not general"

    # Genuinely general patterns still pass — the check must not have
    # become a refusal of everything.
    for general in ([f"{wf}/**"], [".github/**"], ["**"], ["backend/**", f"{wf}/**"]):
        assert _guard_trigger_problem(
            "w.yml", {True: {"pull_request": {"paths": general}}}
        ) is None, general

    # And the real workflow that runs the guards still satisfies it.
    # The assertions above stand on constructed patterns and hold whatever
    # the tree looks like. This half walks the real one, so it needs the
    # same non-empty tripwire: an empty census would skip it in silence.
    real = _workflow_files(_workflow_dir())
    assert real, "no workflow files found — the real-tree half checked nothing"
    for path in real:
        doc = _load_workflow(path)
        for job in (doc.get("jobs") or {}).values():
            commands = "\n".join(s.get("run", "") for s in job.get("steps") or [])
            if "tests/test_adapter_kit.py" in commands:
                assert _guard_trigger_problem(path.name, doc) is None
                break


@pytest.mark.asyncio
async def test_progress_fields_are_read_once_like_the_runner_reads_them():
    """P2: the battery read `status` four times where production reads it once.

    The chassis' progress callback evaluates ``p.step_id`` as the Redis
    hash field and then builds the record — ``p.status``, then
    ``p.detail`` — and serializes THAT first value
    (``agent_runner.py:206-210``). The battery recorded the first read,
    ran the vocabulary check on a second, and serialized a fourth.

    So a stateful property returning something unserializable on its
    first access and ``"complete"`` afterwards split the difference:
    ``result.progress`` held the bad value, while the membership test
    and the ``json.dumps`` emulation both saw ``"complete"`` and passed.
    Production serializes the first value, raises inside the callback,
    and the phase dies — the battery said PASS.

    Same lesson as the result-object replay several rounds earlier, in
    the one place it had not been applied: counting the reads is half of
    it, and WHICH read the outcome hangs on is the other half.
    """
    from app.agents.protocol import StepProgress  # noqa: F401  (shape reference)

    class StatefulProgress:
        """Fine on every read after the first — which production never takes."""

        def __init__(self):
            self.step_id = "work"
            self.detail = None
            self._status_reads = 0

        @property
        def status(self):
            self._status_reads += 1
            if self._status_reads == 1:
                return object()  # json.dumps refuses this
            return "complete"

    probe = StatefulProgress()

    class StatefulAgent(AgentProtocol):
        agent_id = "stateful-progress"
        display_name = "Stateful progress"
        description = "Emits a status that is only bad on the first read"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(probe)
            return AnalysisResult(display={}, structured={"ok": True})

    result = await run_battery(
        StatefulAgent(), Scenario("stateful", {}), require_traces=False
    )

    assert not result.passed, (
        "an agent whose first status read is unserializable passed the "
        "battery — production serializes exactly that first value and dies "
        f"inside the callback. progress={result.progress!r}"
    )

    # The status was read ONCE by the battery, as production reads it.
    # More than one read is the defect itself, whatever the verdict.
    assert probe._status_reads == 1, (
        f"the battery read p.status {probe._status_reads} times; production "
        f"reads it once (agent_runner.py:208), and the extra reads are what "
        f"let a stateful property show the battery a value production never sees"
    )


@pytest.mark.asyncio
async def test_the_battery_does_not_hash_status_before_the_emulated_write():
    """P2: the vocabulary test hashed `status` before the write it emulates.

    The chassis' callback never hashes the status — it serializes the
    record and hands `step_id` to Redis (``agent_runner.py:206-213``).
    The battery's membership test ``status in VALID_STATUSES`` is a
    battery-only operation, and it ran FIRST, so a ``str`` subclass whose
    ``__hash__`` has a side effect could put a sibling value into a state
    production never puts it in.

    Concretely: a ``status`` of ``"complete"`` that sets a flag from
    ``__hash__``, and a ``step_id`` whose ``encode()`` raises until that
    flag is set. The battery hashed, the flag flipped, the encode
    succeeded, PASS — while production, which never hashes, hits the
    Redis encode with the flag unset and aborts the phase.

    Same rule as the runner replay two families over: an emulation may
    not perform operations production does not, in positions where the
    code under test can observe them.
    """
    from app.agents.protocol import StepProgress

    unlocked = []

    class UnlockingStatus(str):
        """A perfectly valid status — except that hashing it has an effect."""

        def __hash__(self):
            unlocked.append(True)
            return str.__hash__(self)

    class LockedStepId(str):
        """Encodable only once something has hashed the status."""

        def encode(self, *args, **kwargs):
            if not unlocked:
                raise ValueError("step id is not encodable yet")
            return str.encode(self, *args, **kwargs)

    class SideEffectAgent(AgentProtocol):
        agent_id = "side-effect"
        display_name = "Side effect"
        description = "Status whose hash unlocks the step id"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(
                StepProgress(
                    step_id=LockedStepId("work"), status=UnlockingStatus("complete")
                )
            )
            return AnalysisResult(display={}, structured={"ok": True})

    result = await run_battery(
        SideEffectAgent(), Scenario("side-effect", {}), require_traces=False
    )

    assert not result.passed, (
        "the battery hashed the status before emulating the write, which "
        "unlocked a step_id that production cannot encode — production "
        "never hashes the status, so the real progress write aborts the phase"
    )
    assert any("cannot be encoded for" in f or "not encodable" in f for f in result.failures), (
        result.failures
    )


@pytest.mark.asyncio
async def test_a_hostile_class_property_is_reported_not_raised():
    """P2: `isinstance` runs `__class__`, which the agent owns.

    A custom mapping with ``keys()`` and ``__getitem__`` is coerced and
    persisted perfectly well by the runner's ``dict(result.structured or
    {})`` — production completes the run. But the battery's declared-type
    check called ``isinstance`` on it, which executes the agent's
    ``__class__`` property, and a raising one escaped ``run_battery`` as
    a traceback instead of a ``BatteryResult``.

    Unaskable is treated as False, which is what a declared-type check
    should conclude, and the reported type comes from ``type()`` rather
    than ``__class__``, so the same lie cannot corrupt the message.
    """
    class LyingMapping:
        def __init__(self):
            self._d = {"ok": True}

        def keys(self):
            return self._d.keys()

        def __getitem__(self, key):
            return self._d[key]

        @property
        def __class__(self):
            raise RuntimeError("class is not available")

    # Precondition: production really can persist this.
    assert dict(LyingMapping()) == {"ok": True}

    class LyingAgent(AgentProtocol):
        agent_id = "lying-class"
        display_name = "Lying class"
        description = "Structured output whose __class__ refuses to be read"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            from app.agents.protocol import StepProgress

            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured=LyingMapping())

    try:
        result = await run_battery(
            LyingAgent(), Scenario("lying-class", {}), require_traces=False
        )
    except Exception as exc:  # pragma: no cover - the defect being fixed
        raise AssertionError(
            f"run_battery raised {type(exc).__name__}: {exc} instead of "
            f"returning a failed BatteryResult"
        ) from exc

    assert not result.passed
    assert any("not a dict" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_a_progress_call_counts_even_when_its_payload_cannot_be_read():
    """P2: a raising `status` property made the battery deny the call happened.

    The call counter exists so a failed write cannot produce "no progress
    was streamed" about a call the agent plainly made. It was placed
    after the three payload reads — which are themselves agent code — so
    a property that raises skipped the counter and produced exactly the
    false report the counter was added to prevent.
    """
    from app.agents.protocol import StepProgress  # noqa: F401

    class UnreadableProgress:
        step_id = "work"
        detail = None

        @property
        def status(self):
            raise RuntimeError("status is not available")

    class UnreadableAgent(AgentProtocol):
        agent_id = "unreadable-progress"
        display_name = "Unreadable progress"
        description = "Streams progress whose status cannot be read"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(UnreadableProgress())
            return AnalysisResult(display={}, structured={"ok": True})

    result = await run_battery(
        UnreadableAgent(), Scenario("unreadable", {}), require_traces=False
    )

    assert not result.passed
    assert not any("no progress was streamed" in f for f in result.failures), (
        "the battery reported that no progress was streamed, but the agent "
        f"called on_progress — it was the payload that could not be read. "
        f"{result.failures}"
    )


@pytest.mark.asyncio
async def test_the_commit_is_replayed_where_the_runner_commits():
    """P2: `await db.commit()` was missing from the ordered replay.

    The runner serializes the result into JSONB at
    ``agent_runner.py:509`` — BETWEEN the ``report_html`` read at :499
    and the ``status`` read at :516 — and that serialization iterates
    the agent's own containers.

    The replay covered the reads and the other operations but not the
    commit, so every ``status`` read happened before any of the agent's
    containers were walked. A container whose iteration flips a flag
    that a later ``status`` read observes therefore passed the battery,
    while production hits the post-commit read with the flag set and
    errors after the work is already stored.
    """
    tripped = []

    class TrippingList(list):
        def __iter__(self):
            tripped.append(True)
            return list.__iter__(self)

    class TrippedResult(AnalysisResult):
        @property
        def status(self):
            if tripped:
                raise RuntimeError("status is not available after serialization")
            return "complete"

        @status.setter
        def status(self, value):
            pass  # constructed normally; only READING is hostile

    class TrippingAgent(AgentProtocol):
        agent_id = "tripping"
        display_name = "Tripping"
        description = "Output whose serialization breaks a later status read"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            from app.agents.protocol import StepProgress

            await on_progress(StepProgress(step_id="work", status="complete"))
            return TrippedResult(display={}, structured={"rows": TrippingList([1, 2])})

    result = await run_battery(
        TrippingAgent(), Scenario("tripping", {}), require_traces=False
    )

    assert tripped, "the battery never serialized the output — the commit step is missing"
    assert not result.passed, (
        "an agent whose JSONB serialization breaks a later status read passed "
        "the battery. Production commits at :509 and re-reads status at :516, "
        f"so the run dies with the work already stored. {result.failures}"
    )
    # And the diagnosis names the read that production actually fails on.
    assert any("516" in f or "status" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_a_commit_that_fails_only_once_is_still_fatal():
    """P2: the persist step swallowed its failure and trusted a later re-read.

    When the replay's ``json.dumps`` at :509 raised, the step returned
    quietly on the reasoning that ``_json_problem`` would rediscover the
    same failure with a better path. That reasoning holds for a value
    that answers the same way twice — and those are exactly the values
    this battery is NOT about. A container that raises only on its FIRST
    serialization lets the later walk succeed, so nothing was reported
    and the phase could be certified while production stopped at the
    failed commit and marked the run errored.

    Asking twice and believing the second answer is the mistake the
    battery exists to catch in adapters. It is not allowed here either.
    """
    class OnceHostileList(list):
        def __init__(self, *args):
            super().__init__(*args)
            self._serialized = False

        def __iter__(self):
            if not self._serialized:
                self._serialized = True
                raise RuntimeError("this container refuses its first serialization")
            return list.__iter__(self)

    class OnceHostileAgent(AgentProtocol):
        agent_id = "once-hostile"
        display_name = "Once hostile"
        description = "Output that fails only the first time it is serialized"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            from app.agents.protocol import StepProgress

            await on_progress(StepProgress(step_id="work", status="complete"))
            return AnalysisResult(display={}, structured={"rows": OnceHostileList([1, 2])})

    result = await run_battery(
        OnceHostileAgent(), Scenario("once-hostile", {}), require_traces=False
    )

    assert not result.passed, (
        "output whose first serialization fails was certified — production "
        "commits once, at agent_runner.py:509, and stops there. "
        f"{result.failures}"
    )
    assert any("509" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_a_commit_that_succeeds_is_not_overturned_by_a_second_look():
    """P2: the battery's extra traversal condemned output production persists.

    The exact mirror of the previous round. There, a commit that failed
    once was excused by a diagnostic walk that succeeded; here, a commit
    that SUCCEEDED is condemned by a diagnostic walk that failed. Both
    come from treating two readings of a changing value as one reading.

    Production commits once, at ``agent_runner.py:509``, and afterwards
    touches only the TOP-LEVEL keys — ``sorted(structured.keys())`` at
    :517. It never performs the nested re-traversal, so a container that
    serializes cleanly the first time and refuses the second is fine in
    production and the run completes. Failing it would be the battery
    being stricter than the chassis on the strength of a question the
    chassis never asks.

    The suppression is narrow: only an inspection FAILURE is dropped,
    and only when the replay reached and passed the commit. A real
    finding — a set, a NaN, a NUL, unorderable keys — is something the
    plain ``json.dumps`` at :509 does not catch, so those stay fatal;
    the test below pins that too.
    """
    class SecondLookHostile(list):
        """Serializes fine once, which is all production ever does."""

        def __init__(self, *args):
            super().__init__(*args)
            self._traversals = 0

        def __iter__(self):
            self._traversals += 1
            if self._traversals > 1:
                raise RuntimeError("this container refuses a second traversal")
            return list.__iter__(self)

    def _agent_returning(payload, agent_name):
        class Agent(AgentProtocol):
            agent_id = agent_name
            display_name = agent_name
            description = "Output for the second-look check"

            def input_schema(self) -> dict:
                return {"type": "object"}

            async def analyze(self, inp: AgentInput, on_progress):
                from app.agents.protocol import StepProgress

                await on_progress(StepProgress(step_id="work", status="complete"))
                return AnalysisResult(display={}, structured=payload)

        return Agent()

    # Production's own view, so the expectation is not taken on faith.
    import json as _json

    probe = {"rows": SecondLookHostile([1, 2])}
    assert _json.dumps(probe) == '{"rows": [1, 2]}'
    assert sorted(probe.keys()) == ["rows"]

    result = await run_battery(
        _agent_returning({"rows": SecondLookHostile([1, 2])}, "second-look"),
        Scenario("second-look", {}),
        require_traces=False,
    )
    assert result.passed, (
        "output that production commits successfully was failed by the "
        "battery's own second traversal — the chassis commits once at :509 "
        f"and then sorts only the top-level keys. {result.failures}"
    )

    # And the suppression must not swallow a REAL persistence defect that
    # the commit's plain `json.dumps` lets through: NaN encodes there and
    # PostgreSQL refuses it.
    nan_result = await run_battery(
        _agent_returning({"score": float("nan")}, "nan-output"),
        Scenario("nan", {}),
        require_traces=False,
    )
    assert not nan_result.passed, (
        "a NaN survived — `json.dumps` accepts it at the commit, so the "
        "walk is the only thing that catches it and it must stay fatal"
    )


@pytest.mark.asyncio
async def test_the_verdict_is_about_the_value_that_was_committed():
    """P2: a value that CHANGED between the commit and the walk failed the phase.

    The previous round suppressed the walk when it *crashed* on a second
    traversal. A walk that merely disagreed still failed the phase: a
    container yielding safe data on its first iteration and a NaN on its
    second serializes cleanly at :509 — production stores that safe
    payload and completes — while the walk saw the NaN and reported an
    ordinary problem string, which the `_Uninspectable` check did not
    cover.

    So the verdict now comes from the committed TEXT rather than from
    another look at the live object. ``json.loads`` of it is inert data:
    no agent code, no third traversal, and it preserves NaN, NUL and
    unpaired surrogates — precisely the defects ``json.dumps`` lets
    through and PostgreSQL refuses.

    Both directions are pinned here in one test, because the previous two
    rounds were each other's mirror and fixing one direction alone is
    what produced the other.
    """
    import json as _json
    import math

    class ChangingList(list):
        """Safe once — which is the only traversal production performs."""

        def __init__(self, *args):
            super().__init__(*args)
            self._traversals = 0

        def __iter__(self):
            self._traversals += 1
            if self._traversals == 1:
                return list.__iter__(self)
            return iter([float("nan")])

    def _agent_returning(payload, name):
        class Agent(AgentProtocol):
            agent_id = name
            display_name = name
            description = "Output for the committed-value check"

            def input_schema(self) -> dict:
                return {"type": "object"}

            async def analyze(self, inp: AgentInput, on_progress):
                from app.agents.protocol import StepProgress

                await on_progress(StepProgress(step_id="work", status="complete"))
                return AnalysisResult(display={}, structured=payload)

        return Agent()

    # Production's own view: what the commit stores is the SAFE payload.
    probe = {"rows": ChangingList([1, 2])}
    assert _json.dumps(probe) == '{"rows": [1, 2]}'

    changed = await run_battery(
        _agent_returning({"rows": ChangingList([1, 2])}, "changing"),
        Scenario("changing", {}),
        require_traces=False,
    )
    assert changed.passed, (
        "output whose committed payload is clean was failed on the strength "
        "of a NaN that only a second traversal produces — production "
        f"serializes once, at :509. {changed.failures}"
    )

    # The other direction, in the same change: a NaN that is genuinely
    # THERE survives into the committed text and must stay fatal. Without
    # this, the suppression above would be a false-PASS machine.
    assert math.isnan(_json.loads('{"score": NaN}')["score"])
    stable = await run_battery(
        _agent_returning({"score": float("nan")}, "stable-nan"),
        Scenario("stable-nan", {}),
        require_traces=False,
    )
    assert not stable.passed, (
        "a stable NaN was suppressed — it is in the committed text, "
        "PostgreSQL refuses it, and the run dies at COMMIT"
    )


@pytest.mark.asyncio
async def test_the_key_sort_is_replayed_before_the_report_read():
    """P2: the runner sorts the keys BETWEEN the status read and the report read.

    ``agent_runner.py`` reads status at :516, evaluates
    ``sorted(list(structured.keys()))`` at :517, and only then reads and
    measures ``report_html`` at :519. The replay had no entry for the
    sort at all — it happened much later, inside the persistence walk —
    so the report was read in a state production never reaches.

    Sorting runs the keys' own ``__lt__``. A comparison that mutates
    state a later ``report_html`` property observes therefore kills the
    real run at :519 while the battery sailed past it.
    """
    from app.agents.protocol import InvestigationResult, StepProgress

    sorted_yet = []

    class MutatingKey(str):
        def __lt__(self, other):
            sorted_yet.append(True)
            return str.__lt__(self, other)

        def __gt__(self, other):
            sorted_yet.append(True)
            return str.__gt__(self, other)

    class SortSensitiveResult(InvestigationResult):
        @property
        def report_html(self):
            if sorted_yet:
                raise RuntimeError("report is not available once the keys are sorted")
            return "<p>ok</p>"

        @report_html.setter
        def report_html(self, value):
            pass  # constructed normally; only READING after the sort is hostile

    class SortSensitiveAgent(AgentProtocol):
        agent_id = "sort-sensitive"
        display_name = "Sort sensitive"
        description = "Keys whose comparison breaks the later report read"

        def input_schema(self) -> dict:
            return {"type": "object"}

        async def analyze(self, inp: AgentInput, on_progress):
            await on_progress(StepProgress(step_id="work", status="complete"))
            return SortSensitiveResult(
                status="complete",
                structured={MutatingKey("b"): 1, MutatingKey("a"): 2},
                report_html="<p>ok</p>",
            )

    result = await run_battery(
        SortSensitiveAgent(),
        Scenario("sort-sensitive", {}),
        require_traces=False,
        output_mode="html_report",
    )

    assert sorted_yet, "the battery never sorted the keys — the :517 step is missing"
    assert not result.passed, (
        "an agent whose key comparison breaks the later report read passed. "
        "The runner sorts at :517 and reads report_html at :519, so the run "
        f"dies with the phase's work already done. {result.failures}"
    )


@pytest.mark.asyncio
async def test_the_committed_text_is_checked_even_when_the_walk_is_happy():
    """P2: a NaN emitted at the COMMIT and nowhere else was reported as PASS.

    Last round made the committed text the verdict, but only reached for
    it once the live-object walk had already found something — the check
    read ``if problem is not None and replay.persisted``. That makes the
    commit a *confirmation* step, and it confirms nothing when the walk
    is happy.

    A stateful container that emits a NaN on its first iteration and safe
    data thereafter is exactly that case. Production serializes once, at
    :509, so ``{"rows": [NaN]}`` is the text the JSONB column receives
    and PostgreSQL refuses — the run dies at COMMIT with the phase's work
    already done. The battery then walked the live object, saw ``[1, 2]``,
    found no problem, and never looked at what had actually been sent.

    So the committed text is judged whenever the replay reaches
    serialization, not only when something else already suspects it. The
    live-object walk is the fallback for when there is no commit to
    judge.
    """
    import json as _json

    class DirtyFirstList(list):
        """A NaN in the payload production stores, and only there."""

        def __init__(self, *args):
            super().__init__(*args)
            self.traversals = 0

        def __iter__(self):
            self.traversals += 1
            if self.traversals == 1:
                return iter([float("nan")])
            return list.__iter__(self)

    def _agent_returning(payload, name):
        class Agent(AgentProtocol):
            agent_id = name
            display_name = name
            description = "Output for the committed-text check"

            def input_schema(self) -> dict:
                return {"type": "object"}

            async def analyze(self, inp: AgentInput, on_progress):
                from app.agents.protocol import StepProgress

                await on_progress(StepProgress(step_id="work", status="complete"))
                return AnalysisResult(display={}, structured=payload)

        return Agent()

    # Production's own view, pinned rather than asserted from memory: the
    # FIRST serialization — the only one the runner performs — carries the
    # NaN, and every look afterwards is clean. That gap is the defect.
    probe = {"rows": DirtyFirstList([1, 2])}
    assert _json.dumps(probe) == '{"rows": [NaN]}'
    assert _json.dumps(probe) == '{"rows": [1, 2]}'

    dirty = await run_battery(
        _agent_returning({"rows": DirtyFirstList([1, 2])}, "dirty-at-commit"),
        Scenario("dirty-at-commit", {}),
        require_traces=False,
    )
    assert not dirty.passed, (
        "the committed payload was {\"rows\": [NaN]} — PostgreSQL refuses it "
        "and the run dies at COMMIT — but the battery walked the live object "
        "afterwards, found it clean, and never checked what was sent"
    )
    assert any("COMMIT" in f for f in dirty.failures), (
        f"failed for some unrelated reason: {dirty.failures}"
    )

    # The opposite direction, in the same change, because the last two
    # rounds were each other's mirror and fixing one alone is what
    # produced the other. Judging the committed text must not invent
    # findings out of encoding-legal transformations: ``json.dumps``
    # flattens tuples into arrays and stringifies non-string keys, so the
    # reloaded value is structurally UNLIKE the live object while being
    # exactly what production stored and PostgreSQL accepts.
    #
    # Two payloads rather than one, because they cannot be combined:
    # ``{"t": ..., 3: "k"}`` mixes key types and dies for real at :517,
    # where ``sorted(structured.keys())`` compares an int to a str. That
    # was this test's first draft, and the battery was right to fail it.
    assert _json.loads(_json.dumps({"n": {"deep": (3, 4)}})) == {"n": {"deep": [3, 4]}}
    assert _json.loads(_json.dumps({1: "a", 2: "b"})) == {"1": "a", "2": "b"}
    for label, payload in (
        ("tuple-flattened-by-encoding", {"n": {"deep": (3, 4)}}),
        ("keys-stringified-by-encoding", {1: "a", 2: "b"}),
    ):
        reshaped = await run_battery(
            _agent_returning(payload, label),
            Scenario(label, {}),
            require_traces=False,
        )
        assert reshaped.passed, (
            f"output whose committed text round-trips to a different shape "
            f"({label}) was failed — the runner stores that text and the run "
            f"completes. {reshaped.failures}"
        )


@pytest.mark.asyncio
async def test_a_collision_in_the_committed_text_is_found_in_the_parser_pairs():
    """P2: the collision check was still reading the live object.

    Last round moved the *encodability* verdict onto the committed text
    and left its sibling behind. A `dict` subclass whose first `items()`
    yields colliding pairs and whose later calls yield safe ones commits
    ``{"1": "first", "1": "second"}`` — JSONB keeps the last, and a value
    the agent produced is gone — while ``_json_lossy``, arriving
    afterwards, saw only the safe answer and reported nothing.

    The collision cannot be read back out of ``json.loads`` either:
    parsing is exactly what resolves it. So the duplicate is found in the
    pairs the parser saw, via ``object_pairs_hook``, in the same single
    parse that answers the encodability question.

    This is the "guard the SET, not the branch review named" lesson
    arriving a round late — the two walks are twins over the same value
    and only one of them was moved.
    """
    import json as _json

    class CollidingOnce(dict):
        """Colliding pairs for the commit; safe pairs for anyone after."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = 0

        def items(self):
            self.calls += 1
            if self.calls == 1:
                return [(1, "first"), ("1", "second")]
            return [("1", "second")]

        def keys(self):
            return [name for name, _ in self.items()]

        def __iter__(self):
            return iter(self.keys())

    def _agent_returning(payload, name):
        class Agent(AgentProtocol):
            agent_id = name
            display_name = name
            description = "Output for the committed-collision check"

            def input_schema(self) -> dict:
                return {"type": "object"}

            async def analyze(self, inp: AgentInput, on_progress):
                from app.agents.protocol import StepProgress

                await on_progress(StepProgress(step_id="work", status="complete"))
                return AnalysisResult(display={}, structured=payload)

        return Agent()

    # Production's own view, pinned rather than reasoned about: the
    # commit writes the name twice, and parsing the result throws the
    # evidence away. Both halves of the defect, in two lines.
    assert _json.dumps({"nested": CollidingOnce({"1": "second"})}) == (
        '{"nested": {"1": "first", "1": "second"}}'
    )
    assert _json.loads('{"nested": {"1": "first", "1": "second"}}') == {
        "nested": {"1": "second"}
    }

    collided = await run_battery(
        _agent_returning({"nested": CollidingOnce({"1": "second"})}, "collides-at-commit"),
        Scenario("collides-at-commit", {}),
        require_traces=False,
    )
    assert any("did not produce" in f for f in collided.failures), (
        "the committed text carried the name \"1\" twice and PostgreSQL kept "
        "only the later value, but the battery walked the live object "
        f"afterwards and saw one clean key. {collided.failures}"
    )
    # Reported, never fatal — production COMPLETES this run, it just
    # stores less than the agent produced. Escalating it to a persist
    # failure would be the battery stricter than the chassis (lesson 34).
    assert not any(
        "cannot persist" in f for f in collided.failures
    ), f"a survivable collision was escalated to a persist failure: {collided.failures}"

    # The opposite direction, in the same change. A dict that collides
    # only on a LATER traversal committed a clean payload — production
    # stored every value the agent produced — so there is nothing to
    # report, and reporting it would be the false-FAIL mirror that
    # rounds 50/51 taught this loop to add up front.
    class CollidingLater(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = 0

        def items(self):
            self.calls += 1
            if self.calls == 1:
                return [("only", "value")]
            return [(1, "first"), ("1", "second")]

        def keys(self):
            return [name for name, _ in self.items()]

        def __iter__(self):
            return iter(self.keys())

    assert _json.dumps({"nested": CollidingLater({"only": "value"})}) == (
        '{"nested": {"only": "value"}}'
    )
    clean = await run_battery(
        _agent_returning({"nested": CollidingLater({"only": "value"})}, "collides-later"),
        Scenario("collides-later", {}),
        require_traces=False,
    )
    assert not any("did not produce" in f for f in clean.failures), (
        "a collision that only a second traversal produces was reported — "
        "the committed text has one name per key and production stored "
        f"everything. {clean.failures}"
    )


@pytest.mark.asyncio
async def test_a_value_the_duplicate_name_discards_still_kills_the_commit():
    """P2: collapsing a duplicate name threw away the evidence with it.

    Last round preserved the duplicate NAME through the parse. The
    VALUES attached to the losing occurrences were still collapsed away,
    and one of those can be the token the database refuses::

        >>> json.loads('{"x": NaN, "x": 1}')
        {'x': 1}

    Nothing left in that result is unstorable. PostgreSQL still rejects
    the commit, because it parses the whole document and the token it
    chokes on does not have to be the one that wins the name. The
    battery reported only the (survivable) collision, so the phase was
    not fatal and later phases ran — while the real run stopped dead at
    COMMIT with the work already done.

    Same root cause as the last two rounds, one level further in:
    parsing is what destroys the evidence, so whatever the verdict needs
    must be taken from the parser's pair stream before it collapses.
    """
    import json as _json

    class DuplicateName(dict):
        """First `items()` names one key twice; later calls are ordinary."""

        def __init__(self, pairs, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pairs = pairs
            self.calls = 0

        def items(self):
            self.calls += 1
            if self.calls == 1:
                return list(self._pairs)
            return [self._pairs[-1]]

        def keys(self):
            return [name for name, _ in self.items()]

        def __iter__(self):
            return iter(self.keys())

    def _agent_returning(payload, name):
        class Agent(AgentProtocol):
            agent_id = name
            display_name = name
            description = "Output for the discarded-value check"

            def input_schema(self) -> dict:
                return {"type": "object"}

            async def analyze(self, inp: AgentInput, on_progress):
                from app.agents.protocol import StepProgress

                await on_progress(StepProgress(step_id="work", status="complete"))
                return AnalysisResult(display={}, structured=payload)

        return Agent()

    # Production's own view. The commit writes NaN; the parse loses it.
    assert _json.dumps(
        {"nested": DuplicateName([("x", float("nan")), ("x", 1)], {"x": 1})}
    ) == '{"nested": {"x": NaN, "x": 1}}'
    assert _json.loads('{"nested": {"x": NaN, "x": 1}}') == {"nested": {"x": 1}}

    dropped = await run_battery(
        _agent_returning(
            {"nested": DuplicateName([("x", float("nan")), ("x", 1)], {"x": 1})},
            "discards-a-nan",
        ),
        Scenario("discards-a-nan", {}),
        require_traces=False,
    )
    assert not dropped.passed, (
        "the committed text was '{\"nested\": {\"x\": NaN, \"x\": 1}}' — "
        "PostgreSQL parses the whole document and refuses it — but the "
        f"discarded NaN was collapsed away before the check saw it. {dropped.failures}"
    )
    assert any("cannot persist" in f for f in dropped.failures), (
        "reported as survivable when the run dies at COMMIT: "
        f"{dropped.failures}"
    )

    # The opposite direction, in the same change. A duplicate name whose
    # DISCARDED value is perfectly storable is the ordinary collision:
    # production completes and merely stores less than the agent
    # produced, so it must stay reported-but-not-fatal. Escalating it
    # would be the battery stricter than the chassis (lesson 34), which
    # is the mirror this fix could so easily have introduced.
    assert _json.dumps(
        {"nested": DuplicateName([("x", "first"), ("x", "second")], {"x": "second"})}
    ) == '{"nested": {"x": "first", "x": "second"}}'
    survivable = await run_battery(
        _agent_returning(
            {"nested": DuplicateName([("x", "first"), ("x", "second")], {"x": "second"})},
            "discards-a-string",
        ),
        Scenario("discards-a-string", {}),
        require_traces=False,
    )
    assert any("did not produce" in f for f in survivable.failures), (
        f"the collision itself went unreported: {survivable.failures}"
    )
    assert not any("cannot persist" in f for f in survivable.failures), (
        "a survivable collision was escalated to a persist failure — "
        f"production completes this run. {survivable.failures}"
    )


# ---------------------------------------------------------------------------
# A workflow must not run a file its own paths filter cannot see
# ---------------------------------------------------------------------------


def _run_steps(doc):
    """``(working_directory, text)`` for every place a step names a file.

    Two sources, not one.

    ``run:`` commands, with the job's working directory, because that is
    what a relative command resolves against: ``unit-suites`` runs in
    ``backend`` and reaches its shared assertion as
    ``../scripts/assert_suite_ran.py``. A checker that read the token as
    written would look for a path starting with ``..``, find nothing, and
    report the workflow clean — which is what the first draft did.

    And ``with:`` inputs, at the repository root, because an action input
    names files too: ``cache-dependency-path`` decides the cache key, so
    a change to the file it names changes what the job restores. Reading
    only ``run:`` left four real dependencies invisible —
    ``backend/adapters/pyproject.toml`` and ``frontend/package-lock.json``
    here, ``sdk/python/librerun-agent/pyproject.toml`` in the container
    battery. All four happen to be selected by their filters today, so
    nothing was slipping through; the guard simply could not have
    noticed if one stopped being. Found by auditing this rule's own
    blind spots after the last one was reported rather than found.
    """
    steps = []
    for job in (doc.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        job_wd = ((job.get("defaults") or {}).get("run") or {}).get(
            "working-directory"
        )
        pythonpath = []
        for key, value in (job.get("env") or {}).items():
            if key == "PYTHONPATH" and isinstance(value, str):
                for entry in value.split(":"):
                    entry = entry.replace("${{ github.workspace }}", "").strip()
                    entry = entry.strip("/")
                    if entry:
                        pythonpath.append(entry)
        pythonpath = tuple(pythonpath)
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if isinstance(step.get("run"), str):
                steps.append(
                    (step.get("working-directory") or job_wd, step["run"], pythonpath)
                )
            for value in (step.get("with") or {}).values():
                if isinstance(value, str):
                    # Repo-root relative: an action input is not resolved
                    # against the job's working-directory.
                    steps.append((None, value, pythonpath))
    return steps


def _paths_named_in_command(command, working_directory):
    """Repo-relative paths a shell command names, resolved and normalised.

    Deliberately generous about what looks like a path and strict about
    what counts: existence in the repository is the discriminator, so
    ``backend-suite.xml`` (written by the run, absent from the tree) and
    ``app.main:app`` fall out on their own rather than needing a list of
    exceptions to keep in step with the workflows.

    In particular it does NOT require a suffix. The first version did,
    and so could not see an extensionless file: ``container-battery``
    passes ``…/echo_container/Dockerfile`` straight to ``docker build``,
    and with its covering pattern removed the guard still reported the
    tree clean (Codex round 4). A guard with a blind spot reports on what
    it looked at as though it had looked at everything — which is the
    failure this guard was added to prevent, committed by the guard.
    """
    import os
    import re

    token = re.compile(r"(?<![\w./-])((?:\.\.?/|[\w.+-]+/)*[\w.+-]+)")
    out = set()
    for match in token.finditer(command):
        resolved = os.path.normpath(
            os.path.join(working_directory or ".", match.group(1))
        )
        if not resolved.startswith(".."):
            out.add(resolved)
    return out


def _parents_level(node):
    """``N`` for ``Path(__file__)[.resolve()].parents[N]``, else ``None``.

    ``.parent`` counts as level 0. Anything else — a plain name like
    ``repo_root``, a call this does not model — returns ``None``, and the
    caller falls back to treating the chain as repository-root relative.
    """
    import ast

    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
        if node.value.attr != "parents":
            return None
        level = node.slice
        if isinstance(level, ast.Constant) and isinstance(level.value, int):
            return level.value
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return 0
    return None


def _ancestor(path, level):
    """The directory ``level`` steps above ``path``'s own directory."""
    import posixpath

    current = posixpath.dirname(path)
    for _ in range(level):
        current = posixpath.dirname(current)
    return current or "."


def _composed_path_literals(tree, of_file=None):
    """Every ``x / "a" / "b"`` chain in an AST, as a normalised path.

    When the chain's base is ``Path(__file__)…parents[N]`` and the file's
    own path is known, the result is resolved against that ancestor
    rather than against the repository root. The SDK's conftest inserts
    ``Path(__file__).resolve().parents[1] / "src"``; reducing that to a
    bare ``src`` looked for a repository-root ``src/``, found none, and
    dropped the real ``sdk/python/librerun-agent/src`` import root
    entirely (Codex round 9). It only appeared to work because
    ``librerun_agent`` has a unique tail today — add a second package
    with that tail and the guard would raise ambiguity for a name pytest
    resolves without difficulty.
    """
    import ast
    import os

    # Only MAXIMAL chains. `ast.walk` visits the nested BinOps of
    # `a / "x" / "y" / "z"` as well as the outer one, so collecting every
    # visited node yields "x/y/z", "x/y" and "x" — three roots where the
    # code declares one, and two of them wrong.
    nested = {
        node.left
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
    }
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        if node in nested:
            continue
        segments, cursor = [], node
        while isinstance(cursor, ast.BinOp) and isinstance(cursor.op, ast.Div):
            if isinstance(cursor.right, ast.Constant) and isinstance(
                cursor.right.value, str
            ):
                segments.append(cursor.right.value)
            cursor = cursor.left
        segments.reverse()
        if not segments:
            continue
        level = _parents_level(cursor)
        if level is not None and of_file is not None:
            base = _ancestor(of_file, level)
            joined = os.path.normpath(os.path.join(base, *segments))
        else:
            joined = os.path.normpath("/".join(segments))
        out.append(joined)
    return out


def _conftest_roots(tracked, repo_root, target):
    """``sys.path`` insertions of the conftests pytest LOADS for a target.

    pytest loads conftests from the rootdir down to the target, so the
    set is the target's ANCESTOR CHAIN — plus, when the target is a
    directory, the conftests inside it that collection will reach.

    Two shapes were wrong before. Contributing every tracked conftest to
    every workflow let an unrelated suite disambiguate imports it never
    loads (round 8). Then scoping strictly to "inside" broke the other
    way: ``chassis-zero-agents`` and ``adapter-battery`` pass explicit
    FILE targets, so ``f"{target}/"`` prefixed nothing, the group was
    empty, and ``backend/tests/conftest.py`` — which pytest really does
    load for those files — contributed nothing (Codex round 9). The
    ancestor chain is the rule pytest itself uses, and it is neither of
    my two guesses.
    """
    import ast
    import posixpath
    from pathlib import Path

    target = (target or ".").strip("/") or "."
    is_directory = target == "." or any(
        path.startswith(f"{target}/") for path in tracked
    )

    directories = []
    cursor = target if is_directory else posixpath.dirname(target)
    while True:
        directories.append(cursor or ".")
        if cursor in ("", "."):
            break
        cursor = posixpath.dirname(cursor)

    candidates = {
        posixpath.normpath(posixpath.join(directory, "conftest.py"))
        for directory in directories
    }
    if is_directory:
        prefix = "" if target == "." else f"{target}/"
        candidates |= {
            path
            for path in tracked
            if path.endswith("conftest.py") and path.startswith(prefix)
        }

    found = []
    for path in sorted(candidates):
        if path not in tracked:
            continue
        try:
            source = (Path(repo_root) / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "sys.path" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for composed in _composed_path_literals(tree, path):
            if composed not in found and any(
                entry.startswith(f"{composed}/") for entry in tracked
            ):
                found.append(composed)
    return found


def _invocation_roots(working_directory, pythonpath, conftest_roots):
    """The import roots for ONE invocation, ordered.

    Per invocation, not per workflow. ``container-battery`` sets its
    working directories on individual STEPS — ``sdk/python/librerun-agent``
    and ``backend`` — and a workflow-wide builder that read only
    ``defaults.run`` produced a list starting at ``.`` with neither of
    them (Codex round 8). Resolution has to happen in the directory the
    command actually runs in, the way the interpreter does it.
    """
    roots = []

    def add(candidate):
        candidate = (candidate or ".").strip().strip("/") or "."
        if candidate not in roots:
            roots.append(candidate)

    add(working_directory)
    for entry in pythonpath or ():
        add(entry)
    for entry in conftest_roots or ():
        add(entry)
    add(".")
    return tuple(roots)


def _suffix_index(tracked):
    """``tail -> sorted paths``, built ONCE for a tracked tree.

    Before this, every import in every traversed file scanned the whole
    tracked set: measured at 4,067 lookups over 393 entries — 1.6 million
    comparisons for one workflow, growing with (files traversed) x (tree
    size) (Codex round 7). The index makes the lookup a dict hit.
    """
    from collections import defaultdict

    index = defaultdict(set)
    for path in tracked:
        if not path.endswith(".py"):
            continue
        stem = path[:-3]
        if stem.endswith("/__init__"):
            stem = stem[: -len("/__init__")]
        parts = stem.split("/")
        for i in range(len(parts)):
            index["/".join(parts[i:])].add(path)
    return {tail: sorted(paths) for tail, paths in index.items()}


class _AmbiguousModule(Exception):
    """A module name that no root resolves and whose tail is not unique."""

    def __init__(self, module, candidates):
        super().__init__(
            f"module {module!r} resolves to {len(candidates)} tracked files "
            f"and no import root disambiguates it: {candidates}"
        )
        self.module = module
        self.candidates = candidates


def _module_file(module, exists, index=None, roots=None):
    """The repository file a module name resolves to, or ``None``.

    ROOTS FIRST, in order, the way an interpreter resolves an import —
    and the roots are derived from the workflow and the tree rather than
    listed (see ``_import_roots``). Only if no root has the module does
    this fall back to the tail index, and then ONLY when the tail is
    unique: an ambiguous tail raises rather than returning whichever
    entry a set happened to yield first.

    Returning an arbitrary candidate was the defect. Thirteen tails in
    this tree match more than one file, and the answer moved with the
    Python hash seed — so a narrowly filtered workflow could follow the
    wrong module, miss its dependencies, and pass. My claim that no tail
    was ambiguous came from a census whose loop `break`ed on its first
    iteration and therefore only ever compared full paths (Codex round
    7); the claim was false and the measurement never tested it.
    """
    rel = module.replace(".", "/")
    for root in roots or ():
        prefix = "" if root == "." else f"{root}/"
        for candidate in (f"{prefix}{rel}.py", f"{prefix}{rel}/__init__.py"):
            if exists(candidate):
                return candidate
    matches = (index or {}).get(rel) or []
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise _AmbiguousModule(module, matches)
    return None


# pytest options whose value is a SEPARATE token, so the word after them
# is not a `file_or_dir` positional. Modelling another program's command
# line is a commitment to keep modelling it — this is the third round of
# findings in that class — so the set is named rather than inlined, and
# the residual risk is stated: a token that both follows an option
# missing from this set AND happens to name a tracked path would still
# be read as a target. Options written `--name=value` need no entry.
# The two spellings pytest gives the JUnit report option. BOTH run --
# measured against the installed interpreter, `--junitxml=a.xml`,
# `--junit-xml=b.xml` and `--junit-xml c.xml` each write their file.
#
# Named once, and that is the finding. The set below already carried
# both while `_pytest_runs`, forty lines from it, looked for the report
# by hand and knew only `--junitxml`: so a workflow writing the
# documented `--junit-xml=` spelling was reported as writing no report
# at all, and `_unchecked_pytest_runs` accused a job that checks itself
# properly. A rule held in one place and hand-rolled in another, which
# is this module's most repeated defect (Codex round 26).
#
# Re-derived from the installed pytest by
# `test_the_junit_report_options_are_pytests_own`, so the pair cannot
# rot the way a hand-kept list does -- the same treatment bash's
# reserved words and FastAPI's route decorators get.
_JUNIT_REPORT_OPTIONS = ("--junitxml", "--junit-xml")

_PYTEST_OPTIONS_TAKING_A_VALUE = frozenset({
    "-k", "-m", "-p", "-c", "-o", "-n", "-W", "-r",
    "--deselect", "--ignore", "--ignore-glob", "--rootdir", "--confcutdir",
    "--basetemp", "--import-mode", *_JUNIT_REPORT_OPTIONS,
    "--override-ini", "--maxfail", "--durations", "--log-file", "--tb",
})


def _shell_invocations(text):
    r"""One ``run:`` block split into the commands it actually runs.

    pytest's arguments belong to ITS invocation, not to the whole block,
    so the block has to be divided before anything is parsed.

    **The division is delegated to ``shlex``, not hand-rolled.** A regex
    on the separators looked safe and was not: I claimed it could only
    narrow a command's token range and that claim was false in both
    directions (Codex round 13). ``python -m pytest -k "fast|slow"
    tests/test_x.py`` split inside the quoted marker, leaving a pytest
    invocation with no positional target that fell back to collecting
    ``.`` — the whole tree, a false positive. A ``\``-continuation split
    the other way, turning the continued line into a separate
    conftest-free command. And that second shape is REAL here:
    ``container-battery`` runs ``python -m adapter_kit.run_contract \``
    across four lines, which the regex cut into four fragments.

    So: continuations are joined first, each physical line is then a
    command list, and ``shlex`` — which knows about quoting — finds the
    operators inside it. Reading the line is ``_shell_tokens``'s job,
    not this one's: that is where the comment comes off, where the lexer
    is configured and where a line ``shlex`` refuses falls back to a
    whitespace split. Doing any of it here instead is exactly how this
    reader and ``_shell_words`` came to disagree about comments
    (Codex round 16).

    Returns LISTS OF TOKENS, not strings. Joining them back and letting
    the caller ``split()`` discarded the quoting immediately: ``-k "x or
    agents"`` became three words, ``-k`` consumed only ``x``, and ``or``
    and ``agents`` were read as positional targets — seeding all 31 files
    under ``backend/agents`` for a job that collects none of them (Codex
    round 14). Tokenising and then re-splitting is not tokenising.
    """
    import re

    joined = re.sub(r"\\\s*\n", " ", text)
    operators = {"&&", "||", ";", "|", "&"}
    # A subshell's delimiters are not operators, but they END a command
    # just as surely: `(python -m pytest tests/test_x.py)` lexes with
    # `(` and `)` as their own tokens, and leaving them in the word list
    # put `(` first -- so the interpreter was not the command, the entry
    # scan skipped the invocation, and the census did not see a pytest
    # run that bash certainly performs (Codex round 22). The closing
    # delimiter is the same defect quieter: `(cd b; python -m pytest
    # x.py)` left `)` as a trailing POSITIONAL, a phantom collection
    # target.
    #
    # This is the sixth shape round 13 named and declined, and the
    # reason it was declined does not apply here. Declining it was
    # about `_shell_words`, where `punctuation_chars=True` made 404 of
    # 412 tracked files phantom dependencies by turning the regex
    # fragments in `[[ =~ ]]` and a `case` arm's `*` into globs. That
    # reader still passes `";|&<>"` and is untouched. This one already
    # lexes with every operator `shlex` knows; it was only failing to
    # act on two of them.
    boundaries = operators | {"(", ")"}
    commands = []
    for line in joined.splitlines():
        if not line.strip():
            continue
        current = []
        for token in _shell_tokens(line, True):
            if token in boundaries:
                if current:
                    commands.append(current)
                    current = []
            else:
                current.append(token)
        if current:
            commands.append(current)
    return commands


def _python_entry_groups(doc, exists, index, tracked, repo_root, extra_steps=()):
    """``(roots, seed_files)`` for every Python invocation a workflow makes.

    A group, not a flat set, because roots are a property of the
    invocation: the step's own working directory, then that job's
    ``PYTHONPATH``, then the ``sys.path`` insertions of conftests pytest
    loads for the target, then the repository root. A module reached
    from a seed is resolved in that seed's context, which is what the
    interpreter does and what a workflow-wide root order cannot express.
    """
    import os

    groups = []
    for working_directory, text, pythonpath in list(_run_steps(doc)) + list(extra_steps):
        if not isinstance(text, str):
            continue
        for words in _shell_invocations(text):
            # The interpreter has to BE the command. `-m` was read
            # wherever it appeared, so `echo -m pytest tests/legacy` in
            # a reached wrapper seeded that whole directory and every
            # import under it -- the guard demanding a filter cover
            # files nothing runs (Codex round 17).
            command = _the_command(words)
            if not _is_the_python_interpreter(command):
                continue
            words = command

            # What the interpreter runs, decided ONCE and before
            # anything is read out of the invocation -- including WHERE
            # the program's own arguments start. A `-m` after the file
            # operand is the script's argument, not the interpreter's
            # flag (Codex round 19), and this used to re-scan for a bare
            # `-m` to find the boundary, which round 24's joined
            # `-mpytest` was invisible to: the entry was known to be a
            # module and its targets were seeded as nothing.
            kind, entry, after = _python_entry_at(
                words, exists, working_directory)

            trees = set()
            module_names = []
            pytest_targets = set()
            if kind == "module" and entry:
                if entry != "pytest":
                    module_names.append(entry)
                else:
                    targets, cursor = [], after
                    while cursor < len(words):
                        token = words[cursor]
                        if token.startswith("-"):
                            # An option that takes its value as a
                            # SEPARATE token consumes the next word,
                            # which is therefore not a positional.
                            # `--opt=value` needs no entry: it is one
                            # token and starts with a dash.
                            cursor += (2 if token in _PYTEST_OPTIONS_TAKING_A_VALUE
                                       else 1)
                            continue
                        # `file_or_dir` positionals may carry a node-ID
                        # selector: `tests/test_x.py::test_a`.
                        targets.append(token.split("::", 1)[0])
                        cursor += 1
                    pytest_targets.update(targets)
                    for target in targets or ["."]:
                        trees.add(
                            os.path.normpath(
                                os.path.join(working_directory or ".", target)
                            )
                        )

            # ONE of an invocation's `.py` words is the entry; the
            # rest are the script's own arguments. `python
            # scripts/main.py scripts/data.py` runs main and hands it
            # the other path, and `python -c 'print(1)' data.py` runs
            # neither -- yet every `.py` token was seeded, and the
            # import closure then followed the arguments' imports and
            # charged the workflow with files the invocation never
            # loads (Codex round 18).
            #
            # `-c` and `-m` say the entry is not a FILE at all, so
            # nothing here is one; otherwise the first `.py` that
            # exists is the script and everything after it is
            # argument. That needs no option grammar, which is the
            # lesson rounds 8 to 12 paid for: an option whose VALUE
            # ends in `.py` would have to be spelled to be a problem,
            # while parsing the interpreter's flags was wrong in six
            # consecutive rounds.
            direct = set()
            if kind == "file" and entry not in pytest_targets:
                direct.add(entry)

            # A plain script or `-m module` collects no conftest, so no
            # suite contributes a root to it.
            plain = _invocation_roots(working_directory, pythonpath, ())
            for module in module_names:
                found = _module_file(module, exists, index, plain)
                if found:
                    direct.add(found)
            if direct:
                groups.append((plain, direct))

            for tree_root in trees:
                prefix = "" if tree_root in (".", "") else f"{tree_root}/"
                seeds = {
                    path
                    for path in tracked
                    if path.startswith(prefix) and path.endswith(".py")
                }
                # An explicit FILE target is a seed in its own right.
                if not seeds and tree_root in tracked:
                    seeds = {tree_root}
                if not seeds:
                    continue
                groups.append(
                    (
                        _invocation_roots(
                            working_directory,
                            pythonpath,
                            _conftest_roots(tracked, repo_root, tree_root),
                        ),
                        seeds,
                    )
                )
    return groups


def _repo_files_a_module_reaches(path, exists, repo_root, index=None, roots=None):
    """First-order imports AND composed path literals, for one Python file.

    Two shapes, because the two gaps found in this tree are one of each.

    ``adapter_kit.run_contract`` IMPORTS ``app.agents.manifest``; the
    container battery runs that module and its filter did not list the
    file. And a chassis test READS ``repo_root / "docs" / "api" /
    "openapi.yaml"`` (``docs/vita_api_spec_v2.yaml`` until S8 moved it) —
    never imported, so no import walker would ever see it, while
    ``unit-suites`` watched neither ``scripts`` nor ``docs``.

    First-order only: the point is to catch a workflow reaching OUT of
    its filter, and a transitive closure over an entire application adds
    noise without adding findings. Measured on this tree, the two shapes
    together report eight files, every one of them real.
    """
    import ast
    import os
    from pathlib import Path

    try:
        tree = ast.parse((Path(repo_root) / path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        # A file that cannot be parsed is reported as reaching nothing
        # rather than failing the guard: this rule is about filters, and
        # a syntax error is another test's business.
        return set()

    # The importing file's OWN directory is the first root, because that
    # is how the interpreter resolves a sibling import: `import conftest`
    # from `backend/tests/test_x.py` means `backend/tests/conftest.py`,
    # and this tree has four files named conftest.py. Without it the
    # ambiguity check below fires on a name that is not actually
    # ambiguous at its point of use.
    import os as _os

    roots = (_os.path.dirname(path) or ".",) + tuple(roots or ())

    reached = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found = _module_file(alias.name, exists, index, roots)
                if found:
                    reached.add(found)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found = _module_file(node.module, exists, index, roots)
            if found:
                reached.add(found)
            for alias in node.names:
                found = _module_file(f"{node.module}.{alias.name}", exists, index, roots)
                if found:
                    reached.add(found)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            # `something / "a" / "b"` — a pathlib join whose string
            # segments name a repo file.
            segments, cursor = [], node
            while isinstance(cursor, ast.BinOp) and isinstance(cursor.op, ast.Div):
                if isinstance(cursor.right, ast.Constant) and isinstance(
                    cursor.right.value, str
                ):
                    segments.append(cursor.right.value)
                cursor = cursor.left
            segments.reverse()
            if segments:
                joined = os.path.normpath("/".join(segments))
                if exists(joined):
                    reached.add(joined)
    return reached


SHELL_SUFFIXES = (".sh", ".bash", ".zsh", ".ksh", ".dash")
SHELL_INTERPRETERS = ("sh", "bash", "zsh", "ksh", "dash", "ash")
# `env` options that consume the word AFTER them, from `env --help`
# (GNU coreutils 9.4). `-S` is NOT one: it splits the rest of the line
# into arguments, which `env` then processes as its own — so it is
# skipped and the walk continues, not skipped along with what follows.
ENV_OPTIONS_TAKING_A_WORD = ("-u", "-C")
# The long spellings of the same options. `env --help` says "Mandatory
# arguments to long options are mandatory for short options too", and
# GNU getopt takes that operand either attached with `=` or as the NEXT
# word — so `--unset python3 bash` runs bash, and skipping only the
# option token answered `python3` (Codex round 8). The
# `--block-signal`/`--default-signal`/`--ignore-signal` family takes an
# OPTIONAL argument, which getopt accepts only when attached, so those
# are deliberately not here.
# `--split-string` is NOT here: it does not merely consume its operand,
# it splits it into arguments `env` then reads as its own, which is a
# different thing and needs the recursive call below (Codex round 9).
ENV_LONG_OPTIONS_TAKING_A_WORD = ("--unset", "--chdir")


def _after_env(words):
    """What `env` would run, given the words after `env` itself.

    Round 6 replaced a parse of this grammar with a whitelist — the
    first word that IS a known interpreter — and claimed a whitelist
    "cannot acquire a new wrong shape". Round 7 produced one:
    `#!/usr/bin/env -S -u python3 bash` runs **bash**, because `-u`
    consumes `python3` as the name of a variable to unset, and the
    whitelist answered `python3`. A whitelist over a grammar you have
    refused to read is still a guess; it just fails on the inputs where
    an operand looks like a command.

    So the grammar is read. It is worth reading because it is CLOSED
    and documented — `env --help` lists every option, and only `-u` and
    `-C` take a separate operand — which is what POSIX word splitting
    was not, and why that one went to `shlex` instead.
    """
    index = 0
    while index < len(words):
        word = words[index]
        if word == "--":
            return words[index + 1:]
        if word in ("-S", "--split-string") and index + 1 < len(words):
            # `-S` takes a STRING and splits it. On a shebang line the
            # words arrive already split, but a lexed command hands the
            # whole operand over as one token — `env -S "bash -e" x.py`
            # — and merely stepping over `-S` left `bash -e` as the
            # apparent command (Codex round 8).
            #
            # `env --help` spells this option `-S, --split-string=S`:
            # it is ONE option, and the two spellings had two branches
            # here doing different things. The `-S` branch only
            # recursed when the operand split into more than one word,
            # so `env -S '\\_bash' wrapper.py` — whose operand splits
            # into exactly one word, `bash` — fell through with the
            # escape unprocessed and answered `\\_bash`. Nothing then
            # recognised the shell, and a shebangless `.py` wrapper
            # lost every dependency it named (Codex round 11). Splitting
            # is what the option DOES; how many words come out is not a
            # condition on doing it.
            return _after_env(_split_env_string(words[index + 1]) + list(words[index + 2:]))
        if word in ("-", "-S", "--split-string"):
            # A bare `-` means `-i`; `-S` splits what follows and `env`
            # reads the pieces as its own arguments, which is this loop.
            index += 1
            continue
        if word.startswith("--split-string="):
            return _after_env(_split_env_string(word.split("=", 1)[1]) + list(words[index + 1:]))
        if word.startswith("-S") and len(word) > 2:
            return _after_env(_split_env_string(word[2:]) + list(words[index + 1:]))
        if word.startswith("--"):
            if "=" not in word and word in ENV_LONG_OPTIONS_TAKING_A_WORD:
                index += 1  # the operand is the next word
            index += 1
            continue
        if word.startswith("-") and len(word) > 1:
            letters = word[1:]
            for position, letter in enumerate(letters):
                if f"-{letter}" not in ENV_OPTIONS_TAKING_A_WORD:
                    continue
                # `-u NAME` takes the next word; `-uNAME` carries it, and
                # so does the tail of a cluster like `-iuNAME`.
                if position == len(letters) - 1:
                    index += 1
                break
            index += 1
            continue
        if "=" in word and not word.startswith("="):
            index += 1  # NAME=VALUE, still not the command
            continue
        # A shebang line is raw text, so a quoted command keeps its
        # quotes: `#!/usr/bin/env -S 'bash' -e` answered "'bash'"
        # (Codex round 8).
        return [word.strip("\"'")] + list(words[index + 1:])
    return []


def _is_an_assignment(word):
    """`MODE=test` is an assignment; `--opt=value` and `a/b=c` are not."""
    import re

    name, sep, _value = word.partition("=")
    return bool(sep) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is not None


def _is_the_python_interpreter(command):
    r"""Is python what this invocation RUNS?

    Round 17 established that the interpreter has to BE the command --
    `echo -m pytest tests/legacy` runs no pytest -- and round 19 found
    the rule applied where that finding pointed and NOT in
    `_runs_alembic`, written two rounds later, which read
    `echo -m alembic` and `cat notes -m alembic` as running alembic and
    would have demanded a filter cover files the workflow never loads.
    Measured before the fix, not inferred.

    So it is one helper, for the reason round 16 gave about the lexer: a
    second caller answering it its own way is how the first one drifts.
    The command is taken from `_the_command`, so a leading assignment or
    `env` cannot hide it.
    """
    return bool(command) and command[0].rsplit("/", 1)[-1].startswith("python")


# `python --help`: the interpreter's syntax is
# `[-c cmd | -m mod | file | -] [arg] ...`, and these are the options
# that take their value as a SEPARATE word. `-c` and `-m` are not here
# because they END the scan rather than being skipped past. Read out of
# `--help` when this was written rather than recalled, the way the
# `env` grammar and pytest's `config_names` are (Codex round 21).
_PYTHON_OPTIONS_TAKING_A_SEPARATE_VALUE = {"-W", "-X", "--check-hash-based-pycs"}


def _python_entry(words, exists, working_directory):
    """What a Python invocation RUNS, without where its arguments start."""
    kind, entry, _after = _python_entry_at(words, exists, working_directory)
    return (kind, entry)


def _python_entry_at(words, exists, working_directory):
    r"""What a Python invocation RUNS, and where its ARGUMENTS begin.

    ``(kind, entry, after)``. ``after`` indexes the first word belonging
    to the program rather than to the interpreter, and it exists because
    `_python_entry_groups` used to re-scan the invocation for a bare
    `-m` to find that boundary itself. Two scans, two answers: the
    joined `-mpytest` this round taught the shared scan was still
    invisible to the re-scan, so the entry was known to be a module and
    its targets were seeded as nothing -- and `python -W -m a -m b`
    would have disagreed outright, the re-scan taking `-W`'s own value
    as the flag. One scan reports both now, which is the rule rounds 16
    and 17 paid for: a second caller answering a question its own way is
    how the first one drifts.

    `python --help` gives the syntax as `[-c cmd | -m mod | file | -]
    [arg] ...`: the interpreter's own options end at the first of those,
    and everything after it belongs to the script. Round 18 asked
    whether `-c` or `-m` appeared ANYWHERE in the invocation, so
    `python scripts/main.py -m fast` -- a script taking its own `-m`
    argument -- suppressed the real entry and lost every import under
    it, while the module scan beside it could read `fast` as the module
    Python launched (Codex round 19). Mine, introduced by round 18's
    fix for the opposite error.

    So the scan stops at whichever comes FIRST. Round 19 spelled the
    file operand as a `.py` that EXISTS, rejecting "the first word
    without a dash" because `python -W ignore scripts/main.py` would
    answer `ignore`. Half of that was right and half was a guess about
    filenames: **Python runs a file by path, not by suffix**, so
    `python scripts/tool` executes an extensionless file and round 19's
    rule answered `("none", None)` for it -- the entry unseeded, every
    import under it lost, and a filtered workflow free to omit them
    (Codex round 21). Measured against the real interpreter first: an
    extensionless file runs, exit 0.

    The `-W ignore` objection is answered where it actually lives, in
    the option grammar, and that grammar is READ rather than recalled:
    `python --help` gives the syntax as `[-c cmd | -m mod | file | -]`
    and names `-W arg`, `-X opt` and `--check-hash-based-pycs` as the
    options taking a SEPARATE value. A word starting with `-` is an
    option, never the operand -- running a file called `-x` needs
    `./-x`.

    `exists` is kept as the second condition, and deliberately: it is
    what makes an incomplete option table harmless rather than wrong.
    Were a value-taking option missing from the set below, its value
    would still have to name a real file before it could be mistaken
    for the entry, which is the same protection the `.py` rule gave
    and the reason this is not simply "the first word without a dash".
    """
    import os

    def is_a_file(word):
        return exists(os.path.normpath(
            os.path.join(working_directory or ".", word)))

    what, position, after = _python_own_words_end(words, is_a_file=is_a_file)
    if what == "command":
        return ("command", None, after)
    if what == "module":
        return ("module", _the_module(words, position), after)
    if what == "operand":
        return ("file", os.path.normpath(
            os.path.join(working_directory or ".", words[position])), after)
    return ("none", None, len(words))


def _the_module(words, position):
    r"""The module a ``-m`` at `position` launches, or ``None``.

    One line of grammar, in one place, because `python --help` spells
    `-m mod` in two ways -- separate and joined -- and a reader that
    knows only the separate one reads `python -mpytest` as launching
    nothing. That is not hypothetical: the joined spelling was
    invisible to every reader here until round 24, and round 25 found
    `_pytest_runs` still detecting pytest by looking for the literal
    word in `words[:3]`, which the joined form never produces.
    """
    word = words[position]
    if word != "-m":
        return word[2:]
    return words[position + 1] if position + 1 < len(words) else None


def _python_own_words_end(words, start=1, is_a_file=None):
    r"""Where the INTERPRETER's own words stop, and what stops them.

    ``(what, index, after)``. `what` is why the scan stopped --
    ``"command"`` for `-c`, ``"module"`` for `-m`, ``"operand"`` for the
    file (or `-`) Python runs, ``"none"`` when the invocation is options
    all the way down. `index` points at the word that stopped it and
    `after` at the first word belonging to the PROGRAM rather than to
    the interpreter.

    `python --help` gives the syntax as `[-c cmd | -m mod | file | -]
    [arg] ...`: the interpreter's own options end at the first of those,
    and everything after it belongs to the script. The option table is
    READ out of `--help` rather than recalled, and names the options
    taking a SEPARATE value; `-c` and `-m` are not in it because they
    END the scan rather than being skipped past.

    `is_a_file` is the caller's POLICY on the operand, and it is a
    parameter rather than a second copy of this loop because round 25
    found two readers answering "what does this invocation run?" their
    own way -- `_pytest_runs` by looking for the literal word `pytest`
    among `words[:3]`, `_suite_ran_assertions` by taking any word that
    ends in the script's name. Nine of sixteen command shapes were
    uncounted and four of ten mentions were read as real runs, measured
    before the fix. The default says every non-option word is the
    operand; `_python_entry_at` passes a filesystem check instead, and
    the difference is deliberate -- see its docstring.
    """
    if is_a_file is None:
        def is_a_file(_word):
            return True

    skip_the_next_word = False
    for position, word in enumerate(words[start:], start=start):
        if skip_the_next_word:
            skip_the_next_word = False
            continue
        if word == "-c":
            return ("command", position, position + 2)
        if word == "-m":
            return ("module", position, position + 2)
        # ...and the JOINED spelling of the same two. `python --help`
        # says `-c cmd` and `-m mod` "terminate the option list", and
        # measured, `python -mthis` and `python -cprint(1)` both run.
        # This scan saw neither, so it walked past them to the script's
        # own argument: `python -cprint(1) data.py` answered
        # ("file", "data.py") and seeded every import under a file the
        # interpreter never opened, and `python -mpytest tests/x.py`
        # answered ("file", "tests/x.py") -- the module invocation lost
        # and the target mis-seeded. `-W` and `-X` need no case: joined,
        # they are one word starting with a dash, which the rule below
        # already skips (Codex round 24, found by surveying the reader
        # that the reported one is twinned with).
        if not word.startswith("--") and len(word) > 2:
            if word.startswith("-c"):
                return ("command", position, position + 1)
            if word.startswith("-m"):
                return ("module", position, position + 1)
        if word in _PYTHON_OPTIONS_TAKING_A_SEPARATE_VALUE:
            skip_the_next_word = True
            continue
        if word.startswith("-"):
            continue
        if is_a_file(word):
            return ("operand", position, position + 1)
    return ("none", len(words), len(words))


# Bash's RESERVED WORDS, which may stand where a command does without
# being one. Read out of `compgen -k` when this was written, the way
# the `env` grammar and pytest's `config_names` are, and re-derived by
# `test_the_reserved_words_are_bashs_own` so the list cannot rot.
#
# A literal here rather than a subprocess at import time: this module is
# collected on machines that may have no bash, and a helper that cannot
# answer is worse than one answering from a checked list.
#
# Only a STANDALONE token is one of these. Brace expansion keeps its
# braces attached (`cat {a,b}.md` is one word), so stripping a bare `{`
# cannot eat a path.
_SHELL_RESERVED_WORDS = frozenset({
    "if", "then", "else", "elif", "fi", "case", "esac", "for", "select",
    "while", "until", "do", "done", "in", "function", "time", "{", "}",
    "!", "[[", "]]", "coproc",
})


def _the_command(words):
    """`words` from the word that names the PROGRAM onwards.

    A command line may open with variable assignments and with `env`,
    and neither is the program: `MODE=test bash scripts/wrapper.py`
    runs bash. Reading `words[0]` instead answered `MODE=test`, so the
    shell went unrecognised and every dependency inside that wrapper
    was lost (Codex round 17).

    It may also open with a RESERVED WORD, which round 17 did not
    consider and round 23 found: `{ python -m pytest x.py; }` is a
    brace group, and the command in it is not `{`. Codex reported that
    one shape; measuring the rest found nine that hide an invocation
    the shell certainly runs -- `!`, `time`, and every branch and loop
    body, where the `;` before `then` or `do` leaves the reserved word
    at the head of the next command. `if ...; then python -m pytest` is
    ordinary CI shell. Latent here -- this repository writes `then`
    three times and `else` once, never before an interpreter -- and
    fixed anyway, because "no workflow does that yet" is the argument
    round 21 used to leave a hole open and round 22 spent a round
    closing.

    One helper, because the same question is asked of the shell mark,
    the Python entry scan and -- since round 23 -- the pytest census,
    which had been reading `words[0]` since before this helper existed.
    """
    rest = list(words)
    while rest:
        if _is_an_assignment(rest[0]):
            rest.pop(0)
            continue
        if rest[0] not in _SHELL_RESERVED_WORDS:
            break
        word = rest.pop(0)
        # Most reserved words simply stand in front of the command. Three
        # take an operand of their OWN, and treating those uniformly left
        # that operand where the program belongs -- `time -p python -m
        # pytest x.py` read `-p` as the executable and the run went
        # uncounted (Codex round 24, the mirror of round 23: there the
        # error was stripping none of them, here it is stripping all of
        # them the same way). Each rule below is MEASURED against bash,
        # not recalled.
        if word == "time":
            # `help time`: `time [-p] pipeline`. `time -p true` and
            # `time -- true` both run, so the options are skipped by
            # shape rather than by a list of two.
            while rest and rest[0].startswith("-"):
                rest.pop(0)
        elif word == "function":
            # `function NAME [()] compound-command` -- the name is never
            # the program.
            if rest:
                rest.pop(0)
        elif word == "coproc":
            # `coproc [NAME] command`, and the NAME is only a name before
            # a COMPOUND command: measured, `coproc C echo hi` answers
            # `C: command not found`, so there `C` IS the command and
            # eating it would lose the invocation.
            if len(rest) > 1 and rest[1] in ("{", "("):
                rest.pop(0)
    return _command_after_env(rest)


def _command_after_env(words):
    """`words` with any leading `env` and its arguments removed."""
    if not words or words[0].rsplit("/", 1)[-1] != "env":
        return list(words)
    return _after_env(list(words[1:]))


def _split_env_string(text):
    """Split an `env -S` operand the way `env` does, not the way a shell
    would.

    `-S` has its OWN escape set — `\\_` is an argument separator, and
    `\\t`, `\\n`, `\\v`, `\\f`, `\\r`, `\\#`, `\\$`, `\\"`, `\\'` and `\\\\`
    stand for themselves. Lexing it with `shlex` consumed the backslash
    in `\\_bash -e` and answered `_bash`, where real `env` answers
    `bash` (Codex round 10, confirmed with `env --debug`).

    Only the escapes and quote removal are done here. `$VAR`
    substitution is `env`'s too and is deliberately NOT attempted: a
    variable's value is not knowable from the file, and guessing one
    would invent a command name rather than miss one.
    """
    escapes = {"t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r",
               "#": "#", "$": "$", '"': '"', "'": "'", "\\": "\\"}
    words, current, index, quote = [], "", 0, None
    while index < len(text):
        character = text[index]
        if character == "\\" and index + 1 < len(text):
            following = text[index + 1]
            if following == "c" and quote is None:
                # `\c` ENDS the string. `env --debug -S 'bash\c python3'`
                # prints "into: ‘bash’" and execs bash; mapping an
                # unknown escape to its own letter gave `bashc`, which is
                # not a shell, so `#!/usr/bin/env -S bash\c ignored` was
                # read as shell by neither the parse nor round 10's
                # fallback (Codex round 12) — the losing direction again.
                break
            if following == "_" and quote is None:
                if current:
                    words.append(current)
                    current = ""
                index += 2
                continue
            current += escapes.get(following, following)
            index += 2
            continue
        if quote is None and character in ("'", '"'):
            quote = character
        elif character == quote:
            quote = None
        elif quote is None and character in " \t":
            if current:
                words.append(current)
                current = ""
        else:
            current += character
        index += 1
    if current:
        words.append(current)
    return words


def _shebang_interpreter(text):
    """The interpreter a `#!` line names, or None.

    Asking the file what it is beats guessing from its name (Codex
    round 4). What the line means after that is `env`'s business, and
    this has now been wrong twice by refusing to read it: reading the
    word after `env` gave `-S` (round 5); skipping the words that start
    with `-` gave `FOO`, `tmp` and `FOO=1`, and then a whitelist gave
    `python3` for a line that runs bash (rounds 6 and 7). `_after_env`
    reads the grammar instead.
    """
    first = text.split("\n", 1)[0]
    if not first.startswith("#!"):
        return None
    words = [word.rsplit("/", 1)[-1] for word in _split_env_string(first[2:])]
    command = _command_after_env(words)
    return command[0] if command else None


def _paths_a_shell_command_names(command, exists, tracked):
    """Existing repo files mentioned by a command that names a shell.

    Round 6 recorded WHICH operand a shell ran, and answering that meant
    reading the invoker's option grammar — `env`'s in round 7, `bash`'s
    in round 8 — so round 8 deleted the channel and accepted that a
    shell file with no shebang, run explicitly, would be missed (Codex
    round 9 named exactly that case).

    This restores the evidence without the grammar, because the grammar
    was only ever answering "which of these words is the file" and the
    TREE answers it already: `-O`'s operand is `extglob`, `-u`'s is a
    variable name, `-c`'s is a command string, and none of them is a
    file this walk will ever reach. So no option is parsed.

    It does not resolve paths itself, and that was round 9's real
    mistake here. It was given its own rule — join the invoking step's
    working directory, then ask `exists` — while everything around it
    had resolved by TAIL against the tracked tree since round 5. So
    `bash inner.py` inside a wrapper marked a nonexistent `./inner.py`
    while the same line resolved `backend/inner.py` into the queue: the
    file the walk went on to READ was never the file that got marked,
    and a shebangless `.py` wrapper lost every dependency it named and
    every pytest in it (Codex round 11). Two resolvers for one question
    is the defect; `_paths_a_script_names` is the one the walk uses, so
    it is the one asked here, and the mark can no longer land on a
    different path than the queue.

    Measured on this repository: it marks **nothing**, because no
    workflow step here runs a shell with a path operand. It is
    insurance against the case round 9 described, and costs no phantom
    dependency on the tree it ships with.
    """
    out = set()
    for line in _logical_lines(command):
        # The shell has to BE the command. "any word names a shell"
        # marked every path on `python plain.py --format bash`, so a
        # shebangless `.py` was read as shell and its string constants
        # became phantom dependencies — the guard accusing a healthy
        # workflow, which is the failure this rule exists to avoid
        # (Codex round 10).
        words = _the_command(_shell_words(line))
        if not words or words[0].rsplit("/", 1)[-1] not in SHELL_INTERPRETERS:
            continue
        out |= _paths_a_script_names(line, exists, tracked)
    return out


def _is_shell_script(path, text, named_by_a_shell=False):
    """Should this file's text be read as shell commands?

    The file answers first: a shell suffix, or a `#!` line naming a
    shell. Round 8 deleted a second channel that read the INVOKER's
    options to decide this, because answering "which operand" needed a
    new option grammar every round.

    `named_by_a_shell` is that evidence restored in the one form that
    needs no grammar — the command names a shell somewhere, and this
    file is a path it mentions. It only decides the case the file
    cannot: **no shebang at all**. A `.py` saying `#!/usr/bin/env
    python3` stays Python however it was invoked, because a wrong
    answer there was measured at 154 phantom dependencies and one false
    finding when every reached `.py` was read as shell as well.

    What is still given up, and is now the only thing: a `.py` that is
    shell, has no shebang, and is reached WITHOUT a shell naming it.
    Nothing can see that without executing the workflow.
    """
    if path.endswith(SHELL_SUFFIXES):
        return True
    first = text.split("\n", 1)[0]
    if first.startswith("#!"):
        shebang = _shebang_interpreter(text)
        if shebang in SHELL_INTERPRETERS:
            return True
        # The parse said "not a shell", and six consecutive rounds have
        # found a way for that parse to be wrong about `env` — `-S`,
        # `-u NAME`, `--unset NAME`, a quoted operand, `\\_`. Every one
        # of those errors pointed the same way: a file that IS shell
        # read as something else, which LOSES a dependency.
        #
        # So the predicate stops depending on the parse being exact. A
        # shebang that names a shell anywhere is read as shell, whatever
        # the grammar says the command is. That over-approximates —
        # `env -u bash python3 x` runs python and is read as shell, so
        # its path-shaped strings become dependencies — and widening a
        # trigger is the error this whole rule prefers.
        #
        # `_shebang_interpreter` keeps its exact answer for the census,
        # which needs to tell python from shell rather than merely ask
        # whether a shell is involved.
        return any(
            word.rsplit("/", 1)[-1] in SHELL_INTERPRETERS
            for word in _split_env_string(first[2:])
        )
    return named_by_a_shell


def _logical_lines(text):
    r"""Script lines with `\`-continuations joined.

    A continued command is one command; lexing its halves separately
    splits a quoted string across them and loses the whole line.
    """
    buffer = ""
    for raw in text.splitlines():
        buffer += raw
        if buffer.endswith("\\"):
            buffer = buffer[:-1] + " "
            continue
        yield buffer
        buffer = ""
    if buffer:
        yield buffer


# Where a WORD begins, for the purpose of finding a comment: after
# whitespace, at the start of a line, or after a control operator.
# POSIX's list, parentheses included -- see `_strip_shell_comment`.
COMMENT_WORD_BOUNDARIES = "|&;<>()"


def _strip_shell_comment(line):
    r"""`echo hi  # note` -> `echo hi`, leaving `$#` and `${#x}` alone.

    A reached script's comments named files, and every path in them
    became a dependency the filter rule then demanded: `# See
    docs/guide.md` made that page something the workflow "runs" (Codex
    round 4).

    This is NOT `shlex`'s job, though replacing it with `shlex`'s
    `comments=True` was the obvious move and was tried: `shlex` treats
    `#` as a comment wherever it appears outside quotes, while a POSIX
    shell starts one only at the beginning of a word. So
    `echo "$#" ${#x} docs/real.md` came back as `['echo', '$#', '${']`
    — the parameter expansion cut in half and the path after it gone,
    which is the direction that loses a dependency. The word SPLITTING
    is `shlex`'s, in `_shell_tokens`; the comment boundary is the
    shell's own rule and is here.

    "The beginning of a word" includes the position right after a
    control OPERATOR, not only after whitespace. `cat docs/a.md;# See
    docs/phantom.md` is a comment to the shell and was not one here, so
    the words of the comment were lexed and `docs/phantom.md` became a
    dependency the filter rule would then demand — the guard inventing
    one, which is the failure it exists to prevent (Codex round 14).
    Measured against the tree: it predates round 13 exactly, in all
    five operator shapes, so the lexer change exposed nothing and hid
    nothing.

    `COMMENT_WORD_BOUNDARIES` includes the parentheses that
    `_shell_words` deliberately leaves out of its operator set, and the
    two are not in tension: where a WORD begins is POSIX's question and
    has one answer, while what to SPLIT on is a choice this guard makes
    against measured cost. `)# note` after a `case` arm is a comment in
    any shell; `docs/a#b.md` is not, because `#` there is not beginning
    anything.

    A BACKSLASH un-says all of it. `cat prefix\ # docs/real.md` passes
    two arguments — the word `prefix #` and the path — because the
    escaped space joins its word instead of ending one, so nothing
    begins at the `#` and there is no comment (Codex round 16). The
    same is true of `cat a\;# docs/real.md`, where the escaped operator
    is no operator, and of `echo \" docs/a.md # note`, where the
    escaped quote opens nothing and the comment after it is real. Three
    shapes of one rule, and the report named one: an escaped character
    is not the character it looks like. Two of them lose a dependency
    and the third invents one, so they do not even fail in the same
    direction.

    That rule is applied here the way `_mask_inactive_globs` applies
    it — consume `\X` as a PAIR — and not by looking behind a character
    to count the backslashes in front of it. Both answer round 5's case
    (`"a\\"` ends its quote, because the second backslash escapes the
    first); only the pair keeps the escaped character from being tested
    as a boundary afterwards, which is what round 16 was.
    """
    out, quote, index, begins_a_word = [], None, 0, True
    while index < len(line):
        char = line[index]
        # Inside SINGLE quotes a backslash is an ordinary character,
        # which is why `echo 'x\' # note` closes at that quote and the
        # `#` begins a comment. Reading it as an escape there left the
        # scanner quoted, kept the comment, and every path in it became
        # a dependency (Codex round 15) — the guard inventing one.
        if quote != "'" and char == "\\" and index + 1 < len(line):
            out.append(char)
            out.append(line[index + 1])
            index += 2
            begins_a_word = False
            continue
        if quote is not None:
            out.append(char)
            if char == quote:
                quote = None
            index += 1
            begins_a_word = False
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
            index += 1
            begins_a_word = False
            continue
        if char == "#" and begins_a_word:
            break
        out.append(char)
        index += 1
        begins_a_word = char.isspace() or char in COMMENT_WORD_BOUNDARIES
    return "".join(out)


def _shell_tokens(line, punctuation_chars):
    r"""One LINE of shell, read the way a shell reads it.

    There is one of these because there were two, and every round they
    disagreed about something.

    Round 15's disagreement was the LEXER. `shlex.split` clears
    `commenters`; a lexer built by hand does not, and the default is
    `#`. Round 13 fixed that for `_shell_words` and left the copy in
    `_shell_invocations` alone, so `echo tag#value; python -m pytest
    tests/test_x.py` lexed to `['echo', 'tag']` there — the whole
    pytest invocation gone, and with it every file reached only through
    that test.

    Round 16's disagreement was what that fix did not cover. The two
    readers were given one lexer and not one READING: `_shell_words`
    stripped the comment first and `_shell_invocations` never had, and
    nobody could see it because the `commenters` default round 15
    removed had been cutting the comment off for the wrong reason the
    whole time. Removing it exposed the older gap: `echo done # python
    -m pytest tests/legacy` came back with the comment as command
    tokens, and `_python_entry_groups` seeded `tests/legacy` from a
    pytest run that is commented out — the guard inventing a
    dependency, on a line that is doing nothing.

    So the stripping, the `shlex` configuration and the fallback are
    all HERE, and a third caller cannot forget any of them because
    there is nowhere else to put them. What a caller still chooses is
    `punctuation_chars`, and that choice is measured rather than
    inherited: `_shell_invocations` wants every operator `shlex` knows,
    while `_shell_words` passing `True` instead of `";|&<>"` made 404
    of the tree's 412 tracked files into phantom dependencies (Codex
    round 13, measured and declined).

    A line `shlex` refuses — an unbalanced quote, usually a multi-line
    `awk` program — falls back to a whitespace split of the STRIPPED
    line rather than being dropped. Dropping it loses a dependency; the
    fallback over-approximates, which only widens a trigger. On this
    tree the fallback fired on 20 workflow lines before this round and
    2 after it, because 18 of the 20 were prose comments holding an
    apostrophe (`the container's log`) — lines that strip to nothing
    and never reach `shlex` at all.
    """
    import shlex

    stripped = _strip_shell_comment(line)
    lexer = shlex.shlex(stripped, posix=True, punctuation_chars=punctuation_chars)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return stripped.split()


def _shell_words(line):
    r"""POSIX word splitting — `shlex`'s job, not mine.

    This used to split on whitespace and `=` and strip a fixed set of
    surrounding characters. Between them rounds 4, 5 and 6 produced a
    finding each, and the last was the plainest: `cat "docs/my
    guide.md"` became `docs/my` and `guide.md`, neither of which names
    anything, so a real dependency vanished. Every one of those was a
    re-implementation of POSIX word splitting, badly, inside a test
    module, when the standard library has done it correctly the whole
    time.

    `punctuation_chars` is the other half of the same delegation, and
    leaving it off cost a dependency in six shapes at once. Default
    `shlex.split` leaves a control operator ATTACHED to the word before
    it — `cat docs/a.md; echo done` yields `docs/a.md;`, which names no
    file, matches no tail and is no glob, so the dependency vanished
    while this guard stayed green. Codex round 13 reported the
    semicolon; measured, the same defect loses `docs/a.md` through
    `|`, `&&`, `>`, a subshell and a glob followed by `;` as well.
    `shlex` has recognised `();<>|&` the whole time and was not asked.

    It changes nothing about a word the shell would not have split
    either: `'a;b'` stays one word, `docs/a.md\;b.md` stays one word,
    and an ordinary command line lexes identically.
    """
    return _shell_tokens(line, ";|&<>")

GLOB_MASK = {"*": "\x00", "?": "\x01", "[": "\x02"}
_GLOB_UNMASK = {mask: character for character, mask in GLOB_MASK.items()}


def _mask_inactive_globs(line):
    """Hide every QUOTED or ESCAPED glob metacharacter behind a
    placeholder, leaving the active ones as they are.

    Quoting decides whether `[ab]` is a pattern or a name, and `shlex`
    in POSIX mode — the right lexer for the VALUE — throws that evidence
    away. Round 10 recovered it by lexing the line a SECOND time with
    `posix=False` and pairing the two lists; round 11 fixed the pairing
    being keyed by token value. Both were patches on the same mistake:
    two lexings of one line can disagree about how many words there
    are. `cat docs/a\\ b'[xy]'.md` is one pathname to the POSIX lexer and
    three words to the other, and the fallback for that disagreement
    dropped the quoting and expanded the bracket — inventing
    `docs/a bx.md` and `docs/a by.md`, which is the guard accusing a
    healthy workflow (Codex round 12).

    So there is ONE lexing. Masking asks a character-level question —
    is this character inside quotes, or preceded by a backslash? — with
    no word-splitting semantics in it, and it cannot mis-align with the
    lexer because it runs first and moves no boundary. A word is an
    active glob afterwards exactly when it still holds a real
    metacharacter.
    """
    out, quote, index = [], None, 0
    while index < len(line):
        character = line[index]
        if quote != "'" and character == "\\" and index + 1 < len(line):
            following = line[index + 1]
            out.append(character)
            out.append(GLOB_MASK.get(following, following))
            index += 2
            continue
        if quote is None and character in ("'", '"'):
            quote = character
        elif character == quote:
            quote = None
        elif quote is not None and character in GLOB_MASK:
            out.append(GLOB_MASK[character])
            index += 1
            continue
        out.append(character)
        index += 1
    return "".join(out)


def _unmask_globs(word):
    """The word as the shell would name it, placeholders undone."""
    for mask, character in _GLOB_UNMASK.items():
        word = word.replace(mask, character)
    return word


def _shell_commands(text):
    """The command lines of a script, comments removed.

    No directory, no control flow, no operand parsing — deliberately,
    and that is round 5's deletion: ten findings across rounds 3-5 were
    each a shape the hand-rolled reading did not handle, answering a
    question it does not need to ask.
    """
    for line in _logical_lines(text):
        words = _shell_words(line)
        if words:
            yield words


def _paths_a_script_names(text, exists, tracked):
    """Repo files a reached script names, resolved by TAIL not by
    directory.

    `python inner.py` after a `cd backend` means `backend/inner.py`, and
    knowing that by following the `cd` requires reading shell — the
    thing that kept being wrong. Matching the tracked tree by tail needs
    no directory at all: the file that exists is the answer, and when
    two files share a tail BOTH are taken.

    That over-approximates, on purpose. A path this yields wrongly
    widens a workflow's trigger, which costs a little CI time; a path it
    misses lets a dependency change without the job that checks it
    running, which is the failure the rule exists to prevent. The two
    errors are not symmetric and this one picks the cheap one.

    The boundary, because "it cannot lose one" was the claim and is not
    true: a token is matched by its SPELLING. A token that names files
    by PATTERN rather than by spelling — `backend/agents/*/` — resolves
    to nothing, and that is the expensive direction. Measured before
    leaving it that way: across every reached script here there are 14
    tokens carrying a glob metacharacter. Twelve are awk and sed regex
    fragments in `compose.sh`, which is the one such script a FILTERED
    workflow reaches, and expanding those would invent dependencies
    rather than find them. The two real pathname globs are in
    `scripts/demo.sh`, reached only by `librerun-smoke.yml`, which has
    no `paths:` filter and so can have no blind spot. Resolving even
    those would need `${dir}agent.yaml` — the variable expansion of the
    loop that reads them — which is shell semantics again, and §12 210
    is the record of where that road goes. The class is named here and
    pinned by a case rather than silently absent.
    """
    from collections import defaultdict

    by_tail = defaultdict(set)
    for path in tracked:
        pieces = path.split("/")
        for index in range(len(pieces)):
            by_tail["/".join(pieces[index:])].add(path)

    found = set()
    for line in _logical_lines(text):
        # ONE lexing, of a line whose inactive metacharacters are masked
        # first, so quoting survives with no second lexer to disagree
        # with it (Codex round 12).
        masked_words = _shell_words(_mask_inactive_globs(line))
        if not masked_words:
            continue
        for masked in masked_words:
            token = _unmask_globs(masked)
            bare = token.lstrip("./")
            if not _path_shaped([masked]):
                # A bare word carries no separator, dot or metacharacter,
                # so TAIL resolution would match every tracked file with
                # that basename — the direction that invents. It gets the
                # exact-path question only, which is what the step
                # resolver has always asked of one. Without this a script
                # running `sh helper` followed `helper` (through the step
                # channel) and never REPORTED it as reached, so the
                # dependency rule never demanded a filter select it —
                # found by checking the neighbourhood of round 12's
                # finding, and measured at zero added files on this tree
                # before being kept.
                if exists(token):
                    found.add(token)
                continue
            if any(character in masked for character in GLOB_CHARACTERS) and any(
                character in bare for character in GLOB_CHARACTERS
            ):
                # BEFORE the exact-path shortcut: an unquoted token is
                # still a glob even when a file is literally named with
                # the brackets, and the shell expands it rather than
                # opening that file. Taking the literal first hid
                # `docs/a.md` and `docs/b.md` behind a tracked
                # `docs/[ab].md` (Codex round 9). The literal is kept
                # too, since it is also a file the pattern names.
                found |= _tail_glob(bare, tracked)
                if exists(token):
                    found.add(token)
                continue
            if exists(token):
                found.add(token)
            else:
                found |= by_tail.get(bare, set())
    return found


GLOB_CHARACTERS = "*?["


def _path_shaped(words):
    """The words of a command that could name a file.

    A glob need not contain a `/` or a `.`: `cat Make*` names `Makefile`
    and `Makerules`, and requiring a separator or a dot discarded it
    before it could be expanded (Codex round 9).

    These are the words of ONE lexing of a MASKED line, so quoting is
    already carried in the word itself. Two earlier shapes are recorded
    because each was a way of losing it: a `{token: is_active}` dict,
    which cannot hold a per-position fact, so the quoted occurrence in
    `cat docs/[ab].md 'docs/[ab].md'` answered for the unquoted one
    (Codex round 11); and pairing two lexings, which cannot survive the
    two disagreeing about how many words there are (round 12).
    """
    return [
        word
        for word in words
        if "/" in word or "." in word or any(c in word for c in GLOB_CHARACTERS)
    ]


def _tail_glob(pattern, tracked):
    """Tracked files a pathname pattern names, matched by tail.

    Shell pathname expansion happens against the filesystem before a
    command ever runs, so `cat docs/*.md` names files no token spells.
    Matching them is not shell interpretation — the tracked tree is the
    same arbiter tail resolution already uses.

    `PurePosixPath.match` IS this question, so it is asked rather than
    answered again. It matches right to left, which is the tail rule;
    its `*` stops at a separator, as the shell's does; and it knows
    `[ab]` and `[!a]`, which the hand-written translation that stood
    here did not — that one `re.escape`d every piece that was not `*`
    or `?`, so a bracket expression matched only a file literally named
    with the brackets, and that is the direction that LOSES a
    dependency (Codex round 7).

    `fnmatch` is the obvious reach and is the wrong one: its `*`
    crosses `/`, so `pkg/*/data.yaml` would take `pkg/a/b/data.yaml`
    and a bare `*.md` the whole tree.
    """
    import re
    from pathlib import PurePosixPath

    if not pattern:
        return set()
    # `PurePosixPath.match` knows `[ab]` and `[!a]` but not a POSIX
    # class, and returned NOTHING for `docs/[[:alpha:]].md` — the
    # losing direction (Codex round 8). Rather than hand-write class
    # support, each class widens to `?`: one character, any character.
    # That over-matches (a digit satisfies `[[:alpha:]]` here), which
    # widens a trigger, and it cannot lose a file the shell would have
    # named, because every character the class admits is a character
    # `?` admits.
    # Any bracket expression CONTAINING a class widens, not only one
    # that is exactly a class: `[![:digit:]]` and `[[:alpha:]_]` are
    # ordinary shell patterns and matched nothing (Codex round 9).
    pattern = re.sub(
        r"\[(?:\[:[a-z]+:\]|[^\[\]])*\]",
        lambda m: "?" if "[:" in m.group(0) else m.group(0),
        pattern,
    )
    return {path for path in tracked if PurePosixPath(path).match(pattern)}


def _files_a_workflow_reaches(doc, exists, repo_root=None, tracked=None):
    """Every repo file the workflow runs, directly or one step in.

    ``tracked`` supplies the tree contents rather than the filesystem,
    because every path here is REPOSITORY-ROOT relative while the test
    process runs from ``backend``. The first wiring of this walked
    ``Path(tree_root)`` against the working directory, found no such
    directory, scanned nothing, and reported the tree clean — with eight
    gaps in it. A pass is not a measurement.
    """
    import shlex

    steps = list(_run_steps(doc))
    reached = set()
    for working_directory, text, _pythonpath in steps:
        reached |= {
            path
            for path in _paths_named_in_command(text, working_directory)
            if exists(path)
        }
    if repo_root is None or tracked is None:
        return reached

    # A shell script a step runs is a step's worth of commands too, and
    # stopping at the script itself was where the reach ended: a
    # workflow running `scripts/outer.sh`, which runs `python
    # scripts/inner.py`, reached `outer.sh` and never `inner.py`. A
    # dependency only `inner.py` has was invisible to the filter rule,
    # and so was a pytest inside it — which is how the
    # root-configuration census came to omit a workflow (Codex round 3;
    # the workflow it omitted was found by reading, not by this).
    # Expanded to a fixpoint, carrying the invoking step's working
    # directory and PYTHONPATH, because that is what the script's own
    # relative commands resolve against.
    shell_steps, expanded = [], set()
    pending_scripts, named_by_a_shell = set(), set()
    while True:
        fresh = []
        for working_directory, text, pythonpath in steps + shell_steps:
            if not isinstance(text, str):
                continue
            shell_named = _paths_a_shell_command_names(text, exists, tracked)
            named_by_a_shell |= shell_named
            # ...and REACHED, not only queued. Adding them to the
            # expansion candidates alone reported the wrapper's
            # dependencies while the wrapper itself was missing from
            # the answer -- which is round 6's finding, in the fix for
            # round 17's.
            reached |= shell_named
            # Those paths are CANDIDATES as well as evidence. The
            # regex reader splits `bash "scripts/my runner.sh"` into
            # `scripts/my` and `runner.sh`, so the wrapper the
            # shell-aware reader had just resolved was never queued and
            # nothing inside it was reached (Codex round 17).
            for path in _paths_named_in_command(text, working_directory) | shell_named:
                if not exists(path) or path in expanded:
                    continue
                # Every reached file is offered to `_is_shell_script`,
                # including `.py`: it answers from the file's own `#!`
                # line, so a wrapper named `.py` needs no exclusion here
                # and no second channel saying who invoked it.
                fresh.append((working_directory, pythonpath, path))
        fresh += [
            (".", None, path)
            for path in sorted(pending_scripts)
            if path not in expanded
        ]
        pending_scripts.clear()
        if not fresh:
            break
        for working_directory, pythonpath, path in fresh:
            expanded.add(path)
            try:
                text = (repo_root / path).read_text(errors="ignore")
            except OSError:
                continue
            # The FILE decides, not its name — and the INVOCATION
            # outranks the file. A `scripts/wrap.txt` carrying
            # `#!/bin/sh` is a script a step executes, and a suffix
            # prefilter never let it reach this question (Codex round
            # 5); a `scripts/wrapper.py` a step runs with `bash` is one
            # too (round 6).
            if not _is_shell_script(
                path, text, named_by_a_shell=path in named_by_a_shell
            ):
                continue
            named = _paths_a_script_names(text, exists, tracked)
            reached |= named
            # A script found by TAIL is a script, and recording it
            # without queueing it ended the walk one wrapper early:
            # `cd backend; sh inner.sh` resolved `backend/inner.sh` into
            # `reached`, and the next pass still looked for a literal
            # `inner.sh` that does not exist, so `backend/leaf.py` and
            # the pytest in it were never seen (Codex round 6). The
            # fixpoint has to be fed what the fixpoint found.
            pending_scripts |= named
            # The script's commands feed the Python entry walk with the
            # INVOKING step's context, which is about `sys.path` roots
            # and has never been the thing that went wrong here.
            for words in _shell_commands(text):
                # `shlex.join`, not `" ".join`: the consumer lexes this
                # back with `shlex`, so a plain join throws away the
                # quoting a line away from where it was parsed.
                # `python "scripts/my runner.py"` became three words and
                # the file stopped being a Python entry seed, so nothing
                # only its imports reach was checked (Codex round 7).
                shell_steps.append((working_directory, shlex.join(words), pythonpath))

    # Built ONCE per workflow, not per import: the previous version
    # scanned the whole tracked set on every lookup — 4,067 lookups over
    # 393 entries, 1.6 million comparisons for one workflow, growing with
    # (files traversed) x (tree size) (Codex round 7).
    index = _suffix_index(tracked)

    # To a FIXPOINT, not one hop. One hop was justified by measuring
    # workflows whose filters say `backend/**`, where a second hop can
    # only re-report files already covered — true there, and false for a
    # workflow with deliberately narrow backend filters. The container
    # battery is that counterexample: it runs `adapter_kit.run_contract`,
    # which imports `pii_service`, which reads `app.config.settings` for
    # the PII threshold. At one hop `backend/app/config.py` was two steps
    # away and invisible (Codex round 6).
    #
    # Measured before choosing (§12 138(d)): the closure over this whole
    # tree costs ~1.4s. Depth is no longer a knob anybody has to justify,
    # and a depth limit is a guess about how far a dependency can be —
    # this rule exists because guesses about the tree go stale.
    for roots, seeds in _python_entry_groups(
        doc, exists, index, tracked, repo_root, extra_steps=shell_steps
    ):
        seen, pending = set(), list(seeds)
        while pending:
            module = pending.pop()
            if module in seen:
                continue
            seen.add(module)
            pending.extend(
                _repo_files_a_module_reaches(
                    module, exists, repo_root, index, roots
                )
                - seen
            )
        reached |= {path for path in seen if exists(path)}
    return reached


def _unfiltered_dependency_problem(name, doc, exists, repo_root=None, tracked=None):
    """Why this workflow's ``paths:`` filter cannot see a file it runs.

    ``None`` when every such file is selected. ``exists`` is a predicate
    rather than a directory so the rule can be driven against a fixture
    tree, and so the real check can use git rather than the working tree
    (the zero-agents job deletes files that the repository still has).

    A workflow with no ``paths:`` filter runs on every change and can
    have no such gap, so it is not a finding.
    """
    triggers = _workflow_triggers(doc)
    filtered = [
        event
        for event in ("pull_request", "push")
        if isinstance(triggers.get(event), dict) and triggers[event].get("paths")
    ]
    if not filtered:
        return None

    reached = _files_a_workflow_reaches(doc, exists, repo_root, tracked)
    blind = {}
    for event in filtered:
        patterns = triggers[event]["paths"]
        for path in reached:
            if not _filter_selects(patterns, path):
                blind.setdefault(event, set()).add(path)
    if not blind:
        return None
    return (
        f"{name} runs files its own `paths:` filter does not select: "
        + "; ".join(
            f"{event} misses {sorted(paths)}" for event, paths in sorted(blind.items())
        )
        + ". A pull request changing only such a file skips the workflow, "
        "so the very thing the job depends on can be weakened and merged "
        "without the job ever running. Add the path to the filter."
    )


def test_no_workflow_runs_a_file_its_own_paths_filter_cannot_see():
    """P2: extracting a shared script moved it out of its own trigger's reach.

    ``unit-suites`` certifies both suites with
    ``scripts/assert_suite_ran.py``. While that assertion was inline in
    the workflow, ``.github/workflows/**`` covered it; the round-2 fix
    that pulled it into one shared script — the right fix, for a real
    defect — left the filter behind, so a pull request narrowing or
    breaking the guard matched no pattern, skipped both jobs, and could
    merge unexercised (Codex round 3).

    That is §12 141 in CI's clothes: an identity holds only while its
    precondition does, and "the guard lives in a watched path" stopped
    being true the moment it moved. The remedy is not to remember — the
    rule is derived from the workflows and the tree, so the next shared
    helper is covered without anyone recalling that this test exists.

    Censusing every workflow for the same question, rather than only the
    file it was reported against, found a second one: the container
    battery installs from ``backend/requirements.txt`` and its filter
    listed ``backend/adapter_kit/**`` but not that file, so a dependency
    bump changed what the battery runs without running it.
    """
    from pathlib import Path

    directory = _workflow_dir()
    repo_root = directory.parents[1]
    tracked = _tracked_paths(repo_root, directories=False)

    def exists(path):
        if tracked is not None:
            return path in tracked
        return (repo_root / path).is_file()

    problems = []
    # A census that comes back EMPTY makes everything below vacuous: no
    # workflows, no problems, green. This is the guard thirteen review
    # rounds went into, and it would have certified a repository with no CI
    # at all — the same "reports success by not looking" shape it exists to
    # forbid, one level in. Measured: with the directory pointed at an empty
    # path it PASSED, while six of its neighbours tripped on assertions of
    # their own.
    workflows = _workflow_files(directory)
    assert workflows, (
        f"no workflow files under {directory} — this guard checked nothing. "
        f"Either the path is wrong or CI has been deleted; both are failures "
        f"and neither should look like success."
    )
    for path in workflows:
        problem = _unfiltered_dependency_problem(
            path.name, _load_workflow(path), exists, repo_root, tracked
        )
        if problem:
            problems.append(problem)
    assert not problems, "\n".join(problems)


def test_the_dependency_rule_resolves_relative_paths_and_catches_the_gap():
    """The rule is negative-tested by injection, including the `..` shape.

    Three cases, because the first draft of this check passed the real
    tree while the defect was in it. It scanned the token as written, and
    ``../scripts/assert_suite_ran.py`` starts with ``..``, so it was
    discarded as outside the repository and the workflow was reported
    clean. A checker that answers "no problem" by not resolving the one
    path shape this repository actually uses is the CLAUDE.md failure
    exactly — and it was MY draft, one round after writing that rule down.

    Driven against fixtures rather than the real tree: a rule that can
    only be exercised by a tree currently containing the violation stops
    being evidence the moment the violation is fixed.
    """
    here = {"scripts/shared.py", "backend/app/main.py"}

    def on_disk(path):
        return path in here

    covered = {
        True: {"pull_request": {"paths": ["backend/**", "scripts/shared.py"]}},
        "jobs": {
            "suite": {
                "defaults": {"run": {"working-directory": "backend"}},
                "steps": [{"run": "python3 ../scripts/shared.py report.xml 10"}],
            }
        },
    }
    assert _unfiltered_dependency_problem("w.yml", covered, on_disk) is None, (
        "a filter that DOES list the shared script was reported as a gap — "
        "either the resolution or the matcher is wrong in the safe direction"
    )

    # The injection: the same workflow, the path taken back out.
    import copy

    gap = copy.deepcopy(covered)
    gap[True]["pull_request"]["paths"] = ["backend/**"]
    problem = _unfiltered_dependency_problem("w.yml", gap, on_disk)
    assert problem is not None, (
        "a workflow running ../scripts/shared.py with a filter of only "
        "backend/** was reported clean — the check is not resolving the "
        "relative path against the job's working-directory, which is the "
        "one path shape this repository uses"
    )
    assert "scripts/shared.py" in problem, problem

    # And the token must be resolved, not merely found: a checker that
    # kept `../scripts/shared.py` verbatim would look for a path that is
    # not in the tree and fall silent.
    named = _paths_named_in_command(
        "python3 ../scripts/shared.py report.xml 10", "backend"
    )
    assert "scripts/shared.py" in named, f"the `..` was not normalised away: {named}"
    assert "../scripts/shared.py" not in named, f"the token was kept raw: {named}"

    # A file the run WRITES rather than reads is not a dependency: it is
    # absent from the tree, so existence — not an exception list — keeps
    # it out. (§12 119: no exception lists in structural tests.)
    assert not on_disk("report.xml")

    # A workflow with no paths filter runs on everything and cannot have
    # this gap; reporting one would be a false alarm that teaches
    # maintainers to ignore the check.
    unfiltered = copy.deepcopy(covered)
    unfiltered[True] = {"pull_request": None}
    assert _unfiltered_dependency_problem("w.yml", unfiltered, on_disk) is None


def test_the_dependency_rule_sees_a_file_with_no_extension():
    """P2: the extractor required a dot, so an extensionless file was invisible.

    ``container-battery`` passes
    ``backend/agents/_examples/echo_container/Dockerfile`` straight to
    ``docker build``. The first version of this rule matched only tokens
    whose last component contained a dot, so that dependency was never
    considered — and with its covering pattern deleted the guard still
    reported the tree clean (Codex round 4). A guard with a blind spot
    reports on what it looked at as though it had looked at everything,
    which is the failure this guard exists to prevent, committed by the
    guard one round after it was written.

    So the extractor no longer requires a suffix, and existence in the
    repository does all the discriminating — no exception list to keep in
    step with the workflows (§12 119).

    Dropping the suffix opens a FALSE-ALARM direction that has to be
    closed in the same breath: a bare word like ``backend`` is a tracked
    DIRECTORY, and demanding that filters cover it would report gaps that
    are not gaps. The oracle is tracked FILES only, and both directions
    are pinned here.
    """
    tracked_files = {
        "agents/echo/Dockerfile",          # extensionless, and a dependency
        "backend/app/main.py",
    }

    def on_disk(path):
        return path in tracked_files

    workflow = {
        True: {"pull_request": {"paths": ["backend/**", "agents/echo/**"]}},
        "jobs": {
            "battery": {
                "steps": [
                    {"run": "docker build -f agents/echo/Dockerfile -t img ."},
                ]
            }
        },
    }

    # The extractor must SEE it. Asserting the guard merely passes would
    # not distinguish "covered" from "never looked at" — which is exactly
    # how the previous version passed.
    named = _paths_named_in_command(
        "docker build -f agents/echo/Dockerfile -t img .", None
    )
    assert "agents/echo/Dockerfile" in named, (
        f"an extensionless file was not extracted at all: {sorted(named)}"
    )

    assert _unfiltered_dependency_problem("w.yml", workflow, on_disk) is None

    # The injection: the pattern covering it removed. This is the case
    # that passed before the suffix requirement was dropped.
    import copy

    gap = copy.deepcopy(workflow)
    gap[True]["pull_request"]["paths"] = ["backend/**"]
    problem = _unfiltered_dependency_problem("w.yml", gap, on_disk)
    assert problem is not None, (
        "an uncovered Dockerfile was reported clean — the extractor is "
        "still requiring a suffix, so extensionless dependencies are "
        "invisible and the derived invariant is not derived"
    )
    assert "agents/echo/Dockerfile" in problem, problem

    # The other direction. `docker`, `build`, `-t` and `img` are words,
    # not files, and `agents` is a directory: none may be demanded.
    for word in ("docker", "build", "img", "agents", "backend"):
        assert not on_disk(word), f"{word} must not read as a tracked file"
    narrow = {
        True: {"pull_request": {"paths": ["agents/echo/**"]}},
        "jobs": {"b": {"steps": [{"run": "docker build -t img ."}]}},
    }
    assert _unfiltered_dependency_problem("w.yml", narrow, on_disk) is None, (
        "a bare command word was demanded as a dependency — dropping the "
        "suffix requirement traded a blind spot for a false alarm"
    )


def test_the_dependency_rule_reads_action_inputs_not_only_run_commands():
    """A `with:` input names files too, and reading only `run:` missed four.

    ``cache-dependency-path`` decides a job's cache key, so a change to
    the file it names changes what the job restores — a dependency by
    exactly the argument that covers every other entry here. Reading only
    ``run:`` left four invisible: ``backend/adapters/pyproject.toml`` and
    ``frontend/package-lock.json`` in ``unit-suites``,
    ``sdk/python/librerun-agent/pyproject.toml`` in the container
    battery, and the adapters file again in ``adapter-battery``.

    All four are selected by their filters today, so nothing was slipping
    through — measured, not assumed. What was missing is the ability to
    notice if one stopped being: with ``sdk/**`` removed from the
    container battery, the previous version reported the tree clean.

    Found by auditing this rule's own blind spots rather than waiting for
    the next round to report one. Three rounds running, each fix here
    created the next defect; the way out is to enumerate what the guard
    cannot see and inject each case, not to wait to be told.

    An action input is resolved at the REPOSITORY ROOT, not against the
    job's working-directory — a distinction that matters for a job
    running in ``backend`` whose ``cache-dependency-path`` still names
    ``backend/requirements.txt`` from the top.
    """
    import copy

    tracked_files = {"sdk/pkg/pyproject.toml", "backend/app/main.py"}

    def on_disk(path):
        return path in tracked_files

    workflow = {
        True: {"pull_request": {"paths": ["backend/**", "sdk/**"]}},
        "jobs": {
            "build": {
                "defaults": {"run": {"working-directory": "backend"}},
                "steps": [
                    {
                        "uses": "actions/setup-python@v5",
                        "with": {
                            "python-version": "3.12",
                            "cache": "pip",
                            "cache-dependency-path": "sdk/pkg/pyproject.toml",
                        },
                    },
                    {"run": "python -m pytest app/main.py"},
                ],
            }
        },
    }
    assert _unfiltered_dependency_problem("w.yml", workflow, on_disk) is None

    # The injection: the pattern covering the `with:`-only file removed.
    # Nothing in any `run:` names it, so only an extractor that reads
    # action inputs can report this.
    gap = copy.deepcopy(workflow)
    gap[True]["pull_request"]["paths"] = ["backend/**"]
    problem = _unfiltered_dependency_problem("w.yml", gap, on_disk)
    assert problem is not None, (
        "a cache-dependency-path outside every filter was reported clean "
        "— the extractor is reading `run:` only, so a file named solely "
        "in an action input can leave the filter without the guard noticing"
    )
    assert "sdk/pkg/pyproject.toml" in problem, problem

    # Repo-root resolution, not the job's working-directory. Resolving
    # this input against `backend` would look for `backend/sdk/pkg/...`,
    # find nothing, and fall silent — a blind spot wearing a fix.
    named = _paths_named_in_command("sdk/pkg/pyproject.toml", None)
    assert "sdk/pkg/pyproject.toml" in named, named
    assert "backend/sdk/pkg/pyproject.toml" not in named, named

    # A non-path input is not a dependency; existence keeps "3.12" and
    # "pip" out with no exception list (§12 119).
    for value in ("3.12", "pip"):
        assert not on_disk(value)


def test_the_dependency_rule_follows_imports_and_composed_reads(tmp_path):
    """A dependency reached BY the code a workflow runs, not named in the command.

    Codex round 5, and the case I had declined one comment earlier —
    wrongly, and on a premise I had not checked. I said transitive
    dependencies had "no gap behind them today" after looking at one
    example (``librerun-smoke``, which has no ``paths:`` filter at all).
    Censusing instead of sampling found **thirteen** live gaps across
    four workflows. Judging a class from the one instance I happened to
    pick is the mistake this file keeps recording.

    Two shapes, because the gaps are one of each and neither mechanism
    finds the other:

    * ``container-battery`` runs ``python -m adapter_kit.run_contract``,
      which IMPORTS ``app.agents.manifest``. An import walker finds it.
    * a chassis test READS ``repo_root / "docs" / "api" / "openapi.yaml"``
      and ``exec_module``s ``scripts/export_openapi.py``. Neither is ever
      imported, so no import walker would see either; they are pathlib
      joins of string literals. (Both were named
      ``docs/vita_api_spec_v2.yaml`` and ``scripts/dump_openapi.py``
      until S8; the guard demanded the new paths in ``unit-suites`` the
      moment they changed, which is the guard working.)

    **What this still cannot see, stated rather than implied.** A path
    built from a variable or an environment lookup; a file a *shell*
    script reads once invoked; anything beyond the first hop. Those are
    outside the rule, and the rule says so here rather than leaving a
    reader to assume a completeness it does not have — the round-5
    finding was as much about the claim as about the code.
    """
    import textwrap

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "entry.py").write_text(
        textwrap.dedent(
            '''
            from pkg.helper import thing
            from pathlib import Path

            SPEC = (Path(__file__).parents[1] / "docs" / "spec.yaml").read_text()
            '''
        )
    )
    (tmp_path / "pkg" / "helper.py").write_text("thing = 1\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.yaml").write_text("openapi: 3.1.0\n")

    tracked = {"pkg/entry.py", "pkg/helper.py", "docs/spec.yaml"}

    def on_disk(path):
        return path in tracked

    # The extractor must reach BOTH, from the one entry point, and the
    # roots it searches must include the repository root for this fixture
    # to resolve `pkg.helper` at all.
    reached = _repo_files_a_module_reaches(
        "pkg/entry.py", on_disk, tmp_path, _suffix_index(tracked), (".",)
    )
    assert "pkg/helper.py" in reached, f"the import was not followed: {reached}"
    assert "docs/spec.yaml" in reached, f"the composed read was not seen: {reached}"

    workflow = {
        True: {"pull_request": {"paths": ["pkg/**", "docs/**"]}},
        "jobs": {"j": {"steps": [{"run": "python -m pkg.entry"}]}},
    }
    assert _unfiltered_dependency_problem(
        "w.yml", workflow, on_disk, tmp_path, tracked
    ) is None

    # The injections, one per shape. Each is invisible to the other
    # mechanism, so passing both proves two things rather than one twice.
    import copy

    for dropped, expected in (("docs/**", "docs/spec.yaml"),
                              ("pkg/**", "pkg/helper.py")):
        gap = copy.deepcopy(workflow)
        gap[True]["pull_request"]["paths"] = [
            p for p in ["pkg/**", "docs/**"] if p != dropped
        ]
        problem = _unfiltered_dependency_problem(
            "w.yml", gap, on_disk, tmp_path, tracked
        )
        assert problem is not None, (
            f"dropping {dropped} left {expected} outside every filter and "
            f"the guard reported clean — that shape is not being discovered"
        )
        assert expected in problem, problem

    # Without a repo root the rule falls back to literally-named files
    # only. That is the bound, and it must be a real fallback rather than
    # a crash: the fixture tests above drive exactly this path.
    assert _unfiltered_dependency_problem("w.yml", workflow, on_disk) is None


def test_module_resolution_is_deterministic_and_refuses_an_ambiguous_tail():
    """P2: resolution by bare suffix picked whichever file a SET yielded first.

    Round 6 replaced a hardcoded root list with suffix matching, and I
    justified it by claiming no module tail in this repository resolves
    to more than one file. **That claim was false**, and the census
    behind it never tested a single real tail: its loop `break`ed at the
    end of the first iteration, so only ``i=0`` ran — the full path,
    which matches exactly one file by construction. Thirteen tails
    actually collide here, ``agents`` among them (three candidates), and
    because ``tracked`` is a set the winner moved with the Python hash
    seed: measured, seed 1 and seed 2 disagreed (Codex round 7).

    A guard whose answer depends on the hash seed is not a guard. Worse,
    following the wrong module means following the wrong *dependencies*,
    so a narrowly filtered workflow could pass while missing the real
    ones — a false negative, the exact failure this rule exists to stop.

    The fix is not a third guess. Roots are DERIVED — the importing
    file's own directory, each job's working-directory, ``PYTHONPATH``
    entries the workflow sets, ``sys.path`` inserts a ``conftest.py``
    performs, then the repository root — and tried in order, the way an
    interpreter resolves an import. The tail index is consulted only
    when no root has the module, and only when the tail is unique. An
    ambiguous tail raises rather than guessing.
    """
    tracked = {
        "backend/agents/__init__.py",
        "backend/app/agents/__init__.py",
        "backend/app/routers/agents.py",
        "backend/app/config.py",
    }

    def on_disk(path):
        return path in tracked

    index = _suffix_index(tracked)

    # With `backend` as a root, `agents` is the regular package — the one
    # an interpreter running from `backend` would bind.
    assert _module_file("agents", on_disk, index, ("backend",)) == (
        "backend/agents/__init__.py"
    )
    # With `backend/app` as the root it is the other one, and that is
    # correct rather than contradictory: the root decides, as it does at
    # runtime.
    assert _module_file("agents", on_disk, index, ("backend/app",)) == (
        "backend/app/agents/__init__.py"
    )

    # With NO root that has it, the tail is ambiguous and must raise —
    # never return an arbitrary candidate.
    try:
        _module_file("agents", on_disk, index, ("services/gateway",))
    except _AmbiguousModule as exc:
        assert len(exc.candidates) == 3, exc.candidates
        assert "backend/agents/__init__.py" in exc.candidates
    else:
        raise AssertionError(
            "an ambiguous module tail resolved silently — the guard is "
            "picking whichever entry the set yielded first, which moves "
            "with the hash seed"
        )

    # A unique tail still resolves through the index when no root has it.
    assert _module_file("app.config", on_disk, index, ("nowhere",)) == (
        "backend/app/config.py"
    )
    # And a stdlib or third-party name resolves to nothing, which is what
    # keeps `import json` out with no exception list (§12 119).
    assert _module_file("json", on_disk, index, ("backend",)) is None


def test_the_traversal_indexes_once_instead_of_scanning_per_import():
    """P2: every import scanned the whole tracked set — quadratic by size.

    Measured before the fix: one `unit-suites` traversal made 4,067
    `_module_file` calls, each scanning all 393 tracked entries — 1.6
    million comparisons, growing with (files traversed) x (tree size).
    At ten times this repository's size that is minutes, in a test every
    suite runs (Codex round 7).

    This is a COST property, so it is pinned structurally and the reason
    is written down rather than left to a timing assertion that would be
    flaky on a loaded runner (§12 136(g)). The index is built once per
    traversal, and `_module_file` has no access to the tracked set at
    all — it takes the index, so a future edit cannot reintroduce the
    scan without changing the signature.
    """
    import inspect

    source = inspect.getsource(_files_a_workflow_reaches)
    assert source.count("_suffix_index(") == 1, (
        "the tail index must be built exactly once per traversal; "
        f"found {source.count('_suffix_index(')} constructions"
    )

    parameters = list(inspect.signature(_module_file).parameters)
    assert "tracked" not in parameters, (
        f"_module_file takes {parameters} — handing it the tracked set "
        f"again is how the per-import linear scan comes back"
    )
    assert "index" in parameters and "roots" in parameters, parameters

    body = inspect.getsource(_module_file)
    assert "for path in tracked" not in body, (
        "_module_file is scanning the tracked set per call again"
    )


def test_import_roots_are_per_invocation_not_per_workflow(tmp_path):
    """P2: a workflow-wide root order cannot express a step's own directory.

    ``container-battery`` sets its working directories on individual
    STEPS — ``sdk/python/librerun-agent`` and ``backend`` — and the root
    builder read only ``defaults.run.working-directory``. Its root list
    therefore began at ``.`` with neither actual execution directory in
    it, and the backend entry point resolved anyway only because an
    unrelated conftest happened to contribute a ``backend`` root. Measured:
    strip every conftest contribution and that workflow's roots collapsed
    to ``['.']`` (Codex round 8).

    Roots are a property of the INVOCATION — the step's own directory,
    then the job's ``PYTHONPATH``, then the conftests of the tree being
    collected, then the repository root — because that is the order the
    interpreter uses, and a workflow that runs two commands in two
    directories means two different things by "where does this import
    resolve".
    """
    tracked = {"pkg/mod.py", "other/pkg/mod.py"}
    exists = tracked.__contains__
    index = _suffix_index(tracked)

    doc = {
        "jobs": {
            "two-steps": {
                "steps": [
                    {"run": "python -m pkg.mod", "working-directory": "."},
                    {"run": "python -m pkg.mod", "working-directory": "other"},
                ]
            }
        }
    }
    groups = _python_entry_groups(doc, exists, index, tracked, tmp_path)
    resolved = {sorted(seeds)[0] for _roots, seeds in groups}
    assert resolved == {"pkg/mod.py", "other/pkg/mod.py"}, (
        f"the same command in two directories resolved to {resolved} — a "
        f"single workflow-wide root order cannot distinguish them"
    )


def test_a_conftest_root_does_not_leak_into_an_unrelated_suite(tmp_path):
    """P2: every tracked conftest contributed its roots to every workflow.

    So a suite that is never collected could silently disambiguate an
    import in a job that has nothing to do with it — turning a name that
    ought to raise with three candidates into a confident, and possibly
    wrong, answer.

    **This one is invisible to the filter guard**, measured rather than
    assumed: injecting the unscoped version leaves
    ``test_no_workflow_runs_a_file_its_own_paths_filter_cannot_see``
    green, because on today's tree it changes which file is followed
    without changing any filter verdict. A property no existing test can
    fail for is a property with no test, which is why this one exists
    (§12 134).
    """
    (tmp_path / "suite_a").mkdir()
    (tmp_path / "suite_a" / "conftest.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parents[1] / 'vendor' / 'src'))\n"
    )
    (tmp_path / "suite_b").mkdir()
    (tmp_path / "vendor").mkdir()
    tracked = {
        "suite_a/conftest.py",
        "suite_b/test_thing.py",
        "vendor/src/thing.py",
    }

    # Collecting suite_a: its own conftest contributes the vendor root.
    assert _conftest_roots(tracked, tmp_path, "suite_a") == ["vendor/src"], (
        "a suite's own conftest must contribute its sys.path insertion"
    )

    # Collecting suite_b: suite_a's conftest is never loaded, so its
    # insertion is not on the path and must not be offered as a root.
    assert _conftest_roots(tracked, tmp_path, "suite_b") == [], (
        "a conftest outside the collected tree contributed a root — an "
        "unrelated suite can now disambiguate imports it never loads"
    )

    # And the repository-wide view is not a licence either: an invocation
    # that collects nothing (a plain script, `python -m module`) loads no
    # conftest at all.
    doc = {"jobs": {"j": {"steps": [{"run": "python -m thing"}]}}}
    groups = _python_entry_groups(
        doc, tracked.__contains__, _suffix_index(tracked), tracked, tmp_path
    )
    for roots, _seeds in groups:
        assert "vendor/src" not in roots, (
            f"a plain `python -m` invocation was given a conftest root: {roots}"
        )


def test_a_composed_conftest_path_resolves_against_its_own_base(tmp_path):
    """P2: `parents[1] / "src"` was flattened to `src` and then discarded.

    ``sdk/python/librerun-agent/tests/conftest.py`` inserts
    ``Path(__file__).resolve().parents[1] / "src"``. Reducing that chain
    to its string segments alone yields ``src``, the existence check
    looks for a repository-root ``src/``, finds none, and the real
    ``sdk/python/librerun-agent/src`` import root is dropped (Codex round
    9). The container battery then resolved ``librerun_agent`` only
    because that tail is unique TODAY — add a second package with the
    same tail and the guard would raise ambiguity for a name pytest
    resolves without difficulty.

    **Invisible to the filter guard**, measured rather than assumed:
    injecting the base-less version leaves
    ``test_no_workflow_runs_a_file_its_own_paths_filter_cannot_see``
    green, because on this tree it changes how a name resolves without
    changing any filter verdict (§12 151(f)).
    """
    import ast

    source = (
        "import sys\n"
        "from pathlib import Path\n"
        "SRC = Path(__file__).resolve().parents[1] / 'src'\n"
        "sys.path.insert(0, str(SRC))\n"
    )
    tree = ast.parse(source)

    composed = _composed_path_literals(tree, "pkg/tests/conftest.py")
    assert composed == ["pkg/src"], (
        f"a conftest-relative insertion resolved to {composed}; "
        f"`parents[1]` of pkg/tests/conftest.py is `pkg`, so the root is "
        f"`pkg/src` — flattening the chain loses the base entirely"
    )

    # `.parent` is level 0, and a base this does not model falls back to
    # a repository-root reading rather than guessing.
    assert _composed_path_literals(
        ast.parse("X = Path(__file__).parent / 'here'"), "pkg/tests/conftest.py"
    ) == ["pkg/tests/here"]
    assert _composed_path_literals(
        ast.parse("X = repo_root / 'docs' / 'spec.yaml'"), "pkg/tests/conftest.py"
    ) == ["docs/spec.yaml"]

    # And through `_conftest_roots`, which is what actually has to pass
    # the base. Asserting only on the helper proved the helper works and
    # said nothing about its caller — injecting the base-less call left
    # the first version of this test GREEN. The fixture is deliberately
    # nested so base-less and based DISAGREE: `parents[1] / "src"` from
    # `pkg/tests/conftest.py` is `pkg/src`, while the flattened reading
    # is a repository-root `src` that does not exist.
    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / "pkg" / "tests" / "conftest.py").write_text(source)
    (tmp_path / "pkg" / "src").mkdir()
    tracked = {"pkg/tests/conftest.py", "pkg/tests/test_x.py", "pkg/src/mod.py"}
    assert _conftest_roots(tracked, tmp_path, "pkg/tests") == ["pkg/src"], (
        "_conftest_roots did not resolve the insertion against the "
        "conftest's own directory — the caller is not passing the base"
    )


def test_a_file_target_seeds_its_group_with_the_conftests_pytest_loads(tmp_path):
    """P2: an explicit file target produced an empty group and was skipped.

    ``chassis-zero-agents`` and ``adapter-battery`` run
    ``python -m pytest tests/<one file>.py``. Treating every target as a
    directory made ``f"{target}/"`` prefix nothing, so the group was
    dropped and the file was reached only as a plain script — without
    ``backend/tests/conftest.py``, which pytest really does load for it
    (Codex round 9).

    This is the mirror of round 8, and both of my answers were wrong in
    turn: contributing every conftest everywhere, then scoping strictly
    to "inside". The rule pytest uses is neither — it loads conftests
    from the rootdir DOWN TO the target, so the set is the target's
    ancestor chain, plus what is inside when the target is a directory.

    Also invisible to the filter guard, measured: injecting "a file
    target is not a seed" leaves it green.
    """
    (tmp_path / "suite").mkdir()
    (tmp_path / "suite" / "conftest.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor'))\n"
    )
    (tmp_path / "vendor").mkdir()
    tracked = {
        "suite/conftest.py",
        "suite/test_one.py",
        "suite/test_two.py",
        "vendor/thing.py",
    }

    # A FILE target: its ancestor chain includes suite/conftest.py, so
    # the vendor root is on the path exactly as pytest would put it.
    assert _conftest_roots(tracked, tmp_path, "suite/test_one.py") == ["vendor"], (
        "a file target got no conftest roots — pytest loads the "
        "conftests from the rootdir down to the target, so its own "
        "directory's conftest is always among them"
    )
    # The directory target agrees, which it must: the same suite.
    assert _conftest_roots(tracked, tmp_path, "suite") == ["vendor"]

    # And the group exists at all, seeded by the file itself.
    doc = {
        "jobs": {
            "j": {"steps": [{"run": "python -m pytest suite/test_one.py -q"}]}
        }
    }
    groups = _python_entry_groups(
        doc, tracked.__contains__, _suffix_index(tracked), tracked, tmp_path
    )
    seeded = [
        (roots, seeds) for roots, seeds in groups if seeds == {"suite/test_one.py"}
    ]
    assert seeded, (
        f"the file target seeded no group of its own: {groups} — it is "
        f"reached only as a plain script, so no conftest applies to it"
    )
    assert any("vendor" in roots for roots, _ in seeded), (
        f"the file target's group has no conftest root: {seeded}"
    )


def test_a_pytest_target_does_not_also_seed_a_plain_group(tmp_path):
    """P2: a `.py` target belonged to pytest AND to a plain interpreter run.

    Round 9 made a file target seed its own conftest-aware group. It did
    not stop the same word being picked up as a plain script, so each of
    ``chassis-zero-agents`` and ``adapter-battery`` emitted TWO groups
    for one file — the second with no conftest roots at all.

    Today that is only redundant, because every module tail those files
    import happens to be unique. The failure it sets up is concrete, and
    this fixture is it rather than an argument: a name that only the
    conftest's root disambiguates makes the conftest-free group raise
    ``_AmbiguousModule`` for an import pytest resolves without
    difficulty (Codex round 10). Measured before the fix:

        RAISED: module 'shared' resolves to 2 tracked files and no
        import root disambiguates it

    A guard that reports a defect in code that is fine is as damaging as
    one that misses a real defect, and faster to get switched off.
    """
    (tmp_path / "suite").mkdir()
    (tmp_path / "suite" / "conftest.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor'))\n"
    )
    (tmp_path / "suite" / "test_one.py").write_text("import shared\n")
    for directory in ("vendor/shared", "other/shared"):
        (tmp_path / directory).mkdir(parents=True)
        (tmp_path / directory / "__init__.py").write_text("")

    tracked = {
        "suite/conftest.py",
        "suite/test_one.py",
        "vendor/shared/__init__.py",
        "other/shared/__init__.py",
    }
    exists = tracked.__contains__
    index = _suffix_index(tracked)
    doc = {
        "jobs": {
            "j": {"steps": [{"run": "python -m pytest suite/test_one.py -q"}]}
        }
    }

    groups = _python_entry_groups(doc, exists, index, tracked, tmp_path)
    for_the_file = [
        (roots, seeds) for roots, seeds in groups if "suite/test_one.py" in seeds
    ]
    assert len(for_the_file) == 1, (
        f"the pytest target seeded {len(for_the_file)} groups: {for_the_file}. "
        f"A `.py` argument to pytest is pytest's positional, not a plain "
        f"script, and the extra group carries no conftest roots."
    )
    assert "vendor" in for_the_file[0][0], for_the_file

    # And the whole traversal completes: with only the conftest-aware
    # group left, `import shared` resolves through `vendor` instead of
    # raising. This is the assertion that fails under the injection —
    # counting groups alone would not show the consequence.
    for roots, seeds in groups:
        for seed in seeds:
            _repo_files_a_module_reaches(seed, exists, tmp_path, index, roots)


def test_pytest_option_values_and_node_ids_are_not_targets():
    """P2 x2: a marker expression became a target, and a node ID became nothing.

    Neither shape appears in this repository today — every pytest
    invocation here is `-q`, `--junitxml=…` and plain positionals — so
    both are latent. They are fixed anyway because the parser is wrong,
    and the consequences are opposite and both bad:

    ``pytest -m agents tests`` produced THREE groups. ``agents`` was read
    as a `file_or_dir` target, and the second ``-m`` was read as a nested
    ``python -m agents`` — so a job that never imports the agents package
    acquired all 31 of its files as dependencies. That is the
    FALSE-POSITIVE direction: the guard demands filter coverage for code
    the job does not run.

    ``pytest tests/test_x.py::test_a`` produced NONE. The ``::`` selector
    was kept in the stored target, no tracked path matched it, the group
    was dropped, and the file's imports and reads went untraversed. That
    is the FALSE-NEGATIVE direction: a narrowly filtered workflow could
    omit a real dependency with the guard green.

    `-m` is the interpreter's module flag only BEFORE the pytest token;
    after it, it is pytest's marker selector.
    """
    tracked = {
        "backend/agents/__init__.py",
        "backend/tests/test_x.py",
        "backend/tests/test_y.py",
    }
    exists = tracked.__contains__
    index = _suffix_index(tracked)
    job = {"defaults": {"run": {"working-directory": "backend"}}}

    def groups_for(command):
        doc = {"jobs": {"j": dict(job, steps=[{"run": command}])}}
        return _python_entry_groups(doc, exists, index, tracked, ".")

    # A marker expression selects tests; it is not a target, and the
    # agents package is not a dependency of this invocation.
    marker = groups_for("python -m pytest -m agents tests -q")
    assert len(marker) == 1, f"marker expression produced {len(marker)} groups: {marker}"
    seeds = marker[0][1]
    assert seeds == {"backend/tests/test_x.py", "backend/tests/test_y.py"}, seeds
    assert "backend/agents/__init__.py" not in seeds, (
        "the marker expression was read as a target, so the agents "
        "package became a dependency of a job that never imports it"
    )

    # A node ID selects one test in a file; the FILE is the target.
    node = groups_for("python -m pytest tests/test_x.py::test_a -q")
    assert len(node) == 1, f"node id produced {len(node)} groups: {node}"
    assert node[0][1] == {"backend/tests/test_x.py"}, node[0][1]

    # An option value taken as a separate token is consumed, not walked.
    for command in (
        "python -m pytest -k test_x tests -q",
        "python -m pytest --rootdir backend tests -q",
        "python -m pytest -p no:cacheprovider tests -q",
    ):
        only = groups_for(command)
        assert len(only) == 1 and only[0][1] == {
            "backend/tests/test_x.py",
            "backend/tests/test_y.py",
        }, f"{command!r} -> {only}"

    # `--name=value` is one token and needs no entry in the option set.
    joined = groups_for("python -m pytest --junitxml=out.xml tests -q")
    assert len(joined) == 1 and joined[0][1] == {
        "backend/tests/test_x.py",
        "backend/tests/test_y.py",
    }, joined

    # And a genuine `python -m module` before pytest still resolves.
    doc = {"jobs": {"j": dict(job, steps=[{"run": "python -m agents"}])}}
    plain = _python_entry_groups(doc, exists, index, tracked, ".")
    assert plain and any(
        "backend/agents/__init__.py" in seeds for _roots, seeds in plain
    ), plain


def test_each_shell_invocation_is_parsed_on_its_own():
    """P2 x2: pytest's arguments leaked across a whole `run:` block.

    Two defects from one cause — the previous version parsed the block as
    one token list.

    **A crash.** The pytest argument scan used a cursor named ``index``,
    which is also the name of the suffix-index PARAMETER. On
    ``python -m deep.mod && python -m pytest tests -q`` the scan replaced
    the dictionary with an integer, and resolving the earlier module then
    called ``.get()`` on it:

        AttributeError: 'int' object has no attribute 'get'

    That is a plain shadowing bug of mine, not a boundary of the model,
    and it is worth separating the two: the guard crashed rather than
    answering, which no amount of better modelling would have prevented.

    **A false negative.** Deciding `-m` by searching every preceding
    token for ``pytest`` made ``echo pytest && python -m app.config``
    suppress the real entry point — zero groups, measured — so the module
    and everything it imports went untraversed. A narrowly filtered
    workflow could then omit a real dependency with the guard green.

    Splitting the block into its commands fixes both, and removes the
    need to search earlier tokens at all: within one invocation there is
    at most one `-m`.
    """
    tracked = {
        "vendor/deep/mod.py",
        "vendor/deep/__init__.py",
        "tests/test_x.py",
    }
    exists = tracked.__contains__
    index = _suffix_index(tracked)

    def groups_for(command, working_directory=None):
        job = {"steps": [{"run": command}]}
        if working_directory:
            job["defaults"] = {"run": {"working-directory": working_directory}}
        return _python_entry_groups(
            {"jobs": {"j": job}}, exists, index, tracked, "."
        )

    # Both invocations are parsed, and neither corrupts the other. The
    # module resolves through the suffix index — which is the path that
    # touched the clobbered parameter, so a test using a module the roots
    # could resolve would pass even with the bug present.
    both = groups_for("python -m deep.mod && python -m pytest tests -q")
    seeds = {frozenset(s) for _roots, s in both}
    assert frozenset({"vendor/deep/mod.py"}) in seeds, both
    assert frozenset({"tests/test_x.py"}) in seeds, both

    # An unrelated earlier token named `pytest` does not disarm the
    # module entry point in a later command.
    for command in (
        "echo pytest && python -m deep.mod",
        "python -m pytest tests -q && python -m deep.mod",
        "python -m pytest tests -q ; python -m deep.mod",
    ):
        found = groups_for(command)
        assert any("vendor/deep/mod.py" in s for _roots, s in found), (
            f"{command!r} lost the module entry point: {found}"
        )

    # And the separators themselves are recognised, including a newline
    # — a `run: |` block is the ordinary way CI writes several commands.
    assert _shell_invocations("a && b || c ; d | e\nf") == [
        ["a"],
        ["b"],
        ["c"],
        ["d"],
        ["e"],
        ["f"],
    ]


def test_a_subshell_does_not_hide_the_command_inside_it():
    r"""`(python -m pytest x.py)` runs pytest, and the guard must see it.

    `_shell_tokens(line, True)` lexes `(` and `)` as their own tokens,
    and this splitter listed five operators as boundaries but not those
    two -- so the word list began with `(`, the interpreter was not the
    command, `_python_entry_groups` skipped the invocation and
    `_pytest_runs` did not count it. Both the dependency rule and the
    suite-verification guard could then pass while a real pytest run
    went unaccounted for (Codex round 22).

    The closing delimiter is the same defect, quieter: `(cd b; python
    -m pytest x.py)` split on the `;` and left `)` as a trailing
    POSITIONAL -- a phantom collection target.

    This is the sixth shape round 13 named and declined, and the reason
    for declining it does not apply here. That decision was about
    `_shell_words`, where `punctuation_chars=True` turned the regex
    fragments inside `[[ =~ ]]` and a `case` arm's `*` into globs and
    made 404 of 412 tracked files phantom dependencies. The asymmetry
    is deliberate and is asserted at the bottom of this case, because a
    future reader "tidying" the two readers into agreement would
    reintroduce a measured disaster.
    """
    # The command inside a subshell is the command.
    assert _shell_invocations("(python -m pytest tests/test_x.py)") == [
        ["python", "-m", "pytest", "tests/test_x.py"]
    ]
    assert _shell_invocations("( python -m pytest tests/test_x.py )") == [
        ["python", "-m", "pytest", "tests/test_x.py"]
    ]
    assert _shell_invocations("cd backend && (python -m pytest tests/test_x.py)") == [
        ["cd", "backend"],
        ["python", "-m", "pytest", "tests/test_x.py"],
    ]
    # ...and the closing delimiter is not an argument to it.
    assert _shell_invocations("(cd backend; python -m pytest tests/test_x.py)") == [
        ["cd", "backend"],
        ["python", "-m", "pytest", "tests/test_x.py"],
    ]

    # The consequence, not just the split: the census must count it.
    subshelled = {"jobs": {"j": {"steps": [{"run": "(python -m pytest tests/test_x.py)"}]}}}
    assert list(_pytest_runs(subshelled)), (
        "a pytest run inside a subshell is still a pytest run, and a "
        "workflow that performs one must be in the census"
    )

    # A REAL line from this repository, which the old reading buried
    # behind `head=$` and `(`: a command substitution executes what is
    # inside it, so the interpreter there is the command too.
    real = "head=$(python -m alembic heads | awk '{print $1}')"
    assert ["python", "-m", "alembic", "heads"] in _shell_invocations(real), (
        f"the command substitution in database-parity.yml is still hidden: "
        f"{_shell_invocations(real)}"
    )

    # The asymmetry, asserted rather than trusted to a comment. The
    # pathname reader must NOT treat a parenthesis as a boundary: round
    # 13 measured that at 404 phantom dependencies out of 412 tracked
    # files and declined it, and nothing since has changed that.
    bracketed = 'case "$x" in *) cat docs/a.md ;; esac'
    assert "(" not in _shell_words(bracketed) and ")" not in _shell_words(bracketed), (
        f"the pathname reader has started lexing parentheses as "
        f"punctuation: {_shell_words(bracketed)}"
    )


def test_shell_splitting_respects_quoting_and_continuations():
    r"""P2: my claim that splitting could only narrow a range was FALSE.

    I wrote, in the previous round's decision and on the pull request,
    that a regex split "can only narrow an invocation's token range,
    never widen one — the safe direction here". Codex round 13 showed it
    wrong in both directions, and I asked for exactly that check because
    I was not sure of it.

    Widening: ``python -m pytest -k "fast|slow" tests/test_x.py`` splits
    inside the quoted marker, leaving a pytest invocation with no
    positional target, which falls back to collecting ``.`` — the whole
    tree. A false positive.

    Narrowing wrongly: a ``\``-continuation splits the continued line
    into a separate command, so a test file becomes a conftest-free
    plain-script group and dependencies reached through its conftest are
    missed. A false negative.

    **And the continuation shape is REAL in this repository**, not a
    constructed input: ``container-battery`` runs
    ``python -m adapter_kit.run_contract \`` across four lines, which the
    regex cut into four fragments. It happened to be harmless, because
    the ``-m`` sat in the first fragment — but the model was wrong about
    code that is actually here, which is the closest any round has come
    to the reversal condition in §12 154(e).

    The fix delegates rather than extends: continuations are joined, each
    physical line is a command list, and ``shlex`` — which knows about
    quoting — finds the operators. A line shlex cannot lex is returned
    whole rather than guessed at.
    """
    # The real block from container-battery, verbatim in shape.
    real = (
        "python -m adapter_kit.run_contract \\\n"
        "  --url http://localhost:8090 \\\n"
        "  --agent-dir agents/_examples/echo_container \\\n"
        "  --relay-port 18080 --json"
    )
    assert _shell_invocations(real) == [
        [
            "python",
            "-m",
            "adapter_kit.run_contract",
            "--url",
            "http://localhost:8090",
            "--agent-dir",
            "agents/_examples/echo_container",
            "--relay-port",
            "18080",
            "--json",
        ]
    ], _shell_invocations(real)

    # A quoted separator is not a separator.
    quoted = 'python -m pytest -k "fast|slow" tests/test_x.py'
    # TOKENS, not a string: the quoted marker has to stay ONE token all
    # the way to the caller. Round 14 caught this returning a joined
    # string, which the caller re-split on whitespace — undoing exactly
    # the quoting shlex had been brought in to respect, so `-k` swallowed
    # `fast|slow`'s first word and the rest became collection targets.
    assert _shell_invocations(quoted) == [
        ["python", "-m", "pytest", "-k", "fast|slow", "tests/test_x.py"]
    ], _shell_invocations(quoted)

    # Real separators still separate — including a bare newline, which is
    # how a `run: |` block writes two commands.
    for text in (
        "python -m a && python -m b",
        "python -m a\npython -m b",
        "python -m a ; python -m b",
    ):
        assert _shell_invocations(text) == [
            ["python", "-m", "a"],
            ["python", "-m", "b"],
        ], (text, _shell_invocations(text))

    # A line shlex cannot lex is returned whole. Guessing at it is how
    # the previous version produced a pytest invocation with no target.
    unbalanced = 'python -m pytest -k "oops tests'
    assert _shell_invocations(unbalanced) == [
        unbalanced.split()
    ], _shell_invocations(unbalanced)

    # The consequence, not just the split: the quoted-marker command must
    # NOT collect the whole tree. That is the assertion the previous
    # version fails.
    tracked = {"tests/test_x.py", "tests/test_y.py", "elsewhere/mod.py"}
    groups = _python_entry_groups(
        {"jobs": {"j": {"steps": [{"run": quoted}]}}},
        tracked.__contains__,
        _suffix_index(tracked),
        tracked,
        ".",
    )
    reached = set().union(*(seeds for _roots, seeds in groups)) if groups else set()
    assert reached == {"tests/test_x.py"}, (
        f"the quoted marker expression widened collection to {reached}; "
        f"a split inside the quotes leaves pytest with no target and it "
        f"falls back to collecting everything"
    )

# ---------------------------------------------------------------------------
# A file that does not exist yet: root-level pytest configuration
# ---------------------------------------------------------------------------


def _pytest_config_filenames():
    """The filenames pytest itself treats as configuration.

    Read out of ``locate_config``'s compiled code rather than retyped,
    because the list is version-dependent and retyping it is how you get
    it wrong: pytest 9 looks for ``pytest.toml`` and ``.pytest.toml``
    too, which I would have dropped as a slip for ``pytest.ini`` if I
    had written the list from memory instead of asking the installed
    pytest. Same rule as the ``paths-ignore`` ordering and the pytest
    option table — where another tool has a real definition, delegate to
    it.

    ``conftest.py`` is appended because it is the same hazard by a
    different mechanism: pytest loads conftests up to ``rootdir``, and
    today ``rootdir`` is ``backend/``, so a conftest at the repo root is
    inert — until one of the seven files above appears, which moves
    ``rootdir`` to the repo root and switches it on. Measured both ways
    against a conftest known to load.
    """
    from _pytest.config import findpaths

    tuples = [
        const
        for const in findpaths.locate_config.__code__.co_consts
        if isinstance(const, tuple) and "pytest.ini" in const
    ]
    # No silent empty. If a pytest release moves this list somewhere the
    # oracle cannot see, the guard must FAIL rather than quietly certify
    # an empty set of filenames — a check that reports success by not
    # looking is worse than no check.
    assert len(tuples) == 1, (
        f"cannot read pytest's own config filenames out of "
        f"{findpaths.__file__} (found {len(tuples)} candidate tuples). "
        f"The guard below would check nothing. Find where pytest keeps "
        f"the list in this version and read it from there."
    )
    return tuple(tuples[0]) + ("conftest.py",)


def _runs_pytest(doc, repo_root=None, exists=None, tracked=None):
    """Does this workflow invoke pytest — in a step, or in a file a step RUNS?

    Reading only ``run:`` text was this census's own blind spot, and the
    workflow it hid is the one that needed it. ``element-coverage`` runs
    ``python scripts/element_expression_coverage.py``; the word
    ``pytest`` appears nowhere in its YAML, and the script's
    ``SUITE = [sys.executable, "-m", "pytest", …]`` is what runs the
    suite seven times. So the guard that exists for root-level
    configuration skipped the one workflow whose filter was missing
    exactly those filenames — reported by Codex round 2 against the
    workflow, while the cause was here.

    A reached file counts when the token appears in it at all, which is
    looser than the shell tokenising above and deliberately so. The
    direction of that error is safe: it can only ADD workflows to the
    census, so it can only demand more paths, never fewer — and a
    workflow wrongly included fails loudly with its name rather than
    passing quietly.
    """
    for _working_directory, text, _pythonpath in _run_steps(doc):
        for words in _shell_invocations(text):
            if "pytest" in words:
                return True
    if repo_root is None:
        return False
    import re
    from pathlib import Path

    for relative in _files_a_workflow_reaches(doc, exists, repo_root, tracked):
        # Only files that could RUN it. Prose cannot invoke pytest, and
        # scanning everything reached made `librerun-smoke` a pytest
        # workflow because `demo.sh` names `docs/Install.md` and that
        # page says "turn it off before running the test suite —
        # `pytest` reads the same…". Harmless there, since that workflow
        # has no `paths:` filter and so can have no blind spot, but a
        # false member is still a false member and this one was visible
        # rather than hypothetical.
        #
        # The first version of this line was a suffix list — `""`,
        # `.py`, `.sh` — which dropped a `scripts/test.bash` that runs
        # pytest and so dropped its workflow from the census (Codex
        # round 4). A list of extensions is a guess about what will run
        # a file; the file's own shebang is not.
        try:
            text = (Path(repo_root) / relative).read_text(errors="ignore")
        except OSError:
            continue
        if not (relative.endswith(".py") or _is_shell_script(relative, text)):
            interpreter = _shebang_interpreter(text) or ""
            if not interpreter.startswith("python"):
                continue
        # Read BEFORE any filename test, so a wrapper named `.txt` with
        # `#!/bin/sh` is judged by what it says it is.
        if re.search(r"\bpytest\b", text):
            return True
    return False


def _root_config_blind_spots(doc, filenames):
    """Which root-level pytest config files each filtered half cannot see.

    ``{half: [filename, ...]}``, empty when every half covers all of
    them. A half with no ``paths`` filter runs unconditionally and so
    has no blind spot.
    """
    blind = {}
    for half, config in _workflow_triggers(doc).items():
        if not isinstance(config, dict):
            continue
        patterns = config.get("paths")
        if not patterns:
            continue
        missed = [n for n in filenames if not _filter_selects(patterns, n)]
        if missed:
            blind[half] = missed
    return blind


def test_every_pytest_workflow_watches_root_level_pytest_configuration():
    """The dependency rule cannot cover a file that does not exist yet.

    Every other path in these filters is there because some file in the
    tree is read or imported, and the derived rule above demands them by
    finding that file. This one is the opposite shape: the hazard is a
    file's ADDITION. pytest resolves its configfile by walking UP from
    the invocation directory, so a ``pytest.ini`` committed at the repo
    root takes effect for ``cd backend && pytest`` — and matches no
    pattern that describes the tree as it stands, because there is
    nothing there to describe. A derivation over existing files can
    never reach it; only a listed path can. That is the limit, and it
    is why these eight are written out.

    What it costs to leave them out, measured rather than argued: a root
    ``pytest.ini`` carrying ``python_files = test_nothing_ever_*.py``
    takes the backend suite from 1051 collected to 0. No workflow here
    selects the file, so nothing runs on the pull request and nothing
    runs on the push to main. ``assert_suite_ran.py`` holds the floor at
    1051 and never gets to look. The suite does not go red; it goes
    absent, which is the failure mode this whole batch exists to close.

    Codex round 14 named it against unit-suites.yml. Censusing the
    question rather than the instance: six workflows run pytest, and all
    six were blind.

    Seven now, and the seventh is why this docstring has a second
    paragraph. ``element-coverage`` runs pytest only from inside the
    script its step names, so the census — which read ``run:`` text —
    did not see it as a pytest workflow at all, and its filter went
    without these eight names until Codex round 2 read the workflow by
    hand. A census that cannot see a member is the same failure as a
    filter that cannot see a file, one level up. ``_runs_pytest``
    follows the files a workflow runs now.

    Round 3 found that following them ONE hop was not enough either: a
    step running a shell wrapper that runs a Python file reached the
    wrapper and stopped, so anything only the inner file names — a
    dependency, or the pytest itself — was still invisible. The reach
    expands shell scripts to a fixpoint now, which gives the filter
    rule above the same depth.
    """
    workflows = _workflow_dir()
    repo_root = workflows.parents[1]
    tracked = _tracked_paths(repo_root, directories=False)
    exists = (
        tracked.__contains__
        if tracked is not None
        else (lambda p: (repo_root / p).is_file())
    )
    filenames = _pytest_config_filenames()
    checked, blind = [], {}
    for path in _workflow_files(workflows):
        doc = _load_workflow(path)
        if not _runs_pytest(doc, repo_root, exists, tracked):
            continue
        checked.append(path.name)
        gaps = _root_config_blind_spots(doc, filenames)
        if gaps:
            blind[path.name] = gaps

    # The census has to be able to return a non-zero answer, or a green
    # result means nothing.
    assert checked, (
        f"no workflow under {workflows} was found to run pytest, so this "
        f"guard checked nothing"
    )
    assert not blind, (
        f"these workflows run pytest but their path filters cannot see a "
        f"root-level pytest configuration file being ADDED: {blind}. Such "
        f"a commit changes what the suite collects and skips the job that "
        f"would notice. Add the missing names to the filter half; they "
        f"are pytest's own config filenames plus conftest.py."
    )



def _alembic_inputs(repo_root, tracked):
    r"""`alembic.ini` and the revision tree its `script_location` names.

    Read from the file rather than listed, so moving `script_location`
    moves what this demands.
    """
    import configparser

    ini = "backend/alembic.ini"
    path = repo_root / ini
    # A tree that HAS no alembic has nothing to demand -- and the
    # question is asked of the DISK as well as the index, because those
    # disagree in two reachable states: git that cannot answer at all,
    # which is what `tracked is None` means, and a sparse or partial
    # checkout whose index lists a file the working tree does not hold.
    # Reading first turned both into a `FileNotFoundError`, so the
    # guard DIED instead of reporting, and the traceback named
    # `alembic.ini` rather than the condition -- round 18's lesson
    # about a crash misdirecting its own reader, found again inside
    # round 19's own code before review saw it.
    if not path.is_file() or (tracked is not None and ini not in tracked):
        return []
    # RAW: alembic defines `%(here)s` itself, and an interpolating
    # parser raises on it before any value can be read.
    parser = configparser.RawConfigParser()
    parser.read_string(path.read_text(encoding="utf-8"))
    location = parser.get("alembic", "script_location", fallback="alembic")
    location = location.replace("%(here)s/", "").strip("/")
    prefix = f"backend/{location}/"
    inputs = [ini]
    inputs += sorted(p for p in (tracked or ()) if p.startswith(prefix))
    return inputs


def _runs_alembic(doc, repo_root, exists, tracked):
    """Does any step this workflow reaches invoke alembic?"""
    for _working_directory, text, _pythonpath in _run_steps(doc):
        if not isinstance(text, str):
            continue
        for words in _shell_invocations(text):
            command = _the_command(words)
            if not command:
                continue
            if command[0].rsplit("/", 1)[-1] == "alembic":
                return True
            # Round 17's rule, which this function did not ask when it
            # was written in round 19: `echo -m alembic` runs nothing.
            if not _is_the_python_interpreter(command):
                continue
            kind, entry = _python_entry(command, exists, _working_directory)
            if kind == "module" and entry == "alembic":
                return True
    return False


def test_a_tree_without_alembic_is_reported_not_crashed(tmp_path):
    r"""The census has to REPORT that there is nothing to demand.

    `_alembic_inputs` read `backend/alembic.ini` before asking whether
    it was there, so two reachable states raised `FileNotFoundError`
    and the guard died instead of giving a verdict: a tree where git
    cannot answer at all (`tracked` is `None`, which `_tracked_paths`
    returns when git fails), and a sparse or partial checkout whose
    INDEX lists a file the working tree does not hold. Measured, both
    of them, before the fix.

    That is round 18's finding in round 19's own code -- a crash is not
    a verdict, and the traceback misdirects its reader by naming
    `alembic.ini` when the condition is "this tree has no alembic" or
    "this checkout is partial". The disk is asked as well as the index
    now, and the caller's `pytest.skip` does the reporting.

    A file that IS present and cannot be read still raises, which is
    the distinction worth keeping: absence is a legitimate tree, an
    unreadable file is a real error and must not be skipped past.
    """
    empty = tmp_path / "no-alembic"
    (empty / "backend").mkdir(parents=True)

    assert _alembic_inputs(empty, None) == []
    assert _alembic_inputs(empty, set()) == []
    assert _alembic_inputs(empty, {"backend/alembic.ini"}) == []

    # ...and the control: a tree that DOES have one is not reported
    # empty, or a rule that simply returned [] would pass the three
    # above. The ini names its own revision directory, so what is
    # demanded follows `script_location` rather than a fixed path.
    real = tmp_path / "has-alembic"
    (real / "backend" / "migrations" / "versions").mkdir(parents=True)
    (real / "backend" / "alembic.ini").write_text(
        "[alembic]\nscript_location = %(here)s/migrations\n", encoding="utf-8"
    )
    (real / "backend" / "migrations" / "env.py").write_text("", encoding="utf-8")
    tracked = {"backend/alembic.ini", "backend/migrations/env.py"}
    assert _alembic_inputs(real, tracked) == [
        "backend/alembic.ini",
        "backend/migrations/env.py",
    ]

    # And the DEGRADED case, pinned so it stays degraded rather than
    # vacuous: git that cannot answer leaves no revision list, but the
    # ini is on disk and is still demanded. Answering `[]` here would
    # skip the guard on any tree git cannot describe, which is the
    # failure mode this whole case is about, one step quieter.
    assert _alembic_inputs(real, None) == ["backend/alembic.ini"]


def test_every_alembic_workflow_watches_its_configuration_and_migrations():
    r"""A migration decides what schema the suite runs against.

    `element-coverage` runs `python -m alembic upgrade head`, which
    reads `backend/alembic.ini` and, through its `script_location`,
    every revision under `backend/alembic`. Its filter listed
    `backend/alembic.py` and `backend/alembic/__init__.py` — the files
    that could PRETEND to be alembic — and nothing alembic actually
    reads, so a pull request changing only a migration skipped the job
    entirely (Codex round 19). Guarding against the impostor and not
    against the input is the shape of that mistake.

    The dependency rule cannot derive these: `-m alembic` names a
    module that is not in this tree, and a configuration file read at
    runtime is not a path the command mentions. So the question is
    asked directly, over a census of the workflows that run alembic at
    all — which is what keeps this from being one more hand-kept list.

    Measured when it was written: three workflows run alembic, and the
    other two select `backend/**`, which already covers both. Only the
    narrow filter was blind, so this asserts a property rather than
    recording that one file was edited.
    """
    workflows = _workflow_dir()
    repo_root = workflows.parents[1]
    tracked = _tracked_paths(repo_root, directories=False)
    exists = (
        tracked.__contains__
        if tracked is not None
        else (lambda p: (repo_root / p).is_file())
    )
    # Asked in THIS order, and the order is the fix. "There are no
    # workflows at all" is a failure; "this tree has no alembic" is a
    # legitimate skip. Asking the second first let an EMPTY census
    # answer it, so driving this guard against an empty directory made
    # it skip instead of raise — and `pytest.skip` raises `Skipped`,
    # which derives from `BaseException`, so it escaped the
    # `except Exception` in
    # `test_no_workflow_guard_passes_on_an_empty_census` and turned
    # that whole meta-guard into a skip. One guard skipping quietly
    # switched off the check that every guard is non-vacuous.
    # `_workflow_files` carries the non-empty invariant, so calling it
    # first restores the raise (found in round 19, in round 19's own
    # code).
    files = _workflow_files(workflows)

    inputs = _alembic_inputs(repo_root, tracked)
    if not inputs:
        pytest.skip("this tree has no backend/alembic.ini to demand")

    checked, blind = [], {}
    for path in files:
        doc = _load_workflow(path)
        if not _runs_alembic(doc, repo_root, exists, tracked):
            continue
        checked.append(path.name)
        for half, config in _workflow_triggers(doc).items():
            if not isinstance(config, dict):
                continue
            patterns = config.get("paths")
            if not patterns:
                continue
            missed = [n for n in inputs if not _filter_selects(patterns, n)]
            if missed:
                blind.setdefault(path.name, {})[half] = missed[:4]

    # The census has to be able to return a non-zero answer, or green
    # means nothing.
    assert checked, (
        f"no workflow under {workflows} was found to run alembic, so this "
        f"guard checked nothing"
    )
    assert not blind, (
        f"these workflows run alembic but their path filters cannot see "
        f"its configuration or its revisions: {blind}. A commit changing "
        f"only a migration changes what schema the job runs against and "
        f"skips the job that would notice."
    )

def test_a_workflow_that_runs_pytest_only_through_a_script_is_censused(tmp_path):
    """The census has to see a member before it can judge it.

    `element-coverage` names `python scripts/element_expression_coverage.py`
    and nothing else; the word `pytest` is not in its YAML. Read by
    `run:` text alone it was not a pytest workflow, so the guard above
    skipped it — and it was the one workflow whose filter was missing
    the eight filenames, which Codex round 2 found by reading the file
    rather than by any check.

    Driven through `_runs_pytest` itself, over a fixture whose shape is
    exactly that: a step that runs a script, and the script that runs
    pytest. The pair is the point — the same workflow without the
    script's pytest must come back False, or this would pass on a
    predicate that simply says yes.
    """
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "runner.py"
    doc = {"jobs": {"j": {"steps": [{"run": "python scripts/runner.py"}]}}}
    tracked = {"scripts/runner.py"}
    exists = tracked.__contains__

    script.write_text("import subprocess\nsubprocess.run(['pytest', '-q'])\n")
    assert _runs_pytest(doc, tmp_path, exists, tracked) is True

    script.write_text("print('nothing to see')\n")
    assert _runs_pytest(doc, tmp_path, exists, tracked) is False

    # And without a root to read from, it can only answer on the YAML —
    # which is what every caller before this one relied on.
    assert _runs_pytest(doc) is False


def test_a_workflow_reaching_pytest_two_steps_in_is_censused(tmp_path):
    """One hop was not enough either.

    A step runs `scripts/outer.sh`, that wrapper runs `python
    scripts/inner.py`, and only the inner file invokes pytest. The reach
    stopped at the shell script — it was a file the step named, and
    nothing opened it — so `inner.py` was reached by nothing, its
    dependencies were invisible to the filter rule, and the census
    omitted the workflow (Codex round 3).

    The same fixture with the pytest taken out of the inner file must
    come back False, or this passes on a predicate that says yes to
    everything two hops away.
    """
    (tmp_path / "scripts").mkdir()
    outer = tmp_path / "scripts" / "outer.sh"
    inner = tmp_path / "scripts" / "inner.py"
    outer.write_text('#!/bin/sh\nexec python scripts/inner.py "$@"\n')
    doc = {"jobs": {"j": {"steps": [{"run": "bash scripts/outer.sh"}]}}}
    tracked = {"scripts/outer.sh", "scripts/inner.py"}
    exists = tracked.__contains__

    inner.write_text("import subprocess\n\nsubprocess.run(['pytest', '-q'])\n")
    reached = _files_a_workflow_reaches(doc, exists, tmp_path, tracked)
    assert reached == {"scripts/outer.sh", "scripts/inner.py"}
    assert _runs_pytest(doc, tmp_path, exists, tracked) is True

    inner.write_text("print('nothing to see')\n")
    assert _runs_pytest(doc, tmp_path, exists, tracked) is False


def _wrapper_tree(tmp_path, files):
    """A tiny repo: write the files, return an `exists` over exactly them."""
    for relative, body in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return set(files).__contains__, set(files)


def test_a_file_under_a_directory_a_script_cds_into_is_still_found(tmp_path):
    """`cd backend` then `python inner.py` means `backend/inner.py`.

    Round 3 resolved a reached script's whole text against the workflow
    STEP's directory, so this looked for a root-level `inner.py` that
    does not exist and the real file — plus the pytest in it — was
    invisible (Codex round 4). Round 4 answered it by following `cd`,
    which is how rounds 4 and 5 then produced ten findings about quotes,
    escapes, `cd -` and conditionals. The name is resolved by its TAIL
    against the tracked tree now: no directory to get wrong.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/outer.sh": "#!/bin/sh\ncd backend\npython inner.py\n",
            "backend/inner.py": "import subprocess\n\nsubprocess.run(['pytest'])\n",
        },
    )
    doc = {"jobs": {"j": {"steps": [{"run": "bash scripts/outer.sh"}]}}}

    reached = _files_a_workflow_reaches(doc, exists, tmp_path, tracked)

    assert reached == {"scripts/outer.sh", "backend/inner.py"}
    assert _runs_pytest(doc, tmp_path, exists, tracked) is True


def test_a_cd_no_reader_could_resolve_costs_nothing(tmp_path):
    """`cd "$TARGET"` is not knowable without running the script.

    Under round 4 this was a careful special case: leave the walk where
    it was, because missing a file beats inventing one. Under tail
    resolution there is nothing to special-case — the directory was
    never consulted. The case stays because the SHAPE still has to
    work, and because its previous answer depended on a rule that is
    gone.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/outer.sh": '#!/bin/sh\ncd "$TARGET"\npython scripts/inner.py\n',
            "scripts/inner.py": "print('hi')\n",
        },
    )
    doc = {"jobs": {"j": {"steps": [{"run": "bash scripts/outer.sh"}]}}}

    reached = _files_a_workflow_reaches(doc, exists, tmp_path, tracked)

    assert reached == {"scripts/outer.sh", "scripts/inner.py"}


def test_a_comment_in_a_reached_script_is_not_a_dependency(tmp_path):
    """`# See docs/guide.md` made that page something the workflow runs.

    Every path in a reached script's comments became a dependency the
    filter rule then demanded of the workflow — phantom triggers, and
    each one widening a filter for no reason (Codex round 4).
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/outer.sh": (
                "#!/bin/sh\n"
                "# See docs/guide.md for the recipe\n"
                "cat docs/real.md  # and docs/aside.md is only a note\n"
            ),
            "docs/guide.md": "prose\n",
            "docs/aside.md": "prose\n",
            "docs/real.md": "prose\n",
        },
    )
    doc = {"jobs": {"j": {"steps": [{"run": "bash scripts/outer.sh"}]}}}

    reached = _files_a_workflow_reaches(doc, exists, tmp_path, tracked)

    # The one it reads, and neither of the two it mentions.
    assert reached == {"scripts/outer.sh", "docs/real.md"}


def test_a_hash_that_is_not_a_comment_survives(tmp_path):
    """`$#` and `${#x}` are parameters, not the start of a comment."""
    assert _shell_words('echo "$#" ${#x} docs/real.md') == [
        "echo",
        "$#",
        "${#x}",
        "docs/real.md",
    ]
    assert _shell_words("echo hi  # docs/aside.md") == ["echo", "hi"]
    assert _shell_words("echo '# not a comment'") == ["echo", "# not a comment"]


def test_a_wrapper_is_expanded_by_its_shebang_not_its_extension(tmp_path):
    """`tools/test` and `scripts/test.bash` are scripts too.

    Round 3 expanded reached files whose name ended `.sh`, and scanned
    files whose name ended `.py`, `.sh` or nothing — two different
    guesses at "what will run this", and neither of them right. An
    extensionless wrapper was executed and not expanded; a `.bash` file
    invoking pytest was dropped from the census by its extension (Codex
    round 4).
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "tools/test": "#!/bin/sh\npython scripts/inner.py\n",
            "scripts/inner.py": "import subprocess\n\nsubprocess.run(['pytest'])\n",
        },
    )
    doc = {"jobs": {"j": {"steps": [{"run": "bash tools/test"}]}}}

    assert _files_a_workflow_reaches(doc, exists, tmp_path, tracked) == {
        "tools/test",
        "scripts/inner.py",
    }
    assert _runs_pytest(doc, tmp_path, exists, tracked) is True

    exists, tracked = _wrapper_tree(
        tmp_path, {"scripts/test.bash": "#!/bin/bash\npytest -q\n"}
    )
    doc = {"jobs": {"j": {"steps": [{"run": "bash scripts/test.bash"}]}}}
    assert _runs_pytest(doc, tmp_path, exists, tracked) is True


def test_an_extensionless_file_that_is_not_a_script_is_not_expanded(tmp_path):
    """The shebang is the discriminator, so its absence has to matter.

    Without this the case above would pass on a rule that expanded every
    extensionless file it reached — `LICENSE`, `Makefile`, a fixture —
    and read their contents as shell commands.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "tools/NOTES": "python scripts/inner.py is how we used to do it\n",
            "scripts/inner.py": "import subprocess\n\nsubprocess.run(['pytest'])\n",
        },
    )
    doc = {"jobs": {"j": {"steps": [{"run": "cat tools/NOTES"}]}}}

    assert _files_a_workflow_reaches(doc, exists, tmp_path, tracked) == {"tools/NOTES"}
    assert _runs_pytest(doc, tmp_path, exists, tracked) is False


def test_a_quote_closed_by_an_escaped_backslash_ends_the_string(tmp_path):
    """`"a\\\\"` closes its quote: the second backslash escapes the first.

    Checking only the immediately previous character left the scanner
    inside the quote, so a trailing comment was kept as a command and
    every path in it became a dependency (Codex round 5).
    """
    assert _shell_words('echo "a\\\\"  # docs/fake.md') == ["echo", "a\\"]
    # And the odd case still escapes, so the quote never closes. `shlex`
    # refuses such a line; the fallback splits it on whitespace rather
    # than dropping it, because dropping a line is how a dependency
    # goes missing.
    assert _shell_words('echo "a\\" # x') == ['echo', '"a\\"', '#', 'x']


def test_an_env_shebang_with_options_names_its_interpreter(tmp_path):
    """`env`'s grammar decides which word is the command.

    Three rounds refused to read it. Round 5 took the word after `env`
    and got `-S`; round 6 skipped the words starting with `-` and got
    `FOO`, `tmp` and `FOO=1`, then took the first KNOWN interpreter and
    got `python3` for a line that runs bash, because `-u` consumes the
    word after it as a variable name (Codex round 7). Every form below
    is from `env --help`; only `-u` and `-C` take a separate operand,
    and `-S` splits what follows for `env` to read as its own.
    """
    assert _shebang_interpreter("#!/bin/sh\n") == "sh"
    assert _shebang_interpreter("#!/usr/bin/env python3\n") == "python3"
    assert _shebang_interpreter("#!/usr/bin/env -S bash -e\n") == "bash"
    # The operand of an option is not the command, even when it is
    # spelled like one.
    assert _shebang_interpreter("#!/usr/bin/env -S -u python3 bash\n") == "bash"
    assert _shebang_interpreter("#!/usr/bin/env -u python3 bash\n") == "bash"
    assert _shebang_interpreter("#!/usr/bin/env -C /tmp bash\n") == "bash"
    # Attached operands, clusters, assignments, `--`, and the long forms.
    assert _shebang_interpreter("#!/usr/bin/env -upython3 bash\n") == "bash"
    assert _shebang_interpreter("#!/usr/bin/env -iu python3 bash\n") == "bash"
    assert _shebang_interpreter("#!/usr/bin/env FOO=1 bash\n") == "bash"
    assert _shebang_interpreter("#!/usr/bin/env --unset=python3 bash\n") == "bash"
    assert _shebang_interpreter("#!/usr/bin/env --split-string=bash -e\n") == "bash"
    assert _shebang_interpreter("#!/usr/bin/env -- bash\n") == "bash"
    # A long option takes its operand as the NEXT word too — `env
    # --help` says mandatory arguments apply to both spellings, and
    # skipping only the option token answered `python3` (round 8).
    assert _shebang_interpreter("#!/usr/bin/env --unset python3 bash\n") == "bash"
    assert _shebang_interpreter("#!/usr/bin/env --chdir /tmp bash\n") == "bash"
    # `-S` takes a string and SPLITS it, and a quoted command keeps its
    # quotes on a shebang line, which is raw text (round 8).
    assert _shebang_interpreter("#!/usr/bin/env -S 'bash' -e\n") == "bash"
    assert _after_env(["-S", "bash -e", "x.py"]) == ["bash", "-e", "x.py"]
    # `--split-string` SPLITS its separate operand rather than merely
    # consuming it, and `-S` does quote-aware splitting of the rest of
    # the line — so the line is lexed, not split on whitespace, or the
    # quoted string is torn apart before `--split-string` sees it
    # (Codex round 9).
    assert (
        _shebang_interpreter("#!/usr/bin/env -S --split-string '-u FOO bash -e'\n")
        == "bash"
    )
    # `-S` has its OWN escapes: `\\_` separates arguments, and lexing it
    # as shell consumed the backslash and answered `_bash` (Codex round
    # 10, confirmed against `env --debug`).
    assert _shebang_interpreter("#!/usr/bin/env -S \\_bash -e\n") == "bash"
    assert _split_env_string("\\_bash -e") == ["bash", "-e"]
    assert _split_env_string("'bash -e'") == ["bash -e"]
    # `-S` IS `--split-string` — `env --help` spells the option
    # `-S, --split-string=S` — so it splits its operand whether or not
    # more than one word comes out. The two spellings had two branches
    # here, and the `-S` one only recursed when the split yielded
    # several words, so `env -S '\\_bash' x.py` kept the escape and
    # answered `\\_bash` (Codex round 11). One word out is still a
    # split; both spellings take the same branch now.
    assert _after_env(["-S", "\\_bash", "x.py"]) == ["bash", "x.py"]
    assert _after_env(["--split-string", "\\_bash", "x.py"]) == ["bash", "x.py"]
    # The shebang line is split by `env`'s rules and not on whitespace,
    # and these two are what MEASURES that. Every `-S` case above
    # stopped measuring it the moment `-S` began re-splitting each word
    # with those same rules: whitespace-splitting `-S 'bash' -e` and
    # `-S \\_bash -e` is then repaired on the way through, and an
    # injection replacing the split went uncaught. A quoted operand
    # belonging to an option that does NOT split is the shape that
    # cannot be repaired afterwards — `-u 'FOO BAR' bash` answers `BAR`
    # and `--unset 'A B' bash` answers `B` under whitespace splitting
    # (Codex round 11, found by the injection ledger, not by the suite).
    assert _shebang_interpreter("#!/usr/bin/env -u 'FOO BAR' bash\n") == "bash"
    assert _shebang_interpreter("#!/usr/bin/env --unset 'A B' bash\n") == "bash"
    # `\c` ENDS the string rather than standing for a letter, which
    # `env --debug -S 'bash\c python3'` prints as "into: ‘bash’".
    # Mapping the unknown escape to its own letter answered `bashc`
    # (Codex round 12).
    assert _split_env_string("bash\\c python3") == ["bash"]
    assert _shebang_interpreter("#!/usr/bin/env -S bash\\c ignored\n") == "bash"
    assert _is_shell_script("w", "#!/usr/bin/env -S bash\\c ignored\n") is True
    # and a `\c` that is quoted is an ordinary two characters
    assert _split_env_string("'bash\\c python3'") == ["bashc python3"]

    # And the predicate no longer depends on any of this being exact.
    # Six rounds found a way for the `env` parse to be wrong, every one
    # of them losing a shell file; a shebang naming a shell ANYWHERE is
    # read as shell, which over-approximates instead.
    assert _is_shell_script("x.txt", "#!/usr/bin/env -S -u python3 bash\n") is True
    assert _is_shell_script("x.txt", "#!/usr/bin/env python3\n") is False
    assert _is_shell_script("x.txt", "#!/usr/bin/env -S python3 -X dev\n") is False
    # The assertion that actually exercises the fallback, and the one
    # the first version of this case was missing: an input where the
    # PARSE says python and a shell is named anyway. `-u bash python3`
    # unsets a variable called `bash` and runs python — this reads it as
    # shell regardless, which is the over-approximation being bought.
    assert _shebang_interpreter("#!/usr/bin/env -u bash python3\n") == "python3"
    assert _is_shell_script("x.txt", "#!/usr/bin/env -u bash python3\n") is True
    # And the negations, since a function only ever asked to say yes is
    # not tested: no shebang, and an `env` line naming no command.
    assert _shebang_interpreter("no shebang here\n") is None
    assert _shebang_interpreter("#!/usr/bin/env -S -u FOO\n") is None


def test_a_shebangless_wrapper_a_shell_names_is_read_as_shell(tmp_path):
    """An explicitly invoked shell script needs no shebang.

    Round 8 deleted the channel that recorded which interpreter ran a
    file, because answering "which operand" needed a new option grammar
    every round, and accepted that this case would be missed (Codex
    round 9 named it). The evidence is back in the one form that needs
    no grammar: the command names a shell somewhere, and this file is a
    path it mentions — `exists` rejects `-O`'s `extglob` and `-u`'s
    variable name without anything parsing them.

    The negation is the half that matters, and is asserted against a
    TRACKED file: a shebangless `.py` that no shell named is walked as
    Python, so a path inside it is not a shell dependency. Reading
    every reached `.py` as shell instead was measured at 154 phantom
    dependencies and one FALSE finding.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/wrapper.py": "python scripts/leaf.py\n",
            "scripts/leaf.py": "import subprocess\n\nsubprocess.run(['pytest'])\n",
        },
    )
    for command in ("bash scripts/wrapper.py",
                    "bash -O extglob scripts/wrapper.py",
                    "env bash scripts/wrapper.py"):
        doc = {"jobs": {"j": {"steps": [{"run": command}]}}}
        assert _files_a_workflow_reaches(doc, exists, tmp_path, tracked) == {
            "scripts/wrapper.py",
            "scripts/leaf.py",
        }, command
        assert _runs_pytest(doc, tmp_path, exists, tracked) is True, command

    # No shell names it: walked as Python, so `docs/aside.md` is not a
    # dependency even though the text mentions it.
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/plain.py": "cat docs/aside.md\n",
            "docs/aside.md": "prose\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "python scripts/plain.py"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/plain.py"}


def test_a_quoted_glob_is_a_literal_name(tmp_path):
    """`cat 'docs/[ab].md'` opens that file; it does not expand.

    The walker recorded `docs/a.md` and `docs/b.md` as well, so a
    workflow correctly watching only the literal file could be reported
    as running files its filter does not select — the guard accusing a
    healthy workflow (Codex round 10). `_shell_words` removes quotes,
    which is right for the value and destroys the only evidence of
    whether a metacharacter is active, so the word is kept in its raw
    form alongside.

    Paired with the unquoted command in the same fixture, since a rule
    that never expanded anything would pass the first assertion.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/quoted.sh": "#!/bin/sh\ncat 'docs/[ab].md'\n",
            "scripts/bare.sh": "#!/bin/sh\ncat docs/[ab].md\n",
            "scripts/escaped.sh": "#!/bin/sh\ncat docs/\\[ab\\].md\n",
            "docs/[ab].md": "literal\n",
            "docs/a.md": "a\n",
            "docs/b.md": "b\n",
        },
    )
    for script in ("scripts/quoted.sh", "scripts/escaped.sh"):
        assert _files_a_workflow_reaches(
            {"jobs": {"j": {"steps": [{"run": f"sh {script}"}]}}},
            exists, tmp_path, tracked,
        ) == {script, "docs/[ab].md"}, script

    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/bare.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/bare.sh", "docs/[ab].md", "docs/a.md", "docs/b.md"}


def test_a_shell_named_as_data_does_not_mark_operands(tmp_path):
    """`python plain.py --format bash` runs python, not bash.

    "Any word names a shell" marked every existing path on the line, so
    a shebangless `.py` was read as shell and its string constants
    became dependencies — the guard accusing a healthy workflow, which
    is the failure the round-9 remedy was chosen to avoid and then
    committed in a narrower form (Codex round 10). The shell has to BE
    the command.

    The paired half keeps round 9's case alive: when a shell really is
    the command, its operand is still marked.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/plain.py": "FMT = 'docs/aside.md'\n",
            "docs/aside.md": "prose\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "python scripts/plain.py --format bash"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/plain.py"}

    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/wrapper.py": "cat docs/real.md\n",
            "docs/real.md": "prose\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "bash scripts/wrapper.py"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/wrapper.py", "docs/real.md"}


def test_a_shell_operand_is_marked_where_the_walk_resolved_it(tmp_path):
    """`bash inner.py` marks the file the walk actually reads.

    The mark and the queue resolved paths by DIFFERENT rules: the mark
    joined the invoking step's working directory and asked `exists`,
    while everything else had resolved by tail against the tracked tree
    since round 5. So a wrapper running `bash inner.py` marked a
    nonexistent `./inner.py` and queued `backend/inner.py` — the file
    that was read was never the file that was marked, so a shebangless
    `.py` wrapper was refused as shell and everything it named was lost
    (Codex round 11).

    The leaf is reachable ONLY through reading `backend/inner.py` as
    shell, so this case cannot pass while the two rules disagree. The
    paired half is a wrapper whose operand IS spelled as it is tracked,
    which the old rule already handled — a rule that marked nothing
    would fail that one.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/outer.sh": "#!/bin/sh\nbash inner.py\n",
            "backend/inner.py": "cat docs/leaf.md\n",
            "docs/leaf.md": "prose\n",
        },
    )
    assert _paths_a_shell_command_names("bash inner.py", exists, tracked) == {
        "backend/inner.py"
    }
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/outer.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/outer.sh", "backend/inner.py", "docs/leaf.md"}

    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/plain.sh": "#!/bin/sh\nbash scripts/near.py\n",
            "scripts/near.py": "cat docs/near.md\n",
            "docs/near.md": "prose\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/plain.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/plain.sh", "scripts/near.py", "docs/near.md"}


def test_an_env_s_operand_is_split_even_when_it_yields_one_word(tmp_path):
    """`env -S '\\_bash' wrapper.py` runs bash.

    `-S` splits; the number of words that come out is not a condition
    on splitting. Guarding the recursion on a multi-word result left
    the `env`-only escape unprocessed, so the command read as `\\_bash`,
    nothing recognised a shell, and a shebangless `.py` wrapper lost
    every dependency it named (Codex round 11).

    Paired with an operand that splits to a non-shell, so a rule that
    marked regardless would fail the second half.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/run.sh": "#!/bin/sh\nenv -S '\\_bash' scripts/wrapper.py\n",
            "scripts/wrapper.py": "cat docs/deep.md\n",
            "docs/deep.md": "prose\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/run.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/run.sh", "scripts/wrapper.py", "docs/deep.md"}

    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/py.sh": "#!/bin/sh\nenv -S 'python3' scripts/other.py\n",
            "scripts/other.py": "cat docs/unseen.md\n",
            "docs/unseen.md": "prose\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/py.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/py.sh", "scripts/other.py"}


def test_two_occurrences_of_one_token_keep_their_own_quoting(tmp_path):
    """Quoting belongs to an occurrence, not to a token.

    Round 10 answered "is this word an active glob?" from a dict keyed
    by the dequoted token, so `cat docs/[ab].md 'docs/[ab].md'` let the
    quoted occurrence answer for the unquoted one and the two files the
    shell really expands went missing (Codex round 11). A dict keyed by
    value cannot hold a per-position fact.

    Both orders, because "the last one wins" passes either half alone.
    """
    files = {
        "docs/[ab].md": "literal\n",
        "docs/a.md": "a\n",
        "docs/b.md": "b\n",
    }
    for name, line in (
        ("scripts/bare_first.sh", "cat docs/[ab].md 'docs/[ab].md'"),
        ("scripts/quoted_first.sh", "cat 'docs/[ab].md' docs/[ab].md"),
    ):
        exists, tracked = _wrapper_tree(
            tmp_path, {name: f"#!/bin/sh\n{line}\n", **files}
        )
        assert _files_a_workflow_reaches(
            {"jobs": {"j": {"steps": [{"run": f"sh {name}"}]}}},
            exists, tmp_path, tracked,
        ) == {name, "docs/[ab].md", "docs/a.md", "docs/b.md"}, line


def test_a_quoted_glob_survives_a_lexer_disagreement(tmp_path):
    """`cat docs/a\\ b'[xy]'.md` opens one literal file.

    Quoting was recovered by lexing the line a SECOND time with
    `posix=False` and pairing the two lists. The two lexers disagree
    about this line — one pathname to POSIX, three words to the other —
    and the fallback for a count mismatch dropped the quoting, expanded
    the bracket, and invented `docs/a bx.md` and `docs/a by.md`: the
    guard accusing a healthy workflow of a `paths:` gap (Codex round
    12). There is one lexing now, of a line whose inactive
    metacharacters are masked first.

    Paired with the unquoted form, so a rule that expanded nothing
    would fail the second half.
    """
    files = {
        "docs/a b[xy].md": "literal\n",
        "docs/a bx.md": "x\n",
        "docs/a by.md": "y\n",
    }
    exists, tracked = _wrapper_tree(
        tmp_path, {"scripts/q.sh": "#!/bin/sh\ncat docs/a\\ b'[xy]'.md\n", **files}
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/q.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/q.sh", "docs/a b[xy].md"}

    exists, tracked = _wrapper_tree(
        tmp_path, {"scripts/w.sh": "#!/bin/sh\ncat docs/a\\ b[xy].md\n", **files}
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/w.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/w.sh", "docs/a b[xy].md", "docs/a bx.md", "docs/a by.md"}


def test_a_bare_word_a_script_runs_is_reached(tmp_path):
    """`sh helper` inside a script names a file, and it is one.

    A bare word carries no separator, dot or metacharacter, so it was
    not path-shaped and `_paths_a_script_names` skipped it. The walk
    still FOLLOWED the file — the step channel resolves bare words — so
    its own dependencies were reached while the file itself never
    entered `reached`, and the dependency rule therefore never demanded
    a workflow's filter select it. Found by checking the neighbourhood
    of round 12's finding rather than by the finding itself.

    Bare words get the EXACT-path question only. Tail resolution would
    match every tracked file sharing that basename, which is the
    direction that invents dependencies; the second half of this case
    is a bare word that exists only deeper in the tree and must not be
    taken.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/outer.sh": "#!/bin/sh\nsh helper\n",
            "helper": "#!/bin/sh\ncat docs/bare.md\n",
            "docs/bare.md": "prose\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/outer.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/outer.sh", "helper", "docs/bare.md"}

    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/build.sh": "#!/bin/sh\nmake release\n",
            "packaging/release": "not a dependency of this script\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/build.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/build.sh"}


def test_a_control_operator_does_not_glue_itself_to_a_path(tmp_path):
    """`cat docs/a.md; echo done` reads `docs/a.md`, not `docs/a.md;`.

    Default `shlex.split` leaves a control operator attached to the word
    before it, and `docs/a.md;` names no file, matches no tail and is no
    glob — so the dependency vanished while this guard stayed green.
    Codex round 13 reported the semicolon; measured, the same defect
    lost the path through `|`, `&&`, `>` and a glob followed by `;` as
    well. `shlex` has recognised these the whole time and was not asked.

    The second half is what must NOT change: a quoted or escaped
    separator is an ordinary character, and the word keeps it.
    """
    assert _shell_words("cat docs/a.md; echo done") == [
        "cat", "docs/a.md", ";", "echo", "done",
    ]
    assert _shell_words("cat docs/a.md|wc -l") == ["cat", "docs/a.md", "|", "wc", "-l"]
    assert _shell_words("cat docs/a.md&&echo ok") == ["cat", "docs/a.md", "&&", "echo", "ok"]
    assert _shell_words("cat docs/a.md>out.txt") == ["cat", "docs/a.md", ">", "out.txt"]
    assert _shell_words("echo 'a;b' docs/a.md") == ["echo", "a;b", "docs/a.md"]
    assert _shell_words("cat docs/a.md\\;b.md") == ["cat", "docs/a.md;b.md"]

    for line in (
        "cat docs/a.md; echo done",
        "cat docs/a.md|wc -l",
        "cat docs/a.md&&echo ok",
        "cat docs/a.md>out.txt",
    ):
        exists, tracked = _wrapper_tree(
            tmp_path, {"scripts/s.sh": f"#!/bin/sh\n{line}\n", "docs/a.md": "a\n"}
        )
        assert _files_a_workflow_reaches(
            {"jobs": {"j": {"steps": [{"run": "sh scripts/s.sh"}]}}},
            exists, tmp_path, tracked,
        ) == {"scripts/s.sh", "docs/a.md"}, line

    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/g.sh": "#!/bin/sh\ncat docs/*.md; echo done\n",
            "docs/a.md": "a\n",
            "docs/b.md": "b\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/g.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/g.sh", "docs/a.md", "docs/b.md"}


def test_a_case_arm_star_is_not_every_file_in_the_tree(tmp_path):
    """`(` and `)` are deliberately NOT control operators here.

    `shlex`'s own default set is `();<>|&`, and taking all of it is the
    obvious move. Measured against this repository before doing so:
    splitting on parentheses isolates the regex fragments inside
    `[[ ... =~ ... ]]` and the patterns of a `case` arm as words of
    their own — `*` from `*)`, `.*` and `[A-Za-z_][A-Za-z0-9_]*` from a
    capture group — and each of those is a glob that matches almost
    the whole tree. It cost **404 phantom dependencies out of 412
    tracked files**, all on `librerun-smoke.yml`, and took the census
    from seven workflows to eight.

    That is the failure this rule exists to prevent, at two and a half
    times the scale that made round 9 decline a remedy, so the operator
    set is `;|&<>` and the parentheses stay attached. What it gives up
    is named rather than hidden: `(cat docs/a.md)` loses `docs/a.md`,
    which no workflow here writes. A word is left alone here precisely
    because leaving it alone matches nothing.
    """
    assert "*)" in _shell_words("case $x in *) : ;; esac")
    assert _tail_glob("*)", {"docs/a.md", "scripts/s.sh"}) == set()

    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/c.sh": '#!/bin/sh\ncase "$x" in *) : ;; esac\n',
            "docs/a.md": "a\n",
            "docs/b.md": "b\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/c.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/c.sh"}

    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/r.sh": '#!/bin/sh\nif [[ "$e" =~ ^([A-Za-z_]*)(.*)$ ]]; then :; fi\n',
            "docs/a.md": "a\n",
            "docs/b.md": "b\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/r.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/r.sh"}


def test_a_comment_after_an_operator_is_still_a_comment(tmp_path):
    """`cat docs/a.md;# See docs/phantom.md` reads one file, not two.

    A comment begins at the start of a WORD, and the position right
    after a control operator is the start of a word. The rule here
    recognised only whitespace and the start of a line, so the words of
    such a comment were lexed and every path in them became a
    dependency the filter rule would demand — the guard inventing one
    (Codex round 14). Measured against the tree: this predates round
    13's lexer change exactly, in all five operator shapes.

    The second half is the boundary in the other direction, and it is
    the round-6 case: a `#` that begins nothing must survive, or a
    parameter expansion is cut in half and the path after it lost.
    """
    for operator in (";", "|", "&&", ">", ")"):
        line = f"cat docs/a.md{operator}# See docs/phantom.md"
        assert "phantom" not in _strip_shell_comment(line), line

    assert _shell_words("cat docs/a#b.md") == ["cat", "docs/a#b.md"]
    assert _shell_words('echo "$#" ${#x} docs/real.md') == [
        "echo", "$#", "${#x}", "docs/real.md",
    ]
    assert _shell_words("grep '#' docs/a.md") == ["grep", "#", "docs/a.md"]

    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/s.sh": "#!/bin/sh\ncat docs/a.md;# See docs/phantom.md\n",
            "docs/a.md": "a\n",
            "docs/phantom.md": "p\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/s.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/s.sh", "docs/a.md"}


def test_every_hand_built_lexer_clears_shlex_comments():
    """`echo tag#value; python -m pytest x.py` runs pytest.

    `shlex.split` clears `commenters`; a lexer built by hand does not,
    and the default is `#`. Round 13 fixed that in `_shell_words` and
    left the second construction alone, so `_shell_invocations` lexed
    that line to `['echo', 'tag']` — the whole pytest invocation gone,
    and with it every file reachable only through that test (Codex
    round 15).

    There is one construction now, so this asserts BOTH readers rather
    than the one the finding named.

    This docstring used to end “a third caller cannot forget the rule
    because there is nowhere left to forget it,” and that was the
    lexer's rule only. The readers still disagreed about where a
    COMMENT begins, which round 16 found the next day; see
    `test_both_readers_strip_the_comment_not_just_one`. One shared
    helper settles one question, not every question two callers can
    answer differently.
    """
    line = "echo tag#value; python -m pytest tests/test_x.py"
    assert list(_shell_commands(line)) == [
        ["echo", "tag#value", ";", "python", "-m", "pytest", "tests/test_x.py"]
    ]
    assert _shell_invocations(line) == [
        ["echo", "tag#value"],
        ["python", "-m", "pytest", "tests/test_x.py"],
    ]


def test_a_backslash_does_not_escape_inside_single_quotes(tmp_path):
    """`echo 'x\\' # note` ends its quote at that quote.

    A backslash escapes nothing inside single quotes. The scanner
    applied the double-quote rule to both, so it stayed inside the
    string, kept the comment, and every path in the comment became a
    dependency — the guard inventing one (Codex round 15).

    Paired with the double-quoted form, where the backslash DOES
    escape, so a rule that ignored backslashes everywhere would fail
    the second half.
    """
    assert _strip_shell_comment("echo 'x\\' # See docs/phantom.md").rstrip() == (
        "echo 'x\\'"
    )
    # The contrast, and it runs the other way: inside DOUBLE quotes the
    # backslash does escape, so `"x\\"` leaves the string open and the
    # `#` is inside it, not starting a comment. A rule that ignored
    # backslashes for both quote types would strip here and fail.
    assert "phantom" in _strip_shell_comment('echo "x\\" # See docs/phantom.md')

    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/s.sh": "#!/bin/sh\necho 'x\\' # See docs/phantom.md\n",
            "docs/phantom.md": "p\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/s.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/s.sh"}



def test_both_readers_strip_the_comment_not_just_one(tmp_path):
    r"""`echo done # python -m pytest tests/legacy` runs no pytest.

    Round 15 gave the two readers of a shell line one LEXER. It did not
    give them one reading: `_shell_words` stripped the comment first and
    `_shell_invocations` never had, which nothing could show while the
    `commenters` default round 15 removed was cutting comments off for
    the wrong reason. Removing it exposed the older gap, and the
    commented-out pytest seeded `tests/legacy` — the guard demanding a
    path off a line that runs nothing (Codex round 16).

    The reply to round 15 said a third caller could not forget the rule
    because there was nowhere left to forget it. That was true of the
    lexer and false of the reading, which is the same finding one level
    up. So this asserts the two readers agree about the COMMENT, and
    `_python_entry_groups` — the consumer the defect actually reached
    — is asserted too, because a token list nobody acts on proves
    nothing.
    """
    line = "echo done # python -m pytest tests/legacy"
    assert _shell_invocations(line) == [["echo", "done"]]
    assert _shell_words(line) == ["echo", "done"]

    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "tests/legacy/test_x.py": "def test_x():\n    pass\n",
            "docs/real.md": "r\n",
        },
    )
    assert _python_entry_groups(
        {"jobs": {"j": {"steps": [{"run": line}]}}},
        exists, {}, tracked, tmp_path,
    ) == []

    # The same line with the comment marker removed MUST still find it,
    # or this test would pass on a reader that had simply stopped
    # looking for pytest at all.
    assert _python_entry_groups(
        {"jobs": {"j": {"steps": [{"run": "python -m pytest tests/legacy"}]}}},
        exists, {}, tracked, tmp_path,
    ) == [((".",), {"tests/legacy/test_x.py"})]


def test_an_escaped_character_does_not_begin_a_word(tmp_path):
    r"""`cat prefix\ # docs/real.md` passes two arguments, not one.

    A comment begins at the start of a word, and round 14 taught this
    scanner that whitespace and a control operator both end one. A
    BACKSLASH un-says that: the escaped space joins its word instead of
    ending one, so nothing begins at the `#`, there is no comment, and
    `docs/real.md` is an ordinary argument. Truncating there lost it
    (Codex round 16).

    The report named the space. The rule is not about spaces — an
    escaped character is not the character it looks like — and it was
    broken in three places at once, so all three are here:

    * `prefix\ #` — the escaped space, which loses a dependency;
    * `a\;#` — the escaped operator, same shape, same direction;
    * `\" docs/a.md # note` — the escaped quote, which opened a string
      that was never open, swallowed the real comment after it and made
      every path inside the comment a dependency: the opposite
      direction, the guard inventing one.

    Each assertion is what `bash` prints for that line, checked against
    `bash` and not reasoned out.
    """
    assert _shell_words(r"cat prefix\ # docs/real.md") == [
        "cat", "prefix #", "docs/real.md",
    ]
    assert _shell_words(r"cat a\;# docs/real.md") == [
        "cat", "a;#", "docs/real.md",
    ]
    assert "phantom" not in _strip_shell_comment(
        r'echo \" docs/a.md # See docs/phantom.md'
    )
    assert "phantom" not in _strip_shell_comment(
        r"echo \' docs/a.md # See docs/phantom.md"
    )

    # The boundary in the other direction, or a scanner that had simply
    # stopped recognising boundaries would pass the four above. An
    # UNESCAPED space and an unescaped operator still begin a word, and
    # so does an EVEN run of backslashes before the space — `\\` is a
    # literal backslash, and the space after it is a real separator.
    assert "phantom" not in _strip_shell_comment("cat docs/a.md # docs/phantom.md")
    assert "phantom" not in _strip_shell_comment("cat docs/a.md;# docs/phantom.md")
    assert "phantom" not in _strip_shell_comment(r"cat a\\ # docs/phantom.md")

    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/s.sh": "#!/bin/sh\ncat prefix\\ # docs/real.md\n",
            "docs/real.md": "r\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/s.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/s.sh", "docs/real.md"}


def test_a_command_that_is_not_python_parses_no_module_flag(tmp_path):
    r"""`echo -m pytest tests/legacy` runs no pytest.

    `-m` was read wherever it appeared in an invocation, with nothing
    asking what the invocation RUNS. A reached wrapper printing
    `echo -m pytest tests/legacy` therefore seeded that directory and
    every import under it, and the filter rule then demanded a
    workflow select files nothing runs — the guard accusing a healthy
    workflow, which is the failure it exists to prevent (Codex round
    17).

    The same question, asked of the same line, is what
    `_paths_a_shell_command_names` has asked since round 10: the
    interpreter has to BE the command. It is one helper now, because
    answering it in one of the two places is what round 16 was about.

    The neighbour the report did not name is the `.py` scan beside it:
    any word ending in `.py` was a Python ENTRY whose imports were
    walked, so `cat scripts/tool.py` charged a workflow with
    everything that file imports. `cat` imports nothing.

    Round 19 found a THIRD reader, `_runs_alembic`, that it had written
    itself two rounds after this rule was established and that did not
    ask the question at all: `echo -m alembic` and `cat notes -m
    alembic` both read as running alembic, and the census would then
    have demanded a workflow watch files it never loads. This case
    covers both readers for that reason -- the recurring finding in
    this work is a rule applied where the report pointed and not
    everywhere it is true, so a case that pins one reader is the shape
    of the next defect.

    That note used to end here saying the gap was written down rather
    than closed: `_python_m_invocations` and `_pytest_runs` asked a
    narrower version -- `words[0]` against a literal
    `("python", "python3")`, without `_the_command`. Round 23 closed
    the second of the two and left the first, so for one round this
    paragraph named a reader that had been fixed and a reader that had
    not, which is the same staleness it was written to prevent.

    BOTH ask now, and nothing here is deferred. The cost of leaving it
    was measured when it was finally closed: 14 of 15 command shapes
    uncounted, three module invocations INVENTED out of a `-m` read
    past the file operand, and the `python3.12` spelling missed
    (Codex round 24, §12). A limitation recorded in a docstring is not
    a limitation managed -- this is the second round running in which
    the survey found the real defect sitting inside a note about it.
    """
    tracked = {"tests/legacy/test_x.py", "scripts/tool.py"}
    exists = tracked.__contains__

    def groups(run):
        return _python_entry_groups(
            {"jobs": {"j": {"steps": [{"run": run}]}}},
            exists, {}, tracked, tmp_path,
        )

    assert groups("echo -m pytest tests/legacy") == []
    assert groups("cat scripts/tool.py") == []

    # The controls, or a scan that had simply stopped looking would
    # pass the two above. A real interpreter still seeds, under any of
    # the spellings a workflow uses.
    assert groups("python -m pytest tests/legacy") == [
        ((".",), {"tests/legacy/test_x.py"})
    ]
    assert groups("python3 -m pytest tests/legacy") == [
        ((".",), {"tests/legacy/test_x.py"})
    ]
    assert groups("MODE=test python -m pytest tests/legacy") == [
        ((".",), {"tests/legacy/test_x.py"})
    ]
    assert groups("env A=1 python -m pytest tests/legacy") == [
        ((".",), {"tests/legacy/test_x.py"})
    ]
    assert groups("python scripts/tool.py") == [((".",), {"scripts/tool.py"})]

    # The SECOND reader, which did not ask at all until round 19.
    def runs_alembic(run):
        return _runs_alembic(
            {"jobs": {"j": {"steps": [{"run": run}]}}},
            tmp_path, exists, tracked,
        )

    assert runs_alembic("echo -m alembic") is False
    assert runs_alembic("cat notes -m alembic") is False
    # ...and its own controls, or a reader that had stopped looking
    # would pass the two above. Both spellings this tree uses, plus the
    # ones `_the_command` exists to see through.
    assert runs_alembic("python -m alembic upgrade head") is True
    assert runs_alembic("alembic upgrade head") is True
    assert runs_alembic("python3.12 -m alembic upgrade head") is True
    assert runs_alembic("MODE=test python -m alembic upgrade head") is True


def test_the_reserved_words_are_bashs_own():
    r"""The list is bash's, re-derived, not a list I once typed.

    `compgen -k` prints bash's reserved words. Three hand-kept lists on
    this pull request went stale within a round of being written -- the
    harness counts twice, the route decorators, the registration
    methods -- so this one is checked against its source rather than
    trusted.

    Coverage, not equality: a word bash has dropped can only make the
    strip slightly eager, which costs nothing because a reserved word
    is never a program name. A word bash HAS and this set does not is
    the hole, and it is the one that hides an invocation.

    bash is REQUIRED rather than skipped around. A skip here would be
    invisible -- this guard does not walk the workflow census, so the
    meta-guard that reports vacuous guards never sees it -- and the
    suite that runs this already requires a Postgres.
    """
    import shutil
    import subprocess

    bash = shutil.which("bash")
    assert bash, (
        "bash is not on PATH, so the reserved-word list cannot be checked "
        "against its source; this suite already assumes a developer machine "
        "or CI runner, and a silent skip here is how the other three lists "
        "on this PR went stale"
    )
    printed = subprocess.run(
        [bash, "-c", "compgen -k"], capture_output=True, text=True
    )
    derived = {w for w in printed.stdout.split() if w}

    assert len(derived) >= 20, (
        f"`compgen -k` answered {sorted(derived)}, which is too few to be "
        f"bash's keyword list -- the derivation is not measuring what it "
        f"claims and the coverage assertion below would be vacuous"
    )
    assert {"if", "then", "do", "time", "{"} <= derived, (
        f"`compgen -k` is missing words every shell has: {sorted(derived)}"
    )
    missing = derived - _SHELL_RESERVED_WORDS
    assert not missing, (
        f"bash reserves {sorted(missing)} and this module does not know "
        f"them, so a command standing after one of them is read as though "
        f"the reserved word were the program. Add them to "
        f"_SHELL_RESERVED_WORDS."
    )


def test_a_reserved_word_does_not_hide_the_command_after_it():
    r"""`{ python -m pytest x.py; }` runs pytest, and so do eight more.

    Codex round 23 reported the brace group. Measuring the rest found
    nine shapes where a reserved word stands at the head of the
    invocation and the reader took it for the program: `!`, `time`,
    every branch and every loop body -- because the `;` before `then`
    or `do` ends the previous command and leaves the reserved word
    first. `if ...; then python -m pytest x.py; fi` is ordinary CI
    shell.

    **And the same measurement found three shapes that have nothing to
    do with reserved words**: `MODE=test python -m pytest x.py` and
    both `env` spellings were invisible to this census too. Round 17
    established that a line may open with assignments or `env`, and
    taught the shell mark and the Python entry scan to ask
    `_the_command` -- but `_pytest_runs` kept reading `words[0]` and
    was never swept. Nine of ten shapes missed, measured before
    anything changed. That is the finding here; the brace group is one
    symptom of it.

    Latent on this tree: it writes `then` three times and `else` once,
    never before an interpreter, and the census is the same six
    workflows before and after. Fixed anyway -- "no workflow does that
    yet" is the argument round 21 used to leave a hole open and round
    22 spent a round closing.
    """
    invocation = "python -m pytest tests/test_x.py --junitxml=r.xml"

    def censused(text):
        return bool(list(_pytest_runs(
            {"jobs": {"j": {"steps": [{"run": text}]}}})))

    # The shapes a shell runs and the census must see.
    for shape in (
        invocation,                                        # control
        "{ %s; }" % invocation,                            # the report
        "! %s" % invocation,
        "time %s" % invocation,
        "if true; then %s; fi" % invocation,
        "if false; then echo x; else %s; fi" % invocation,
        "while false; do %s; done" % invocation,
        "for f in a b; do %s; done" % invocation,
        "(%s)" % invocation,                               # round 22
        "MODE=test %s" % invocation,                       # round 17's shape,
        "env %s" % invocation,                             # never swept into
        "env FOO=1 %s" % invocation,                       # this reader
    ):
        assert censused(shape), f"a pytest run went uncounted: {shape!r}"

    # ...and a command is still REQUIRED. Stripping reserved words must
    # not turn text that merely mentions pytest into a run: these are
    # the assertions that would fail if the strip were a search.
    for shape in (
        "echo python -m pytest tests/test_x.py",
        "echo then python -m pytest tests/test_x.py",
        "cat scripts/pytest.txt",
    ):
        assert not censused(shape), f"not a pytest run, but counted: {shape!r}"

    # The helper itself, on the two families it now answers for.
    assert _the_command(["{", "python", "-m", "pytest"]) == ["python", "-m", "pytest"]
    assert _the_command(["then", "python", "-m", "pytest"]) == ["python", "-m", "pytest"]
    assert _the_command(["time", "!", "python"]) == ["python"]
    assert _the_command(["MODE=1", "env", "A=2", "python"]) == ["python"]
    # A word that merely CONTAINS a reserved word is not one.
    assert _the_command(["iffy", "python"]) == ["iffy", "python"]
    assert _the_command(["{a,b}.md"]) == ["{a,b}.md"]


def test_a_reserved_word_that_takes_an_operand_does_not_eat_the_command():
    r"""Three reserved words are followed by their OWN operand.

    Round 23 taught this scan that a reserved word may stand where the
    program does. Round 24 is the mirror of that: treating all of them
    identically left `time`'s option where the program belongs, so
    `time -p python -m pytest x.py` read `-p` as the executable and the
    run went uncounted (Codex round 24). Codex named `time -p`;
    measuring the rest found `time --`, `function NAME` and
    `coproc NAME`, four shapes in all.

    Every rule here is MEASURED against bash rather than recalled,
    which is what settled the one that is not obvious:

    * `help time` gives `time [-p] pipeline`, and both `time -p true`
      and `time -- true` run, so the options are skipped by SHAPE -- a
      leading dash -- rather than by a list of two that would rot.
    * `function NAME [()] compound-command`: the name is never the
      program.
    * `coproc [NAME] command`, and the NAME is only a name in front of
      a COMPOUND command. `coproc C echo hi` answers
      ``C: command not found`` -- there `C` IS the program, so eating
      the word after `coproc` unconditionally would lose the
      invocation. The rule looks at what FOLLOWS the candidate name.

    `for`, `select` and `case` also take operands and need no rule: the
    command in each sits after a `;` or `do`, which is already a
    boundary, so it arrives as its own invocation.
    """
    invocation = "python -m pytest tests/test_x.py --junitxml=r.xml"

    def censused(text):
        return bool(list(_pytest_runs(
            {"jobs": {"j": {"steps": [{"run": text}]}}})))

    for shape in (
        "time -p %s" % invocation,            # the report
        "time -- %s" % invocation,
        "time %s" % invocation,               # still works without options
        "function run { %s; }" % invocation,
        "coproc C { %s; }" % invocation,
        "coproc %s" % invocation,             # NO name: the command follows
    ):
        assert censused(shape), f"a pytest run went uncounted: {shape!r}"

    # Eating a word too many is the other failure, and it is silent:
    # these must still be found, and `echo` must still not be a run.
    assert censused(invocation)
    assert not censused("echo %s" % invocation)
    assert not censused("echo time -p %s" % invocation)

    # The helper directly, including the coproc discrimination bash
    # itself settles.
    assert _the_command(["time", "-p", "python"]) == ["python"]
    assert _the_command(["time", "--", "python"]) == ["python"]
    assert _the_command(["function", "run", "{", "python"]) == ["python"]
    assert _the_command(["coproc", "C", "{", "python"]) == ["python"]
    assert _the_command(["coproc", "python", "-m", "pytest"]) == [
        "python", "-m", "pytest"]
    # A word that merely starts with a dash after something that is NOT
    # `time` is left alone -- only `time` takes options here.
    assert _the_command(["!", "-p", "python"]) == ["-p", "python"]


def test_the_module_census_asks_what_every_other_reader_asks():
    r"""The twin `_pytest_runs` was swept away from, one round later.

    `test_a_python_entry_is_what_the_interpreter_runs` has named two
    readers since round 21 as asking a narrower question --
    `_python_m_invocations` and `_pytest_runs`, both reading `words[0]`
    against a literal `("python", "python3")`. Round 23 fixed the
    second and left the first, so the note recording the limitation
    went stale in the same round it was half-answered. **A limitation
    written down is not a limitation managed**, and this case is what
    closing it looks like.

    Measured before the change, all three directions:

    * **14 of 15 command shapes uncounted** -- every shape the twin
      counts after rounds 17, 19, 23 and 24. False NEGATIVES, so
      `_module_shadow_gaps` protected nothing for such a step while
      reporting no gap.
    * **3 invocations invented.** `-m` was read wherever it appeared,
      so `python s.py -m victim` reported a module. The real
      interpreter prints ``SCRIPT ran, argv= ['-m', 'victim']``.
      Round 19 fixed precisely this in `_python_entry`; the sweep
      stopped where the report pointed.
    * **`python3.12` uncounted**, the spelling half of the same note.

    And the survey found the JOINED spelling in the shared helper
    itself, which neither reader could have got right alone: measured,
    `python -mthis` and `python -cprint(1)` both run, and `python
    --help` says `-c cmd` and `-m mod` "terminate the option list".
    `_python_entry` saw neither, so it walked on to the script's own
    argument -- `python -cprint(1) data.py` answered
    `("file", "data.py")` and seeded every import under a file the
    interpreter never opened. `-W` and `-X` need no case: joined, they
    are one dashed word, which the existing rule already skips, and
    this case pins that so a later "fix" cannot widen it into them.

    Latent on this tree, stated rather than implied: no workflow here
    writes a joined `-m`, an assignment before an interpreter, or a
    `python3.12`. The census is byte-identical across the change.
    """
    import subprocess
    import sys

    exists = {"s.py", "data.py", "tests/test_x.py"}.__contains__

    def census(text):
        return _python_m_invocations(
            {"jobs": {"j": {"steps": [{"run": text}]}}}, exists)

    invocation = "python -m adapter_kit.run_contract --agent echo"
    for shape in (
        invocation,
        "MODE=test " + invocation,
        "A=1 B=2 " + invocation,
        "env " + invocation,
        "env A=1 " + invocation,
        "{ %s; }" % invocation,
        "if x; then %s; fi" % invocation,
        "for f in a; do %s; done" % invocation,
        "! " + invocation,
        "time " + invocation,
        "time -p " + invocation,
        "time -- " + invocation,
        "function run { %s; }" % invocation,
        "coproc C { %s; }" % invocation,
        "coproc " + invocation,
        invocation.replace("python", "python3.12", 1),
        invocation.replace("python", "/usr/bin/python3", 1),
        invocation.replace("-m ", "-m"),
    ):
        assert census(shape) == [("", "adapter_kit")], (
            f"the module census missed or misread: {shape!r}")

    # The other direction, and it is the silent one: a module the
    # interpreter never runs must not be reported, or the shadow guard
    # demands a filter path for a hazard that cannot occur.
    for invented in (
        "python s.py -m victim",
        "python -c x -m victim",
        "python -cprint(1) s.py",
        "echo python -m victim",
        "cat notes -m victim",
    ):
        assert census(invented) == [], (
            f"a module invocation was invented: {invented!r}")

    # The shared helper, on the joined forms the readers disagreed
    # about -- including the two that must NOT be read as entries.
    assert _python_entry(["python", "-mpytest", "tests/test_x.py"],
                         exists, "") == ("module", "pytest")
    assert _python_entry(["python", "-cprint(1)", "data.py"],
                         exists, "") == ("command", None)
    assert _python_entry(["python", "-Wignore", "data.py"],
                         exists, "") == ("file", "data.py")
    assert _python_entry(["python", "-Xutf8", "data.py"],
                         exists, "") == ("file", "data.py")

    # The grammar is READ, not recalled: `python --help` names the
    # options taking a value, and the two that TERMINATE the option
    # list are the two that need a joined case. Coverage, not
    # equality, so a future interpreter adding one does not fail here
    # for saying more than this rule needs.
    helptext = subprocess.run(
        [sys.executable, "--help"], capture_output=True, text=True,
        timeout=30).stdout
    terminating = {
        line.strip()[:2]
        for line in helptext.splitlines()
        if "terminates option list" in line and line.strip().startswith("-")
    }
    assert terminating, "python --help named no option that terminates the list"
    assert terminating <= {"-c", "-m"}, (
        f"the interpreter terminates its option list on {terminating}, which "
        f"this scan does not all handle")

    # ...and that they really do take a joined value, measured on the
    # interpreter running this test rather than asserted from the help.
    for argv in (["-mthis"], ["-cprint(1)"]):
        assert subprocess.run(
            [sys.executable] + argv, capture_output=True, timeout=60
        ).returncode == 0, f"python {argv[0]} did not run"


def test_one_scan_says_both_what_runs_and_where_its_arguments_start(tmp_path):
    r"""The SECOND scan, which disagreed with the first.

    `_python_entry_groups` knew what an invocation runs -- it has asked
    `_python_entry` since round 19 -- and then re-scanned the words
    itself for a bare `-m` to find where pytest's targets begin. Two
    scans of one invocation, and this round made them disagree:
    teaching the shared scan the joined `-mpytest` left the re-scan
    blind to it, so the entry was known to be a module and its targets
    seeded as **nothing** -- the import closure lost and a filtered
    workflow free to omit every file under it. Found by re-reading this
    round's own diff, not reported.

    The pair also disagreed outright on an invocation neither round had
    considered: in `python -W -m a -m b` the shared scan skips `-W`'s
    value and answers `b`, while a re-scan taking the first `-m` it
    sees answers `a` -- and `-W`'s value IS that `-m`. One scan reports
    both facts now, which is the rule rounds 16 and 17 paid for: a
    second caller answering a question its own way is how the first one
    drifts.
    """
    tracked = {"tests/legacy/test_x.py", "x.py"}
    exists = tracked.__contains__

    def groups(run):
        return _python_entry_groups(
            {"jobs": {"j": {"steps": [{"run": run}]}}},
            exists, {}, tracked, tmp_path,
        )

    seeded = [((".",), {"tests/legacy/test_x.py"})]
    # The spelling must not decide what a workflow is charged with.
    assert groups("python -m pytest tests/legacy") == seeded
    assert groups("python -mpytest tests/legacy") == seeded
    assert groups("python -W ignore -m pytest tests/legacy") == seeded
    assert groups("time -p python -mpytest tests/legacy") == seeded
    assert groups("MODE=1 python -mpytest tests/legacy") == seeded

    # Round 19's rule, which this restructuring must not cost: a `-m`
    # AFTER a file operand is the script's argument, so the script is
    # the entry and pytest's targets are not seeded.
    assert groups("python x.py -m pytest tests/legacy") == [((".",), {"x.py"})]

    # ...and the interpreter still has to BE the command (round 17).
    assert groups("echo -m pytest tests/legacy") == []

    # The boundary itself, including the invocation the two scans
    # answered differently.
    assert _python_entry_at(["python", "-m", "pytest", "t"], exists, "") == (
        "module", "pytest", 3)
    assert _python_entry_at(["python", "-mpytest", "t"], exists, "") == (
        "module", "pytest", 2)
    assert _python_entry_at(["python", "-W", "-m", "a", "-m", "b"],
                            exists, "") == ("module", "b", 6)
    assert _python_entry_at(["python", "x.py", "-m", "pytest"],
                            exists, "") == ("file", "x.py", 2)
    # `_python_entry` is the same answer with the boundary dropped, or
    # there would be two scans again by another name.
    for words in (["python", "-m", "pytest", "t"], ["python", "-mpytest", "t"],
                  ["python", "-W", "-m", "a", "-m", "b"],
                  ["python", "x.py", "-m", "pytest"], ["python", "-cprint(1)"]):
        assert _python_entry(words, exists, "") == \
            _python_entry_at(words, exists, "")[:2], words


def test_a_leading_assignment_does_not_hide_the_command(tmp_path):
    r"""`MODE=test bash scripts/w.py` runs bash.

    A command line may open with variable assignments, and they are not
    the program. Reading `words[0]` answered `MODE=test`, so the shell
    went unrecognised, a shebangless wrapper named `.py` was not read as
    shell, and every dependency inside it vanished — the quiet
    direction (Codex round 17).

    Paired with what must NOT be taken for an assignment, or a rule
    that stripped any word containing `=` would pass the first half and
    lose the command in the second: `--opt=value` is an option and
    `a/b=c` is not a name.
    """
    assert _the_command(["MODE=test", "bash", "x"]) == ["bash", "x"]
    assert _the_command(["A=1", "B=2", "bash", "x"]) == ["bash", "x"]
    assert _the_command(["--opt=v", "bash", "x"]) == ["--opt=v", "bash", "x"]
    assert _the_command(["a/b=c", "bash", "x"]) == ["a/b=c", "bash", "x"]
    assert _the_command(["A=1", "env", "B=2", "bash", "x"]) == ["bash", "x"]

    exists, tracked = _wrapper_tree(
        tmp_path,
        {"scripts/w.py": "cat docs/leaf.md\n", "docs/leaf.md": "x\n"},
    )
    assigned = _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "MODE=test bash scripts/w.py"}]}}},
        exists, tmp_path, tracked,
    )
    assert assigned == {"scripts/w.py", "docs/leaf.md"}
    # It must reach exactly what the same line without the assignment
    # reaches, or this passes on a rule that reads every line as shell.
    assert assigned == _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "bash scripts/w.py"}]}}},
        exists, tmp_path, tracked,
    )
    # And `python` is not a shell, so the same wrapper run that way is
    # reached WITHOUT being read as shell.
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "python scripts/w.py"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/w.py"}


def test_a_path_the_shell_aware_reader_resolves_is_queued_and_reached(tmp_path):
    r"""`bash "scripts/my runner.sh"` reaches the wrapper and its files.

    Two readers of one line disagreed, which is this entry's oldest
    shape and round 16's whole finding. `_paths_a_shell_command_names`
    resolved the quoted operand correctly and its answer was kept as
    type EVIDENCE only; the discovery loop went on asking the regex
    reader, which split the spelling into `scripts/my` and
    `runner.sh`. Neither names anything, so the wrapper was never
    queued, never expanded and never reported — a workflow could omit
    every file inside it and this guard stayed green (Codex round 17).

    What the shell-aware reader resolves is now a candidate and a
    result, not evidence alone. Recording it only as a candidate was
    round 6's finding committed inside the fix for this one: the run
    reported `docs/leaf.md` while the wrapper that names it was absent
    from the answer.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/my runner.sh": "#!/bin/sh\ncat docs/leaf.md\n",
            "docs/leaf.md": "x\n",
        },
    )
    quoted = _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": 'bash "scripts/my runner.sh"'}]}}},
        exists, tmp_path, tracked,
    )
    assert quoted == {"scripts/my runner.sh", "docs/leaf.md"}

    # The unquoted spelling of the same shape, which has always worked,
    # so a rule that merely stopped resolving anything cannot pass.
    exists2, tracked2 = _wrapper_tree(
        tmp_path / "plain",
        {"scripts/runner.sh": "#!/bin/sh\ncat docs/leaf.md\n", "docs/leaf.md": "x\n"},
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "bash scripts/runner.sh"}]}}},
        exists2, tmp_path / "plain", tracked2,
    ) == {"scripts/runner.sh", "docs/leaf.md"}


def test_only_the_entry_operand_of_a_python_invocation_is_seeded(tmp_path):
    r"""`python scripts/main.py scripts/data.py` runs ONE of them.

    Every word ending `.py` was seeded as a Python entry, so the import
    closure followed the ARGUMENTS' imports too and charged a workflow
    with files the invocation never loads — the guard demanding a
    filter cover code nothing runs, which is the loud direction (Codex
    round 18). `python -c 'print(1)' data.py` runs neither, and
    `python -m mod data.py` runs the module.

    `-c` and `-m` say the entry is not a file at all; otherwise the
    first `.py` that exists is the script and the rest are its
    arguments. That is deliberately not an option grammar: parsing the
    interpreter's flags was wrong in six consecutive rounds and round 8
    deleted the channel that needed it. An option whose VALUE ends in
    `.py` would have to be written to be a problem here, and none is.
    """
    tracked = {"scripts/main.py", "scripts/data.py", "tests/legacy/test_x.py"}
    exists = tracked.__contains__

    def groups(run):
        return _python_entry_groups(
            {"jobs": {"j": {"steps": [{"run": run}]}}},
            exists, {}, tracked, tmp_path,
        )

    assert groups("python scripts/main.py scripts/data.py") == [
        ((".",), {"scripts/main.py"})
    ]
    assert groups("python -c 'print(1)' scripts/data.py") == []
    assert groups("python -m somemod scripts/data.py") == []

    # ...and the same rule from the other side. `-c` and `-m` end the
    # INTERPRETER's options; after the file operand they are the
    # script's own arguments. Round 18 asked whether either appeared
    # anywhere in the invocation, so a script taking `-m` lost its
    # entry and every import under it (Codex round 19) — mine, from
    # the fix for the opposite error.
    assert groups("python scripts/main.py -m fast") == [
        ((".",), {"scripts/main.py"})
    ]
    assert groups("python scripts/main.py -c something") == [
        ((".",), {"scripts/main.py"})
    ]

    # The controls. A rule that had simply stopped seeding would pass
    # the three above, and `-m pytest` must still collect BOTH of its
    # positional targets — which it reaches through the tree branch,
    # not this one.
    assert groups("python scripts/main.py") == [((".",), {"scripts/main.py"})]
    assert sorted(
        tuple(sorted(seeds)) for _roots, seeds in
        groups("python -m pytest tests/legacy scripts/data.py")
    ) == [("scripts/data.py",), ("tests/legacy/test_x.py",)]

def test_an_extensionless_file_is_a_python_entry(tmp_path):
    r"""Python runs a file by PATH, not by suffix.

    Round 19 spelled the entry operand as a `.py` that exists, which
    made `python scripts/tool` -- an ordinary extensionless script --
    answer `("none", None)`: the entry was never seeded, every import
    under it was lost, and a workflow's `paths:` filter could omit them
    all while the guard reported nothing (Codex round 21). Mine, and
    the third round running in which the fix for one reading of this
    operand has been wrong about another.

    The premise is MEASURED here rather than asserted, the way the
    shell cases are measured against what `bash` prints: this runs the
    real interpreter on an extensionless file first, and the rest of
    the case only means something if that succeeds.

    `-W ignore` is the objection round 19 raised against "the first
    word without a dash", and it is answered by the option grammar
    instead of by the filename. The file named `ignore` below EXISTS
    on purpose -- without it that assertion would pass for the wrong
    reason, which is this batch's most-repeated lesson about a test
    that cannot fail.
    """
    import subprocess
    import sys

    extensionless = tmp_path / "tool"
    extensionless.write_text("print('ran')\n")
    proof = subprocess.run(
        [sys.executable, str(extensionless)], capture_output=True, text=True
    )
    assert proof.returncode == 0 and proof.stdout.strip() == "ran", (
        f"this interpreter did not run an extensionless file "
        f"({proof.returncode}: {proof.stderr[:200]}), so the rule this case "
        f"asserts would not be the right rule"
    )

    on_disk = {"scripts/tool", "scripts/main.py", "ignore", "-x"}.__contains__

    # The finding itself, with and without interpreter options in front.
    assert _python_entry(["python", "scripts/tool"], on_disk, "") == (
        "file", "scripts/tool")
    assert _python_entry(["python", "-u", "scripts/tool"], on_disk, "") == (
        "file", "scripts/tool")
    assert _python_entry(
        ["python", "-X", "dev", "scripts/tool"], on_disk, "") == (
        "file", "scripts/tool")
    assert _python_entry(
        ["python", "--check-hash-based-pycs", "always", "scripts/tool"],
        on_disk, "") == ("file", "scripts/tool")

    # An option's VALUE is not the entry, even when a file of that name
    # exists -- which is why `ignore` is in `on_disk`.
    assert _python_entry(
        ["python", "-W", "ignore", "scripts/main.py"], on_disk, "") == (
        "file", "scripts/main.py")

    # A leading dash is an option, never the operand: running a file
    # called `-x` needs `./-x`. `-x` exists here for the same reason.
    assert _python_entry(["python", "-x", "scripts/tool"], on_disk, "") == (
        "file", "scripts/tool")

    # And round 19's own property is untouched: `-m` AFTER the operand
    # belongs to the script.
    assert _python_entry(
        ["python", "scripts/main.py", "-m", "fast"], on_disk, "") == (
        "file", "scripts/main.py")
    assert _python_entry(["python", "-m", "fast"], on_disk, "") == (
        "module", "fast")
    assert _python_entry(["python", "-c", "print(1)", "scripts/tool"],
                         on_disk, "") == ("command", None)
    assert _python_entry(["python", "-"], on_disk, "") == ("none", None)


def test_a_posix_character_class_widens_rather_than_matching_nothing(tmp_path):
    """`docs/[[:alpha:]].md` is a glob `PurePosixPath` cannot express.

    It returned NOTHING for it, which is the direction that loses a
    dependency (Codex round 8). Each class widens to `?` instead: one
    character, any character. That over-matches — `docs/1.md` is taken
    for `[[:alpha:]]` — which widens a trigger and cannot lose a file
    the shell would have named.

    Three halves, really. The class matches; `docs/ab.md` does NOT,
    because one class is ONE character and widening it to `*` would be
    a different rule; and `docs/deep/b.md` does not, because `?` never
    crosses a separator. The first fixture here had only
    single-character names, so an injection widening to `*` gave the
    same answer and the case stayed green — vacuous for two of the
    three things it claims.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/s.sh": "#!/bin/sh\ncat docs/[[:alpha:]].md\n",
            "docs/a.md": "a\n",
            "docs/1.md": "1\n",
            "docs/ab.md": "ab\n",
            "docs/deep/b.md": "b\n",
        },
    )
    doc = {"jobs": {"j": {"steps": [{"run": "sh scripts/s.sh"}]}}}

    assert _files_a_workflow_reaches(doc, exists, tmp_path, tracked) == {
        "scripts/s.sh",
        "docs/a.md",
        "docs/1.md",
    }

    # A class EMBEDDED in or NEGATED by the bracket expression is an
    # ordinary shell pattern and matched nothing (Codex round 9). Any
    # bracket expression containing a class widens, not only one that
    # is exactly a class.
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/e.sh": "#!/bin/sh\ncat docs/[[:alpha:]_].md\n",
            "scripts/n.sh": "#!/bin/sh\ncat docs/[![:digit:]].md\n",
            "docs/a.md": "a\n",
            "docs/_.md": "u\n",
            "docs/deep/z.md": "z\n",
        },
    )
    for script in ("scripts/e.sh", "scripts/n.sh"):
        assert _files_a_workflow_reaches(
            {"jobs": {"j": {"steps": [{"run": f"sh {script}"}]}}},
            exists, tmp_path, tracked,
        ) == {script, "docs/a.md", "docs/_.md"}, script


def test_a_quoted_path_survives_the_handoff_to_the_python_walk(tmp_path):
    """Tokenising and then re-splitting is not tokenising.

    The script's words were lexed correctly and then joined with spaces
    for the Python entry walk, which lexes them again — so `python
    "scripts/my runner.py"` came back as three words, the file stopped
    being an entry seed, and nothing only its imports reach was checked
    (Codex round 7). `shlex.join` hands over what `shlex` parsed.

    The file itself was reached either way, which is why this asserts
    on the file only its IMPORTS reach.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/w.sh": '#!/bin/sh\npython "scripts/my runner.py"\n',
            "scripts/my runner.py": "import helper\n",
            "scripts/helper.py": "x = 1\n",
        },
    )
    doc = {"jobs": {"j": {"steps": [{"run": "sh scripts/w.sh"}]}}}

    assert _files_a_workflow_reaches(doc, exists, tmp_path, tracked) == {
        "scripts/w.sh",
        "scripts/my runner.py",
        "scripts/helper.py",
    }


def test_shell_control_flow_cannot_misplace_a_command(tmp_path):
    """`if false; then / cd backend / fi` never runs that `cd`.

    Splitting a script into physical lines made the `cd` unconditional,
    so a later `python inner.py` was resolved under `backend` — a
    directory the script never entered, and a root-level `inner.py`
    missed (Codex round 5). Deciding which branch runs means running
    the script; resolving by tail means never asking.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/outer.sh": (
                '#!/bin/sh\nif false; then\ncd "backend"\nfi\npython inner.py\n'
            ),
            "backend/inner.py": "import subprocess\n\nsubprocess.run(['pytest'])\n",
        },
    )
    doc = {"jobs": {"j": {"steps": [{"run": "bash scripts/outer.sh"}]}}}

    assert _files_a_workflow_reaches(doc, exists, tmp_path, tracked) == {
        "scripts/outer.sh",
        "backend/inner.py",
    }
    assert _runs_pytest(doc, tmp_path, exists, tracked) is True


def test_the_same_wrapper_reached_twice_is_read_for_both(tmp_path):
    """One `expanded` set keyed by path, and a second caller got nothing.

    `scripts/common` is run from the repository root by one step, and
    reached again through a wrapper that enters `backend` — in a LATER
    pass, by which time the path was marked expanded, so its second
    context contributed nothing (Codex round 5). Under tail resolution
    one reading covers both, because the reading never depended on which
    directory asked.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/common": "#!/bin/sh\npython inner.py\n",
            "scripts/outer.sh": "#!/bin/sh\ncd backend\nsh ../scripts/common\n",
            "backend/inner.py": "import subprocess\n\nsubprocess.run(['pytest'])\n",
        },
    )
    doc = {
        "jobs": {
            "j": {
                "steps": [
                    {"run": "sh scripts/common"},
                    {"run": "bash scripts/outer.sh"},
                ]
            }
        }
    }

    assert "backend/inner.py" in _files_a_workflow_reaches(
        doc, exists, tmp_path, tracked
    )
    assert _runs_pytest(doc, tmp_path, exists, tracked) is True


def test_a_script_is_expanded_whatever_its_name(tmp_path):
    """A `scripts/wrap.txt` carrying `#!/bin/sh` is a script.

    Round 4 replaced a suffix guess at the SCAN with a shebang, and left
    a suffix guess in front of the EXPANSION — so a wrapper named `.txt`
    never reached the question, and the file it launches stayed
    unreached (Codex round 5). The file decides at both sites now.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/wrap.txt": "#!/bin/sh\npython scripts/inner.py\n",
            "scripts/inner.py": "import subprocess\n\nsubprocess.run(['pytest'])\n",
        },
    )
    doc = {"jobs": {"j": {"steps": [{"run": "bash scripts/wrap.txt"}]}}}

    assert _files_a_workflow_reaches(doc, exists, tmp_path, tracked) == {
        "scripts/wrap.txt",
        "scripts/inner.py",
    }
    assert _runs_pytest(doc, tmp_path, exists, tracked) is True


def test_a_path_named_only_by_a_glob_is_resolved(tmp_path):
    """`cat docs/*.md` names files no token spells.

    Pathname expansion happens against the filesystem before a command
    runs, so a script can depend on a file whose name appears nowhere
    in it. Tail resolution matched a SPELLING, so those were lost —
    the expensive direction, and one the claim "it cannot lose one"
    denied (Codex round 6, and measured here first: the tree's only
    real pathname globs are in `scripts/demo.sh`, reached by a workflow
    with no `paths:` filter, so nothing was being lost yet).

    `fnmatch` would have been the obvious reach and is wrong: its `*`
    crosses `/`, so `pkg/*/data.yaml` would take `pkg/a/b/data.yaml`
    and a bare `*.md` would take the whole tree. The shell's `*` stops
    at a separator and so does this.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/s.sh": "#!/bin/sh\ncat docs/*.md\n",
            "docs/a.md": "a\n",
            "docs/b.md": "b\n",
            "docs/deep/c.md": "c\n",
            "docs/notes.txt": "t\n",
        },
    )
    doc = {"jobs": {"j": {"steps": [{"run": "sh scripts/s.sh"}]}}}

    assert _files_a_workflow_reaches(doc, exists, tmp_path, tracked) == {
        "scripts/s.sh",
        "docs/a.md",
        "docs/b.md",
    }

    # A bracket expression is a glob too, and the hand-written
    # translation escaped it literally so it matched nothing — the
    # losing direction (Codex round 7). Both halves, since a rule that
    # ignored the negation would pass the first.
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/b.sh": "#!/bin/sh\ncat docs/[ab].md\n",
            "scripts/n.sh": "#!/bin/sh\ncat docs/[!a].md\n",
            "docs/a.md": "a\n",
            "docs/b.md": "b\n",
            "docs/c.md": "c\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/b.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/b.sh", "docs/a.md", "docs/b.md"}
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/n.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/n.sh", "docs/b.md", "docs/c.md"}

    # A glob need not contain a `/` or a `.`, and a token that LOOKS
    # like a tracked file is still a glob — the shell expands it rather
    # than opening the bracketed name (Codex round 9). The literal is
    # kept as well, since the pattern names it too.
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/base.sh": "#!/bin/sh\ncat Make*\n",
            "scripts/lit.sh": "#!/bin/sh\ncat docs/[ab].md\n",
            "Makefile": "m\n",
            "Makerules": "r\n",
            "docs/[ab].md": "literal\n",
            "docs/a.md": "a\n",
            "docs/b.md": "b\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/base.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/base.sh", "Makefile", "Makerules"}
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/lit.sh"}]}}},
        exists, tmp_path, tracked,
    ) == {"scripts/lit.sh", "docs/[ab].md", "docs/a.md", "docs/b.md"}


def test_a_quoted_path_with_a_space_is_one_word(tmp_path):
    """`cat "docs/my guide.md"` names one file, not two non-files.

    A whitespace split produced `docs/my` and `guide.md`, neither of
    which is in the tree, so the dependency vanished (Codex round 6).
    Hand-written word splitting is what produced a finding in rounds 4,
    5 and 6; `shlex` has done it correctly the whole time.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/s.sh": '#!/bin/sh\ncat "docs/my guide.md"\n',
            "docs/my guide.md": "prose\n",
        },
    )
    doc = {"jobs": {"j": {"steps": [{"run": "sh scripts/s.sh"}]}}}

    assert "docs/my guide.md" in _files_a_workflow_reaches(
        doc, exists, tmp_path, tracked
    )


def test_a_script_found_by_tail_is_expanded_too(tmp_path):
    """The fixpoint has to be fed what the fixpoint found.

    `cd backend; sh inner.sh` resolves `backend/inner.sh` by tail, and
    recording it as reached without queueing it ended the walk one
    wrapper early: the next pass still looked for a literal `inner.sh`,
    so `backend/leaf.py` and the pytest in it were never seen (Codex
    round 6).

    The second tree is here because round 17 DISARMED the injection for
    the first one. Queueing the paths the shell-aware reader resolves
    gave `sh inner.sh` a second route into the fixpoint, so removing the
    requeue stopped changing that answer -- a fix making its own
    injection harmless, which this entry has had a rule about since
    round 11, and which the LEDGER caught rather than review. A command
    that is not a shell names the same script through the requeue
    alone, so that is what isolates it now. Both trees stay: the first
    is round 6's case, the second is what still tests it.
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/outer.sh": "#!/bin/sh\ncd backend\nsh inner.sh\n",
            "backend/inner.sh": "#!/bin/sh\npython leaf.py\n",
            "backend/leaf.py": "import subprocess\n\nsubprocess.run(['pytest'])\n",
        },
    )
    doc = {"jobs": {"j": {"steps": [{"run": "sh scripts/outer.sh"}]}}}

    assert _files_a_workflow_reaches(doc, exists, tmp_path, tracked) == {
        "scripts/outer.sh",
        "backend/inner.sh",
        "backend/leaf.py",
    }
    assert _runs_pytest(doc, tmp_path, exists, tracked) is True


    # Named by a command that is NOT a shell, so the shell-aware reader
    # contributes nothing and only the requeue can carry it.
    exists2, tracked2 = _wrapper_tree(
        tmp_path / "by_data",
        {
            "scripts/outer.sh": "#!/bin/sh\ncd backend\ncat inner.sh\n",
            "backend/inner.sh": "#!/bin/sh\npython leaf.py\n",
            "backend/leaf.py": "import subprocess\n\nsubprocess.run(['pytest'])\n",
        },
    )
    assert _files_a_workflow_reaches(
        {"jobs": {"j": {"steps": [{"run": "sh scripts/outer.sh"}]}}},
        exists2, tmp_path / "by_data", tracked2,
    ) == {"scripts/outer.sh", "backend/inner.sh", "backend/leaf.py"}

def test_a_shell_wrapper_named_py_is_read_as_shell(tmp_path):
    """The file's `#!` decides, whoever invoked it.

    Round 6 answered this with a second channel — which interpreter the
    command used — and keeping that channel meant parsing the invoker's
    options, which produced a finding in each of rounds 7 and 8. The
    channel is gone; the shebang answers it.

    The paired half is the one that matters now, and it is asserted
    against a TRACKED file so it cannot pass vacuously: a `.py` with a
    Python shebang is walked as Python, so a path named in it is not a
    shell dependency. (The previous version of this case named an
    untracked file, and after the channel was removed it passed whether
    or not the rule held — green for the wrong reason.)
    """
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/wrapper.py": "#!/bin/sh\npython scripts/leaf.py\n",
            "scripts/leaf.py": "import subprocess\n\nsubprocess.run(['pytest'])\n",
        },
    )
    for command in ("bash scripts/wrapper.py", "env bash scripts/wrapper.py",
                    "bash -O extglob scripts/wrapper.py", "python scripts/wrapper.py"):
        doc = {"jobs": {"j": {"steps": [{"run": command}]}}}
        assert _files_a_workflow_reaches(doc, exists, tmp_path, tracked) == {
            "scripts/wrapper.py",
            "scripts/leaf.py",
        }, command
        assert _runs_pytest(doc, tmp_path, exists, tracked) is True, command

    # A `.py` that says it is Python is not read as commands, so the
    # page it merely mentions is not a dependency.
    exists, tracked = _wrapper_tree(
        tmp_path,
        {
            "scripts/plain.py": "#!/usr/bin/env python3\ncat docs/aside.md\n",
            "docs/aside.md": "prose\n",
        },
    )
    plain = {"jobs": {"j": {"steps": [{"run": "bash scripts/plain.py"}]}}}
    assert _files_a_workflow_reaches(plain, exists, tmp_path, tracked) == {
        "scripts/plain.py"
    }


def test_prose_a_workflow_reaches_is_not_a_pytest_invocation(tmp_path):
    """A page cannot run anything.

    Scanning every reached file for the token made `librerun-smoke` a
    pytest workflow, because `demo.sh` names the install page and that
    page says "`pytest` reads the same…". It cost nothing there — that
    workflow has no `paths:` filter, so it can have no blind spot — but
    a census with a false member is still wrong about its members.

    The fixture uses names that are NOT in this repository, and that is
    load-bearing rather than tidy. Existence in the tree is the
    discriminator the dependency walker uses, and it reads path-shaped
    STRINGS out of any file the suite reaches — including this one. The
    first version of this case spelled the real `scripts/demo.sh` and
    `docs/Install.md`, and those two strings, sitting in a test module
    the backend suite imports, promptly became files that
    `unit-suites` and `chassis-zero-agents` "reach" and do not select:
    two failures in the neighbouring guard, invented entirely by a
    fixture.
    """
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "scripts" / "probe_wrapper.sh").write_text(
        "#!/bin/sh\ncat docs/Probe_Page.md\n"
    )
    (tmp_path / "docs" / "Probe_Page.md").write_text(
        "Turn it off before running the test suite: `pytest` reads the same file.\n"
    )
    doc = {"jobs": {"j": {"steps": [{"run": "bash scripts/probe_wrapper.sh"}]}}}
    tracked = {"scripts/probe_wrapper.sh", "docs/Probe_Page.md"}
    exists = tracked.__contains__

    # Reached — the dependency rule should still demand it in the filter…
    assert "docs/Probe_Page.md" in _files_a_workflow_reaches(
        doc, exists, tmp_path, tracked
    )
    # …and it is still not a workflow that runs pytest.
    assert _runs_pytest(doc, tmp_path, exists, tracked) is False


def test_a_workflow_blind_to_a_root_pytest_config_is_caught(tmp_path):
    """Injection: take the paths back out and the rule must report it.

    Driven through ``_root_config_blind_spots`` — the same predicate the
    guard above calls, not a restatement of it — over the REAL
    unit-suites.yml with the eight paths stripped. Asserting on a
    hand-built fixture would prove the fixture; asserting on real
    content minus the fix proves the guard would have caught the tree as
    it stood before this commit.
    """
    filenames = _pytest_config_filenames()
    source = (_workflow_dir() / "unit-suites.yml").read_text()

    intact = _load_workflow(_workflow_dir() / "unit-suites.yml")
    assert _runs_pytest(intact), "unit-suites.yml stopped running pytest"
    assert not _root_config_blind_spots(intact, filenames), (
        "unit-suites.yml is expected to be covered before the injection"
    )

    stripped = "\n".join(
        line
        for line in source.splitlines()
        if line.strip() not in {f'- "{name}"' for name in filenames}
    )
    removed = len(source.splitlines()) - len(stripped.splitlines())
    assert removed == 2 * len(filenames), (
        f"the injection removed {removed} lines, not {2 * len(filenames)} — "
        f"it is not reproducing the defect it claims to"
    )

    injected = tmp_path / "unit-suites.yml"
    injected.write_text(stripped)
    blind = _root_config_blind_spots(_load_workflow(injected), filenames)
    assert set(blind) == {"push", "pull_request"}, blind
    for half, missed in blind.items():
        assert set(missed) == set(filenames), (half, missed)


def _python_m_invocations(doc, exists):
    """Every ``python -m MODULE`` a workflow runs, with its directory.

    ``[(working_directory, top_level_module), ...]``. Only the FIRST
    dotted component matters: ``-m adapter_kit.run_contract`` is captured
    by an ``adapter_kit`` module or package, not by anything named
    ``run_contract``.

    This reader asked its OWN narrower question until round 24 -- and
    that is the finding, not the shapes. `test_a_python_entry_is_what_
    the_interpreter_runs` had named it since round 21 as one of two
    readers reading ``words[0]`` against a literal
    ``("python", "python3")``; round 23 swept the twin named beside it
    and left this one, so a limitation written down stayed open while
    the sentence recording it went stale. Measured before the fix:

    * **14 of 15 command shapes uncounted** -- every one the twin now
      counts. A leading assignment, ``env``, a brace group, ``then``,
      ``do``, ``!``, ``time`` and its options, ``function`` and
      ``coproc``. All false NEGATIVES: the shadow guard below simply
      protected nothing for that step.
    * **3 shapes invented** -- ``-m`` was read wherever it appeared, so
      ``python s.py -m victim`` reported a module the interpreter never
      ran. Measured against the real interpreter: it prints
      ``SCRIPT ran, argv= ['-m', 'victim']``. Round 19 fixed exactly
      this in ``_python_entry`` and this reader was not swept then
      either.
    * ``python3.12`` uncounted, the spelling half of the same note.

    So it asks the three helpers the other readers ask, and answers
    none of it itself. ``exists`` is threaded through because
    ``_python_entry`` needs it to tell a file operand from an option's
    value -- all three callers already had it.
    """
    found = []
    for working_directory, text, _pythonpath in _run_steps(doc):
        for words in _shell_invocations(text):
            command = _the_command(words)
            if not _is_the_python_interpreter(command):
                continue
            kind, module = _python_entry(command, exists, working_directory)
            if kind != "module" or not module:
                continue
            found.append(((working_directory or "").strip("/"),
                          module.split(".")[0]))
    return found


def _module_shadow_gaps(doc, exists):
    """Which ``python -m`` invocations a filter cannot protect.

    ``{half: [path, ...]}``. ``python -m`` puts the working directory at
    the FRONT of ``sys.path``, ahead of site-packages, so committing
    ``<dir>/<module>.py`` — or ``<dir>/<module>/`` with a ``__main__.py``
    — replaces the program the step meant to run.

    An invocation whose module ALREADY resolves inside its own working
    directory is skipped, and that is measured rather than assumed: with
    ``backend/adapter_kit/`` present, adding ``backend/adapter_kit.py``
    beside it changes nothing, because a package beats a module of the
    same name on the same path entry. The job's own code is not a shadow
    of itself.
    """
    gaps = {}
    for working_directory, module in _python_m_invocations(doc, exists):
        prefix = f"{working_directory}/" if working_directory else ""
        as_module = f"{prefix}{module}.py"
        in_package = f"{prefix}{module}/__init__.py"
        if exists(as_module) or exists(in_package):
            continue
        for half, config in _workflow_triggers(doc).items():
            if not isinstance(config, dict) or not config.get("paths"):
                continue
            missed = [p for p in (as_module, in_package)
                      if not _filter_selects(config["paths"], p)]
            if missed:
                gaps.setdefault(half, []).extend(missed)
    return {half: sorted(set(paths)) for half, paths in gaps.items()}


def test_no_workflow_can_be_captured_by_a_module_added_beside_it():
    """`python -m` trusts the working directory before site-packages.

    The companion to the root-configuration rule above, and the
    interesting difference is that THIS one is derivable. There, the
    hazard was a file nothing in the tree named, so only a listed path
    could reach it. Here the command names its own module: ``python -m
    pytest`` says ``pytest``, so the path whose addition would capture it
    can be computed, and a `python -m` added in some future workflow is
    covered the day it lands rather than the day someone remembers.

    Measured, from the repository root, against the gateway job's own
    command:

        no shadow                            rc=0  collected=1253
        root pytest.py                       rc=0  collected=0
        root pytest/ with no __main__.py     rc=1  collected=0
        root pytest/ with a __main__.py      rc=0  collected=0

    A green job that collected nothing, which is this batch's whole
    subject. The bare package is the one safe shape — it fails loudly —
    but it is one file away from the dangerous one, so the package path
    is watched too rather than reasoning about which half of a directory
    is harmless.

    Codex round 15 reported `pytest` in the gateway job. Censusing the
    question rather than the instance found `spacy` there as well, one
    step above it and the same mechanism.
    """
    tracked = _tracked_paths(_workflow_dir().parents[1], directories=False)
    exists = tracked.__contains__ if tracked is not None else (lambda _p: False)
    checked, gaps = [], {}
    for path in _workflow_files(_workflow_dir()):
        doc = _load_workflow(path)
        invocations = _python_m_invocations(doc, exists)
        if not invocations:
            continue
        checked.append(path.name)
        found = _module_shadow_gaps(doc, exists)
        if found:
            gaps[path.name] = found

    assert checked, (
        "no workflow was found to run `python -m`, so this guard checked "
        "nothing"
    )
    assert not gaps, (
        f"a file committed at these paths would capture a `python -m` step "
        f"and the workflow would not run on the commit that added it: "
        f"{gaps}. `python -m` searches the working directory before "
        f"site-packages. Add the paths to the filter half, or run the step "
        f"from a directory the repository root cannot shadow."
    )


def test_a_module_shadow_gap_is_caught_and_an_owned_package_is_not(tmp_path):
    """Injection, and the false alarm the rule must NOT raise.

    Two directions, because a rule that flags everything is as useless as
    one that flags nothing. Driven through `_module_shadow_gaps`, the
    same predicate the guard calls.
    """
    source = (_workflow_dir() / "gateway.yml").read_text()
    tracked = _tracked_paths(_workflow_dir().parents[1], directories=False)
    exists = tracked.__contains__

    intact = _load_workflow(_workflow_dir() / "gateway.yml")
    assert not _module_shadow_gaps(intact, exists), (
        "gateway.yml is expected to be covered before the injection"
    )

    # Only spacy: the suite step uses the `pytest` console script now, so
    # `python -m pytest` is gone from this workflow and the rule correctly
    # stops demanding a path for it. A guard that kept demanding it would
    # be asserting a list rather than deriving one.
    shadows = {'- "spacy.py"', '- "spacy/**"'}
    stripped = "\n".join(
        line for line in source.splitlines() if line.strip() not in shadows
    )
    removed = len(source.splitlines()) - len(stripped.splitlines())
    assert removed == 2 * len(shadows), (
        f"the injection removed {removed} lines, not {2 * len(shadows)} — it "
        f"is not reproducing the defect it claims to"
    )
    injected = tmp_path / "gateway.yml"
    injected.write_text(stripped)
    gaps = _module_shadow_gaps(_load_workflow(injected), exists)
    assert set(gaps) == {"push", "pull_request"}, gaps
    for half, missed in gaps.items():
        assert set(missed) == {"spacy.py", "spacy/__init__.py"}, (half, missed)

    # And the direction that must stay quiet. container-battery runs
    # `python -m adapter_kit.run_contract` from backend/, where
    # backend/adapter_kit/ already exists. A sibling backend/adapter_kit.py
    # cannot capture it — a package wins over a module on the same path
    # entry — so demanding a filter path for it would be a false alarm on
    # real repository content, which is the one thing worse than a miss.
    battery = _load_workflow(_workflow_dir() / "container-battery.yml")
    assert ("backend", "adapter_kit") in _python_m_invocations(battery, exists), (
        "container-battery stopped running adapter_kit, so this case no "
        "longer proves anything"
    )
    assert not _module_shadow_gaps(battery, exists), (
        "an owned package was reported as a shadow gap"
    )


# The shared assertion every suite-running job ends with. Named once:
# `_suite_ran_assertions` matches it as a whole BASENAME rather than as
# a suffix, because `endswith` also accepted `my_assert_suite_ran.py`,
# a different program.
_SUITE_ASSERTION = "assert_suite_ran.py"


def _pytest_runs(doc):
    """Every pytest invocation, as ``(working_directory, report)``.

    ``report`` is the ``--junitxml`` value, or ``None`` when the command
    writes no machine-readable report at all — in which case nothing
    downstream can check what it did.
    """
    runs = []
    for working_directory, text, _pythonpath in _run_steps(doc):
        for words in _shell_invocations(text):
            # THE COMMAND, not `words[0]`. Round 17 established that a
            # line may open with assignments or `env` and taught the
            # shell mark and the entry scan to ask; this reader was
            # never swept, so `MODE=test python -m pytest x.py` and
            # `env python -m pytest x.py` were not pytest runs as far
            # as the census was concerned -- nine of ten shapes missed,
            # measured (Codex round 23 reported the tenth).
            words = _the_command(words)
            if not words:
                continue
            # ...and then the SAME question `_runs_alembic` asks, asked
            # the same way. This reader had three rules of its own --
            # the literal word `pytest` somewhere in `words[:3]`, and
            # `words[0]` against the literal set
            # `("python", "python3", "pytest")` -- and each of the three
            # was a way of missing a real pytest run, which for a gate
            # against suites that report success by not looking is the
            # dangerous direction. Measured: nine of sixteen command
            # shapes uncounted. `python3.12` and `pythonw` failed the
            # name set; `python -mpytest` produces no word spelled
            # `pytest`; and `python -W ignore -m pytest` puts `pytest`
            # at index four, outside the three-word window. Codex round
            # 25 reported the name set. The other two came from asking
            # what else in this function was answering a question the
            # module had already answered elsewhere.
            if words[0].rsplit("/", 1)[-1] != "pytest":
                if not _is_the_python_interpreter(words):
                    continue
                what, position, _after = _python_own_words_end(words)
                if what != "module":
                    continue
                module = _the_module(words, position) or ""
                if module.split(".")[0] != "pytest":
                    continue
            # ...and the report is read through the SAME pair the option
            # table holds, in both the joined and separate spellings.
            report = None
            for i, word in enumerate(words):
                for option in _JUNIT_REPORT_OPTIONS:
                    if word.startswith(option + "="):
                        report = word.split("=", 1)[1]
                    elif word == option and i + 1 < len(words):
                        report = words[i + 1]
            runs.append(((working_directory or "").strip("/"), report))
    return runs


def _suite_ran_assertions(doc):
    r"""Every report name handed to ``scripts/assert_suite_ran.py``.

    Handed to it, not merely named beside it. This scanned every word
    of every invocation for one ending in the script's name and took
    the word after it, so `echo scripts/assert_suite_ran.py r.xml`,
    `cat scripts/assert_suite_ran.py r.xml`, `git add
    scripts/assert_suite_ran.py r.xml` and even `python -m pytest
    scripts/assert_suite_ran.py r.xml` all marked `r.xml` asserted --
    four of ten shapes, measured, every one a FALSE POSITIVE that lets
    a real pytest step drop out of `_unchecked_pytest_runs`. A gate
    that a mention can satisfy is the failure this one exists to catch
    (Codex round 25).

    Round 17's rule is the answer and it was never applied here: the
    program has to BE the command. That is two shapes,

        scripts/assert_suite_ran.py r.xml 1 backend
        python3 ../scripts/assert_suite_ran.py r.xml 1 backend

    and the second needs the interpreter's option grammar to know which
    word is the script -- `python -W ignore ../scripts/... r.xml` is a
    real invocation. That grammar is `_python_own_words_end`, asked
    rather than re-implemented here, because a second scan answering
    the same question its own way is exactly the defect this round is
    about.
    """
    asserted = set()
    for working_directory, text, _pythonpath in _run_steps(doc):
        for words in _shell_invocations(text):
            words = _the_command(words)
            if not words:
                continue
            if words[0].rsplit("/", 1)[-1] == _SUITE_ASSERTION:
                report_at = 1
            elif _is_the_python_interpreter(words):
                what, position, after = _python_own_words_end(words)
                if what != "operand":
                    continue
                if words[position].rsplit("/", 1)[-1] != _SUITE_ASSERTION:
                    continue
                report_at = after
            else:
                continue
            if report_at < len(words):
                asserted.add(
                    ((working_directory or "").strip("/"), words[report_at]))
    return asserted


def _unchecked_pytest_runs(doc):
    """Which pytest invocations nobody checks the outcome of."""
    asserted = _suite_ran_assertions(doc)
    unchecked = []
    for working_directory, report in _pytest_runs(doc):
        if report is None:
            unchecked.append((working_directory, "writes no --junitxml report"))
        elif (working_directory, report) not in asserted:
            unchecked.append((working_directory, f"{report} is never asserted"))
    return unchecked


def _a_workflow_running(*runs):
    """A workflow whose steps run `runs`, in `backend`.

    One builder for every case below, including the two-step ones: a
    second `yaml.safe_load` beside it would be a second answer to
    "what does a workflow look like here", which is the shape of the
    defect this round is about.
    """
    import yaml
    steps = "".join("      - run: %r\n" % (run,) for run in runs)
    return yaml.safe_load(
        "jobs:\n"
        "  j:\n"
        "    defaults:\n"
        "      run:\n"
        "        working-directory: backend\n"
        "    steps:\n" + steps
    )


def test_the_pytest_census_asks_what_runs_not_what_a_word_spells():
    r"""`python3.12 -m pytest` is a pytest run.

    This census had three rules of its OWN where the module already had
    one answer: the literal word `pytest` somewhere in `words[:3]`, and
    `words[0]` against the literal set `("python", "python3",
    "pytest")`. Each was a way of MISSING a run, and for a gate whose
    whole premise is that a suite can report success by not looking,
    missing one is the dangerous direction -- the census returns
    nothing, `test_every_pytest_job_checks_that_its_suite_actually_ran`
    sees no pytest in the workflow, and the job needs no assertion at
    all.

    Nine of the sixteen shapes below were uncounted, measured before
    the fix. Codex round 25 reported the name set; the window and the
    joined spelling came from asking what ELSE in this function was
    answering a question `_runs_alembic` and `_python_m_invocations`
    already ask one helper. That is the same finding as round 24's and
    round 23's, one function further along: a rule applied where the
    report pointed and not everywhere it is true.

    The cases are named for the PROPERTY -- does this line run pytest
    -- so they survive the next change to how that is recognised.
    """
    counts = [
        ("pytest --junitxml=r.xml", True),
        ("python -m pytest --junitxml=r.xml", True),
        ("python3 -m pytest --junitxml=r.xml", True),
        # The name set. `python --help` is not a fixed list of names:
        # a version-suffixed interpreter is the normal spelling in CI.
        ("python3.12 -m pytest --junitxml=r.xml", True),
        ("python3.13 -m pytest --junitxml=r.xml", True),
        ("pythonw -m pytest --junitxml=r.xml", True),
        ("/usr/bin/python3.12 -m pytest --junitxml=r.xml", True),
        # The joined spelling, which produces no word spelled `pytest`.
        ("python -mpytest --junitxml=r.xml", True),
        ("python3.12 -mpytest --junitxml=r.xml", True),
        # ...and the three-word window, which `-W ignore` pushes past.
        ("python -W ignore -m pytest --junitxml=r.xml", True),
        # Round 17's prefixes, which `_the_command` strips.
        ("MODE=t python3.12 -m pytest --junitxml=r.xml", True),
        ("env python3.12 -m pytest --junitxml=r.xml", True),
        ("/usr/local/bin/pytest --junitxml=r.xml", True),
        # And what must NOT count, or the gate accuses a healthy
        # workflow: the interpreter still has to BE the command, and a
        # module whose name merely starts with pytest is not pytest.
        ("echo -m pytest --junitxml=r.xml", False),
        ("cat notes -m pytest", False),
        ("python -m pytest_cov --junitxml=r.xml", False),
    ]
    wrong = [
        (line, want) for line, want in counts
        if bool(_pytest_runs(_a_workflow_running(line))) != want
    ]
    assert not wrong, f"the pytest census reads these wrongly: {wrong}"

    # And the consequence, end to end: a real run nobody checks.
    assert _unchecked_pytest_runs(_a_workflow_running(
        "python3.12 -m pytest --junitxml=r.xml"
    )) == [("backend", "r.xml is never asserted")]


def test_the_report_is_read_in_both_of_pytests_spellings():
    r"""`--junit-xml=r.xml` names a report as surely as `--junitxml=`.

    Measured against the installed pytest before the fix: all of
    `--junitxml=a.xml`, `--junit-xml=b.xml` and `--junit-xml c.xml`
    write their file, and this reader knew only the first spelling. So
    a workflow using the documented `--junit-xml=` form was reported as
    writing no machine-readable report, and `_unchecked_pytest_runs`
    accused a job that checks itself properly -- a FALSE accusation
    against a healthy workflow, which is the other way this guard can
    be wrong (Codex round 26).

    The fix is not another literal. `_PYTEST_OPTIONS_TAKING_A_VALUE`
    already held both spellings, forty lines above the hand-rolled scan
    that held one; they share `_JUNIT_REPORT_OPTIONS` now.
    """
    shapes = [
        ("python -m pytest --junitxml=r.xml",  "r.xml"),
        ("python -m pytest --junitxml r.xml",  "r.xml"),
        ("python -m pytest --junit-xml=r.xml", "r.xml"),
        ("python -m pytest --junit-xml r.xml", "r.xml"),
        # ...and no report is still no report.
        ("python -m pytest -q", None),
    ]
    wrong = [
        (line, want) for line, want in shapes
        if _pytest_runs(_a_workflow_running(line))[0][1] != want
    ]
    assert not wrong, f"the report is read wrongly from: {wrong}"

    # The consequence: a job that writes and checks its report the
    # documented way is not reported unchecked.
    assert _unchecked_pytest_runs(_a_workflow_running(
        "python -m pytest --junit-xml=r.xml",
        "python3 ../scripts/assert_suite_ran.py r.xml 1 backend",
    )) == []

    # ...and the option table skips the value of BOTH, or the separate
    # form's path would be read as a test target.
    for option in _JUNIT_REPORT_OPTIONS:
        assert option in _PYTEST_OPTIONS_TAKING_A_VALUE


def test_the_junit_report_options_are_pytests_own():
    """The pair is DERIVED from the installed pytest, not remembered.

    A hand-kept spelling list is what this module keeps finding wrong,
    so the same treatment bash's reserved words and FastAPI's route
    decorators get: ask the real thing, and fail when it stops
    matching. pytest registers both names on one option, so its parser
    is the authority rather than `--help`, which prints only the longer
    spelling.
    """
    from _pytest.config.argparsing import Parser
    import _pytest.junitxml

    parser = Parser(_ispytest=True)
    _pytest.junitxml.pytest_addoption(parser)
    registered = None
    for group in parser._groups:
        for option in group.options:
            names = list(option.names())
            if "--junitxml" in names or "--junit-xml" in names:
                registered = names
    assert registered, (
        "the installed pytest registers no --junitxml option at all; this "
        "derivation is reading the wrong thing, so the pair below cannot "
        "be trusted either"
    )
    assert set(_JUNIT_REPORT_OPTIONS) == set(registered), (
        f"pytest registers {sorted(registered)} for its JUnit report but "
        f"this module carries {sorted(_JUNIT_REPORT_OPTIONS)}. A spelling "
        f"it does not know is a report this census cannot see."
    )


def test_an_authoring_scan_that_finds_no_pages_is_not_a_pass(tmp_path):
    r"""A missing `docs/authoring/` must not read as "every page is fine".

    `_workflow_files` has carried this invariant in the COLLECTOR since
    round 20. This scan was written later, did not adopt it, and used a
    silent api besides: measured, `Path.iterdir()` raises on a directory
    that is not there while `Path.glob()` returns nothing, exactly as a
    real-but-empty one does. So renaming the directory left
    `test_the_authoring_docs_never_name_a_file_that_does_not_exist`
    looping over zero pages and asserting `not missing` -- green, having
    looked at nothing.

    Codex round 26 reported this shape in the checker's own scan root;
    the survey found this second one.
    """
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[2]

    # A fixture's tree may legitimately be empty -- that is what the
    # negative tests below build.
    assert _doc_files(tmp_path) == []

    # The refusal is driven through `_missing_doc_paths`, the CONSUMER,
    # and not through the collector alone. Asserting on `_doc_files`
    # here passed while an injection made the consumer glob directly
    # again -- the case testing the part rather than the wiring, which
    # is how a guard keeps its shape and loses its effect. The injection
    # `doc_scan_has_no_collector.py` is what said so.
    import unittest.mock
    with unittest.mock.patch(f"{__name__}._authoring_dir", lambda: tmp_path):
        with pytest.raises(AssertionError, match="no authoring pages"):
            _missing_doc_paths(tmp_path, repo_root)

    # And on the real tree it finds pages, or the guard above has been
    # passing vacuously all along.
    assert _doc_files(_authoring_dir()), "no authoring pages in the real tree"


def test_a_mention_of_the_assertion_script_is_not_an_assertion():
    r"""`echo scripts/assert_suite_ran.py r.xml` asserts nothing.

    The reader took ANY word ending in the script's name and read the
    next word as the report it checks, so four of the ten shapes below
    marked `r.xml` asserted while nothing ran -- and each one is a
    FALSE POSITIVE, which here means a real pytest step drops out of
    `_unchecked_pytest_runs` and its job goes green unchecked. A gate a
    mention can satisfy is the failure this gate exists to catch
    (Codex round 25).

    Round 17's rule is the fix and it was never applied here: the
    program has to BE the command, either directly or as the
    interpreter's entry. The second shape needs the option grammar,
    and it ASKS for it rather than scanning again -- `_python_own_words_end`
    is the one place that knows `python --help`'s syntax.
    """
    mentions = [
        ("../scripts/assert_suite_ran.py r.xml 1 backend", True),
        ("python3 ../scripts/assert_suite_ran.py r.xml 1 backend", True),
        ("python3.12 ../scripts/assert_suite_ran.py r.xml 1 backend", True),
        ("python -W ignore ../scripts/assert_suite_ran.py r.xml 1 b", True),
        ("MODE=t python3 ../scripts/assert_suite_ran.py r.xml 1 b", True),
        # ...and every way of naming it without running it.
        ("echo ../scripts/assert_suite_ran.py r.xml", False),
        ("cat scripts/assert_suite_ran.py r.xml", False),
        ("git add scripts/assert_suite_ran.py r.xml", False),
        ("python -m pytest scripts/assert_suite_ran.py r.xml", False),
        ("echo 'see scripts/assert_suite_ran.py r.xml for the gate'", False),
    ]
    wrong = [
        (line, want) for line, want in mentions
        if (("backend", "r.xml")
            in _suite_ran_assertions(_a_workflow_running(line))) != want
    ]
    assert not wrong, f"these mentions are read wrongly: {wrong}"

    # The consequence, end to end: a real run whose only "check" is an
    # `echo` is unchecked.
    assert _unchecked_pytest_runs(_a_workflow_running(
        "python -m pytest --junitxml=r.xml",
        "echo ../scripts/assert_suite_ran.py r.xml 1 backend",
    )) == [("backend", "r.xml is never asserted")]


def test_one_grammar_answers_where_pythons_own_words_end():
    r"""Three readers, one scan of `python --help`'s syntax.

    `_python_entry_at`, `_pytest_runs` and `_suite_ran_assertions` all
    need to know which word of a Python invocation is the program.
    Rounds 16, 17, 19, 21, 23, 24 and 25 are all the same finding --
    a second caller answering a question its own way is how the first
    one drifts -- so this pins that there is ONE scan and that the two
    questions asked of it agree.
    """
    exists = {"x.py"}.__contains__

    # The grammar itself, with no filesystem: where do python's own
    # words stop and what stops them?
    assert _python_own_words_end(["python", "-m", "pytest", "t"]) == (
        "module", 1, 3)
    assert _python_own_words_end(["python", "-mpytest", "t"]) == (
        "module", 1, 2)
    assert _python_own_words_end(["python", "-c", "print(1)"]) == (
        "command", 1, 3)
    assert _python_own_words_end(["python", "-cprint(1)"]) == (
        "command", 1, 2)
    assert _python_own_words_end(["python", "-W", "ignore", "s.py", "a"]) == (
        "operand", 3, 4)
    assert _python_own_words_end(["python", "-v"]) == ("none", 2, 2)

    # `_the_module` reads BOTH spellings, in one place, or
    # `_pytest_runs` would need its own.
    assert _the_module(["python", "-m", "pytest"], 1) == "pytest"
    assert _the_module(["python", "-mpytest"], 1) == "pytest"
    assert _the_module(["python", "-m"], 1) is None

    # `_python_entry_at` is that scan plus ONE policy of its own: an
    # operand that names no file it can see is treated as some
    # option's value and the scan goes on, which is what makes an
    # incomplete option table harmless rather than wrong (round 19).
    assert _python_entry_at(["python", "x.py", "-m", "pytest"],
                            exists, "") == ("file", "x.py", 2)
    assert _python_entry_at(["python", "nosuch", "-m", "pytest"],
                            exists, "") == ("module", "pytest", 4)
    # ...and it gets the JOINED spellings for free, because it does not
    # carry its own copy of the scan. A copy is only ever one round
    # behind: round 24 taught the shared scan `-mMOD` and `-cCMD`, and a
    # second scan would have to be taught again.
    assert _python_entry_at(["python", "-mpytest", "t"],
                            exists, "") == ("module", "pytest", 2)
    assert _python_entry_at(["python", "-cprint(1)", "x.py"],
                            exists, "") == ("command", None, 2)
    # ...and the grammar alone, without that policy, stops at the word.
    assert _python_own_words_end(["python", "nosuch", "-m", "pytest"]) == (
        "operand", 1, 2)

    # The two questions the other readers ask, answered off the same
    # scan rather than off a name.
    assert bool(_pytest_runs(_a_workflow_running(
        "python3.12 -W ignore -mpytest --junitxml=r.xml"))) is True
    assert _suite_ran_assertions(_a_workflow_running(
        "python3.12 -W ignore ../scripts/assert_suite_ran.py r.xml 1 b"
    )) == {("backend", "r.xml")}


def test_every_pytest_job_checks_that_its_suite_actually_ran():
    """An exit code cannot tell a green run from a run that did nothing.

    This is the batch's whole premise, and censusing it found the premise
    unapplied in five of the six jobs that run pytest. Only unit-suites
    asserted anything; adapter-battery wrote a report and then carried
    its OWN inline copy of the rule, the weaker `tests == 0` version that
    `scripts/assert_suite_ran.py` exists to replace; the other four
    trusted the exit code alone.

    What that costs is not hypothetical — it is the round-16 finding.
    Watching a path only decides whether a job RUNS. A root `pytest.py`
    still captured `python -m pytest` once it did, and the job went green
    having collected nothing:

        python -m pytest, shadow present   rc=0  collected=0
        python -m pytest, no shadow        rc=0  collected=1253

    A filter cannot see that. A floored count can, and so can every other
    way a suite quietly shrinks — a narrowed `testpaths`, a deleted
    directory, a dependency that stopped installing. One rule, one
    implementation, one place to strengthen it.
    """
    checked, unchecked = [], {}
    for path in _workflow_files(_workflow_dir()):
        doc = _load_workflow(path)
        if not _pytest_runs(doc):
            continue
        checked.append(path.name)
        problems = _unchecked_pytest_runs(doc)
        if problems:
            unchecked[path.name] = problems

    assert checked, "no workflow was found to run pytest, so this guard checked nothing"
    assert not unchecked, (
        f"these pytest invocations have nothing checking that they ran: "
        f"{unchecked}. Add --junitxml=<report> to the command and a step "
        f"calling scripts/assert_suite_ran.py against it at a baseline "
        f"measured from the job's own CI run. Do not write a second copy "
        f"of the rule — the weaker one is what survives."
    )


def test_an_unchecked_pytest_run_is_caught(tmp_path):
    """Injection, both shapes, driven through the same predicate.

    A command with no report at all, and a command whose report nobody
    asserts. Both are ways for a job to run a suite and learn nothing,
    and the second is the one that looks fine in review.
    """
    real = _load_workflow(_workflow_dir() / "database-parity.yml")
    assert _pytest_runs(real), "database-parity stopped running pytest"
    assert not _unchecked_pytest_runs(real), (
        "database-parity is expected to be covered before the injection"
    )

    source = (_workflow_dir() / "database-parity.yml").read_text()

    # (1) strip the --junitxml flags: the commands run, nothing is written.
    no_report = source.replace(" --junitxml=h6-suite.xml", "")
    assert no_report != source
    stripped = tmp_path / "no-report.yml"
    stripped.write_text(no_report)
    problems = _unchecked_pytest_runs(_load_workflow(stripped))
    assert any("writes no --junitxml report" in why for _wd, why in problems), problems

    # (2) keep the report, drop the assertion step that reads it. This is
    # the shape that passes review: the command still looks thorough.
    lines = source.splitlines()
    kept = [
        line for line in lines
        if "assert_suite_ran.py h6-suite.xml" not in line
    ]
    assert len(kept) == len(lines) - 1, "the injection removed the wrong number of lines"
    unasserted = tmp_path / "unasserted.yml"
    unasserted.write_text("\n".join(kept))
    problems = _unchecked_pytest_runs(_load_workflow(unasserted))
    assert any("h6-suite.xml is never asserted" in why for _wd, why in problems), problems


_VERIFIER = "scripts/assert_suite_ran.py"


def _run_verifier(report, baseline, label="probe"):
    """Run the shared verifier the way CI runs it: as a subprocess.

    A subprocess and not an import, deliberately. The failure this exists
    to catch is the script becoming a successful NO-OP — deleting the
    ``main(sys.argv)`` call is enough — and an import-and-call test would
    sail straight past that by calling ``main`` itself. What CI executes
    is ``python3 …/assert_suite_ran.py REPORT BASELINE LABEL``, so that
    is what gets tested.
    """
    import subprocess
    import sys as _sys

    script = _workflow_dir().parents[1] / _VERIFIER
    proc = subprocess.run(
        [_sys.executable, str(script), str(report), str(baseline), label],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_the_shared_suite_verifier_actually_verifies(tmp_path):
    """Nine jobs call one script. Nobody was checking it does anything.

    This batch ends with every pytest invocation in CI handing its report
    to ``scripts/assert_suite_ran.py``. The guard above proves they all
    CALL it. That is the wiring, not the function — and the wiring being
    right is worth nothing if the thing on the end of it returns success
    unconditionally. Measured, before this test existed: changing

        if __name__ == "__main__":
            main(sys.argv)

    to ``pass`` makes a report of one test with one skip and three
    failures exit **0**, every baseline and zero-skip rule in the
    repository silently stops applying — and the whole guard suite is
    still ``135 passed`` (Codex round 18).

    So the verifier is exercised here against reports built to fail it,
    each required to come back non-zero. A check that certifies itself by
    returning success is the exact thing this batch was opened to remove;
    it would have been an unpleasant irony to leave that shape at the
    root of the tree of checks.
    """
    def report(name, body):
        path = tmp_path / name
        path.write_text(body)
        return path

    def suite(tests, cases=None, **attrs):
        """A report that RECORDS the cases it declares.

        The fixtures here used to be summary-only — `<testsuite
        tests="10"/>` with no `<testcase>` children — and that is exactly
        the shape the verifier now refuses, because a producer can
        manufacture the number without running anything (Codex round 19).
        Writing the fixtures that way is how I failed to notice: the
        good-path case asserted that a hollow report PASSES.

        `cases` defaults to `tests` so every fixture takes the path its
        own case is about, rather than tripping the records rule first
        and testing nothing it claims to test.
        """
        if cases is None:
            cases = tests
        declared = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        bodies = "".join(
            f'<testcase classname="c" name="t{i}"/>' for i in range(cases)
        )
        return (
            f'<testsuites><testsuite tests="{tests}" {declared}>'
            f"{bodies}</testsuite></testsuites>"
        )

    good = report("good.xml", suite(10, skipped=0, errors=0, failures=0))
    code, out = _run_verifier(good, 10)
    assert code == 0, f"a clean report at its baseline must pass: {out}"
    assert "tests=10" in out and "recorded=10" in out, out

    # The round-19 shape: a report that DECLARES the baseline and records
    # nothing. It satisfied every other rule in the script.
    hollow = report("hollow.xml", suite(10, cases=0, skipped=0, errors=0, failures=0))
    code, out = _run_verifier(hollow, 10)
    assert code != 0, f"a report with no <testcase> records was accepted: {out}"
    assert "records 0 <testcase>" in out, out

    # Every shape that must be refused. The point of the table is that
    # each one fails for its OWN reason — a verifier that returns 1
    # unconditionally would satisfy a single case and is not what is
    # being asked for, which is why the messages are checked too.
    refusals = [
        (
            "below the baseline",
            report("low.xml", suite(9, skipped=0)),
            10,
            "LOWER",
        ),
        (
            "a skipped test",
            report("skip.xml", suite(10, skipped=1)),
            10,
            "skipped",
        ),
        (
            "a failure",
            report("fail.xml", suite(10, failures=1)),
            10,
            "failure",
        ),
        (
            "a collection error",
            report("err.xml", suite(10, errors=1)),
            10,
            "error",
        ),
        (
            "no testsuite element",
            report("empty.xml", "<testsuites/>"),
            10,
            "produced nothing",
        ),
        (
            "not XML at all",
            report("junk.xml", "this is not a report"),
            10,
            "cannot read",
        ),
        ("a report that was never written", tmp_path / "absent.xml", 10, "cannot read"),
    ]
    for name, path, baseline, expected in refusals:
        code, out = _run_verifier(path, baseline)
        assert code != 0, (
            f"the verifier accepted {name} — it is not verifying anything: {out}"
        )
        assert expected in out, (
            f"the refusal of {name} does not say why ({expected!r} absent): {out}"
        )

    # The floor's diagnostic must point the RIGHT way. Round 2's finding:
    # a message telling an author to RAISE the baseline when the count
    # fell would talk them into deleting the gate.
    code, out = _run_verifier(refusals[0][1], 10)
    assert "LOWER" in out and "RAISE" not in out, out


def test_a_neutered_verifier_is_caught(tmp_path):
    """Injection: switch the verifier off and the test above must fail.

    Driven through the real script on disk, copied and edited, rather
    than through a fixture — the defect is a one-line edit to a specific
    file and the proof should be that same edit.
    """
    import subprocess
    import sys as _sys

    source = (_workflow_dir().parents[1] / _VERIFIER).read_text()
    call = 'if __name__ == "__main__":\n    main(sys.argv)\n'
    assert source.count(call) == 1, (
        "the verifier no longer ends with the entry point this injection "
        "removes; update the injection rather than deleting it"
    )
    neutered = tmp_path / "assert_suite_ran.py"
    neutered.write_text(source.replace(call, 'if __name__ == "__main__":\n    pass\n'))

    bad = tmp_path / "bad.xml"
    bad.write_text('<testsuites><testsuite tests="1" skipped="1" failures="3"/></testsuites>')

    real = subprocess.run(
        [_sys.executable, str(_workflow_dir().parents[1] / _VERIFIER), str(bad), "10", "p"],
        capture_output=True, text=True,
    )
    assert real.returncode != 0, "the real verifier should refuse this report"

    off = subprocess.run(
        [_sys.executable, str(neutered), str(bad), "10", "p"], capture_output=True, text=True
    )
    assert off.returncode == 0, (
        "the injection did not actually neuter the verifier, so it proves "
        f"nothing: {off.stdout + off.stderr}"
    )


def test_the_meta_guard_is_not_silenced_by_a_guard_that_skips(monkeypatch):
    r"""A guard that skips must not take the meta-guard down with it.

    `test_no_workflow_guard_passes_on_an_empty_census` drives every
    census guard twice: once against the REAL tree to discover which
    ones look at the workflow directory, then against an empty one to
    prove none passes vacuously. The discovery pass caught only
    `Exception`, and pytest's outcome exceptions derive from
    `BaseException`, so a guard that skipped there escaped the handler
    and marked the whole meta-test skipped -- leaving every other
    census guard unchecked while the suite read green (Codex round 20).

    That is not hypothetical: the alembic census skips by design on a
    tree with no `backend/alembic.ini`, and removing that file turned
    the meta-test from `1 passed` into `1 skipped`. Measured before the
    fix, and measured at `b9fba09` too, where the same handler existed
    but NO guard could skip -- so round 19 is what made the latent hole
    reachable, and round 19 is where its twin one loop below was fixed
    while this one was not. The recurring finding of this work,
    committed inside the fix for it.

    This drives the meta-guard with a guard that looks and then skips.
    It must reach its own verdict -- reporting that guard by name --
    rather than being aborted, so the assertion is on the REPORT and
    not merely on "it did not skip".
    """
    import sys

    module = sys.modules[__name__]

    def test_zz_probe_that_skips():
        _workflow_dir()                      # it DID look
        pytest.skip("this tree has nothing for me to demand")

    monkeypatch.setitem(vars(module), "test_zz_probe_that_skips",
                        test_zz_probe_that_skips)

    # NOT `pytest.raises(AssertionError)`: under the defect the escaping
    # `Skipped` sails straight through that and marks THIS test skipped,
    # so the case would be silenced by the very thing it exists to
    # catch -- measured, it read `180 passed, 1 skipped` and the
    # injection went unreported. The escape has to be caught and turned
    # into a failure by hand.
    try:
        test_no_workflow_guard_passes_on_an_empty_census()
    except AssertionError as exc:
        report = str(exc)
    except BaseException as exc:                    # a skip, for instance
        pytest.fail(
            f"the meta-guard was ABORTED by {type(exc).__name__} instead "
            f"of reaching a verdict, so no census guard was checked: {exc}"
        )
    else:
        pytest.fail(
            "the meta-guard passed although a guard skipped on the empty "
            "census; it should have reported that guard by name"
        )

    assert "test_zz_probe_that_skips" in report, (
        f"the meta-guard ran but did not name the skipping guard: "
        f"{report[:200]}"
    )
    assert "SKIP" in report


def test_no_workflow_guard_passes_on_an_empty_census():
    """A guard that iterates the workflows must fail when there are none.

    The fifth link in the chain rounds 15-18 walked, and the one closest
    to home: every guard in this file that walks the real
    ``.github/workflows`` is a loop over a census, and a loop over an
    EMPTY census does nothing and reports success. Measured before this
    existed, with ``_workflow_dir()`` pointed at an empty directory: six
    guards tripped on non-empty assertions of their own, and **two passed
    — including the dependency guard thirteen review rounds went into**.
    It would have certified a repository whose CI had been deleted.

    This drives each of them against an empty directory and requires a
    failure. Behavioural, not a source-text check: a rule that looked for
    the word ``assert`` in the body would be satisfied by any assertion
    anywhere, which is the proxy mistake this batch has already made
    twice.

    Any exception counts, not only ``AssertionError`` — the guards that
    open a named workflow file raise ``FileNotFoundError`` on an empty
    directory, which is also a refusal to certify nothing.
    """
    import inspect
    import sys
    import tempfile
    from pathlib import Path

    module = sys.modules[__name__]

    def driveable(fn):
        """The arguments this probe can supply, or None.

        Coroutine functions are refused outright rather than called and
        dropped. Calling one returns a coroutine that never runs, so it
        would look like a test that never touched the census — the probe
        would silently enumerate LESS while reporting the same. (It did:
        94 `coroutine ... was never awaited` warnings were the tell.)
        There are 94 async tests in this module and none of them names
        `_workflow_dir`, so nothing is lost today; the assertion below
        keeps that true rather than assuming it.
        """
        if inspect.iscoroutinefunction(fn):
            return None
        kwargs = {}
        for parameter in inspect.signature(fn).parameters:
            if parameter != "tmp_path":
                return None
            kwargs[parameter] = Path(tempfile.mkdtemp())
        return kwargs

    undrivable = sorted(
        name
        for name, fn in vars(module).items()
        if name.startswith("test_")
        and callable(fn)
        and inspect.iscoroutinefunction(fn)
        and "_workflow_dir" in inspect.getsource(fn)
    )
    assert not undrivable, (
        f"these async tests walk the workflow census and this probe cannot "
        f"drive them, so they would be silently exempt: {undrivable}. Either "
        f"give them a non-empty assertion of their own, or teach this probe "
        f"to run a coroutine."
    )

    # Which tests actually WALK the census — detected by running each one
    # with `_workflow_dir` replaced by a recorder that still returns the
    # real directory, and seeing who calls it.
    #
    # This used to enumerate by looking for `_workflow_dir` in the source,
    # which is the weakness I published in the round-20 request before
    # anyone reported it: a name can appear for reasons other than use.
    # It did immediately — `test_the_workflow_directory_has_exactly_one_route`
    # names the function while reading this module's SYNTAX TREE, never the
    # directory, and the old enumeration demanded it fail on an empty
    # census it does not consult. A behavioural enumeration has no opinion
    # about names.
    # The ways a pytest call can end without returning, named ONCE and
    # used by both loops below. Round 20 found the discovery loop
    # catching only `Exception` while the loop beneath it had been
    # taught the whole family in round 19 — the recurring finding of
    # this work, committed inside the fix for it. Two handlers
    # enumerating the same family separately is how they drift, so they
    # do not enumerate it separately any more.
    #
    # NO_VERDICT: the call ended without looking, which the census loop
    # must report and the discovery loop must merely tolerate.
    # OBJECTED: it complained, however it spelled the complaint.
    NO_VERDICT = (pytest.skip.Exception, pytest.xfail.Exception)
    OBJECTED = (Exception, pytest.fail.Exception)

    real = _workflow_dir
    candidates = []
    for name, fn in sorted(vars(module).items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        if name == "test_no_workflow_guard_passes_on_an_empty_census":
            continue
        kwargs = driveable(fn)
        if kwargs is None:
            continue
        called = []
        module._workflow_dir = lambda: (called.append(1), real())[1]
        try:
            fn(**kwargs)
        except (*NO_VERDICT, *OBJECTED):
            # Any ending tells us it looked; `called` is the only
            # evidence this loop wants. A guard that SKIPS here is
            # ordinary -- the alembic census does exactly that on a tree
            # with no `backend/alembic.ini` -- and catching only
            # `Exception` let that skip escape and mark this entire
            # meta-test skipped, so no census guard was checked at all.
            # Measured before the fix: removing `backend/alembic.ini`
            # turned this test from `1 passed` into `1 skipped`.
            pass
        finally:
            module._workflow_dir = real
        if called:
            candidates.append((name, fn))

    assert len(candidates) >= 8, (
        f"only {len(candidates)} guards were found to walk the workflow "
        f"directory; this meta-guard is looking in the wrong place"
    )

    # Why the clauses below cannot be one: pytest's outcome exceptions
    # are NOT `Exception` subclasses. Asserted rather than left in a
    # comment, because on the day pytest changes that, folding them
    # WOULD be correct and this guard should be the thing that says so.
    #
    # All THREE, not just the one that bit: fixing `Skipped` and
    # leaving `Failed` beside it would be this project's most repeated
    # finding -- a rule applied where the defect was found and not
    # everywhere it is true -- committed in the line that fixes it.
    # Measured: nothing in this module calls `pytest.fail` or
    # `pytest.xfail` today, so this is a latent hole rather than a live
    # one; it is closed anyway because the cost is a tuple.
    for _outcome in (
        pytest.skip.Exception, pytest.fail.Exception, pytest.xfail.Exception,
    ):
        assert not issubclass(_outcome, Exception), (
            f"pytest now raises {_outcome.__name__} as an Exception "
            f"subclass; the clauses below can be folded into the general one"
        )

    empty = Path(tempfile.mkdtemp())
    passed_vacuously, skipped_vacuously = [], []
    try:
        module._workflow_dir = lambda: empty
        for name, fn in candidates:
            try:
                fn(**driveable(fn))
            except NO_VERDICT:
                # A guard that SKIPS has not looked, which is the thing
                # this meta-guard exists to refuse — and it cannot be
                # folded into the clause below, because `Skipped`
                # derives from `BaseException` and would otherwise
                # escape the loop entirely and take this test with it.
                # That is not hypothetical: it happened in round 19,
                # when a guard began skipping on an empty census and
                # silently switched this whole check off. `xfail` is
                # here for the same reason and on the same evidence: it
                # ends the call without a verdict.
                skipped_vacuously.append(name)
            except OBJECTED:
                # It objected, however it spelled the objection.
                # `pytest.fail` is named explicitly because `Failed` is
                # not an `Exception` either, and a guard that used it
                # would otherwise abort this loop exactly as the skip
                # did.
                continue
            else:
                passed_vacuously.append(name)
    finally:
        module._workflow_dir = real

    assert not skipped_vacuously, (
        f"these guards SKIP (or xfail) when there are no workflows to "
        f"look at, so they neither pass nor fail and this meta-guard "
        f"cannot see them: {skipped_vacuously}. A skip is not a verdict — "
        f"make the guard census the workflow directory before it decides "
        f"it has nothing to do."
    )

    assert not passed_vacuously, (
        f"these guards pass with NO workflows to look at, so they would "
        f"certify a repository with no CI at all: {passed_vacuously}. Give "
        f"each one a non-empty assertion on its census before it loops."
    )


def test_the_workflow_directory_has_exactly_one_route():
    """The meta-guard above patches ``_workflow_dir``. This is why that is
    enough.

    That guard enumerates the census guards by looking for
    ``_workflow_dir`` in their source, and I said as much to the reviewer
    while calling it a weak defence: a future guard reaching
    ``.github/workflows`` by some other route would not be enumerated,
    and the meta-guard would pass having checked less. The floor of eight
    candidates is a smoke alarm, not an argument.

    The argument is a CHOKEPOINT. If exactly one expression in this module
    builds a filesystem path to that directory, then patching the function
    containing it reaches every guard by construction, and enumeration
    cannot miss one. Measured: it is true today — `_workflow_dir` is the
    only such route, and every other mention of `.github` here is a
    docstring or a glob PATTERN string, which matches paths rather than
    opening them.

    So this asserts the precondition rather than the conclusion, over the
    AST rather than the text: any ``/`` join with a string containing
    ``.github`` is a route, wherever it is written and however it is
    spelled. It is deliberately a structural rule and not a behavioural
    one — the behavioural check is the meta-guard itself, and this is what
    makes its enumeration complete. Substituting a textual rule FOR a
    behavioural one is the mistake this batch made twice; using one to
    guard the other's precondition is not the same thing, and saying
    which is which is the point.
    """
    import ast
    import inspect
    import sys

    source = inspect.getsource(sys.modules[__name__])
    tree = ast.parse(source)

    def names_the_directory(node):
        return (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and ".github" in node.value
        )

    routes = []
    for parent in ast.walk(tree):
        if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(parent):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                if names_the_directory(node.left) or names_the_directory(node.right):
                    routes.append((parent.name, node.lineno))

    assert routes, (
        "no expression in this module builds a path to .github/workflows — "
        "the rule below would be vacuous, which is the very shape it exists "
        "to forbid"
    )
    outside = sorted({name for name, _ in routes if name != "_workflow_dir"})
    assert not outside, (
        f"these functions reach .github/workflows without going through "
        f"_workflow_dir: {outside}. The meta-guard patches that one function "
        f"to prove no guard passes on an empty census; a second route means "
        f"it silently checks less than it claims. Route the access through "
        f"_workflow_dir, or make the meta-guard enumerate by something other "
        f"than that name."
    )


# ---------------------------------------------------------------------------
# The author-facing docs must not name a file that does not exist
# ---------------------------------------------------------------------------

_DOC_PATH_ROOTS = ("backend/", "frontend/", "sdk/", "scripts/", "docs/", ".github/")


def _repo_paths_named_in(text: str) -> set[str]:
    """Every backticked repo-relative path a doc names.

    A backticked token counts when it starts with one of the tree's top
    level directories — precise enough that URL templates
    (``/v1/runs/{id}/events``), env vars and code fragments are not
    mistaken for files.
    """
    import re

    named = set()
    for token in re.findall(r"`([^`\n]+)`", text):
        token = token.strip().rstrip(",.;:)")
        if not token.startswith(_DOC_PATH_ROOTS):
            continue
        if any(c in token for c in "{}<>*$ "):  # a template, a glob, an expansion
            continue
        named.add(token.rstrip("/"))
    return named


def _tracked_paths(repo_root, directories=True):
    """Every path the REPOSITORY contains, from git — or None outside one.

    ``directories=False`` returns files alone. A caller that resolves
    bare shell words needs that: ``backend`` is a tracked directory, and
    treating it as a dependency would demand filters cover it.

    Not the working tree. The zero-agents job deletes the agents
    directory to prove the chassis boots without one, and a doc naming a
    file under it is not wrong because a job removed the file: the
    repository still has it, which is what a reader who clones will see.
    A guard that read the working tree failed there, correctly detecting
    a deletion and incorrectly calling it a broken link.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True, check=True, text=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    files = {f for f in out.split("\0") if f}
    if not files:
        return None
    if not directories:
        return files
    # Directories are paths too: a doc may name one.
    found = set()
    for f in files:
        parts = f.split("/")
        for i in range(1, len(parts)):
            found.add("/".join(parts[:i]))
    return files | found


def _doc_files(doc_dir):
    r"""Every authoring page under `doc_dir`, refusing an empty real one.

    `_workflow_files` has carried this invariant IN THE COLLECTOR since
    round 20, for the reason its comment gives: a guard that loops over
    an empty list does nothing and reports success, and enforcing the
    rule at each consumer means enumerating the consumers. This scan was
    written afterwards and did not adopt it -- the module's own rule,
    applied where it was written and not everywhere it is true (Codex
    round 26 reported the same shape in `find_candidates`; this one the
    survey found).

    It also uses a SILENT api where that one uses a loud one. Measured:
    `Path.iterdir()` raises `FileNotFoundError` on a directory that is
    not there, while `Path.glob()` and `Path.rglob()` return nothing at
    all. So renaming `docs/authoring/` left this guard passing over zero
    pages, which is the failure it exists to catch, one level up.

    Only the REAL directory is held to it, exactly as in
    `_workflow_files`: the negative tests below build their own trees
    and are legitimately empty while testing that very case.
    """
    files = sorted(doc_dir.glob("*.md"))
    if not files and doc_dir == _authoring_dir():
        raise AssertionError(
            f"no authoring pages under {doc_dir}. Whatever asked for this "
            f"would have looped over nothing and reported success; either "
            f"the path is wrong or the pages have been moved, and neither "
            f"should look like a pass."
        )
    return files


def _authoring_dir():
    """The real `docs/authoring/`, named once so the collector can tell
    it from a fixture's throwaway tree."""
    from pathlib import Path
    return Path(__file__).resolve().parents[2] / "docs" / "authoring"


def _missing_doc_paths(doc_dir, repo_root):
    """Which paths the docs under ``doc_dir`` name that the repo lacks."""
    tracked = _tracked_paths(repo_root)

    def present(path):
        if tracked is not None:
            return path in tracked
        return (repo_root / path).exists()  # a tarball, not a clone

    missing = {}
    for doc in _doc_files(doc_dir):
        gone = sorted(p for p in _repo_paths_named_in(doc.read_text()) if not present(p))
        if gone:
            missing[doc.name] = gone
    return missing


def test_the_authoring_docs_never_name_a_file_that_does_not_exist():
    """An author-facing doc that points at a missing file is a dead end.

    ``docs/authoring/`` is what someone writing an agent reads and
    follows literally: a path there is an instruction to go look, so a
    path that resolves to nothing costs them the ten minutes the page
    promises. This shipped twice in one batch — both SDK pages pointed
    at a TypeScript reference server that S5 has not written yet — which
    is exactly why the rule is a test and not a review habit.

    Scoped to ``docs/authoring/`` deliberately: the blueprint and the gap
    register name files that are *supposed* not to exist yet, and a
    guard that forbade that would forbid planning.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    missing = _missing_doc_paths(repo_root / "docs" / "authoring", repo_root)
    assert not missing, (
        f"these authoring docs name paths that are not in the tree: {missing}. "
        "Either add the file or describe it as future work without a path."
    )


def test_the_missing_path_rule_catches_an_injected_dead_end(tmp_path):
    """Negative test: the rule above must fail on a doc that lies.

    On a clean tree a checker that never looks and one that works are
    indistinguishable, so the violation is injected here — a doc naming
    a file that is not on disk, alongside one naming a file that is.
    """
    repo = tmp_path / "repo"
    (repo / "backend" / "app").mkdir(parents=True)
    (repo / "backend" / "app" / "real.py").write_text("x = 1\n")
    docs = repo / "docs" / "authoring"
    docs.mkdir(parents=True)
    (docs / "Good.md").write_text("Copy `backend/app/real.py`, and POST `/v1/runs/{id}/events`.\n")
    assert _missing_doc_paths(docs, repo) == {}

    (docs / "Bad.md").write_text("The reference server in `backend/app/imaginary.py` shows the shape.\n")
    assert _missing_doc_paths(docs, repo) == {"Bad.md": ["backend/app/imaginary.py"]}


def test_a_deleted_working_tree_file_is_not_a_broken_link(tmp_path):
    """The repository is the authority, not the working tree.

    The zero-agents job deletes the agents directory to prove the chassis
    boots without one. A doc naming a file under it is not wrong because
    a job removed the file — a reader who clones still gets it — and the
    first version of this rule failed there, correctly seeing a deletion
    and incorrectly calling it a dead end.
    """
    import subprocess

    repo = tmp_path / "clone"
    (repo / "backend" / "agents" / "_examples").mkdir(parents=True)
    kept = repo / "backend" / "agents" / "_examples" / "echo.py"
    kept.write_text("x = 1\n")
    docs = repo / "docs" / "authoring"
    docs.mkdir(parents=True)
    (docs / "Guide.md").write_text(
        "Copy `backend/agents/_examples/echo.py`, or the whole of "
        "`backend/agents/_examples`.\n"
    )
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@e.st", "-c", "user.name=t", "commit", "-qm", "x"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)

    assert _missing_doc_paths(docs, repo) == {}
    kept.unlink()  # what the zero-agents job does
    assert _missing_doc_paths(docs, repo) == {}, "a deletion read as a broken link"

    # And the rule still catches a path the repository never had.
    (docs / "Guide.md").write_text("See `backend/agents/_examples/never.py`.\n")
    assert _missing_doc_paths(docs, repo) == {
        "Guide.md": ["backend/agents/_examples/never.py"]
    }


def test_regenerating_the_openapi_spec_keeps_its_header(tmp_path):
    """The header survives a regeneration, whatever it says.

    It did not. The first documented command redirected a ``print()``,
    which queue-only logging swallowed, and the file came out empty. The
    replacement split the existing file on ``openapi: 3.1.0`` to find
    where the header ended — and that string was IN the header, because
    the command text quoted it, so regenerating truncated the very thing
    it was preserving. The rule is the document's shape instead: the
    header is the leading comment block, which no comment can imitate.
    """
    import importlib.util
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    spec_module = importlib.util.spec_from_file_location(
        "export_openapi_under_test", repo_root / "scripts" / "export_openapi.py"
    )
    export_openapi = importlib.util.module_from_spec(spec_module)
    spec_module.loader.exec_module(export_openapi)
    header_of = export_openapi.header_of

    spec = tmp_path / "spec.yaml"
    spec.write_text(
        "# GENERATED FILE.\n"
        "# Regenerate with the command that mentions openapi: 3.1.0 itself.\n"
        "#   python3 scripts/export_openapi.py\n"
        "openapi: 3.1.0\n"
        "info:\n  title: LibreRun\n"
    )
    header = header_of(spec.read_text())
    assert header.endswith("export_openapi.py\n")
    assert "openapi: 3.1.0 itself" in header, "the header lost the line quoting the marker"
    assert not header.endswith("openapi: 3.1.0\n"), "the document leaked into the header"
    assert header.count("\n") == 3

    # And the real file's header comes back whole.
    live = (repo_root / "docs" / "api" / "openapi.yaml").read_text()
    assert header_of(live).startswith("# LibreRun API specification")
    assert "openapi:" not in header_of(live)
