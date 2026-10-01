"""The two walking classes both planes export through (blueprint S4):
:class:`WalkingSpanExporter` in front of the OTLP span exporter, and the
log-record walk the queue-only logging pipeline runs
(``app.logging_queue`` hosts the pipeline; the record walk lives here so
the gateway, which S4a creates, installs the same classes at its boot).

Spans are walked **as OTLP**: the batch is encoded with the SDK's own
encoder, ``otlp_walk`` walks the protobuf by descriptor, and the walked
request goes to a sender that speaks the configured protocol — so the
positions walked are exactly the positions on the wire.
"""
from __future__ import annotations

import logging
import sys
import traceback
from typing import Any, Sequence

import structlog
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from app.observability import otlp_walk
from app.services import pii_service

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------
# Spans
# --------------------------------------------------------------------------


def _grpc_sender(endpoint: str):
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    class _Sender(OTLPSpanExporter):
        """The SDK's gRPC exporter with its encoder bypassed: ``_export``
        receives the already-walked request."""

        def _translate_data(self, data):  # noqa: D401 — the SDK's hook
            return data

        def _count_data(self, data) -> int:
            return sum(
                len(ss.spans) for rs in data.resource_spans for ss in rs.scope_spans
            )

        def send(self, request) -> SpanExportResult:
            return self._export(request)

    return _Sender(endpoint=endpoint)


def _http_sender(endpoint: str):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter as HTTPSpanExporter,
    )

    if not endpoint.rstrip("/").endswith("/v1/traces"):
        endpoint = endpoint.rstrip("/") + "/v1/traces"

    class _Sender(HTTPSpanExporter):
        """The SDK's HTTP exporter fed a pre-walked request: one attempt
        through its own ``_export`` (the batch processor drops a failed
        batch either way; the SDK's retry loop is bound to its encoder)."""

        def send(self, request) -> SpanExportResult:
            try:
                resp = self._export(request.SerializePartialToString(), self._timeout)
            except Exception as exc:  # noqa: BLE001
                logger.warning("otlp_span_export_failed", error=str(exc)[:200])
                return SpanExportResult.FAILURE
            if getattr(resp, "ok", False):
                return SpanExportResult.SUCCESS
            logger.warning(
                "otlp_span_export_rejected",
                status_code=getattr(resp, "status_code", None),
            )
            return SpanExportResult.FAILURE

    return _Sender(endpoint=endpoint)


def build_sender(endpoint: str, protocol: str):
    proto = protocol.lower()
    if proto in ("grpc", "otlp"):
        return _grpc_sender(endpoint)
    if proto in ("http", "http/protobuf"):
        return _http_sender(endpoint)
    raise ValueError(f"unknown OTLP protocol {protocol!r}")


def _grpc_log_sender(endpoint: str):
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

    class _Sender(OTLPLogExporter):
        def _translate_data(self, data):
            return data

        def _count_data(self, data) -> int:
            return sum(
                len(sl.log_records) for rl in data.resource_logs for sl in rl.scope_logs
            )

        def send(self, request):
            return self._export(request)

    return _Sender(endpoint=endpoint)


def _http_log_sender(endpoint: str):
    from opentelemetry.exporter.otlp.proto.http._log_exporter import (
        OTLPLogExporter as HTTPLogExporter,
    )

    if not endpoint.rstrip("/").endswith("/v1/logs"):
        endpoint = endpoint.rstrip("/") + "/v1/logs"

    class _Sender(HTTPLogExporter):
        def send(self, request):
            from opentelemetry.sdk._logs.export import LogExportResult

            try:
                resp = self._export(request.SerializeToString(), self._timeout)
            except Exception as exc:  # noqa: BLE001
                logger.warning("otlp_log_export_failed", error=str(exc)[:200])
                return LogExportResult.FAILURE
            if getattr(resp, "ok", False):
                return LogExportResult.SUCCESS
            logger.warning(
                "otlp_log_export_rejected", status_code=getattr(resp, "status_code", None)
            )
            return LogExportResult.FAILURE

    return _Sender(endpoint=endpoint)


def build_log_sender(endpoint: str, protocol: str):
    """The logs twin of :func:`build_sender`: a sender fed a pre-walked
    ``ExportLogsServiceRequest`` (the relay's)."""
    proto = protocol.lower()
    if proto in ("grpc", "otlp"):
        return _grpc_log_sender(endpoint)
    if proto in ("http", "http/protobuf"):
        return _http_log_sender(endpoint)
    raise ValueError(f"unknown OTLP protocol {protocol!r}")


