"""``app.observability.run_trace`` — the run's root span and its W3C
context (blueprint S4, promise 3).

Covers the inbound header grammar (``traceparent`` fixed, ``tracestate``
within the W3C limits), the identifier check that drops a trace state
carrying PII with a warning that names the header and never the value,
the root span opened as a root even inside another active span (or under
the validated upstream parent), the persisted pair and its restoration
as a remote parent, the run-plane sampler that records the root whatever
an upstream ``00`` flag says, and the outbound headers.
"""
from __future__ import annotations

import logging
import uuid

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ParentBased
from opentelemetry.trace import get_current_span

from app.observability import run_trace

FIXTURE_EMAIL = "pii.fixture@example.com"
UPSTREAM_TRACE = "4bf92f3577b34da6a2ce929d0e0e4736"
UPSTREAM_SPAN = "00f067aa0ba902b7"


class _Row:
    def __init__(self, **kw):
        self.id = kw.pop("id", uuid.uuid4())
        self.tenant_id = kw.pop("tenant_id", uuid.uuid4())
        self.run_number = kw.pop("run_number", "RUN-1000")
        self.trace_id = None
        self.root_traceparent = None
        self.root_tracestate = None
        for k, v in kw.items():
            setattr(self, k, v)


@pytest.fixture
def provider(monkeypatch):
    exporter = InMemorySpanExporter()
    prov = TracerProvider(sampler=run_trace.RunRootSampler())
    prov.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(run_trace, "_tracer", prov.get_tracer("test"))
    yield prov, exporter
    prov.shutdown()


# ---------------------------------------------------------------- grammar --


@pytest.mark.parametrize(
    "value",
    [
        f"00-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-01",
        f"00-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-00",
        f"00-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-ff",
    ],
)
def test_traceparent_fixed_grammar_is_accepted(value):
    trace_id, span_id, flags = run_trace.parse_traceparent(value)
    assert format(trace_id, "032x") == UPSTREAM_TRACE
    assert format(span_id, "016x") == UPSTREAM_SPAN
    assert flags == int(value[-2:], 16)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        f"00-{UPSTREAM_TRACE.upper()}-{UPSTREAM_SPAN}-01",  # uppercase hex
        f"01-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-01",  # not version 00
        f"00-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-01-extra",  # extra field
        f"00-{'0' * 32}-{UPSTREAM_SPAN}-01",  # zero trace id
        f"00-{UPSTREAM_TRACE}-{'0' * 16}-01",  # zero span id
        f"00-{UPSTREAM_TRACE[:-1]}-{UPSTREAM_SPAN}-01",  # short trace id
        f"00-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-1",  # short flags
        f" 00-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-01",  # padding
        f"00-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-01\n",
    ],
)
def test_traceparent_anything_else_is_rejected(value):
    assert run_trace.parse_traceparent(value) is None


@pytest.mark.parametrize(
    "value, normalized",
    [
        ("vendor=abc", "vendor=abc"),
        (" vendor=abc , rojo=00f067aa0ba902b7 ", "vendor=abc,rojo=00f067aa0ba902b7"),
        ("tenant1@sys=x/y*z", "tenant1@sys=x/y*z"),
        ("a=b,,c=d,", "a=b,c=d"),
        ("k=v with spaces", "k=v with spaces"),
        ("", None),
        ("  ,  ", None),
    ],
)
def test_tracestate_within_the_w3c_grammar_is_normalized(value, normalized):
    assert run_trace.parse_tracestate(value) == (normalized, None)


@pytest.mark.parametrize(
    "value, reason",
    [
        ("A=b", "member_grammar"),  # uppercase key
        ("a", "member_grammar"),  # no =
        ("a=b=c", "member_grammar"),  # = in value
        ("a=b ", "member_grammar") if False else ("a=b\x7f", "member_grammar"),  # control char
        ("a=" + "x" * 257, "member_grammar"),  # value too long
        ("=v", "member_grammar"),
        ("a=b,a=c", "duplicate_key"),
        (",".join(f"k{i}=v" for i in range(33)), "too_many_members"),
        ("k=" + "x" * 256 + "," + "j=" + "y" * 256, "too_long"),
        (None, "not_a_string"),
    ],
)
def test_tracestate_outside_the_limits_is_rejected(value, reason):
    assert run_trace.parse_tracestate(value) == (None, reason)


# -------------------------------------------------------- inbound context --


def _upstream_headers(flags="01", tracestate=None):
    headers = {"traceparent": f"00-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-{flags}"}
    if tracestate is not None:
        headers["tracestate"] = tracestate
    return headers


def test_upstream_context_carries_the_remote_parent_flags_and_state():
    ctx = run_trace.upstream_context(_upstream_headers("00", "vendor=abc"))
    sc = get_current_span(ctx).get_span_context()
    assert sc.is_remote and sc.is_valid
    assert format(sc.trace_id, "032x") == UPSTREAM_TRACE
    assert format(sc.span_id, "016x") == UPSTREAM_SPAN
    assert not sc.trace_flags.sampled
    assert sc.trace_state.to_header() == "vendor=abc"


