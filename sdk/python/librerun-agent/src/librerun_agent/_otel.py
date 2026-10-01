"""Trace and log export to the chassis OTLP relay, one request per run
token (blueprint S4; the ``otel`` extra).

The SDK keeps a map from the trace id each invocation arrived under (its
``traceparent``) to that invocation's token, for the invocation's life
and a short grace after it. The exporters partition every batch by
trace id and send one relay request per token carrying only that
trace's spans and log records — an ASGI agent serving overlapping
invocations never mixes runs in one request — and a span or record
whose trace id the SDK does not own (before the first invocation,
outside any invocation, a chassis with tracing off) is dropped before
export. Each invocation's handler runs inside a span whose parent is the
chassis phase span, so everything the agent instruments joins the run's
one tree; a captured ``print()`` becomes a log record under the same
trace.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable

from . import _capture
from ._context import CURRENT, Invocation

TOKEN_GRACE_SECONDS = 20.0
_MODULE_LOGGER = logging.getLogger("librerun_agent")


INVOCATION_ATTRIBUTE = "librerun.invocation_id"


class _SpanOwners:
    """Which invocation opened each span, recorded beside the span.

    NOT a span attribute. Attributes are a mutable collection the agent
    shares, so ownership written there is ownership the agent can
    rewrite: handler code in invocation A could set
    ``librerun.invocation_id`` to B's, and since the two share an
    upstream trace the relay's trace check passes and A's telemetry is
    filed as B's run — and B's tenant.

    Keyed by the span's own ids, which the agent cannot choose. Bounded,
    because a span that is created and never exported would otherwise
    leave its entry: losing an owner drops the span, which is the safe
    direction.
    """

    def __init__(self, limit: int = 20_000) -> None:
        self._lock = threading.Lock()
        self._owners: "OrderedDict[tuple[int, int], str]" = OrderedDict()
        self._limit = limit

    def record(self, trace_id: int, span_id: int, invocation_id: str) -> None:
        with self._lock:
            self._owners[(trace_id, span_id)] = invocation_id
            while len(self._owners) > self._limit:
                self._owners.popitem(last=False)

    def take(self, trace_id: int, span_id: int) -> str | None:
        with self._lock:
            return self._owners.pop((trace_id, span_id), None)

    def clear(self) -> None:
        with self._lock:
            self._owners.clear()


span_owners = _SpanOwners()


class _TokenRegistry:
    """invocation id → token, for that invocation's life plus a grace.

    Keyed by INVOCATION, not by trace id. Two invocations can share a
    trace id legitimately — a caller may submit several runs, of
    different tenants even, under one upstream W3C trace — and a
    trace-keyed registry silently replaced the first invocation's token
    with the second's. Every span of the first then left under the
    second's token, and the relay stamped them as the second run: one
    tenant's telemetry filed under another's. The first invocation's
    ``end()`` could also start the grace clock on the live one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, str] = {}
        self._ended: dict[str, float] = {}
        self.dropped = 0

    def _prune(self, now: float) -> None:
        """Drop every entry whose grace has passed. Caller holds the lock.

        Expiry used to happen only inside ``token_for``, and a finished
        invocation flushes its telemetry BEFORE ``end()`` — so nothing
        ever asked again, and its entry and bearer token stayed for the
        life of the process. A busy agent accumulated one per run.
        """
        expired = [
            invocation_id
            for invocation_id, ended in self._ended.items()
            if now - ended > TOKEN_GRACE_SECONDS
        ]
        for invocation_id in expired:
            self._tokens.pop(invocation_id, None)
            self._ended.pop(invocation_id, None)

    def register(self, invocation_id: str, token: str) -> None:
        with self._lock:
            self._prune(time.monotonic())
            self._tokens[invocation_id] = token
            self._ended.pop(invocation_id, None)

    def end(self, invocation_id: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if invocation_id in self._tokens:
                self._ended[invocation_id] = now

    def token_for(self, invocation_id: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            return self._tokens.get(invocation_id)

    def size(self) -> int:
        """How many invocations the registry is holding."""
        with self._lock:
            return len(self._tokens)


registry = _TokenRegistry()


class _State:
    configured = False
    endpoint: str | None = None
    tracer_provider: Any = None
    logger_provider: Any = None
    tracer: Any = None
    log_handler: Any = None
    sent: int = 0


_state = _State()


def _post(url: str, token: str, body: bytes, timeout: float = 10.0) -> bool:
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-protobuf",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        _MODULE_LOGGER.warning("relay refused an export: %s %s", exc.code, exc.reason)
        return False
    except Exception as exc:  # noqa: BLE001
        _MODULE_LOGGER.warning("relay unreachable: %s", exc)
        return False


def _partition(items: Iterable[Any], owner_of: Callable[[Any], str | None]) -> dict[str, list]:
    """Group by the invocation that produced each item; drop the rest.

    An item with no invocation was emitted outside one — there is no
    token that could carry it, and guessing from the trace id is what
    let one invocation's telemetry leave under another's token.
    """
    groups: dict[str, list] = {}
    for item in items:
        owner = owner_of(item)
        if not owner:
            registry.dropped += 1
            continue
        groups.setdefault(owner, []).append(item)
    return groups


def _span_owner(span: Any) -> str | None:
    context = span.get_span_context() if hasattr(span, "get_span_context") else span.context
    return span_owners.take(context.trace_id, context.span_id)


def _record_owner(item: Any) -> str | None:
    attributes = getattr(item.log_record, "attributes", None) or {}
    return attributes.get(INVOCATION_ATTRIBUTE)


def _make_exporters(endpoint: str):
    from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.sdk._logs.export import LogExporter, LogExportResult
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    base = endpoint.rstrip("/")

    class PartitionedSpanExporter(SpanExporter):
        def export(self, spans):
            ok = True
            for owner, group in _partition(spans, _span_owner).items():
                token = registry.token_for(owner)
                if token is None:
                    registry.dropped += len(group)
                    continue
                body = encode_spans(group).SerializePartialToString()
                if _post(f"{base}/v1/traces", token, body):
                    _state.sent += len(group)
                else:
                    ok = False
            return SpanExportResult.SUCCESS if ok else SpanExportResult.FAILURE

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    class PartitionedLogExporter(LogExporter):
        def export(self, batch):
            ok = True
            groups = _partition(batch, _record_owner)
            for owner, group in groups.items():
                token = registry.token_for(owner)
                if token is None:
                    registry.dropped += len(group)
                    continue
                body = encode_logs(group).SerializeToString()
                if _post(f"{base}/v1/logs", token, body):
                    _state.sent += len(group)
                else:
                    ok = False
            return LogExportResult.SUCCESS if ok else LogExportResult.FAILURE

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    return PartitionedSpanExporter(), PartitionedLogExporter()


def _stamp_invocation_on_record(record: logging.LogRecord) -> bool:
    """Mark a log record with the invocation that emitted it, and keep
    the SDK's own records out of the export entirely.

    The second half is load-bearing. This module logs a warning when a
    post to the relay fails; if that warning became a log record the
    same exporter tried to send, a refused export would warn, and the
    warning would be exported, and fail, and warn — a live loop that
    only stayed quiet while nothing was ever posted. A captured
    ``print()`` reaches the same logger and IS telemetry, so the
    distinction is the ``librerun.stream`` marker the capture sink sets,
    not the logger's name.
    """
    own = record.name == "librerun_agent" or record.name.startswith("librerun_agent.")
    captured = "librerun.stream" in record.__dict__  # a print(), which IS telemetry
    if own and not captured:
        return False
    invocation = CURRENT.get()
    if invocation is not None:
        record.__dict__[INVOCATION_ATTRIBUTE] = invocation.id
    else:
        # Set or removed, never left: agent code can pass anything in
        # ``extra``, and a record it labelled with another invocation's
        # id would be exported under that invocation's token.
        record.__dict__.pop(INVOCATION_ATTRIBUTE, None)
    return True


def _invocation_stamper():
    """A span processor that marks every span with its invocation.

    Built here rather than at module level so this module still imports
    without the ``otel`` extra, and subclassing the SDK's own
    ``SpanProcessor`` rather than duck-typing it: the provider's
    multi-processor calls private hooks (``_on_ending``) that only the
    base class supplies.
    """
    from opentelemetry.sdk.trace import SpanProcessor

    class _InvocationStamper(SpanProcessor):
        def on_start(self, span, parent_context=None) -> None:  # noqa: ARG002
            invocation = CURRENT.get()
            if invocation is None:
                return
            context = span.get_span_context()
            span_owners.record(context.trace_id, context.span_id, invocation.id)

    return _InvocationStamper()


def configure(*, service_name: str, endpoint: str | None = None) -> bool:
    """Set up export to the relay at ``endpoint`` (default:
    ``OTEL_EXPORTER_OTLP_ENDPOINT``). Returns False — and the SDK works
    without tracing — when the endpoint is blank or the ``otel`` extra
    is not installed."""
    if _state.configured:
        return True
    endpoint = (endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        _MODULE_LOGGER.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but the otel extra is not "
            "installed: pip install 'librerun-agent[otel]'"
        )
        return False
    span_exporter, log_exporter = _make_exporters(endpoint)
    resource = Resource.create({"service.name": service_name})
    tracer_provider = TracerProvider(resource=resource)
    # Stamp first, export second: every span started inside an
    # invocation carries whose it is, so the exporter never has to infer
    # ownership from a trace id two invocations can share.
    tracer_provider.add_span_processor(_invocation_stamper())
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter, schedule_delay_millis=500))
    trace.set_tracer_provider(tracer_provider)
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter, schedule_delay_millis=500))
    handler = LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider)
    # The same stamp for log records: OpenTelemetry's bridge copies a
    # record's extra attributes onto the log record, so the filter is
    # how a record says which invocation it belongs to.
    handler.addFilter(_stamp_invocation_on_record)
    root = logging.getLogger()
    root.addHandler(handler)
    _state.log_handler = handler
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    _state.configured = True
    _state.endpoint = endpoint
    _state.tracer_provider = tracer_provider
    _state.logger_provider = logger_provider
    _state.tracer = tracer_provider.get_tracer("librerun_agent")
    _capture.set_line_sink(_otlp_line_sink)
    return True


