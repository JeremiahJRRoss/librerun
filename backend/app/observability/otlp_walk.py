"""Walk an OTLP export request before it leaves the box (blueprint S4;
gaps H9, H10).

An agent's own instrumentation is a door too: every span, span event,
log record and resource attribute the backend exports — and, through
the relay, everything a container exports — passes this walker first.
The classification is **by field over the protobuf descriptor**, so the
list of positions is the protocol's and not ours: every field of
``ExportTraceServiceRequest`` and ``ExportLogsServiceRequest`` is in
exactly one class below, and ``tests/test_otlp_walk.py`` walks the
descriptor and fails on any field it cannot place — a new field in an
OTLP upgrade fails the suite until it is classified.

Classes:

- ``content``   — free text an agent supplies: a span's name, an event's
                  name, a status message, a log record's body and
                  severity text, every ``string_value``. Redacted in
                  place with the intake placeholders (the boundary
                  policy: dates and place names stay).
- ``identifier`` — a name the protocol uses as a key: attribute keys,
                  the instrumentation scope's name and version, entity
                  references, schema URLs, ``trace_state`` (a string an
                  upstream caller controls) and a log record's
                  ``event_name``. Checked as its literal text, never
                  rewritten: a flagged one refuses the position — the
                  span is stripped to its identity, the log record is
                  dropped, the trace state cleared, the attribute
                  removed from a resource or scope.
- ``number``    — ``int_value`` and ``double_value``: checked by the
                  walker's number rule (an integral double as the
                  integer it is); a flagged one refuses like an
                  identifier. The one documented footgun is an integer
                  attribute that is a Luhn-valid card number or a
                  region-valid phone number.
- ``bytes``     — ``bytes_value`` anywhere: **dropped** before export,
                  never forwarded and never decoded, the span or record
                  carrying ``librerun.bytes_dropped`` with the count.
- ``metadata``  — the protocol's own scalars, which no agent supplies as
                  content and which must never be checked: timestamps
                  (an epoch in nanoseconds is nineteen digits, which the
                  stage-1 card pattern would flag on every span), trace
                  and span ids, kind, flags, status code, severity
                  number, dropped counts, the string-dictionary
                  ``*_strindex`` fields, booleans.
- ``recurse``   — message-typed fields, walked structurally.

A stripped span keeps its ids, timing, kind, status code, the stamped
``librerun.*`` and ``agent.*`` attributes and the name ``redacted``, so
the trace tree keeps its shape; a dropped log record is one
platform-plane warning naming the scope and the count, never the text.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import structlog
from google.protobuf.descriptor import FieldDescriptor
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2

from app.services import pii_service

logger = structlog.get_logger(__name__)

CONTENT = "content"
IDENTIFIER = "identifier"
NUMBER = "number"
BYTES = "bytes"
METADATA = "metadata"
RECURSE = "recurse"

BYTES_DROPPED_ATTRIBUTE = "librerun.bytes_dropped"
REDACTED_ATTRIBUTE = "librerun.redacted"
REDACTED_REASON_ATTRIBUTE = "librerun.redacted.reason"
STRIPPED_SPAN_NAME = "redacted"
# An attribute the walk leaves alone, because the CHASSIS wrote it: a run
# id, a tenant id, a service name. Walking one would destroy it — the
# intake recognizers read a UUID's digit run as a card number and
# ``librerun-backend`` as a person's name — and the trace's identity is
# what an operator navigates by.
#
# Which attributes those are cannot be decided from the key. It was: a
# prefix list (``agent.``, ``run.``, ``user.``, ``librerun.`` and five
# more), and then an exact key set. Both are the same mistake, because
# nothing stops an agent *choosing* a key. A container that exports
# ``librerun.note`` or ``user.id`` carrying an address gets it forwarded
# unwalked, and a key name is not provenance.
#
# So the chassis says so, per container of attributes, in a marker it
# writes itself: ``librerun.stamped`` holds the exact key→value pairs it
# set. A key skips the walk only when the marker names it AND the value
# still matches, so agent code overwriting a stamped key is walked like
# anything else. The marker is removed before anything is forwarded.
#
# The marker is unforgeable from outside because the relay deletes any
# that arrives from a container (``_strip_stamp_markers``) before it
# walks, and stamps the identity attributes afterwards, re-deriving them
# from the run token rather than trusting what was sent.
# The name of a marker that must never survive: an attribute called
# this can only have come from an agent trying to exempt itself.
STAMP_MARKER = "librerun.stamped"

# The one reserved entry in a chassis record that is not an attribute: a
# span's NAME, which is CONTENT by default and rightly so — an agent
# names its own spans. The gateway's are the exception, because the
# GenAI convention makes the name `{operation} {model}` and a model name
# is what the recognizers destroy: `chat claude-sonnet-4-6` reaches the
# viewer as `chat [REDACTED_PERSON_1]`, which is the one thing D13 is
# about, on the field an operator reads first. Value-keyed like every
# other pair — the name skips the walk only when the chassis recorded
# THAT name — and unreachable from the span, so an agent cannot claim it.
STAMPED_NAME_KEY = "librerun.span_name"


class ChassisStamps:
    """What the chassis wrote, recorded where agent code cannot reach it.

    NOT a span attribute. A span's attributes are a mutable collection
    the agent shares, so a marker kept there authenticates nothing —
    in-process agent code can set ``librerun.stamped`` to
    ``{"note": "<an address>"}`` alongside a matching ``note`` and exempt
    itself. Carrying the proof inside the thing it is meant to
    authenticate is the same mistake as reading provenance off a key.

    So the span enricher records here, keyed by the span's own ids, and
    the export reads and removes it. Bounded: a span that is created and
    never exported would otherwise leave its entry behind, so the oldest
    are dropped past ``limit`` — losing an exemption costs a mangled id,
    never a leak.
    """

    def __init__(self, limit: int = 20_000) -> None:
        self._lock = threading.Lock()
        self._spans: "OrderedDict[tuple[int, int], dict]" = OrderedDict()
        self._resource: dict = {}
        self._limit = limit

    def record_span(self, trace_id: int, span_id: int, pairs: dict) -> None:
        with self._lock:
            self._spans[(trace_id, span_id)] = {k: str(v) for k, v in pairs.items()}
            while len(self._spans) > self._limit:
                self._spans.popitem(last=False)

    def amend_span(self, trace_id: int, span_id: int, pairs: dict) -> None:
        """Add to what was already recorded for a span, rather than
        replacing it.

        A span's chassis-written pairs are not all known when it opens:
        the gateway sets the resolved model and provider at the start and
        the token counts and cost when the reply lands, and a second
        ``record_span`` would drop the first set — the identity
        attributes among them.
        """
        with self._lock:
            key = (trace_id, span_id)
            existing = self._spans.get(key)
            if existing is None:
                self._spans[key] = {k: str(v) for k, v in pairs.items()}
                while len(self._spans) > self._limit:
                    self._spans.popitem(last=False)
                return
            existing.update({k: str(v) for k, v in pairs.items()})

    def take_span(self, trace_id: int, span_id: int) -> dict:
        with self._lock:
            return self._spans.pop((trace_id, span_id), {})

    def set_resource(self, pairs: dict) -> None:
        with self._lock:
            self._resource = {k: str(v) for k, v in pairs.items()}

    def resource(self) -> dict:
        with self._lock:
            return dict(self._resource)

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()
            self._resource = {}


stamps = ChassisStamps()


def _drop_stamp_marker(kvs) -> None:
    """Delete any ``librerun.stamped`` attribute, wherever it came from.

    The chassis does not write one, so an attribute by this name is an
    agent's forgery — and it would be exported as content besides.
    """
    for index in range(len(kvs) - 1, -1, -1):
        if kvs[index].key == STAMP_MARKER:
            del kvs[index]


def is_stamped(key: str, value, stamped: dict) -> bool:
    """True when the chassis wrote exactly this key and value."""
    if not stamped or key not in stamped:
        return False
    which = value.WhichOneof("value")
    if which is None:
        return False
    return str(getattr(value, which)) == str(stamped[key])

# (message full name, field name) -> class. Every scalar field of both
# export requests must appear here; the totality test proves it.
CLASSIFICATION: dict[tuple[str, str], str] = {
    # -- common ----------------------------------------------------------
    ("opentelemetry.proto.common.v1.KeyValue", "key"): IDENTIFIER,
    ("opentelemetry.proto.common.v1.KeyValue", "key_strindex"): METADATA,
    ("opentelemetry.proto.common.v1.KeyValue", "value"): RECURSE,
    ("opentelemetry.proto.common.v1.AnyValue", "string_value"): CONTENT,
    ("opentelemetry.proto.common.v1.AnyValue", "string_value_strindex"): METADATA,
    ("opentelemetry.proto.common.v1.AnyValue", "bool_value"): METADATA,
    ("opentelemetry.proto.common.v1.AnyValue", "int_value"): NUMBER,
    ("opentelemetry.proto.common.v1.AnyValue", "double_value"): NUMBER,
    ("opentelemetry.proto.common.v1.AnyValue", "array_value"): RECURSE,
    ("opentelemetry.proto.common.v1.AnyValue", "kvlist_value"): RECURSE,
    ("opentelemetry.proto.common.v1.AnyValue", "bytes_value"): BYTES,
    ("opentelemetry.proto.common.v1.ArrayValue", "values"): RECURSE,
    ("opentelemetry.proto.common.v1.KeyValueList", "values"): RECURSE,
    ("opentelemetry.proto.common.v1.InstrumentationScope", "name"): IDENTIFIER,
    ("opentelemetry.proto.common.v1.InstrumentationScope", "version"): IDENTIFIER,
    ("opentelemetry.proto.common.v1.InstrumentationScope", "attributes"): RECURSE,
    ("opentelemetry.proto.common.v1.InstrumentationScope", "dropped_attributes_count"): METADATA,
    ("opentelemetry.proto.common.v1.EntityRef", "schema_url"): IDENTIFIER,
    ("opentelemetry.proto.common.v1.EntityRef", "type"): IDENTIFIER,
    ("opentelemetry.proto.common.v1.EntityRef", "id_keys"): IDENTIFIER,
    ("opentelemetry.proto.common.v1.EntityRef", "description_keys"): IDENTIFIER,
    # -- resource --------------------------------------------------------
    ("opentelemetry.proto.resource.v1.Resource", "attributes"): RECURSE,
    ("opentelemetry.proto.resource.v1.Resource", "dropped_attributes_count"): METADATA,
    ("opentelemetry.proto.resource.v1.Resource", "entity_refs"): RECURSE,
    # -- traces ----------------------------------------------------------
    ("opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest", "resource_spans"): RECURSE,
    ("opentelemetry.proto.trace.v1.ResourceSpans", "resource"): RECURSE,
    ("opentelemetry.proto.trace.v1.ResourceSpans", "scope_spans"): RECURSE,
    ("opentelemetry.proto.trace.v1.ResourceSpans", "schema_url"): IDENTIFIER,
    ("opentelemetry.proto.trace.v1.ScopeSpans", "scope"): RECURSE,
    ("opentelemetry.proto.trace.v1.ScopeSpans", "spans"): RECURSE,
    ("opentelemetry.proto.trace.v1.ScopeSpans", "schema_url"): IDENTIFIER,
    ("opentelemetry.proto.trace.v1.Span", "trace_id"): METADATA,
    ("opentelemetry.proto.trace.v1.Span", "span_id"): METADATA,
    ("opentelemetry.proto.trace.v1.Span", "trace_state"): IDENTIFIER,
    ("opentelemetry.proto.trace.v1.Span", "parent_span_id"): METADATA,
    ("opentelemetry.proto.trace.v1.Span", "flags"): METADATA,
    ("opentelemetry.proto.trace.v1.Span", "name"): CONTENT,
    ("opentelemetry.proto.trace.v1.Span", "kind"): METADATA,
    ("opentelemetry.proto.trace.v1.Span", "start_time_unix_nano"): METADATA,
    ("opentelemetry.proto.trace.v1.Span", "end_time_unix_nano"): METADATA,
    ("opentelemetry.proto.trace.v1.Span", "attributes"): RECURSE,
    ("opentelemetry.proto.trace.v1.Span", "dropped_attributes_count"): METADATA,
    ("opentelemetry.proto.trace.v1.Span", "events"): RECURSE,
    ("opentelemetry.proto.trace.v1.Span", "dropped_events_count"): METADATA,
    ("opentelemetry.proto.trace.v1.Span", "links"): RECURSE,
    ("opentelemetry.proto.trace.v1.Span", "dropped_links_count"): METADATA,
    ("opentelemetry.proto.trace.v1.Span", "status"): RECURSE,
    ("opentelemetry.proto.trace.v1.Span.Event", "time_unix_nano"): METADATA,
    ("opentelemetry.proto.trace.v1.Span.Event", "name"): CONTENT,
    ("opentelemetry.proto.trace.v1.Span.Event", "attributes"): RECURSE,
    ("opentelemetry.proto.trace.v1.Span.Event", "dropped_attributes_count"): METADATA,
    ("opentelemetry.proto.trace.v1.Span.Link", "trace_id"): METADATA,
    ("opentelemetry.proto.trace.v1.Span.Link", "span_id"): METADATA,
    ("opentelemetry.proto.trace.v1.Span.Link", "trace_state"): IDENTIFIER,
    ("opentelemetry.proto.trace.v1.Span.Link", "attributes"): RECURSE,
    ("opentelemetry.proto.trace.v1.Span.Link", "dropped_attributes_count"): METADATA,
    ("opentelemetry.proto.trace.v1.Span.Link", "flags"): METADATA,
    ("opentelemetry.proto.trace.v1.Status", "message"): CONTENT,
    ("opentelemetry.proto.trace.v1.Status", "code"): METADATA,
    # -- logs ------------------------------------------------------------
    ("opentelemetry.proto.collector.logs.v1.ExportLogsServiceRequest", "resource_logs"): RECURSE,
    ("opentelemetry.proto.logs.v1.ResourceLogs", "resource"): RECURSE,
    ("opentelemetry.proto.logs.v1.ResourceLogs", "scope_logs"): RECURSE,
    ("opentelemetry.proto.logs.v1.ResourceLogs", "schema_url"): IDENTIFIER,
    ("opentelemetry.proto.logs.v1.ScopeLogs", "scope"): RECURSE,
    ("opentelemetry.proto.logs.v1.ScopeLogs", "log_records"): RECURSE,
    ("opentelemetry.proto.logs.v1.ScopeLogs", "schema_url"): IDENTIFIER,
    ("opentelemetry.proto.logs.v1.LogRecord", "time_unix_nano"): METADATA,
    ("opentelemetry.proto.logs.v1.LogRecord", "observed_time_unix_nano"): METADATA,
    ("opentelemetry.proto.logs.v1.LogRecord", "severity_number"): METADATA,
    ("opentelemetry.proto.logs.v1.LogRecord", "severity_text"): CONTENT,
    ("opentelemetry.proto.logs.v1.LogRecord", "body"): RECURSE,
    ("opentelemetry.proto.logs.v1.LogRecord", "attributes"): RECURSE,
    ("opentelemetry.proto.logs.v1.LogRecord", "dropped_attributes_count"): METADATA,
    ("opentelemetry.proto.logs.v1.LogRecord", "flags"): METADATA,
    ("opentelemetry.proto.logs.v1.LogRecord", "trace_id"): METADATA,
    ("opentelemetry.proto.logs.v1.LogRecord", "span_id"): METADATA,
    ("opentelemetry.proto.logs.v1.LogRecord", "event_name"): IDENTIFIER,
}


def descriptor_fields(descriptor, prefix: str = "", seen: frozenset = frozenset()):
    """Every (message, field, path) of a descriptor, message types entered
    once — the enumeration the totality test and the walker share."""
    for f in descriptor.fields:
        path = f"{prefix}.{f.name}" if prefix else f.name
        yield descriptor.full_name, f.name, path, f
        if f.type == FieldDescriptor.TYPE_MESSAGE:
            key = f.message_type.full_name
            if key in seen:
                continue
            yield from descriptor_fields(f.message_type, path, seen | {key})


def classify(message_name: str, field_name: str) -> str | None:
    return CLASSIFICATION.get((message_name, field_name))


@dataclass
class WalkReport:
    """What the walk did — counts only, never text."""

    redactions: int = 0
    spans_stripped: int = 0
    records_dropped: int = 0
    attributes_dropped: int = 0
    bytes_dropped: int = 0
    trace_states_cleared: int = 0
    # Blueprint S4c: content positions the detector could not walk, and
    # spans/records stamped because the operator opted into regex-only.
    detector_unavailable: int = 0
    degraded: int = 0
    dropped_by_scope: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "redactions": self.redactions,
            "spans_stripped": self.spans_stripped,
            "records_dropped": self.records_dropped,
            "attributes_dropped": self.attributes_dropped,
            "bytes_dropped": self.bytes_dropped,
            "trace_states_cleared": self.trace_states_cleared,
            "detector_unavailable": self.detector_unavailable,
            "degraded": self.degraded,
        }


@dataclass
class _Outcome:
    """The result of walking one attribute list or value."""

    flagged: str | None = None  # "<kind>:<pii_type>" of the first refusal
    bytes_dropped: int = 0


# Blueprint S4c (gap H15): the flag a CONTENT position raises when the
# detector could not run. It travels the same way an identifier's or a
# number's flag does — the span it is in is stripped to its identity,
# the log record it is in is dropped, the resource or scope attribute it
# is in is removed — because "we could not look at this string" and "we
# looked and it was PII" have exactly the same right answer on the way
# out of the box.
DETECTOR_FLAG = "content:pii_detector_unavailable"


def _redact(text: str, report: WalkReport) -> tuple[str, str | None]:
    """The redacted text, and the flag when the detector could not run."""
    if not text:
        return text, None
    try:
        redacted, applied = pii_service.redact(
            text,
            skip_entities=pii_service.BOUNDARY_SKIP_ENTITIES,
            quiet=True,
            stage="otlp_walk",
        )
    except pii_service.PiiDetectorUnavailable:
        report.detector_unavailable += 1
        return text, DETECTOR_FLAG
    report.redactions += len(applied)
    return redacted, None


def _identifier_flag(text: str) -> str | None:
    if not text:
        return None
    finding = pii_service.check_identifier(text, path="identifier")
    return f"identifier:{finding.pii_type}" if finding is not None else None


def _number_flag(value, keys: tuple = ()) -> str | None:
    """The walker's number rule with the attribute key path as context —
    an epoch under ``created_at`` is a timestamp, not a card."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not (value.is_integer() and 0 <= value < 2**53):
            return None
        value = int(value)
    if not isinstance(value, int) or value < 0:
        return None
    finding = pii_service.check_number_text(str(value), keys, path="number")
    return f"number:{finding.pii_type}" if finding is not None else None


