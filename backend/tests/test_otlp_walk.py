"""The OTLP walker (blueprint S4; gaps H9, H10): totality over the
protobuf descriptor, the fixture at every string position, a Luhn-valid
card number at every integer and integral-double position, bytes at
every bytes position — none reaching the exporter unwalked or undropped
— and, as the clean case, a real span and log record with
epoch-nanosecond timestamps and 64-bit ids that lose nothing.
"""
from __future__ import annotations

import os
import time

import pytest
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2

from app.observability import otlp_walk as w

FIXTURE_EMAIL = "pii.fixture@example.com"
CARD = 4111111111111111  # Luhn-valid
PLACEHOLDER = "[REDACTED_EMAIL_ADDRESS_1]"
EPOCH_NS = 1757534400000000000
TRACE_ID = os.urandom(16)
SPAN_ID = os.urandom(8)


# ------------------------------------------------------------- totality --


def test_every_descriptor_field_is_classified():
    unclassified = []
    for request in (
        trace_service_pb2.ExportTraceServiceRequest,
        logs_service_pb2.ExportLogsServiceRequest,
    ):
        for message, field_name, path, _f in w.descriptor_fields(request.DESCRIPTOR):
            if w.classify(message, field_name) is None:
                unclassified.append(path)
    assert unclassified == []


def test_totality_fails_on_an_unclassified_field(monkeypatch):
    """The negative case: remove one classification and the walk over the
    descriptor must report it — a new field in an OTLP upgrade fails the
    suite until it is placed."""
    key = ("opentelemetry.proto.trace.v1.Span", "name")
    monkeypatch.delitem(w.CLASSIFICATION, key)
    missing = [
        path
        for message, field_name, path, _f in w.descriptor_fields(
            trace_service_pb2.ExportTraceServiceRequest.DESCRIPTOR
        )
        if w.classify(message, field_name) is None
    ]
    assert missing and all(p.endswith(".name") for p in missing)


def test_classes_partition_the_descriptor_as_the_blueprint_says():
    assert w.classify("opentelemetry.proto.trace.v1.Span", "start_time_unix_nano") == w.METADATA
    assert w.classify("opentelemetry.proto.trace.v1.Span", "trace_id") == w.METADATA
    assert w.classify("opentelemetry.proto.trace.v1.Span", "trace_state") == w.IDENTIFIER
    assert w.classify("opentelemetry.proto.trace.v1.Span.Link", "trace_state") == w.IDENTIFIER
    assert w.classify("opentelemetry.proto.common.v1.KeyValue", "key") == w.IDENTIFIER
    assert w.classify("opentelemetry.proto.common.v1.AnyValue", "int_value") == w.NUMBER
    assert w.classify("opentelemetry.proto.common.v1.AnyValue", "double_value") == w.NUMBER
    assert w.classify("opentelemetry.proto.common.v1.AnyValue", "bytes_value") == w.BYTES
    assert w.classify("opentelemetry.proto.logs.v1.LogRecord", "time_unix_nano") == w.METADATA
    assert w.classify("opentelemetry.proto.logs.v1.LogRecord", "severity_number") == w.METADATA


# ------------------------------------------------------------ builders --


def _kv(key, value):
    kv = common_pb2.KeyValue(key=key)
    if isinstance(value, bool):
        kv.value.bool_value = value
    elif isinstance(value, int):
        kv.value.int_value = value
    elif isinstance(value, float):
        kv.value.double_value = value
    elif isinstance(value, bytes):
        kv.value.bytes_value = value
    elif isinstance(value, list):
        for item in value:
            av = kv.value.array_value.values.add()
            if isinstance(item, str):
                av.string_value = item
            elif isinstance(item, int):
                av.int_value = item
            elif isinstance(item, bytes):
                av.bytes_value = item
    elif isinstance(value, dict):
        for k, v in value.items():
            inner = kv.value.kvlist_value.values.add()
            inner.key = k
            if isinstance(v, str):
                inner.value.string_value = v
            elif isinstance(v, int):
                inner.value.int_value = v
    else:
        kv.value.string_value = str(value)
    return kv