def test_no_traceparent_means_no_upstream_and_tracestate_alone_is_ignored(caplog):
    assert run_trace.upstream_context({}) is None
    with caplog.at_level(logging.WARNING):
        assert run_trace.upstream_context({"tracestate": "vendor=abc"}) is None
    assert "tracestate" not in caplog.text


def test_malformed_traceparent_is_ignored_with_a_warning_naming_the_header(caplog):
    with caplog.at_level(logging.WARNING):
        ctx = run_trace.upstream_context(
            {"traceparent": "00-not-hex-01", "tracestate": "vendor=abc"}
        )
    assert ctx is None
    assert "traceparent_ignored" in caplog.text


def test_invalid_tracestate_is_ignored_and_the_traceparent_still_adopted(caplog):
    with caplog.at_level(logging.WARNING):
        ctx = run_trace.upstream_context(_upstream_headers("01", "A=b"))
    sc = get_current_span(ctx).get_span_context()
    assert format(sc.trace_id, "032x") == UPSTREAM_TRACE
    assert sc.trace_state.to_header() == ""
    assert "tracestate_ignored" in caplog.text


def test_tracestate_carrying_pii_is_dropped_with_a_warning_and_never_the_value(caplog):
    with caplog.at_level(logging.WARNING):
        ctx = run_trace.upstream_context(
            _upstream_headers("01", f"vendor={FIXTURE_EMAIL}")
        )
    sc = get_current_span(ctx).get_span_context()
    assert format(sc.trace_id, "032x") == UPSTREAM_TRACE  # the parent survives
    assert sc.trace_state.to_header() == ""  # the vendor state does not
    assert "tracestate_dropped" in caplog.text
    assert "header=tracestate" in caplog.text or "'header': 'tracestate'" in caplog.text
    assert FIXTURE_EMAIL not in caplog.text
    assert "example.com" not in caplog.text


def test_starlette_style_multi_valued_headers_are_joined():
    class _Headers(dict):
        def getlist(self, name):
            return [v for k, v in self.items() if k == name] + self.get("_extra", {}).get(name, [])

    headers = _Headers(_upstream_headers("01", "a=b"))
    headers["_extra"] = {"tracestate": ["c=d"]}
    ctx = run_trace.upstream_context(headers)
    sc = get_current_span(ctx).get_span_context()
    assert sc.trace_state.to_header() == "a=b,c=d"


# ---------------------------------------------------------- the root span --


def test_root_span_is_a_root_even_inside_another_active_span(provider):
    prov, exporter = provider
    other = prov.get_tracer("request")
    row = _Row()
    with other.start_as_current_span("POST /runs") as request_span:
        with run_trace.root_span(
            upstream=None,
            run_id=row.id,
            run_number=row.run_number,
            agent_id="echo-v1",
            tenant_id=row.tenant_id,
        ) as root:
            assert run_trace.persist_root(row, root)
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["run"].parent is None
    assert spans["run"].context.trace_id != request_span.get_span_context().trace_id
    assert spans["run"].attributes[run_trace.RUN_ROOT_ATTRIBUTE] is True
    assert spans["run"].attributes["run.id"] == str(row.id)
    assert spans["run"].attributes["agent.id"] == "echo-v1"
    # The persisted pair: the root's own trace and span, sampled — and,
    # for a trace id the chassis generated, the SDK's random-trace-id
    # bit beside it (W3C Level 2), persisted as the SDK set it.
    trace_hex = format(spans["run"].context.trace_id, "032x")
    span_hex = format(spans["run"].context.span_id, "016x")
    assert row.root_traceparent == f"00-{trace_hex}-{span_hex}-03"
    assert run_trace.parse_traceparent(row.root_traceparent)[2] & 0x01
    assert row.root_tracestate is None
    assert row.trace_id == trace_hex


def test_root_span_under_an_unsampled_upstream_is_still_sampled(provider):
    """The Accept case: an upstream ``traceparent`` ending in ``00`` still
    yields the full tree, ``root_traceparent`` ending in ``01``."""
    _prov, exporter = provider
    row = _Row()
    upstream = run_trace.upstream_context(_upstream_headers("00", "vendor=abc"))
    with run_trace.root_span(
        upstream=upstream,
        run_id=row.id,
        run_number=row.run_number,
        agent_id="echo-v1",
        tenant_id=row.tenant_id,
    ) as root:
        assert root.is_recording()
        assert run_trace.persist_root(row, root)
    (span,) = exporter.get_finished_spans()
    assert span.name == "run"
    assert format(span.context.trace_id, "032x") == UPSTREAM_TRACE
    assert format(span.parent.span_id, "016x") == UPSTREAM_SPAN
    assert row.root_traceparent.endswith("-01")
    assert row.root_traceparent.startswith(f"00-{UPSTREAM_TRACE}-")
    assert row.root_tracestate == "vendor=abc"
    assert row.trace_id == UPSTREAM_TRACE