def _apply(obj, field_name: str, report: WalkReport, *, keys: tuple = ()) -> str | None:
    """Do to ``obj.field_name`` what CLASSIFICATION says, and only that.

    The table is the walk, not a description of it. It was a description
    of it: every scalar position was handled by hand, and ``classify``
    was called by nothing but the totality test — so the table could say
    CONTENT while the code redacted nothing, or say METADATA while the
    code redacted, and the suite would be green either way. A complete
    table proves nothing about the walk if the walk never reads it.

    Returns the flag for an identifier or number position (the caller
    decides what a flag means for its container). Content is redacted in
    place and returns None — unless the detector could not run at all
    (S4c), which is a flag too: the caller strips or drops the container
    rather than forwarding a string nothing walked.
    """
    kind = classify(obj.DESCRIPTOR.full_name, field_name)
    if kind == CONTENT:
        value = getattr(obj, field_name)
        if value:
            redacted, flag = _redact(value, report)
            setattr(obj, field_name, redacted)
            return flag
        return None
    if kind == IDENTIFIER:
        value = getattr(obj, field_name)
        return _identifier_flag(value) if value else None
    if kind == NUMBER:
        return _number_flag(getattr(obj, field_name), keys)
    if kind == BYTES:
        obj.ClearField(field_name)
        return None
    # METADATA is left alone; RECURSE is the caller's to descend.
    return None