def _trace_request(*, span_attrs=None, name="work", event_attrs=None, link_attrs=None,
                   resource_attrs=None, scope_name="agent.echo", status_message="",
                   trace_state="", event_name="checkpoint"):
    req = trace_service_pb2.ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    for kv in resource_attrs or [_kv("service.name", "echo")]:
        rs.resource.attributes.add().CopyFrom(kv)
    ss = rs.scope_spans.add()
    ss.scope.name = scope_name
    ss.scope.version = "1.0"
    span = ss.spans.add()
    span.trace_id = TRACE_ID
    span.span_id = SPAN_ID
    span.parent_span_id = os.urandom(8)
    span.name = name
    span.kind = 1
    span.trace_state = trace_state
    span.start_time_unix_nano = EPOCH_NS
    span.end_time_unix_nano = EPOCH_NS + 5_000_000
    span.status.code = 2
    span.status.message = status_message
    for kv in span_attrs or [_kv("librerun.scope", "run"), _kv("agent.id", "echo-v1"), _kv("http.status_code", 200)]:
        span.attributes.add().CopyFrom(kv)
    event = span.events.add()
    event.name = event_name
    event.time_unix_nano = EPOCH_NS + 1000
    for kv in event_attrs or []:
        event.attributes.add().CopyFrom(kv)
    link = span.links.add()
    link.trace_id = os.urandom(16)
    link.span_id = os.urandom(8)
    for kv in link_attrs or [_kv("previous_phase.completed", True)]:
        link.attributes.add().CopyFrom(kv)
    return req


def _span(req):
    return req.resource_spans[0].scope_spans[0].spans[0]


def _attrs(kvs) -> dict:
    out = {}
    for kv in kvs:
        which = kv.value.WhichOneof("value")
        out[kv.key] = getattr(kv.value, which) if which else None
    return out


# ---------------------------------------------------------- clean case --


def test_a_real_span_loses_nothing():
    """Epoch-nanosecond timestamps, 64-bit ids, a Luhn-valid epoch under
    a time-named key, ordinary counts and ratios: byte-identical."""
    req = _trace_request(span_attrs=[
        _kv("librerun.scope", "run"),
        _kv("run.id", "82f9f5af-dfe4-45fb-8704-76d4ce7528b5"),
        _kv("created_at", 1788998400005),
        _kv("ratio", 0.85),
        _kv("count", 42),
        _kv("ok", True),
        _kv("tags", ["agent:echo", "phase:echo"]),
    ])
    before = req.SerializeToString()
    w.walk_trace_request(req)
    assert req.SerializeToString() == before


def test_a_real_log_record_loses_nothing():
    req = logs_service_pb2.ExportLogsServiceRequest()
    rl = req.resource_logs.add()
    rl.resource.attributes.add().CopyFrom(_kv("service.name", "echo"))
    sl = rl.scope_logs.add()
    sl.scope.name = "agent.echo"
    rec = sl.log_records.add()
    rec.time_unix_nano = EPOCH_NS
    rec.observed_time_unix_nano = EPOCH_NS + 5
    rec.severity_number = 9
    rec.severity_text = "INFO"
    rec.body.string_value = "step gather finished in 1200 ms"
    rec.trace_id = TRACE_ID
    rec.span_id = SPAN_ID
    rec.attributes.add().CopyFrom(_kv("step", "gather"))
    rec.attributes.add().CopyFrom(_kv("duration_ms", 1200))
    before = req.SerializeToString()
    report = w.walk_logs_request(req)
    assert req.SerializeToString() == before
    assert report.records_dropped == 0


# ---------------------------------------------------------- the fixture --