def _otlp_line_sink(invocation: Invocation, stream: str, level: str, line: str) -> None:
    """A captured line as a log record under the invocation's trace: the
    ``logging`` bridge stamps the current span's context, and the line
    is emitted from the invocation's own context."""
    numeric = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}.get(level, 20)
    logging.getLogger(f"librerun_agent.{stream}").log(numeric, "%s", line, extra={"librerun.stream": stream})


def configured() -> bool:
    return _state.configured


class _NoSpan:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def invocation_span(invocation: Invocation):
    """The handler span, a child of the chassis phase span (the incoming
    ``traceparent``), so the agent's spans join the run's one trace."""
    if not _state.configured or _state.tracer is None:
        return _NoSpan()
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    carrier: dict[str, str] = {}
    if invocation.traceparent:
        carrier["traceparent"] = invocation.traceparent
    if invocation.tracestate:
        carrier["tracestate"] = invocation.tracestate
    parent = TraceContextTextMapPropagator().extract(carrier) if carrier else None
    return _state.tracer.start_as_current_span(
        f"invocation {invocation.phase}",
        context=parent,
        attributes={
            "librerun.phase": invocation.phase,
            INVOCATION_ATTRIBUTE: invocation.id,
            "run.id": invocation.run_id,
        },
    )


def flush(timeout_millis: int = 10000) -> None:
    if not _state.configured:
        return
    try:
        _state.tracer_provider.force_flush(timeout_millis)
        _state.logger_provider.force_flush(timeout_millis)
    except Exception:  # noqa: BLE001
        pass