def _walk_any_value(
    value: common_pb2.AnyValue, report: WalkReport, keys: tuple = ()
) -> _Outcome:
    """Walk one ``AnyValue`` in place. A bytes value is cleared (dropped)
    and counted; the caller decides what a flag means for its container.
    ``keys`` is the attribute key path above the value, the number rule's
    context."""
    out = _Outcome()
    which = value.WhichOneof("value")
    if which is None:
        return out
    kind = classify(value.DESCRIPTOR.full_name, which)
    if kind == RECURSE:
        if which == "array_value":
            for item in value.array_value.values:
                inner = _walk_any_value(item, report, keys)
                out.bytes_dropped += inner.bytes_dropped
                out.flagged = out.flagged or inner.flagged
        elif which == "kvlist_value":
            inner = _walk_key_values(
                value.kvlist_value.values, report, drop_flagged=False, keys=keys
            )
            out.bytes_dropped += inner.bytes_dropped
            out.flagged = out.flagged or inner.flagged
        return out
    if kind == BYTES:
        out.bytes_dropped += 1
    out.flagged = _apply(value, which, report, keys=keys)
    # bool_value, *_strindex and an unset value are metadata.
    return out


def _walk_key_values(
    kvs, report: WalkReport, *, drop_flagged: bool, keys: tuple = (), stamped: dict | None = None
) -> _Outcome:
    """Walk a repeated ``KeyValue`` in place. With ``drop_flagged`` a
    flagged attribute (key or number) is removed and counted — the rule
    for a resource or a scope; without it the flag is returned for the
    caller — the rule for a span (stripped) or a log record (dropped).
    Bytes values are dropped either way."""
    out = _Outcome()
    kept: list = []
    for kv in kvs:
        if is_stamped(kv.key, kv.value, stamped or {}):
            kept.append(kv)
            continue
        flag = _apply(kv, "key", report)
        inner = (
            _walk_any_value(kv.value, report, keys + (kv.key,))
            if flag is None
            else _Outcome(flagged=flag)
        )
        out.bytes_dropped += inner.bytes_dropped
        if inner.flagged:
            if drop_flagged:
                report.attributes_dropped += 1
                continue
            out.flagged = out.flagged or inner.flagged
        if kv.value.WhichOneof("value") is None and inner.bytes_dropped:
            # A bare bytes attribute: nothing left to forward.
            continue
        kept.append(kv)
    if len(kept) != len(kvs):
        copies = [common_pb2.KeyValue() for _ in kept]
        for copy, kv in zip(copies, kept):
            copy.CopyFrom(kv)
        del kvs[:]
        for copy in copies:
            kvs.add().CopyFrom(copy)
    return out


