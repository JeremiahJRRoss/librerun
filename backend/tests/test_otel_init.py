"""Tests for ``app.observability.otel_init`` (vendor-neutral OTLP init, B3).

The old suite tested the retired vendor-SDK registration path; this one locks the
replacement's contract:

- endpoint unset (and OTEL_DEBUG off) → clean no-op: nothing instrumented,
  global tracer provider untouched.
- endpoint set → a real ``TracerProvider`` is built with
  ``service.name=librerun-backend``, the OTLP exporter is installed, and
  the SAME provider is threaded explicitly to FastAPIInstrumentor and to
  every LLM instrumentor (never relied on via the global — a displaced
  global would silently route LLM spans to a no-op provider).
- OTEL_DEBUG → ConsoleSpanExporter processor installed, and debug-only
  boots (no endpoint) still build a provider.
- D2 candidate order: first importable candidate per provider wins and
  the family label is reported.
"""
from __future__ import annotations

from pathlib import Path

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from app import config as config_module
from app.observability import otel_init as oi


@pytest.fixture
def otel_settings(monkeypatch):
    """Baseline: endpoint set, grpc, debug off. Tests override as needed."""
    monkeypatch.setattr(
        config_module.settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317"
    )
    monkeypatch.setattr(config_module.settings, "OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setattr(config_module.settings, "OTEL_SERVICE_NAME", "librerun-backend")
    monkeypatch.setattr(config_module.settings, "OTEL_DEBUG", False)


def test_endpoint_unset_is_clean_noop(monkeypatch):
    monkeypatch.setattr(config_module.settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
    monkeypatch.setattr(config_module.settings, "OTEL_DEBUG", False)

    with (
        patch(
            "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app"
        ) as mock_fastapi,
        patch("opentelemetry.trace.set_tracer_provider") as mock_set_global,
    ):
        result = oi.init_otel(FastAPI())

    assert result.enabled is False
    assert result.skipped_reason == "endpoint unset"
    assert result.tracer_provider is None
    mock_fastapi.assert_not_called()
    mock_set_global.assert_not_called()


def test_endpoint_set_builds_provider_with_service_name(otel_settings):
    with (
        patch(
            "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app"
        ),
        patch("opentelemetry.trace.set_tracer_provider"),
    ):
        result = oi.init_otel(FastAPI())

    assert result.enabled is True
    assert result.exporter_installed is True
    assert result.enricher_installed is True
    assert result.fastapi_instrumented is True
    assert result.console_exporter is False
    resource = result.tracer_provider.resource
    assert resource.attributes["service.name"] == "librerun-backend"
    assert resource.attributes["service.namespace"] == "librerun"
    # Installed dependency versions ride on the resource so "which SDK was in
    # that image?" is answerable from a trace. opentelemetry-sdk is always
    # installed wherever this assertion can run.
    from importlib.metadata import version

    assert resource.attributes["librerun.dependency.opentelemetry-sdk"] == version(
        "opentelemetry-sdk"
    )
    # The enricher must be attached as a live span processor (it bridges
    # agent/run/session context onto GenAI-convention LLM spans).
    from app.observability.span_enricher import AgentSpanEnricher

    processors = result.tracer_provider._active_span_processor._span_processors
    assert any(isinstance(p, AgentSpanEnricher) for p in processors)
    # The run plane samples itself (blueprint S4): the provider's sampler
    # is the one that records the root ``run`` span whatever an upstream
    # traceparent's flags say. The SDK default would not (its negative
    # case lives in tests/test_run_trace.py).
    from app.observability.run_trace import RunRootSampler

    assert isinstance(result.tracer_provider.sampler, RunRootSampler)


def test_otel_debug_installs_console_exporter(otel_settings, monkeypatch):
    monkeypatch.setattr(config_module.settings, "OTEL_DEBUG", True)

    with (
        patch(
            "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app"
        ),
        patch("opentelemetry.trace.set_tracer_provider"),
    ):
        result = oi.init_otel(FastAPI())

    assert result.console_exporter is True
    assert result.exporter_installed is True  # endpoint still set


def test_otel_debug_alone_boots_provider_without_exporter(monkeypatch):
    """No endpoint + OTEL_DEBUG=true → console-only provider (used in dev)."""
    monkeypatch.setattr(config_module.settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
    monkeypatch.setattr(config_module.settings, "OTEL_DEBUG", True)

    with (
        patch(
            "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app"
        ),
        patch("opentelemetry.trace.set_tracer_provider"),
    ):
        result = oi.init_otel(FastAPI())

    assert result.enabled is True
    assert result.exporter_installed is False
    assert result.console_exporter is True


def test_build_exporter_http_appends_traces_path():
    from app.observability.walkers import WalkingSpanExporter

    exporter = oi._build_exporter("http://collector:4318", "http/protobuf")
    assert isinstance(exporter, WalkingSpanExporter)  # every span is walked (S4)
    assert exporter._sender._endpoint.endswith("/v1/traces")


def test_build_exporter_unknown_protocol_returns_none():
    assert oi._build_exporter("http://collector:4317", "carrier-pigeon") is None


def test_this_process_installs_no_llm_instrumentor(monkeypatch):
    """Blueprint S4a, gap H8. The backend makes no model call: every one
    goes to the gateway, which writes the single LLM span itself with
    content that has been through the walker.

    An instrumentor here would record a provider response VERBATIM the
    moment some agent's own dependency pulled a provider SDK into this
    process — which is exactly how a model-generated address used to
    reach Jaeger. So the machinery is gone, not merely unconfigured, and
    this says so by name.
    """
    from app.observability import otel_init as oi

    assert not hasattr(oi, "_install_llm_instrumentors")
    assert not hasattr(oi, "_LLM_INSTRUMENTOR_CANDIDATES")
    assert not hasattr(oi, "_apply_content_capture_setting")

    app = FastAPI()
    monkeypatch.setattr(oi._config.settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
    monkeypatch.setattr(oi._config.settings, "OTEL_DEBUG", True)
    result = oi.init_otel(app)

    assert result.instrumentors == []


def test_the_backend_image_installs_no_provider_client():
    """The other half of the same rule: no credential, and nothing to use
    one with.

    Asserted against ``requirements.txt`` — the thing the image is built
    from — rather than against what happens to be importable, because a
    developer venv shared with the gateway has litellm in it and litellm
    brings ``openai`` along. The container-level check (``pip show``
    inside the running backend) is the smoke's, where a container
    exists.
    """
    requirements = (
        Path(__file__).resolve().parents[1] / "requirements.txt"
    ).read_text()
    names = [
        line.split("[")[0].split("=")[0].split(">")[0].split("<")[0].strip()
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    for banned in ("openai", "anthropic", "google-genai", "litellm"):
        assert banned not in names, (
            f"{banned} is in the backend's requirements — a provider client "
            f"here is one every in-process agent shares"
        )
    # …and no instrumentor whose whole job is recording a provider's reply.
    assert not [n for n in names if "instrumentation-openai" in n]
    assert not [n for n in names if "instrumentation-anthropic" in n]
    assert not [n for n in names if "instrumentation-google" in n]


# ---------------------------------------------------------------------------
# Dependency-version capture.
#
# Added after an image rebuild silently crossed an SDK major version and took
# out every Anthropic-backed pipeline step. The traces could not say which SDK
# was installed, so the first diagnostic step was asking the customer.
# ---------------------------------------------------------------------------


def test_dependency_versions_reports_installed_distributions():
    from importlib.metadata import version

    versions = oi.dependency_versions()

    assert (
        versions["librerun.dependency.opentelemetry-sdk"]
        == version("opentelemetry-sdk")
    )
    assert all(name.startswith("librerun.dependency.") for name in versions)
    assert all(value for value in versions.values())


def test_dependency_versions_skips_distributions_that_are_not_installed(monkeypatch):
    """The negative guard: an absent distribution is skipped, not fatal.

    A build can legitimately omit a provider SDK — a keyless demo, or a
    deployment that uses one provider. Injecting a name that cannot resolve is
    what proves the skip actually runs; on a tree where everything listed
    happens to be installed, a broken except-branch and a working one look
    exactly the same.
    """
    monkeypatch.setattr(
        oi,
        "_VERSIONED_DISTRIBUTIONS",
        ("opentelemetry-sdk", "librerun-nonexistent-distribution"),
    )

    versions = oi.dependency_versions()

    assert "librerun.dependency.opentelemetry-sdk" in versions
    assert "librerun.dependency.librerun-nonexistent-distribution" not in versions