class WalkingSpanExporter(SpanExporter):
    """Walk every span before it leaves the box.

    ``export`` encodes the batch with the SDK's encoder, walks the
    ``ExportTraceServiceRequest`` by descriptor (string positions
    redacted, keys and numbers checked with a flagged span stripped to
    its identity, bytes dropped, the protocol's own scalars untouched)
    and hands the walked request to the sender. Runs on the batch
    processor's thread, off the request path.
    """

    def __init__(self, sender: Any):
        self._sender = sender
        self._shutdown = False

    @classmethod
    def for_endpoint(cls, endpoint: str, protocol: str) -> "WalkingSpanExporter":
        return cls(build_sender(endpoint, protocol))

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._shutdown:
            return SpanExportResult.FAILURE
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

        request = encode_spans(spans)
        # This process's own spans: the chassis's record of what it
        # stamped applies, so its ids survive the walk intact.
        report = otlp_walk.walk_trace_request(request, chassis=otlp_walk.stamps)
        otlp_walk.log_report(report, signal="traces")
        return self._sender.send(request)

    def shutdown(self) -> None:
        self._shutdown = True
        shutdown = getattr(self._sender, "shutdown", None)
        if callable(shutdown):
            shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        flush = getattr(self._sender, "force_flush", None)
        return bool(flush(timeout_millis)) if callable(flush) else True


# --------------------------------------------------------------------------
# Log records
# --------------------------------------------------------------------------

# The stdlib's own LogRecord attributes, each in one class. ``metadata``
# is never checked (a Linux pthread id such as 139000000000384 is
# Luhn-valid and would otherwise drop every record of its thread);
# ``content`` is walked as text; ``rendered`` is args-shaped and is
# consumed at capture. tests/test_logging_queue.py fails on an attribute
# the stdlib adds that is not placed here.
STDLIB_RECORD_ATTRIBUTES: dict[str, str] = {
    "name": "metadata",
    "msg": "content",
    "args": "rendered",
    "levelname": "metadata",
    "levelno": "metadata",
    "pathname": "metadata",
    "filename": "metadata",
    "module": "metadata",
    "exc_info": "rendered",
    "exc_text": "content",
    "stack_info": "content",
    "lineno": "metadata",
    "funcName": "metadata",
    "created": "metadata",
    "msecs": "metadata",
    "relativeCreated": "metadata",
    "thread": "metadata",
    "threadName": "metadata",
    "process": "metadata",
    "processName": "metadata",
    "taskName": "metadata",
    "message": "content",
    "asctime": "metadata",
}
# Attributes the chassis attaches to a record on its way through the
# pipeline; never agent content.
PIPELINE_RECORD_ATTRIBUTES = frozenset(
    {"_logger", "_name", "_librerun_ctx", "_librerun_otel", "_librerun_walked",
     "_librerun_from_walk", "_librerun_access",
     # Written BY the walk (blueprint S4c), so never walked as an extra.
     pii_service.DEGRADED_EVENT_FIELD}
)
# Fields the chassis writes on an event dict, which the walk leaves
# alone: opaque ids, timestamps, names of its own. Walking them would
# destroy them — a UUID's digit run reads as a card number, and a 32-hex
# trace id contains a valid phone number often enough to drop real lines.
#
# Which fields those are cannot be decided from the NAME. It was: any
# key in one set, so an agent calling
# ``log.info("x", user_id="someone@example.com")`` had that value copied
# through unwalked, because an explicitly passed kwarg beats the bound
# context and the exemption only ever looked at the key. So each group
# is exempt for a different, checkable reason:
#
#   - PROCESSOR: a structlog processor writes these AFTER the caller's
#     kwargs, so a caller's value cannot survive to be exempted;
#   - ACCESS: written by ``reshape_uvicorn_access_record``, which marks
#     the record with the field names it produced — the logger NAME will
#     not do, since agent code can call
#     ``structlog.get_logger("uvicorn.access").info(…)``;
#   - CONTEXT: exempt only when the value still equals what the CHASSIS
#     bound, captured on the record at emit time (``_librerun_ctx``);
#   - HEX: a trace or span id, exempt when it is hex of the right
#     length, which no address or phone number can be;
#   - DIGEST: a public key's fingerprint (K7, D34), exempt when it is
#     exactly ``SHA256:`` and 64 lowercase hex — a digest, which no
#     address, number or name can be. Walked, it never survives: the
#     base64-blob rule takes any 40-character run, and the NER stage tags
#     some chunk of a grouped digest as a nationality one time in ten
#     (measured, K blueprint §11), so the operator's one out-of-band check
#     — the gateway's boot line against the admin page — would read a
#     placeholder.
PROCESSOR_EVENT_FIELDS = frozenset({
    "timestamp", "level", "logger", "stream", "librerun_scope",
    "_record", "_from_structlog", "positional_args", "exc_info", "stack_info",
})
ACCESS_EVENT_FIELDS = frozenset({
    "client_addr", "method", "path", "http_version", "status_code", "has_query",
})
CONTEXT_EVENT_FIELDS = frozenset({
    "run_id", "case_id", "tenant_id", "user_id", "session_id", "request_id",
    "agent_id", "run_number", "case_number", "phase", "run_mode",
})
HEX_EVENT_FIELDS = {"trace_id": 32, "span_id": 16}
DIGEST_EVENT_FIELDS = {"fingerprint": "SHA256:"}
_DIGEST_HEX_LENGTH = 64
STAMPED_EVENT_FIELDS = frozenset(
    PROCESSOR_EVENT_FIELDS | ACCESS_EVENT_FIELDS | CONTEXT_EVENT_FIELDS | set(HEX_EVENT_FIELDS)
    | set(DIGEST_EVENT_FIELDS)
)
_HEX_DIGITS = set("0123456789abcdef")