def _set_int_attribute(kvs, key: str, value: int) -> None:
    for kv in kvs:
        if kv.key == key:
            kv.value.int_value = int(value)
            return
    kv = kvs.add()
    kv.key = key
    kv.value.int_value = int(value)


def _set_attribute(kvs, key: str, value) -> None:
    for kv in kvs:
        if kv.key == key:
            break
    else:
        kv = kvs.add()
        kv.key = key
    if isinstance(value, bool):
        kv.value.bool_value = value
    elif isinstance(value, int):
        kv.value.int_value = value
    else:
        kv.value.string_value = str(value)


def _walk_entity_refs(refs, report: WalkReport) -> None:
    kept = []
    for ref in refs:
        # Singles through the table; the two repeated string fields are
        # checked with the same rule the table gives them, since `_apply`
        # addresses one scalar and these are lists of them.
        flagged = _apply(ref, "schema_url", report) or _apply(ref, "type", report)
        for field_name in ("id_keys", "description_keys"):
            if classify(ref.DESCRIPTOR.full_name, field_name) != IDENTIFIER:
                continue
            flagged = flagged or next(
                (f for f in (_identifier_flag(t) for t in getattr(ref, field_name)) if f), None
            )
        if flagged:
            report.attributes_dropped += 1
            continue
        kept.append(ref)
    if len(kept) != len(refs):
        copies = [common_pb2.EntityRef() for _ in kept]
        for copy, ref in zip(copies, kept):
            copy.CopyFrom(ref)
        del refs[:]
        for copy in copies:
            refs.add().CopyFrom(copy)