def test_the_fixture_at_every_string_position_comes_out_as_the_placeholder():
    req = _trace_request(
        name=f"call {FIXTURE_EMAIL}",
        status_message=f"failed for {FIXTURE_EMAIL}",
        event_name=f"mailed {FIXTURE_EMAIL}",
        span_attrs=[_kv("note", f"see {FIXTURE_EMAIL}"), _kv("list", [f"a {FIXTURE_EMAIL}"]),
                    _kv("nested", {"inner": f"b {FIXTURE_EMAIL}"})],
        event_attrs=[_kv("who", f"c {FIXTURE_EMAIL}")],
        link_attrs=[_kv("why", f"d {FIXTURE_EMAIL}")],
        resource_attrs=[_kv("service.name", "echo"), _kv("owner", f"e {FIXTURE_EMAIL}")],
    )
    req.resource_spans[0].scope_spans[0].scope.attributes.add().CopyFrom(_kv("desc", f"f {FIXTURE_EMAIL}"))
    report = w.walk_trace_request(req)
    dump = req.SerializeToString()
    assert FIXTURE_EMAIL.encode() not in dump
    assert dump.count(b"[REDACTED_EMAIL_ADDRESS_1]") >= 9
    assert report.spans_stripped == 0  # content only: nothing was refused
    span = _span(req)
    assert span.trace_id == TRACE_ID and span.start_time_unix_nano == EPOCH_NS


def test_a_flagged_attribute_key_or_number_strips_the_span_to_its_identity():
    for bad in (_kv(FIXTURE_EMAIL, "x"), _kv("card", CARD), _kv("card_float", float(CARD)),
                _kv("phone", 2125551234), _kv("nested", {FIXTURE_EMAIL: "x"}), _kv("arr", [CARD])):
        pairs = {"librerun.scope": "run", "agent.id": "echo-v1"}
        req = _trace_request(span_attrs=[_kv("librerun.scope", "run"), _kv("agent.id", "echo-v1"),
                                         _kv("secret", "keep me?"), bad])
        report = w.walk_trace_request(req, chassis=_chassis_for(req, pairs))
        span = _span(req)
        assert span.name == w.STRIPPED_SPAN_NAME, bad.key
        attrs = _attrs(span.attributes)
        assert attrs["librerun.scope"] == "run" and attrs["agent.id"] == "echo-v1"
        assert attrs[w.REDACTED_ATTRIBUTE] is True
        assert "secret" not in attrs and bad.key not in attrs
        assert span.trace_id == TRACE_ID and span.end_time_unix_nano == EPOCH_NS + 5_000_000
        assert len(span.events) == 0 and len(span.links) == 1 and len(span.links[0].attributes) == 0
        assert span.status.code == 2 and span.status.message == ""
        assert report.spans_stripped == 1
        assert FIXTURE_EMAIL.encode() not in req.SerializeToString()
        assert str(CARD).encode() not in req.SerializeToString()


def test_a_flagged_event_or_link_attribute_strips_the_span_too():
    req = _trace_request(event_attrs=[_kv("n", CARD)])
    w.walk_trace_request(req)
    assert _span(req).name == w.STRIPPED_SPAN_NAME
    req = _trace_request(link_attrs=[_kv(FIXTURE_EMAIL, 1)])
    w.walk_trace_request(req)
    assert _span(req).name == w.STRIPPED_SPAN_NAME


def test_bytes_values_are_dropped_never_decoded_and_counted():
    payload = FIXTURE_EMAIL.encode()
    req = _trace_request(
        span_attrs=[_kv("blob", payload), _kv("arr", [payload, "ok"]), _kv("ok", "fine")],
        resource_attrs=[_kv("service.name", "echo"), _kv("rblob", payload)],
    )
    report = w.walk_trace_request(req)
    dump = req.SerializeToString()
    assert payload not in dump
    span = _span(req)
    attrs = _attrs(span.attributes)
    assert "blob" not in attrs and attrs["ok"] == "fine"
    assert attrs[w.BYTES_DROPPED_ATTRIBUTE] == 2
    assert span.name == "work"  # bytes are dropped, not a refusal
    assert report.bytes_dropped == 3
    assert w.BYTES_DROPPED_ATTRIBUTE in _attrs(req.resource_spans[0].resource.attributes)


def test_trace_state_is_checked_never_rewritten_and_cleared_when_flagged():
    req = _trace_request(trace_state=f"vendor={FIXTURE_EMAIL}")
    _span(req).links[0].trace_state = "vendor=abc"
    report = w.walk_trace_request(req)
    assert _span(req).trace_state == ""
    assert _span(req).links[0].trace_state == "vendor=abc"
    assert _span(req).name == "work"
    assert report.trace_states_cleared == 1


