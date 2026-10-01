"""Tests for ``app.observability.span_enricher`` (B3 follow-up).

The enricher exists because the GenAI-convention LLM instrumentors
(decision D2) do not read OpenInference's ``using_attributes`` context —
so agent / run / session identity must be stamped one layer below the
instrumentors, at span start, from the structlog contextvars that
``agent_runner`` binds. These tests drive a real ``TracerProvider`` with
an in-memory exporter: any span created inside a bound context — which
is exactly what an auto-instrumented LLM span is — must come out carrying
the identity attributes.
"""
from __future__ import annotations

from uuid import UUID

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.logging_context import log_context
from app.observability.span_enricher import AgentSpanEnricher


@pytest.fixture
def provider_and_exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider.add_span_processor(AgentSpanEnricher())
    return provider, exporter


def test_spans_inside_phase_context_get_identity_attributes(provider_and_exporter):
    """Simulates what a GenAI-convention instrumentor does: create a span
    with its own tracer while the runner's context is bound. The span must
    carry the full identity set even though no OpenInference machinery ran.
    """
    provider, exporter = provider_and_exporter
    tracer = provider.get_tracer("genai-instrumentor-simulation")

    with log_context(
        run_id="11111111-1111-1111-1111-111111111111",
        tenant_id="22222222-2222-2222-2222-222222222222",
        agent_id="vita-v1",
        agent_name="VITA",
        phase="phase2",
        session_id="11111111-1111-1111-1111-111111111111",
        user_id="33333333-3333-3333-3333-333333333333",
        run_number="VITA-1042",
    ):
        with tracer.start_as_current_span("chat gpt-x"):
            pass

    (span,) = exporter.get_finished_spans()
    attrs = span.attributes
    assert attrs["agent.id"] == "vita-v1"
    assert attrs["agent.name"] == "VITA"
    assert attrs["tenant.id"] == "22222222-2222-2222-2222-222222222222"
    assert attrs["run.id"] == "11111111-1111-1111-1111-111111111111"
    assert attrs["run.number"] == "VITA-1042"
    assert attrs["phase"] == "phase2"
    assert attrs["session.id"] == "11111111-1111-1111-1111-111111111111"
    assert attrs["user.id"] == "33333333-3333-3333-3333-333333333333"


def test_spans_outside_context_get_platform_scope_and_no_identity(provider_and_exporter):
    """Outside any bound context (startup, health checks) a span carries NO
    identity attributes — but it still carries the plane stamp, and it is
    ``platform``: an explicit value beats making routers infer "no
    agent.id means chassis"."""
    provider, exporter = provider_and_exporter
    tracer = provider.get_tracer("t")

    with tracer.start_as_current_span("startup"):
        pass

    (span,) = exporter.get_finished_spans()
    for attribute in ("agent.id", "run.id", "session.id", "user.id"):
        assert attribute not in span.attributes
    assert span.attributes["librerun.scope"] == "platform"


def test_unmapped_and_blank_context_keys_are_ignored(provider_and_exporter):
    provider, exporter = provider_and_exporter
    tracer = provider.get_tracer("t")

    with log_context(
        agent_id="vita-v1",
        run_number="",          # blank → skipped
        favourite_colour="teal",  # unmapped
        request_id="req-1",      # unmapped
    ):
        with tracer.start_as_current_span("s"):
            pass

    (span,) = exporter.get_finished_spans()
    assert span.attributes["agent.id"] == "vita-v1"
    assert "run.number" not in span.attributes
    assert "favourite_colour" not in span.attributes
    assert "request_id" not in span.attributes
    # ``user_email`` is not merely unmapped: since S4 it cannot be bound
    # at all (tests/test_platform_plane_ids_only.py).
    assert "user.email" not in span.attributes


def test_non_primitive_values_are_coerced_to_str(provider_and_exporter):
    provider, exporter = provider_and_exporter
    tracer = provider.get_tracer("t")

    with log_context(run_id=UUID("11111111-1111-1111-1111-111111111111")):
        with tracer.start_as_current_span("s"):
            pass

    (span,) = exporter.get_finished_spans()
    assert span.attributes["run.id"] == "11111111-1111-1111-1111-111111111111"


def test_custom_attribute_map_overrides_default(provider_and_exporter):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider.add_span_processor(AgentSpanEnricher({"deploy_ring": "deploy.ring"}))
    tracer = provider.get_tracer("t")

    with log_context(deploy_ring="canary", agent_id="ignored-by-custom-map"):
        with tracer.start_as_current_span("s"):
            pass

    (span,) = exporter.get_finished_spans()
    assert span.attributes["deploy.ring"] == "canary"
    assert "agent.id" not in span.attributes


def test_scope_is_run_only_when_agent_id_is_bound(provider_and_exporter):
    """The plane predicate is "is ``agent_id`` bound" — a request context
    *about* a run (request_id, even run_id) is still the platform plane;
    only executing agent work flips it to ``run``. Both directions, per
    the negative-test rule."""
    provider, exporter = provider_and_exporter
    tracer = provider.get_tracer("t")

    with log_context(request_id="req-1", run_id="11111111-1111-1111-1111-111111111111"):
        with tracer.start_as_current_span("http_request_about_a_run"):
            pass
    with log_context(agent_id="vita-v1"):
        with tracer.start_as_current_span("agent_work"):
            pass

    request_span, run_span = exporter.get_finished_spans()
    assert request_span.attributes["librerun.scope"] == "platform"
    assert run_span.attributes["librerun.scope"] == "run"


def test_run_scope_rides_along_with_identity_attributes(provider_and_exporter):
    """The simulated-instrumentor case from the first test, extended: an
    LLM span created inside the runner's context carries the plane stamp
    alongside the identity set."""
    provider, exporter = provider_and_exporter
    tracer = provider.get_tracer("genai-instrumentor-simulation")

    with log_context(agent_id="vita-v1", agent_name="VITA", phase="phase2"):
        with tracer.start_as_current_span("chat gpt-x"):
            pass

    (span,) = exporter.get_finished_spans()
    assert span.attributes["librerun.scope"] == "run"
    assert span.attributes["agent.id"] == "vita-v1"


def test_run_identity_is_duplicated_under_the_pre_s1_names_for_one_release(provider_and_exporter):
    """Blueprint S1 (L18): ``run.id`` / ``run.number`` are authoritative and
    ``case.id`` / ``case.number`` carry the same values so saved trace
    queries keep matching; the duplicates go at v1.1."""
    provider, exporter = provider_and_exporter
    tracer = provider.get_tracer("genai-instrumentor-simulation")
    with log_context(run_id="11111111-1111-1111-1111-111111111111", run_number="RUN-1000"):
        with tracer.start_as_current_span("chat gpt-x"):
            pass
    (span,) = exporter.get_finished_spans()
    attrs = span.attributes
    assert attrs["run.id"] == attrs["case.id"] == "11111111-1111-1111-1111-111111111111"
    assert attrs["run.number"] == attrs["case.number"] == "RUN-1000"