def _walk_resource(resource, report: WalkReport, stamped: dict) -> None:
    _drop_stamp_marker(resource.attributes)
    out = _walk_key_values(resource.attributes, report, drop_flagged=True, stamped=stamped)
    if out.bytes_dropped:
        report.bytes_dropped += out.bytes_dropped
        _set_int_attribute(resource.attributes, BYTES_DROPPED_ATTRIBUTE, out.bytes_dropped)
    _walk_entity_refs(resource.entity_refs, report)


def _walk_scope(scope, report: WalkReport) -> bool:
    """Walk an instrumentation scope in place. Returns True when the
    scope's name or version was flagged — the caller strips or drops
    everything under it, because the name is part of every child's
    identity."""
    out = _walk_key_values(scope.attributes, report, drop_flagged=True)
    if out.bytes_dropped:
        report.bytes_dropped += out.bytes_dropped
        _set_int_attribute(scope.attributes, BYTES_DROPPED_ATTRIBUTE, out.bytes_dropped)
    flagged = _apply(scope, "name", report) or _apply(scope, "version", report)
    if flagged:
        scope.name = STRIPPED_SPAN_NAME
        scope.ClearField("version")
        report.reasons.append(f"scope:{flagged}")
        return True
    return False


def _clear_trace_state_if_flagged(message, report: WalkReport) -> None:
    if _apply(message, "trace_state", report):
        message.ClearField("trace_state")
        report.trace_states_cleared += 1