def test_resource_and_scope_flags_drop_the_attribute_and_a_flagged_scope_name_strips_its_spans():
    req = _trace_request(resource_attrs=[_kv("service.name", "echo"), _kv(FIXTURE_EMAIL, "x"), _kv("n", CARD)])
    report = w.walk_trace_request(req)
    assert _attrs(req.resource_spans[0].resource.attributes) == {"service.name": "echo"}
    assert report.attributes_dropped == 2
    assert _span(req).name == "work"

    req = _trace_request(scope_name=FIXTURE_EMAIL)
    w.walk_trace_request(req)
    scope = req.resource_spans[0].scope_spans[0].scope
    assert scope.name == w.STRIPPED_SPAN_NAME and scope.version == ""
    assert _span(req).name == w.STRIPPED_SPAN_NAME


def test_log_records_with_a_flagged_key_number_or_event_name_are_dropped_and_counted():
    req = logs_service_pb2.ExportLogsServiceRequest()
    rl = req.resource_logs.add()
    sl = rl.scope_logs.add()
    sl.scope.name = "agent.echo"
    clean = sl.log_records.add()
    clean.body.string_value = f"contact {FIXTURE_EMAIL}"
    for bad in ({"key": FIXTURE_EMAIL}, {"number": CARD}, {"event": FIXTURE_EMAIL}, {"body": CARD}, {"bytes": True}):
        rec = sl.log_records.add()
        rec.body.string_value = "x"
        if "key" in bad:
            rec.attributes.add().CopyFrom(_kv(bad["key"], "v"))
        if "number" in bad:
            rec.attributes.add().CopyFrom(_kv("n", bad["number"]))
        if "event" in bad:
            rec.event_name = bad["event"]
        if "body" in bad:
            rec.body.int_value = bad["body"]
        if "bytes" in bad:
            rec.body.bytes_value = FIXTURE_EMAIL.encode()
    report = w.walk_logs_request(req)
    remaining = list(sl.log_records)
    # The clean record (content redacted) and the bytes-body record (body dropped) stay.
    assert len(remaining) == 2
    assert remaining[0].body.string_value == "contact [REDACTED_EMAIL_ADDRESS_1]"
    assert remaining[1].body.WhichOneof("value") is None
    assert _attrs(remaining[1].attributes)[w.BYTES_DROPPED_ATTRIBUTE] == 1
    assert report.records_dropped == 4
    assert report.dropped_by_scope == {"agent.echo": 4}
    assert FIXTURE_EMAIL.encode() not in req.SerializeToString()


def test_the_report_logs_counts_and_scopes_never_text(caplog):
    import logging

    req = logs_service_pb2.ExportLogsServiceRequest()
    sl = req.resource_logs.add().scope_logs.add()
    sl.scope.name = "agent.echo"
    rec = sl.log_records.add()
    rec.attributes.add().CopyFrom(_kv(FIXTURE_EMAIL, "v"))
    with caplog.at_level(logging.WARNING):
        w.log_report(w.walk_logs_request(req), signal="logs")
    assert "otlp_log_records_dropped" in caplog.text
    assert "agent.echo" in caplog.text
    assert FIXTURE_EMAIL not in caplog.text


# ------------------------------------- what the walk is allowed to skip --


def test_a_key_name_is_not_provenance():
    """An agent that picks a reserved-looking key is still walked.

    The skip was a prefix list, then an exact key set. Both are the same
    mistake: nothing stops an agent CHOOSING a key. A container that
    exports ``librerun.note`` or ``user.id`` carrying an address would
    have had it forwarded untouched, and neither name tells you who
    wrote it. Without the chassis's marker, nothing is exempt.
    """
    agent_chosen = [
        _kv("librerun.note", FIXTURE_EMAIL),      # the chassis's own namespace
        _kv("user.id", FIXTURE_EMAIL),            # an exact key the chassis does stamp
        _kv("agent.id", FIXTURE_EMAIL),
        _kv("run.id", FIXTURE_EMAIL),
        _kv("service.name", FIXTURE_EMAIL),
        _kv("telemetry.sdk.name", FIXTURE_EMAIL),
        _kv("agent.probe.text", FIXTURE_EMAIL),
        _kv("phase_notes", FIXTURE_EMAIL),
    ]
    for attribute in agent_chosen:
        req = _trace_request(span_attrs=[attribute])
        w.walk_trace_request(req)
        assert FIXTURE_EMAIL.encode() not in req.SerializeToString(), attribute.key
        assert _attrs(_span(req).attributes)[attribute.key] == PLACEHOLDER, attribute.key