def _is_digest(key: str, value) -> bool:
    prefix = DIGEST_EVENT_FIELDS.get(key)
    if prefix is None or not isinstance(value, str) or not value.startswith(prefix):
        return False
    digest = value[len(prefix):]
    return len(digest) == _DIGEST_HEX_LENGTH and set(digest) <= _HEX_DIGITS


def chassis_written(event: dict, record: logging.LogRecord) -> dict:
    """The entries of ``event`` the CHASSIS wrote — by value, not by name.

    Everything else goes through the walk, whatever it is called.
    """
    bound = getattr(record, "_librerun_ctx", None) or {}
    access = set(getattr(record, "_librerun_access", ()) or ())
    written = {}
    for key, value in event.items():
        if key in PROCESSOR_EVENT_FIELDS:
            written[key] = value
        elif key in access and key in ACCESS_EVENT_FIELDS:
            written[key] = value
        elif key in CONTEXT_EVENT_FIELDS and key in bound and value == bound[key]:
            written[key] = value
        elif (
            key in HEX_EVENT_FIELDS
            and isinstance(value, str)
            and len(value) == HEX_EVENT_FIELDS[key]
            and set(value) <= _HEX_DIGITS
        ):
            written[key] = value
        elif _is_digest(key, value):
            written[key] = value
    return written
# Loggers whose records carry counts and names by construction — the
# walk's own warnings — and are never walked, so a drop can never recurse.
WALK_LOGGER = "app.logging_walk"


class RecordRefused(Exception):
    """The record carried a flagged identifier or number; drop it."""

    def __init__(self, kind: str, pii_type: str, path: str):
        super().__init__(f"{kind}:{pii_type} at {path}")
        self.kind = kind
        self.pii_type = pii_type
        self.path = path


def _walk_text(text: str | None) -> str | None:
    if not text:
        return text
    try:
        return pii_service.redact(
            text,
            skip_entities=pii_service.BOUNDARY_SKIP_ENTITIES,
            quiet=True,
            stage="log_walk",
        )[0]
    except pii_service.PiiDetectorUnavailable as exc:
        # Blueprint S4c: a record the walk could not complete is
        # DROPPED, not exported unwalked. ``logging_queue`` already
        # counts a ``RecordRefused`` and logs the position; raising the
        # same exception keeps the drop in the path that names it,
        # rather than in the generic ``except Exception`` beside it,
        # which cannot say why.
        raise RecordRefused("detector", exc.state, "$") from exc


def _walk_json(value, *, path: str):
    try:
        result = pii_service.walk(value, path=path, quiet=True)
    except pii_service.PiiDetectorUnavailable as exc:
        raise RecordRefused("detector", exc.state, path) from exc
    if result.refused:
        f = result.refusals[0]
        raise RecordRefused(f.kind, f.pii_type, f.path)
    return result.value


def prepare_record(record: logging.LogRecord, *, stack_skip: int = 0) -> logging.LogRecord:
    """The emission-time half of getting a record ready for the walk.

    Three things can only be done on the thread that logged, while its
    stack and its ``sys.exc_info()`` are still the live ones:

    - a structlog ``exception()`` leaves ``exc_info=True`` for a
      formatter to resolve later; resolve it now, or later is a
      formatter on another thread — or after the walk — finding nothing;
    - ``stack_info`` becomes text here for the same reason;
    - **positional args are rendered into the message**. This is the one
      that bites: ``log.warning("hello %s", name)`` leaves the name in
      ``record.args``, which the walk does not touch (it is a stdlib
      attribute) and the formatter interpolates *after* the walk. Every
      ``%s`` in every dependency's log line is that shape.

    ``uvicorn.access`` keeps its args: ``reshape_uvicorn_access_record``
    reads them into named fields and marks what it produced.

    ``stack_skip`` is how many of the CALLER's own frames to drop from a
    rendered stack; this function's frame is dropped on top of it.
    """
    msg = record.msg
    if isinstance(msg, dict):
        if msg.get("exc_info") is True:
            msg["exc_info"] = sys.exc_info()
        if msg.get("stack_info"):
            msg.pop("stack_info", None)
            msg["stack"] = "".join(traceback.format_stack()[: -(stack_skip + 1)])
    elif record.args and record.name != "uvicorn.access":
        try:
            record.msg = record.getMessage()
        except Exception:  # noqa: BLE001
            record.msg = str(record.msg)
        record.args = None
    return record