def _strip_span(span, reason: str, report: WalkReport, stamped: dict) -> None:
    """Strip a span to its identity: ids, timing, kind, status code, the
    chassis's own stamps, the name ``redacted``.

    "Identity" is what the CHASSIS stamped, by value — not what looks
    like an id. It was every ``librerun.*`` key, so a span stripped
    *because* of ``librerun.card=4111111111111111`` kept the number that
    caused the stripping: the walk flags a number without removing it,
    and the filter then preserved it by name.
    """
    span.name = STRIPPED_SPAN_NAME
    span.status.ClearField("message")
    del span.events[:]
    for link in span.links:
        del link.attributes[:]
        _clear_trace_state_if_flagged(link, report)
    kept = [
        kv for kv in span.attributes
        if is_stamped(kv.key, kv.value, stamped)
        and not kv.key.startswith((REDACTED_ATTRIBUTE, BYTES_DROPPED_ATTRIBUTE))
    ]
    copies = [common_pb2.KeyValue() for _ in kept]
    for copy, kv in zip(copies, kept):
        copy.CopyFrom(kv)
    del span.attributes[:]
    for copy in copies:
        span.attributes.add().CopyFrom(copy)
    _set_attribute(span.attributes, REDACTED_ATTRIBUTE, True)
    _set_attribute(span.attributes, REDACTED_REASON_ATTRIBUTE, reason)
    report.spans_stripped += 1
    report.reasons.append(reason)


