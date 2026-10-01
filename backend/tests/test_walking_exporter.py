"""``WalkingSpanExporter``: the batch is encoded, walked by descriptor and
handed to the sender as the walked request — the fixture in a span's
own instrumentation (name, string and int64 attributes, an event, a
link attribute, a resource attribute, a bytes attribute) never reaches
the sender, and ``init_otel`` installs the wrapper in front of the OTLP
exporter.
"""
from __future__ import annotations

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.trace import Link, SpanContext, TraceFlags

from app.observability import otlp_walk
from app.observability.walkers import WalkingSpanExporter
from tests.test_otel_init import otel_settings  # noqa: F401  (fixture)

FIXTURE_EMAIL = "pii.fixture@example.com"
CARD = 4111111111111111


class _Sender:
    def __init__(self):
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return SpanExportResult.SUCCESS


def _provider(sender):
    provider = TracerProvider(
        resource=Resource.create({"service.name": "echo", "owner": f"x {FIXTURE_EMAIL}"})
    )
    provider.add_span_processor(SimpleSpanProcessor(WalkingSpanExporter(sender)))
    return provider


def test_an_agents_own_instrumentation_is_walked_before_the_sender_sees_it():
    sender = _Sender()
    provider = _provider(sender)
    tracer = provider.get_tracer("agent.echo")
    link_ctx = SpanContext(trace_id=1, span_id=2, is_remote=True, trace_flags=TraceFlags(1))
    with tracer.start_as_current_span(
        f"custom {FIXTURE_EMAIL}",
        attributes={"note": f"see {FIXTURE_EMAIL}", "librerun.scope": "run"},
        links=[Link(link_ctx, attributes={"why": f"d {FIXTURE_EMAIL}"})],
    ) as span:
        span.add_event(f"mailed {FIXTURE_EMAIL}", {"who": f"c {FIXTURE_EMAIL}"})
    with tracer.start_as_current_span("numbers", attributes={"card": CARD, "agent.id": "echo-v1"}):
        pass
    with tracer.start_as_current_span("bytes", attributes={"blob": FIXTURE_EMAIL.encode()}):
        pass
    provider.shutdown()

    dump = b"".join(r.SerializeToString() for r in sender.requests)
    assert FIXTURE_EMAIL.encode() not in dump
    assert str(CARD).encode() not in dump
    spans = {
        s.name: s
        for r in sender.requests
        for rs in r.resource_spans
        for ss in rs.scope_spans
        for s in ss.spans
    }
    assert "custom [REDACTED_EMAIL_ADDRESS_1]" in spans
    custom = spans["custom [REDACTED_EMAIL_ADDRESS_1]"]
    assert custom.events[0].name == "mailed [REDACTED_EMAIL_ADDRESS_1]"
    assert otlp_walk.STRIPPED_SPAN_NAME in spans  # the card number stripped that span
    stripped = spans[otlp_walk.STRIPPED_SPAN_NAME]
    # This provider has no AgentSpanEnricher, so the chassis vouched for
    # nothing on these spans and a stripped one keeps only the walk's own
    # marks. In the backend the enricher records its identity pairs and
    # they survive — see test_the_chassis_record_exempts_its_own_pairs.
    kept = {kv.key for kv in stripped.attributes}
    assert otlp_walk.REDACTED_ATTRIBUTE in kept
    assert "agent.id" not in kept, "an unvouched key survived the stripping"
    # The Python SDK decodes a bytes attribute to text before it is ever
    # encoded, so from an in-process agent the fixture arrives as a
    # string position and is redacted; a ``bytes_value`` on the wire (a
    # container's SDK) is dropped by the walker (tests/test_otlp_walk.py).
    blob = {kv.key: kv.value.string_value for kv in spans["bytes"].attributes}
    assert blob == {"blob": "[REDACTED_EMAIL_ADDRESS_1]"}
    resource_attrs = {kv.key: kv.value.string_value for rs in sender.requests[0].resource_spans for kv in rs.resource.attributes}
    assert resource_attrs["owner"] == "x [REDACTED_EMAIL_ADDRESS_1]"


def test_init_otel_installs_the_walking_exporter(otel_settings):
    from unittest.mock import patch

    from fastapi import FastAPI
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from app.observability import otel_init as oi

    with (
        patch("opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app"),
        patch("opentelemetry.trace.set_tracer_provider"),
    ):
        result = oi.init_otel(FastAPI())
    processors = result.tracer_provider._active_span_processor._span_processors
    batch = [p for p in processors if isinstance(p, BatchSpanProcessor)]
    assert batch, "the OTLP batch processor is installed"
    exporter = batch[0].span_exporter
    assert isinstance(exporter, WalkingSpanExporter)
    result.tracer_provider.shutdown()


def test_build_sender_speaks_both_protocols():
    from app.observability.walkers import build_sender

    grpc = build_sender("http://vector:4317", "grpc")
    assert hasattr(grpc, "send")
    http = build_sender("http://vector:4318", "http/protobuf")
    assert http._endpoint.endswith("/v1/traces")
    grpc.shutdown()
    http.shutdown()