def test_without_the_sampler_an_unsampled_upstream_would_silence_the_run(monkeypatch):
    """The negative case for the sampler: the SDK's default parent-based
    sampler drops the root under a ``00`` parent — which is why the
    provider installs ``RunRootSampler``."""
    exporter = InMemorySpanExporter()
    prov = TracerProvider()  # default: ParentBased(ALWAYS_ON)
    prov.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(run_trace, "_tracer", prov.get_tracer("test"))
    row = _Row()
    upstream = run_trace.upstream_context(_upstream_headers("00"))
    with run_trace.root_span(
        upstream=upstream, run_id=row.id, run_number=None, agent_id="a", tenant_id=row.tenant_id
    ) as root:
        assert not root.is_recording()
        run_trace.persist_root(row, root)
    assert exporter.get_finished_spans() == ()
    assert row.root_traceparent.endswith("-00")


def test_sampler_leaves_other_spans_to_the_parent_based_default():
    sampler = run_trace.RunRootSampler()
    upstream = run_trace.upstream_context(_upstream_headers("00"))
    root = sampler.should_sample(
        upstream, 1, "run", attributes={run_trace.RUN_ROOT_ATTRIBUTE: True}
    )
    assert root.decision.is_sampled()
    # A span merely NAMED run, without the root marker, is not the root.
    named = sampler.should_sample(upstream, 1, "run", attributes={})
    assert not named.decision.is_sampled()
    other = sampler.should_sample(upstream, 1, "phase")
    assert not other.decision.is_sampled()
    no_parent = sampler.should_sample(None, 1, "phase")
    assert no_parent.decision.is_sampled()
    assert "RunRootSampler" in sampler.get_description()


def test_sampler_delegate_is_honoured_for_non_root_spans():
    sampler = run_trace.RunRootSampler(ParentBased(ALWAYS_OFF))
    assert not sampler.should_sample(None, 1, "phase").decision.is_sampled()
    assert sampler.should_sample(
        None, 1, "run", attributes={run_trace.RUN_ROOT_ATTRIBUTE: True}
    ).decision.is_sampled()


def test_persist_root_writes_nothing_when_tracing_is_off():
    row = _Row()
    assert not run_trace.persist_root(row, otel_trace.INVALID_SPAN)
    assert row.root_traceparent is None and row.trace_id is None


# ---------------------------------------------------------------- restore --


def test_restore_root_yields_the_persisted_remote_parent(provider):
    _prov, exporter = provider
    row = _Row(
        root_traceparent=f"00-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-01",
        root_tracestate="vendor=abc,rojo=1",
    )
    ctx = run_trace.restore_root(row)
    sc = get_current_span(ctx).get_span_context()
    assert sc.is_remote and sc.trace_flags.sampled
    assert format(sc.trace_id, "032x") == UPSTREAM_TRACE
    assert format(sc.span_id, "016x") == UPSTREAM_SPAN
    assert sc.trace_state.to_header() == "vendor=abc,rojo=1"
    # A child started in it joins the trace under the root and inherits
    # the vendor state.
    with run_trace._tracer.start_as_current_span("phase", context=ctx) as child:
        csc = child.get_span_context()
        assert format(csc.trace_id, "032x") == UPSTREAM_TRACE
        assert csc.trace_state.to_header() == "vendor=abc,rojo=1"
        assert run_trace.propagation_headers() == {
            "traceparent": f"00-{UPSTREAM_TRACE}-{format(csc.span_id, '016x')}-01",
            "tracestate": "vendor=abc,rojo=1",
        }
        assert run_trace.current_trace_id() == UPSTREAM_TRACE
    (span,) = exporter.get_finished_spans()
    assert format(span.parent.span_id, "016x") == UPSTREAM_SPAN


def test_restore_root_is_none_for_a_row_without_a_pair_or_with_a_bad_one(caplog):
    assert run_trace.restore_root(_Row()) is None
    with caplog.at_level(logging.WARNING):
        assert run_trace.restore_root(_Row(root_traceparent="garbage")) is None
    assert "run_root_unparseable" in caplog.text


def test_ensure_root_mints_a_root_for_a_row_without_one(provider, caplog):
    _prov, exporter = provider
    row = _Row()
    with caplog.at_level(logging.INFO):
        ctx = run_trace.ensure_root(row, agent_id="vita-v1")
    assert ctx is not None
    (root,) = exporter.get_finished_spans()
    assert root.name == "run" and root.parent is None
    assert root.attributes["librerun.run.root_minted_late"] is True
    assert row.root_traceparent == (
        f"00-{format(root.context.trace_id, '032x')}-{format(root.context.span_id, '016x')}-03"
    )
    assert row.trace_id == format(root.context.trace_id, "032x")
    assert "run_root_minted_late" in caplog.text
    # The second call restores rather than mints again.
    assert run_trace.ensure_root(row, agent_id="vita-v1") is not None
    assert len(exporter.get_finished_spans()) == 1


def test_ensure_root_is_none_when_tracing_is_off(monkeypatch):
    monkeypatch.setattr(run_trace, "_tracer", otel_trace.NoOpTracer())
    row = _Row()
    assert run_trace.ensure_root(row, agent_id="a") is None
    assert row.root_traceparent is None


def test_propagation_headers_are_empty_outside_any_span():
    assert run_trace.propagation_headers() == {}
    assert run_trace.current_trace_id() is None