def _chassis_for(req, pairs, *, resource=None):
    """A ChassisStamps that vouches for ``pairs`` on the request's span."""
    chassis = w.ChassisStamps()
    span = _span(req)
    chassis.record_span(
        int.from_bytes(span.trace_id, "big"), int.from_bytes(span.span_id, "big"), pairs
    )
    if resource:
        chassis.set_resource(resource)
    return chassis


def test_the_chassis_record_exempts_its_own_pairs():
    """The chassis's own ids must survive intact.

    Walking them would destroy the trace's identity — the intake
    recognizers read a UUID's digit run as a card number and
    ``librerun-backend`` as a person — so the chassis records the exact
    key→value pairs it wrote and only those are left alone.
    """
    pairs = {
        "librerun.scope": "run",
        "agent.id": "echo-v1",
        "tenant.id": "3f1c5a1e-0000-4000-8000-000000000001",
        "run.id": "9a2b7c3d-0000-4000-8000-000000000002",
    }
    req = _trace_request(
        span_attrs=[_kv(k, v) for k, v in pairs.items()],
        resource_attrs=[_kv("service.name", "librerun-backend")],
    )
    report = w.walk_trace_request(
        req, chassis=_chassis_for(req, pairs, resource={"service.name": "librerun-backend"})
    )
    assert {k: _attrs(_span(req).attributes)[k] for k in pairs} == pairs
    assert _attrs(req.resource_spans[0].resource.attributes)["service.name"] == "librerun-backend"
    assert report.spans_stripped == 0


def test_a_span_name_the_chassis_wrote_survives_the_walk():
    """A span name is CONTENT — an agent names its own spans — so the
    walk redacts it, and the GenAI convention makes the gateway's name
    `{operation} {model}`. `chat claude-sonnet-4-6` reaches the viewer as
    `chat [REDACTED_PERSON_1]` unless the chassis vouches for it, and the
    name is the first thing an operator reads.

    Asserted both ways: the first half proves the recognizers really do
    take it, so this stops proving anything loudly rather than quietly.
    """
    name = "chat claude-sonnet-4-6"

    walked = _trace_request(name=name)
    w.walk_trace_request(walked, chassis=w.ChassisStamps())
    assert _span(walked).name != name

    stamped = _trace_request(name=name)
    w.walk_trace_request(
        stamped, chassis=_chassis_for(stamped, {w.STAMPED_NAME_KEY: name})
    )
    assert _span(stamped).name == name


def test_a_record_does_not_vouch_for_a_different_name():
    """Value-keyed, like every other pair: a record made for one name
    exempts that name and no other."""
    req = _trace_request(name="chat claude-sonnet-4-6")
    w.walk_trace_request(
        req, chassis=_chassis_for(req, {w.STAMPED_NAME_KEY: "chat gpt-4o"})
    )
    assert _span(req).name != "chat claude-sonnet-4-6"


def test_amend_adds_to_a_record_rather_than_replacing_it():
    """Not every pair the chassis writes is known when the span opens.

    The gateway sets the resolved provider and model at the start and the
    reply's model when it lands; a second ``record_span`` would drop the
    first set, identity attributes included.
    """
    chassis = w.ChassisStamps()
    chassis.record_span(1, 2, {"librerun.scope": "run", "run.id": "abc"})
    chassis.amend_span(1, 2, {"gen_ai.response.model": "claude-sonnet-4-20250514"})
    assert chassis.take_span(1, 2) == {
        "librerun.scope": "run",
        "run.id": "abc",
        "gen_ai.response.model": "claude-sonnet-4-20250514",
    }
    # And on a span nothing was recorded for, it records.
    chassis.amend_span(3, 4, {"gen_ai.system": "anthropic"})
    assert chassis.take_span(3, 4) == {"gen_ai.system": "anthropic"}