def shutdown() -> None:
    if not _state.configured:
        return
    try:
        _state.tracer_provider.shutdown()
        _state.logger_provider.shutdown()
    except Exception:  # noqa: BLE001
        pass


def _reset_for_tests() -> None:
    """Undo ``configure`` completely, so the next one takes effect.

    A container configures once and lives; a test process configures
    many times, and two things do not undo themselves. The root logger
    keeps every ``LoggingHandler`` ever added, and OpenTelemetry refuses
    to replace a tracer provider once one is set — so without this, a
    second ``configure`` is silently ignored and the agent's OWN tracer
    (``trace.get_tracer(...)``, which reads the global provider) keeps
    handing spans to a provider that has been shut down. Nothing is
    exported and nothing says so, which is the worst way for a test to
    pass.
    """
    global registry
    shutdown()
    if _state.log_handler is not None:
        logging.getLogger().removeHandler(_state.log_handler)
    try:
        from opentelemetry import trace as _trace

        _trace._TRACER_PROVIDER = None
        _trace._TRACER_PROVIDER_SET_ONCE._done = False
    except Exception:  # noqa: BLE001 — without the otel extra there is nothing to reset
        pass
    _state.configured = False
    _state.endpoint = None
    _state.tracer_provider = None
    _state.logger_provider = None
    _state.tracer = None
    _state.log_handler = None
    _state.sent = 0
    registry = _TokenRegistry()
    _capture.set_line_sink(None)
    span_owners.clear()


__all__ = ["TOKEN_GRACE_SECONDS", "configure", "configured", "flush", "invocation_span", "registry", "shutdown"]