def render_exceptions(record: logging.LogRecord) -> logging.LogRecord:
    """Tracebacks and stacks to text, in place, BEFORE the walk.

    A traceback is a string the walk can rewrite; ``exc_info`` is a live
    exception triple the walk cannot touch, and a formatter renders it
    downstream of the walk — so a record walked with its ``exc_info``
    still a tuple reaches the sink with an unwalked traceback in it.
    Every process that walks records calls this first: the backend's
    listener thread does, and so does the gateway at emission.
    """
    msg = record.msg
    if isinstance(msg, dict):
        exc = msg.get("exc_info")
        if isinstance(exc, BaseException):
            exc = (type(exc), exc, exc.__traceback__)
        if isinstance(exc, tuple) and exc[0] is not None:
            msg["exception"] = "".join(traceback.format_exception(*exc))
        if "exc_info" in msg:
            msg.pop("exc_info", None)
    if record.exc_info:
        exc = record.exc_info
        if isinstance(exc, BaseException):
            exc = (type(exc), exc, exc.__traceback__)
        if isinstance(exc, tuple) and exc[0] is not None:
            record.exc_text = "".join(traceback.format_exception(*exc))
        record.exc_info = None
    return record


def walk_log_record(record: logging.LogRecord) -> logging.LogRecord:
    """Walk one record in place, once, after its traceback and stack were
    rendered to text. The message (a structlog event dict or a rendered
    string), the rendered traceback, every string field and every
    caller-supplied extra are walked; the stdlib's numbers and the
    chassis's own writes (see ``chassis_written``) are not. Raises
    :class:`RecordRefused` when
    an identifier or a number is flagged — the caller drops the record —
    and, since blueprint S4c, when the PII detector could not run at all:
    a record the walk could not COMPLETE is dropped for the same reason
    one it completed unhappily is. Under the
    ``LIBRERUN_PII_ALLOW_DEGRADED`` opt-out the record is kept, walked by
    the regex stages alone, and stamped to say so.
    """
    if getattr(record, "_librerun_walked", False) or record.name == WALK_LOGGER:
        return record
    with pii_service.observe_degradation() as degradation:
        record = _walk_record_fields(record)
    if degradation.degraded:
        # Blueprint S4c: the operator opted into regex-only redaction,
        # so this record says so. On the record AND in the structlog
        # event, because the event dict is what the JSON line and the
        # OTLP body are rendered from — an attribute alone would be
        # invisible in both.
        setattr(record, pii_service.DEGRADED_EVENT_FIELD, True)
        if isinstance(record.msg, dict):
            record.msg[pii_service.DEGRADED_EVENT_FIELD] = True
    record._librerun_walked = True  # type: ignore[attr-defined]
    return record


def _walk_record_fields(record: logging.LogRecord) -> logging.LogRecord:
    if isinstance(record.msg, dict):
        event = record.msg
        stamped = chassis_written(event, record)
        walked = _walk_json(
            {k: v for k, v in event.items() if k not in stamped}, path="$"
        )
        event.clear()
        event.update(stamped)
        event.update(walked)
    elif record.msg is not None:
        record.msg = _walk_text(str(record.msg))
    if record.exc_text:
        record.exc_text = _walk_text(record.exc_text)
    if record.stack_info:
        record.stack_info = _walk_text(record.stack_info)
    extras = {
        k: v
        for k, v in record.__dict__.items()
        if k not in STDLIB_RECORD_ATTRIBUTES and k not in PIPELINE_RECORD_ATTRIBUTES
    }
    if extras:
        for k, v in _walk_json(extras, path="$.extra").items():
            setattr(record, k, v)
    return record


__all__ = [
    "PIPELINE_RECORD_ATTRIBUTES",
    "RecordRefused",
    "STAMPED_EVENT_FIELDS",
    "chassis_written",
    "STDLIB_RECORD_ATTRIBUTES",
    "WALK_LOGGER",
    "WalkingSpanExporter",
    "build_log_sender",
    "build_sender",
    "prepare_record",
    "render_exceptions",
    "walk_log_record",
]