def test_a_resolved_model_name_survives_only_because_it_is_stamped():
    """The one fact D13 is about, and the walk would take it.

    The recognizers read a dated model name as a person's, exactly as
    they read ``librerun-backend``. The first half asserts that — so if
    they ever stop, this test says the exemption has stopped proving
    anything, rather than passing on regardless.
    """
    model = "claude-sonnet-4-20250514"

    walked = _trace_request(span_attrs=[_kv("gen_ai.request.model", model)])
    w.walk_trace_request(walked, chassis=w.ChassisStamps())
    assert _attrs(_span(walked).attributes)["gen_ai.request.model"] != model

    stamped = _trace_request(span_attrs=[_kv("gen_ai.request.model", model)])
    w.walk_trace_request(
        stamped, chassis=_chassis_for(stamped, {"gen_ai.request.model": model})
    )
    assert _attrs(_span(stamped).attributes)["gen_ai.request.model"] == model


def test_a_record_only_covers_the_value_it_recorded():
    """Agent code overwriting a stamped key is walked.

    In-process agent code runs after the enricher and can set `user.id`
    on the chassis's own span. The record holds the VALUE, so a changed
    one no longer matches and goes through the walk — which is the
    difference between vouching for a write and vouching for a name.
    """
    req = _trace_request(span_attrs=[
        _kv("user.id", FIXTURE_EMAIL),   # what the agent overwrote it with
        _kv("agent.id", "echo-v1"),      # left as the chassis wrote it
    ])
    w.walk_trace_request(req, chassis=_chassis_for(req, {"user.id": "u-1", "agent.id": "echo-v1"}))
    kept = _attrs(_span(req).attributes)
    assert kept["user.id"] == PLACEHOLDER
    assert kept["agent.id"] == "echo-v1"
    assert FIXTURE_EMAIL.encode() not in req.SerializeToString()


def test_an_agent_cannot_forge_the_record_by_writing_an_attribute():
    """The proof does not live in the thing it authenticates.

    The record was a span attribute, and a span's attributes are a
    mutable collection the agent shares: in-process code could set
    ``librerun.stamped`` to its own map alongside the values it wanted
    exempted. It is kept beside the span now, keyed by the span's ids,
    where agent code has no handle — and an attribute by that name can
    only be a forgery, so it is deleted wherever it appears.
    """
    import json as _json

    forged = _json.dumps({"note": FIXTURE_EMAIL, "librerun.note": FIXTURE_EMAIL})
    req = _trace_request(span_attrs=[
        _kv("note", FIXTURE_EMAIL),
        _kv("librerun.note", FIXTURE_EMAIL),
        _kv(w.STAMP_MARKER, forged),
    ])
    w.walk_trace_request(req, chassis=w.ChassisStamps())
    kept = _attrs(_span(req).attributes)
    assert w.STAMP_MARKER not in kept, "a forged marker was forwarded"
    assert kept["note"] == PLACEHOLDER
    assert kept["librerun.note"] == PLACEHOLDER
    assert FIXTURE_EMAIL.encode() not in req.SerializeToString()


def test_a_stripped_span_drops_the_attribute_that_caused_the_stripping():
    """The number that flagged the span must not survive it.

    A span is stripped to "its identity", and identity was every
    ``librerun.*`` key — so a span stripped BECAUSE of
    ``librerun.card=4111111111111111`` kept the card: the walk flags a
    number without removing it, and the filter then preserved it by
    name. Identity is what the chassis vouched for, by value.
    """
    req = _trace_request(span_attrs=[
        _kv("agent.id", "echo-v1"),
        _kv("librerun.card", CARD),
        _kv("librerun.note", "kept by name, once"),
    ])
    report = w.walk_trace_request(req, chassis=_chassis_for(req, {"agent.id": "echo-v1"}))
    span = _span(req)
    assert span.name == w.STRIPPED_SPAN_NAME and report.spans_stripped == 1
    kept = _attrs(span.attributes)
    assert kept["agent.id"] == "echo-v1"
    assert "librerun.card" not in kept and "librerun.note" not in kept
    assert str(CARD).encode() not in req.SerializeToString()