def _walk_span(span, report: WalkReport, *, scope_flagged: bool, stamped: dict) -> None:
    flagged: str | None = "scope" if scope_flagged else None
    bytes_dropped = 0
    # S4c: one answer per span for "did any position here degrade", so
    # the stamp lands on the span an operator would look at.
    with pii_service.observe_degradation() as degradation:
        _drop_stamp_marker(span.attributes)
        _clear_trace_state_if_flagged(span, report)
        if stamped.get(STAMPED_NAME_KEY) != span.name:
            # The span NAME is content, and a name the detector could
            # not walk flags the span exactly as a flagged attribute
            # does. Ignoring this return was safe only while content
            # never flagged.
            flagged = flagged or _apply(span, "name", report)
        flagged = flagged or _apply(span.status, "message", report)
        out = _walk_key_values(
            span.attributes, report, drop_flagged=False, stamped=stamped
        )
        bytes_dropped += out.bytes_dropped
        flagged = flagged or out.flagged
        for event in span.events:
            flagged = flagged or _apply(event, "name", report)
            out = _walk_key_values(event.attributes, report, drop_flagged=False)
            bytes_dropped += out.bytes_dropped
            flagged = flagged or out.flagged
        for link in span.links:
            _clear_trace_state_if_flagged(link, report)
            out = _walk_key_values(link.attributes, report, drop_flagged=False)
            bytes_dropped += out.bytes_dropped
            flagged = flagged or out.flagged
    if flagged:
        _strip_span(span, flagged, report, stamped)
    if bytes_dropped:
        report.bytes_dropped += bytes_dropped
        _set_int_attribute(span.attributes, BYTES_DROPPED_ATTRIBUTE, bytes_dropped)
    if degradation.degraded:
        # After the strip, not before: ``_strip_span`` keeps only what
        # the chassis stamped, and this stamp is about the walk itself.
        report.degraded += 1
        _set_attribute(span.attributes, pii_service.DEGRADED_ATTRIBUTE, True)


