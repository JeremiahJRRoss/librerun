"""Vendor-neutral OpenTelemetry bootstrap for the LibreRun backend.

Replaces the retired vendor-SDK registration path (batch B3 of
the LibreRun blueprint, decision L2 — that document was retired at
B17; read it at commit 3a1b0b4, which is already on main:
``git show 3a1b0b4:docs/LibreRun_Blueprint.md``): the chassis emits standard
OTLP to whatever ``OTEL_EXPORTER_OTLP_ENDPOINT`` points at — from B4 that
is the bundled Vector router by default — and no vendor SDK participates
in trace export. Ported from the retired community backend's
``otel_init.py`` (blueprint §2.4 salvage list), minus its
community-specific span enricher: the chassis stamps ``agent.*`` /
``run.*`` attributes manually in ``services/agent_runner.py``, which is
plain OTEL and unaffected by this module.

Tracing is **endpoint-gated**: a blank ``OTEL_EXPORTER_OTLP_ENDPOINT``
means a clean no-op boot (the global tracer provider stays untouched, so
manual spans become no-ops). ``OTEL_DEBUG=true`` additionally streams
every finished span to stderr via a ``ConsoleSpanExporter`` — with or
without an endpoint — replacing the vendor-debug plumbing B3 removed.

LLM instrumentation follows blueprint decision D2: prefer the official
OpenTelemetry GenAI-convention instrumentation where one exists for a
provider, fall back to the OpenInference instrumentor (vendor-neutral
OTEL) where there is a gap. Candidates are tried in order per provider;
the first that imports and instruments wins, and the roll-up label in
``otel_init_complete`` records which family attached (e.g.
``openai:genai`` vs ``anthropic:openinference``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import FastAPI

from app import config as _config
from app.observability import otlp_walk

logger = structlog.get_logger(__name__)

# Content capture for the GenAI-convention instrumentations. Both
# installed packages route through opentelemetry-util-genai, whose
# parser takes an ENUM (NO_CONTENT / SPAN_ONLY / EVENT_ONLY /
# SPAN_AND_EVENT) — including openai-v2, whose legacy boolean grammar is
# dead code because util-genai's ``is_experimental_mode()`` is hardwired
# True. A boolean value here would warn and degrade to NO_CONTENT. When
# the operator has NOT set the variable, default to full capture for
# parity with the OpenInference behavior this init replaced; the value
# is set before any instrumentor runs so construction-time and lazy
# reads both see it. An operator-set value is respected untouched.
# The GenAI content-capture variable lives in the GATEWAY's environment
# from blueprint S4a on, with the provider keys: this process makes no
# model call, so there is nothing here for it to govern. Its resolution
# — the enum, the legacy boolean spellings, and failing closed on an
# invalid value — moved to ``services/gateway/gateway/telemetry.py``.


def _build_exporter(endpoint: str, protocol: str) -> Any | None:
    """Construct an OTLP span exporter for the requested protocol.

    Returns ``None`` (with a logged warning) on import failure or unknown
    protocol — the rest of the init still proceeds so FastAPI
    instrumentation and the console debug exporter stay available even
    when the export side cannot be configured.
    """
    proto = protocol.lower()
    try:
        if proto in ("grpc", "otlp", "http", "http/protobuf"):
            # Every span is walked before it leaves the box (blueprint
            # S4): the OTLP exporter sits behind the walking wrapper,
            # which encodes the batch, walks the protobuf by descriptor
            # and hands the walked request to the protocol's sender.
            from app.observability.walkers import WalkingSpanExporter

            return WalkingSpanExporter.for_endpoint(endpoint, proto)
    except Exception as exc:
        logger.warning(
            "otel_exporter_unavailable",
            protocol=proto,
            endpoint=endpoint,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None
    logger.warning("otel_exporter_unknown_protocol", protocol=proto)
    return None



@dataclass
class OTelInitResult:
    """What ``init_otel`` actually configured.

    Returned so the lifespan can log a structured roll-up and tests can
    assert exact shape without scraping log lines.
    """

    enabled: bool
    endpoint: str | None = None
    protocol: str | None = None
    service_name: str | None = None
    # Always empty from blueprint S4a on: this process installs no LLM
    # instrumentor, because it makes no model call. Kept on the result
    # for one release so a caller reading it does not break.
    instrumentors: list[str] = field(default_factory=list)
    exporter_installed: bool = False
    console_exporter: bool = False
    enricher_installed: bool = False
    fastapi_instrumented: bool = False
    skipped_reason: str | None = None
    tracer_provider: Any | None = None


# Distributions whose installed version changes runtime behaviour in ways a
# trace cannot otherwise show. The Anthropic sampling outage is the worked
# example: an image rebuild crossed a major version, every Anthropic-backed
# step began failing before it built a request, and telemetry could not say
# which SDK was installed — so the first question back to the customer was one
# the traces should have answered.
#
# This is a list of *dependencies*, not of agents: it learns nothing about any
# agent by recording what is installed beside it. Distributions that are not
# installed are skipped, so a build with no agents at all still boots and
# still reports.
#
# The provider SDKs are deliberately NOT here. S4a moved every model call to
# the gateway (L23), so this process no longer installs anthropic, openai or
# google-genai, and naming them here would report nothing forever while
# reading as coverage — a version list that answers no question is the same
# shape of defect as the outage that prompted it. The gateway stamps its own
# provider versions, in the process that has them.
_VERSIONED_DISTRIBUTIONS = (
    "fastapi",
    "sqlalchemy",
    "alembic",
    "pydantic",
    "opentelemetry-sdk",
    "openinference-instrumentation",
)


def dependency_versions() -> dict[str, str]:
    """``{attribute name: version}`` for the installed dependencies above.

    Resource attributes rather than span attributes: the value is fixed for
    the life of the process, and putting it on the resource means every span
    the process emits carries it without per-span cost.
    """
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _distribution_version

    versions: dict[str, str] = {}
    for distribution in _VERSIONED_DISTRIBUTIONS:
        try:
            versions[f"librerun.dependency.{distribution}"] = _distribution_version(
                distribution
            )
        except PackageNotFoundError:
            continue
        except Exception:  # pragma: no cover - metadata should never break boot
            continue
    return versions


def init_otel(app: FastAPI) -> OTelInitResult:
    """Configure vendor-neutral OTEL tracing for the backend process.

    Sets up a :class:`TracerProvider` (resource:
    ``service.name=librerun-backend`` by default) with an OTLP exporter
    pointed at ``settings.OTEL_EXPORTER_OTLP_ENDPOINT``, installs it as
    the global provider, instruments FastAPI for per-request spans, and
    registers the per-provider LLM instrumentors (decision D2).

    Returns an :class:`OTelInitResult` for the caller to log / assert on.
    Errors at every step are logged and swallowed — observability
    failures must never block startup.
    """
    # Read via the module on each call so test fixtures that swap
    # ``app.config.settings`` are observed.
    settings = _config.settings
    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
    debug = settings.OTEL_DEBUG

    # Logged before the disabled-path return as well as on the resource, so a
    # deployment with no trace backend still records what it was built with.
    logger.info(
        "dependency_versions",
        app_version=app.version or "unknown",
        **{
            name.removeprefix("librerun.dependency."): value
            for name, value in dependency_versions().items()
        },
    )

    if not endpoint and not debug:
        logger.info(
            "otel_init_skipped",
            reason="OTEL_EXPORTER_OTLP_ENDPOINT is blank and OTEL_DEBUG is off",
        )
        return OTelInitResult(enabled=False, skipped_reason="endpoint unset")

    protocol = settings.OTEL_EXPORTER_OTLP_PROTOCOL
    service_name = settings.OTEL_SERVICE_NAME

    logger.info(
        "otel_init_started",
        service_name=service_name,
        endpoint=endpoint or None,
        protocol=protocol,
        debug=debug,
    )

    result = OTelInitResult(
        enabled=True,
        endpoint=endpoint or None,
        protocol=protocol,
        service_name=service_name,
    )

    try:
        from opentelemetry import trace as _otel_trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from app.observability.run_trace import RunRootSampler

        # The run plane samples itself (blueprint S4, promise 3): the
        # root ``run`` span is recorded and sampled whatever an upstream
        # ``traceparent`` says, so a caller's ``00`` can never silence a
        # run; every other span keeps the SDK's parent-based always-on.
        resource_attributes = {
            "service.name": service_name,
            "service.namespace": "librerun",
            "service.version": app.version or "unknown",
            # Installed dependency versions, so "which SDK was in that
            # image?" is answerable from a trace. They go in BEFORE
            # Resource.create() so the stamp record below picks them up
            # and the export walk leaves them alone — they are chassis
            # facts, not user content.
            **dependency_versions(),
        }
        resource = Resource.create(resource_attributes)
        # What the CHASSIS put on the resource, recorded so the export
        # walk leaves it alone. Resource.create() adds the SDK's own
        # detected attributes too, and those are recorded from the
        # result rather than from the request, so the record is what is
        # actually there.
        otlp_walk.stamps.set_resource(dict(resource.attributes))
        provider = TracerProvider(resource=resource, sampler=RunRootSampler())

        if endpoint:
            exporter = _build_exporter(endpoint, protocol)
            if exporter is not None:
                provider.add_span_processor(BatchSpanProcessor(exporter))
                result.exporter_installed = True

        if debug:
            try:
                from opentelemetry.sdk.trace.export import (
                    ConsoleSpanExporter,
                    SimpleSpanProcessor,
                )

                provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
                result.console_exporter = True
            except Exception as exc:
                logger.warning(
                    "otel_console_exporter_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        # Identity on every span, regardless of instrumentation family:
        # the GenAI-convention instrumentors don't read OpenInference's
        # ``using_attributes`` context, so agent/run/session metadata
        # must be stamped one layer below the instrumentors. Separate
        # processor so its ``on_start`` runs at span creation (sampler
        # decisions see the attributes).
        try:
            from app.observability.span_enricher import AgentSpanEnricher

            provider.add_span_processor(AgentSpanEnricher())
            result.enricher_installed = True
        except Exception as exc:
            logger.warning(
                "otel_enricher_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

        # Install as global so module-level ``trace.get_tracer(...)``
        # callers (agent_runner's manual spans) attach to this provider.
        _otel_trace.set_tracer_provider(provider)
        result.tracer_provider = provider

        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
            result.fastapi_instrumented = True
        except Exception as exc:
            logger.warning(
                "otel_fastapi_instrument_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

        # No LLM instrumentor is installed here any more (blueprint
        # S4a, gap H8). This process makes no model call: every one goes
        # to the gateway, which writes the single LLM span itself with
        # content that has been through the walker. An instrumentor here
        # would record a provider response VERBATIM the moment some
        # agent's own dependency happened to pull `openai` in — which is
        # exactly how a model-generated address used to reach Jaeger.

        logger.info(
            "otel_init_complete",
            service_name=service_name,
            endpoint=endpoint or None,
            protocol=protocol,
            exporter_installed=result.exporter_installed,
            console_exporter=result.console_exporter,
            enricher_installed=result.enricher_installed,
            fastapi_instrumented=result.fastapi_instrumented,
            instrumentors=result.instrumentors,
        )
    except Exception as exc:
        logger.error(
            "otel_init_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            service_name=service_name,
            exc_info=True,
        )

    return result


__all__ = ["OTelInitResult", "init_otel"]
