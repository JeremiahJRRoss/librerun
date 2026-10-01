"""Server-side OpenTelemetry translator for browser telemetry.

The browser sends the closed LibreRun RUM v1 envelope (``rum_envelope.py``)
to the authenticated relay; *this* module is the only place OpenTelemetry
objects are constructed for it. The browser is never an OTLP client — the
trust boundary (identity, authorization, quotas) sits in the relay, and
OTLP stays what it is best at: the protocol between trusted telemetry
infrastructure.

Signal shapes follow OTel's current browser direction (decided
2026-08-24 from the RUM research round):

* point-in-time observations are **LogRecord events** — web vitals as
  ``browser.web_vital`` (SemConv 1.44 attribute shape, pinned below) and
  errors as ``exception`` with the Stable ``exception.type`` attribute
  and deliberately **no** ``exception.message``/``exception.stacktrace``
  (arbitrary strings never leave the browser in the first place);
* operations with duration are **spans** — a route commit whose
  navigation intent was observed becomes a ``browser.route.commit`` span
  with explicit client timestamps;
* metrics are the pipeline's job, derived downstream — never emitted here.

Everything is emitted through providers dedicated to the web resource
(``service.name=librerun-web``): browser telemetry is the UX plane
(``librerun.scope=ux``) and must never ride the backend's providers,
whose spans are stamped ``platform``/``run`` by the enricher. Emission is
endpoint-gated exactly like ``otel_init``: a blank
``OTEL_EXPORTER_OTLP_ENDPOINT`` means records validate, count, and drop
cleanly. Every record is explicitly emitted with an EMPTY context so the
relay's own request span never becomes the parent of browser telemetry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import structlog

from app import config as _config
from app.observability.rum_envelope import (
    JsExceptionRecord,
    PageViewRecord,
    RouteChangeRecord,
    RumRecord,
    WebVitalRecord,
)

logger = structlog.get_logger(__name__)

# The browser vocabulary below (browser.web_vital.*) is Development-status
# semconv and has already taken one breaking rework while Development.
# This pin names the version the mapping implements; it is stamped on the
# web resource so operators can route/adapt by it, and the translator is
# the ONLY place a rename lands when the convention moves.
SEMCONV_VERSION = "1.44.0"

_SEVERITY_INFO = 9
_SEVERITY_ERROR = 17


@dataclass
class RelayContext:
    """Server-derived facts stamped onto every emitted record.

    Provenance rules: ``tenant_id`` and ``user_pseudonym`` come from the
    verified session (never the payload); ``session_id`` and
    ``app_version`` are validated client claims, useful for correlation
    but marked untrusted by ``librerun.telemetry.source``.
    """

    tenant_id: str
    user_pseudonym: str
    session_id: str
    app_version: str | None
    received_at_ns: int


class WebTelemetry:
    """Holds the dedicated web-resource providers, or nothing (disabled)."""

    def __init__(self, tracer_provider: Any | None, logger_provider: Any | None):
        self._tracer_provider = tracer_provider
        self._logger_provider = logger_provider
        self._tracer = (
            tracer_provider.get_tracer("librerun.web") if tracer_provider else None
        )
        self._logger = (
            logger_provider.get_logger("librerun.web") if logger_provider else None
        )

    @property
    def enabled(self) -> bool:
        return self._tracer is not None or self._logger is not None

    def force_flush(self, timeout_millis: int = 5000) -> None:
        for provider in (self._tracer_provider, self._logger_provider):
            if provider is not None and hasattr(provider, "force_flush"):
                try:
                    provider.force_flush(timeout_millis=timeout_millis)
                except Exception:  # noqa: BLE001 — flush must never raise at shutdown
                    pass

    # -- emission ---------------------------------------------------------

    def emit(self, ctx: RelayContext, records: list[RumRecord]) -> int:
        """Translate validated records into OTel signals. Returns how many
        were emitted; emission failures are logged and never propagate —
        the relay has already accepted the batch."""
        if not self.enabled:
            return 0
        emitted = 0
        for rec in records:
            try:
                if isinstance(rec, WebVitalRecord):
                    self._emit_web_vital(ctx, rec)
                elif isinstance(rec, JsExceptionRecord):
                    self._emit_exception(ctx, rec)
                elif isinstance(rec, RouteChangeRecord):
                    self._emit_route_change(ctx, rec)
                elif isinstance(rec, PageViewRecord):
                    self._emit_page_view(ctx, rec)
                else:  # pragma: no cover — union is closed
                    continue
                emitted += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "web_telemetry_emit_failed",
                    record_type=getattr(rec, "type", "unknown"),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        return emitted

    def _common_attrs(self, ctx: RelayContext, rec: RumRecord) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "librerun.scope": "ux",
            "librerun.telemetry.source": "browser_untrusted",
            "session.id": ctx.session_id,
            "librerun.page.id": str(rec.page_id),
            "librerun.route.template": rec.route,
            "librerun.ui.owner": rec.ui_owner,
            "librerun.tenant.id": ctx.tenant_id,
            "librerun.user.pseudonym": ctx.user_pseudonym,
        }
        if rec.agent_id:
            attrs["librerun.agent.id"] = rec.agent_id
        if rec.run_id:
            attrs["librerun.run.id"] = str(rec.run_id)
        if ctx.app_version:
            attrs["librerun.app.version"] = ctx.app_version
        return attrs

    def _emit_log(
        self,
        ctx: RelayContext,
        rec: RumRecord,
        event_name: str,
        severity_number: int,
        body: str | None,
        extra_attrs: dict[str, Any],
    ) -> None:
        if self._logger is None:
            return
        from opentelemetry._logs import LogRecord
        from opentelemetry.context import Context

        attrs = self._common_attrs(ctx, rec)
        attrs.update(extra_attrs)
        self._logger.emit(
            LogRecord(
                timestamp=rec.occurred_at_ms * 1_000_000,
                observed_timestamp=ctx.received_at_ns,
                # Empty context on purpose: the relay's request span must
                # never become the browser event's trace correlation.
                context=Context(),
                severity_number=severity_number,
                body=body,
                attributes=attrs,
                event_name=event_name,
            )
        )

    def _emit_web_vital(self, ctx: RelayContext, rec: WebVitalRecord) -> None:
        extra: dict[str, Any] = {
            "browser.web_vital.name": rec.name,
            "browser.web_vital.value": rec.value,
            "browser.web_vital.id": rec.metric_id,
            "browser.web_vital.rating": rec.rating,
        }
        if rec.navigation_type:
            extra["browser.web_vital.navigation_type"] = rec.navigation_type
        self._emit_log(
            ctx, rec, "browser.web_vital", _SEVERITY_INFO, rec.name, extra
        )

    def _emit_exception(self, ctx: RelayContext, rec: JsExceptionRecord) -> None:
        # Stable exception semconv, minus the free-text fields on purpose.
        extra: dict[str, Any] = {
            "exception.type": rec.error_type,
            "librerun.exception.mechanism": rec.mechanism,
            "librerun.error.fingerprint": rec.fingerprint,
        }
        if rec.bundle_module:
            extra["librerun.error.bundle_module"] = rec.bundle_module
        self._emit_log(ctx, rec, "exception", _SEVERITY_ERROR, rec.error_type, extra)

    def _emit_route_change(self, ctx: RelayContext, rec: RouteChangeRecord) -> None:
        extra: dict[str, Any] = {
            "librerun.navigation.from_route": rec.from_route,
            "librerun.navigation.trigger": rec.trigger,
        }
        if rec.navigation_id:
            extra["librerun.navigation.id"] = str(rec.navigation_id)
        if rec.duration_ms is None or self._tracer is None:
            # No observed intent (or no tracer): a point-in-time commit
            # fact, not a fabricated zero-duration operation.
            self._emit_log(
                ctx, rec, "librerun.route_change", _SEVERITY_INFO, rec.route, extra
            )
            return
        from opentelemetry.context import Context

        end_ns = rec.occurred_at_ms * 1_000_000
        start_ns = end_ns - rec.duration_ms * 1_000_000
        attrs = self._common_attrs(ctx, rec)
        attrs.update(extra)
        span = self._tracer.start_span(
            "browser.route.commit",
            context=Context(),  # fresh root — never parented to the relay request
            start_time=start_ns,
            attributes=attrs,
        )
        span.end(end_time=end_ns)

    def _emit_page_view(self, ctx: RelayContext, rec: PageViewRecord) -> None:
        extra: dict[str, Any] = {"librerun.navigation.kind": rec.navigation_kind}
        if rec.referrer_route:
            extra["librerun.navigation.referrer_route"] = rec.referrer_route
        self._emit_log(ctx, rec, "librerun.page_view", _SEVERITY_INFO, rec.route, extra)


def _build_log_exporter(endpoint: str, protocol: str) -> Any | None:
    """OTLP log exporter for the requested protocol — the logs twin of
    ``otel_init._build_exporter``, same failure posture (warn + None)."""
    proto = protocol.lower()
    try:
        if proto in ("grpc", "otlp"):
            from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
                OTLPLogExporter,
            )

            return OTLPLogExporter(endpoint=endpoint)
        if proto in ("http", "http/protobuf"):
            from opentelemetry.exporter.otlp.proto.http._log_exporter import (
                OTLPLogExporter as HTTPLogExporter,
            )

            if not endpoint.rstrip("/").endswith("/v1/logs"):
                endpoint = endpoint.rstrip("/") + "/v1/logs"
            return HTTPLogExporter(endpoint=endpoint)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "web_telemetry_log_exporter_unavailable",
            protocol=proto,
            endpoint=endpoint,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None
    logger.warning("web_telemetry_unknown_protocol", protocol=proto)
    return None


def _web_resource() -> Any:
    from opentelemetry.sdk.resources import Resource

    from app.version import __version__

    return Resource.create(
        {
            "service.name": "librerun-web",
            "service.namespace": "librerun",
            # Same value the backend plane stamps, so one deployment's three
            # telemetry planes can be correlated by build rather than by
            # guesswork. The ux plane carried no version at all before.
            "service.version": __version__,
            "librerun.semconv.version": SEMCONV_VERSION,
        }
    )


def build_web_telemetry(
    span_exporter: Any | None = None, log_exporter: Any | None = None
) -> WebTelemetry:
    """Build a :class:`WebTelemetry` around explicit exporters.

    Tests pass in-memory exporters; production callers go through
    :func:`get_web_telemetry`, which resolves exporters from settings.
    """
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = _web_resource()
    tracer_provider = None
    logger_provider = None
    if span_exporter is not None:
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    if log_exporter is not None:
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    return WebTelemetry(tracer_provider, logger_provider)


_instance: WebTelemetry | None = None


def get_web_telemetry() -> WebTelemetry:
    """Module singleton, endpoint-gated from settings on first use."""
    global _instance
    if _instance is not None:
        return _instance

    settings = _config.settings
    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
    if not endpoint:
        logger.info(
            "web_telemetry_disabled",
            reason="OTEL_EXPORTER_OTLP_ENDPOINT is blank — UX-plane emission off",
        )
        _instance = WebTelemetry(None, None)
        return _instance

    protocol = settings.OTEL_EXPORTER_OTLP_PROTOCOL
    try:
        from app.observability.otel_init import _build_exporter

        span_exporter = _build_exporter(endpoint, protocol)
        log_exporter = _build_log_exporter(endpoint, protocol)
        _instance = build_web_telemetry(span_exporter, log_exporter)
        if settings.OTEL_DEBUG:
            _attach_console_exporters(_instance)
        logger.info(
            "web_telemetry_initialized",
            endpoint=endpoint,
            protocol=protocol,
            spans=span_exporter is not None,
            logs=log_exporter is not None,
            semconv_version=SEMCONV_VERSION,
        )
    except Exception as exc:  # noqa: BLE001 — observability must not break serving
        logger.error(
            "web_telemetry_init_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        _instance = WebTelemetry(None, None)
    return _instance


def _attach_console_exporters(instance: WebTelemetry) -> None:
    try:
        from opentelemetry.sdk._logs.export import (
            ConsoleLogExporter,
            SimpleLogRecordProcessor,
        )
        from opentelemetry.sdk.trace.export import (
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )

        if instance._tracer_provider is not None:
            instance._tracer_provider.add_span_processor(
                SimpleSpanProcessor(ConsoleSpanExporter())
            )
        if instance._logger_provider is not None:
            instance._logger_provider.add_log_record_processor(
                SimpleLogRecordProcessor(ConsoleLogExporter())
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "web_telemetry_console_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )


def _reset_for_tests() -> None:
    global _instance
    _instance = None


def now_ms() -> int:
    return time.time_ns() // 1_000_000


__all__ = [
    "SEMCONV_VERSION",
    "RelayContext",
    "WebTelemetry",
    "build_web_telemetry",
    "get_web_telemetry",
    "now_ms",
]