def walk_trace_request(
    request: trace_service_pb2.ExportTraceServiceRequest,
    *,
    chassis: ChassisStamps | None = None,
) -> WalkReport:
    """Walk every position of a trace export request in place.

    ``chassis`` is the record of what THIS process stamped — the
    backend's own export passes it, the relay does not, because nothing
    arriving from a container was written by the chassis.
    """
    report = WalkReport()
    resource_stamped = chassis.resource() if chassis else {}
    for resource_spans in request.resource_spans:
        _walk_resource(resource_spans.resource, report, resource_stamped)
        if _apply(resource_spans, "schema_url", report):
            resource_spans.ClearField("schema_url")
        for scope_spans in resource_spans.scope_spans:
            scope_flagged = _walk_scope(scope_spans.scope, report)
            if _apply(scope_spans, "schema_url", report):
                scope_spans.ClearField("schema_url")
            for span in scope_spans.spans:
                stamped = (
                    chassis.take_span(
                        int.from_bytes(span.trace_id, "big"),
                        int.from_bytes(span.span_id, "big"),
                    )
                    if chassis
                    else {}
                )
                _walk_span(span, report, scope_flagged=scope_flagged, stamped=stamped)
    return report


def _walk_log_record(record, report: WalkReport) -> str | None:
    """Walk one log record in place; the flag (never the text) when the
    record must be dropped."""
    bytes_dropped = 0
    flagged: str | None = None
    with pii_service.observe_degradation() as degradation:
        _drop_stamp_marker(record.attributes)
        flagged = _apply(record, "event_name", report)
        flagged = flagged or _apply(record, "severity_text", report)
        if record.HasField("body"):
            out = _walk_any_value(record.body, report)
            bytes_dropped += out.bytes_dropped
            flagged = flagged or out.flagged
        out = _walk_key_values(record.attributes, report, drop_flagged=False)
        bytes_dropped += out.bytes_dropped
        flagged = flagged or out.flagged
    if flagged:
        return flagged
    if degradation.degraded:
        report.degraded += 1
        _set_attribute(record.attributes, pii_service.DEGRADED_ATTRIBUTE, True)
    if bytes_dropped:
        report.bytes_dropped += bytes_dropped
        _set_int_attribute(record.attributes, BYTES_DROPPED_ATTRIBUTE, bytes_dropped)
    return None


def walk_logs_request(
    request: logs_service_pb2.ExportLogsServiceRequest,
    *,
    chassis: ChassisStamps | None = None,
) -> WalkReport:
    """Walk every position of a logs export request in place; a flagged
    record is dropped and counted by its scope."""
    report = WalkReport()
    resource_stamped = chassis.resource() if chassis else {}
    for resource_logs in request.resource_logs:
        _walk_resource(resource_logs.resource, report, resource_stamped)
        if _apply(resource_logs, "schema_url", report):
            resource_logs.ClearField("schema_url")
        for scope_logs in resource_logs.scope_logs:
            scope_flagged = _walk_scope(scope_logs.scope, report)
            if _apply(scope_logs, "schema_url", report):
                scope_logs.ClearField("schema_url")
            kept = []
            for record in scope_logs.log_records:
                flag = "scope" if scope_flagged else _walk_log_record(record, report)
                if flag:
                    report.records_dropped += 1
                    name = scope_logs.scope.name or "<unnamed>"
                    report.dropped_by_scope[name] = report.dropped_by_scope.get(name, 0) + 1
                    report.reasons.append(flag)
                    continue
                kept.append(record)
            if len(kept) != len(scope_logs.log_records):
                copies = [type(r)() for r in kept]
                for copy, r in zip(copies, kept):
                    copy.CopyFrom(r)
                del scope_logs.log_records[:]
                for copy in copies:
                    scope_logs.log_records.add().CopyFrom(copy)
    return report


def log_report(report: WalkReport, *, signal: str) -> None:
    """One platform-plane line per walk that changed something — counts
    and scope names, never text."""
    if report.records_dropped:
        for scope, count in report.dropped_by_scope.items():
            logger.warning(
                "otlp_log_records_dropped", signal=signal, scope=scope, count=count
            )
    if (
        report.spans_stripped
        or report.attributes_dropped
        or report.bytes_dropped
        or report.trace_states_cleared
        or report.detector_unavailable
        or report.degraded
    ):
        logger.warning("otlp_export_walked", signal=signal, **report.as_dict())


__all__ = [
    "BYTES_DROPPED_ATTRIBUTE",
    "DETECTOR_FLAG",
    "CLASSIFICATION",
    "REDACTED_ATTRIBUTE",
    "REDACTED_REASON_ATTRIBUTE",
    "STAMP_MARKER",
    "STRIPPED_SPAN_NAME",
    "ChassisStamps",
    "WalkReport",
    "classify",
    "descriptor_fields",
    "log_report",
    "stamps",
    "walk_logs_request",
    "walk_trace_request",
]