def test_a_stripped_span_keeps_only_identity_never_an_agent_chosen_key():
    """The same hole on the other path: a span stripped for a flagged key
    kept everything under ``agent.``, so the address survived the very
    act of stripping the span that carried it. What it keeps now is the
    ids an operator navigates by — already walked, so a flagged one is
    gone rather than preserved."""
    req = _trace_request(span_attrs=[
        _kv("agent.id", "echo-v1"),
        _kv("agent.probe.text", FIXTURE_EMAIL),
        _kv("card", CARD),  # the flag that strips the span
    ])
    w.walk_trace_request(req, chassis=_chassis_for(req, {"agent.id": "echo-v1"}))
    span = _span(req)
    assert span.name == w.STRIPPED_SPAN_NAME
    attrs = _attrs(span.attributes)
    assert attrs["agent.id"] == "echo-v1"
    assert "agent.probe.text" not in attrs
    assert FIXTURE_EMAIL.encode() not in req.SerializeToString()


def test_removing_one_positions_walk_makes_the_string_sweep_fail():
    """The other half of totality: classified is not the same as walked.

    ``test_every_descriptor_field_is_classified`` proves no field is
    missing from the table, and ``test_totality_fails_on_an_unclassified
    _field`` proves that check looks. Neither would notice a field that
    is *in* the table under the wrong class — CONTENT demoted to
    METADATA walks nothing and reports nothing, and on a clean tree the
    sweep above would still pass on the eight positions that remain.

    So each string position is demoted in turn and the sweep must fail.
    A position that stays clean with its own walk removed was never
    being walked by this test in the first place.
    """
    # One payload per position, carrying the fixture ONLY there: a
    # payload that also carried a flagged key would strip the whole span
    # and hide the address for a reason that has nothing to do with the
    # position under test.
    positions = [
        (("opentelemetry.proto.common.v1.AnyValue", "string_value"),
         {"span_attrs": [_kv("note", f"see {FIXTURE_EMAIL}")]}),
        (("opentelemetry.proto.trace.v1.Span", "name"),
         {"name": f"call {FIXTURE_EMAIL}"}),
        (("opentelemetry.proto.trace.v1.Status", "message"),
         {"status_message": f"failed for {FIXTURE_EMAIL}"}),
        (("opentelemetry.proto.trace.v1.Span.Event", "name"),
         {"event_name": f"mailed {FIXTURE_EMAIL}"}),
        (("opentelemetry.proto.common.v1.KeyValue", "key"),
         {"span_attrs": [_kv(FIXTURE_EMAIL, "keyed")]}),
    ]
    for (message, field), payload in positions:
        assert (message, field) in w.CLASSIFICATION, f"{message}.{field} left the table"
        # The positive side first: with the table intact the fixture goes.
        req = _trace_request(**payload)
        w.walk_trace_request(req)
        assert FIXTURE_EMAIL.encode() not in req.SerializeToString(), f"{message}.{field}"

        original = w.CLASSIFICATION[(message, field)]
        w.CLASSIFICATION[(message, field)] = w.METADATA
        try:
            req = _trace_request(**payload)
            w.walk_trace_request(req)
            assert FIXTURE_EMAIL.encode() in req.SerializeToString(), (
                f"demoting {message}.{field} to metadata changed nothing — "
                "the position is not what its assertion is testing"
            )
        finally:
            w.CLASSIFICATION[(message, field)] = original

    # And with the table intact the sweep is clean again, so the loop
    # above restored what it borrowed.
    req = _trace_request(name=f"call {FIXTURE_EMAIL}", status_message=f"failed for {FIXTURE_EMAIL}")
    w.walk_trace_request(req)
    assert FIXTURE_EMAIL.encode() not in req.SerializeToString()
