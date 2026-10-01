"""Translator tests: envelope records → OTel signals, plus the PII canary
sweep.

The canary suite enforces the design's core privacy claim from three
angles, and — per the house rule that a gate must be proven in BOTH
directions — includes a guard-the-guard run: a deliberately leaky
translator variant must be CAUGHT by the same detector that passes the
healthy one. A canary sweep that cannot catch a planted leak is
indistinguishable from one that never looks.
"""
from __future__ import annotations

import json
import uuid

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.observability.rum_envelope import record_adapter
from app.observability.web_telemetry import (
    SEMCONV_VERSION,
    RelayContext,
    WebTelemetry,
    build_web_telemetry,
)
from app.version import __version__

PAGE_ID = str(uuid.uuid4())
RUN_ID = str(uuid.uuid4())
NOW_MS = 1_724_500_000_000
RECEIVED_NS = 1_724_500_003_000_000_000

CANARIES = [
    "LIBRERUN_SECRET_CANARY_9f31",
    "alice+canary@example.test",
    "Bearer canary-token-abc123",
    'password":"canary-password',
    "customer-acme-production-secret",
]


def _ctx() -> RelayContext:
    return RelayContext(
        tenant_id="11111111-2222-3333-4444-555555555555",
        user_pseudonym="a" * 32,
        session_id=str(uuid.uuid4()),
        app_version="0.1.0",
        received_at_ns=RECEIVED_NS,
    )


def _build():
    span_exp = InMemorySpanExporter()
    log_exp = InMemoryLogRecordExporter()
    wt = build_web_telemetry(span_exp, log_exp)
    return wt, span_exp, log_exp


def _rec(payload: dict):
    return record_adapter.validate_python(payload)


def _vital(**overrides) -> dict:
    d = {
        "type": "web_vital",
        "occurred_at_ms": NOW_MS,
        "page_id": PAGE_ID,
        "route": "run.detail",
        "ui_owner": "agent",
        "agent_id": "vita-v1",
        "run_id": RUN_ID,
        "name": "inp",
        "value": 143.0,
        "rating": "good",
        "metric_id": "v4-1712345678901-1234567890123",
        "navigation_type": "navigate",
    }
    d.update(overrides)
    return d


def _exception(**overrides) -> dict:
    d = {
        "type": "js_exception",
        "occurred_at_ms": NOW_MS,
        "page_id": PAGE_ID,
        "route": "run.new",
        "ui_owner": "platform",
        "error_type": "SyntaxError",
        "mechanism": "window.error",
        "fingerprint": "deadbeef",
        "bundle_module": "/_next/static/chunks/app/runs/[id]/page-abc.js",
    }
    d.update(overrides)
    return d


def _flush_all(wt, log_exp):
    wt.force_flush()
    return [entry.log_record for entry in log_exp.get_finished_logs()]


def _sev(lr) -> int:
    """Severity as an int whether the SDK stored the enum or the raw value."""
    return getattr(lr.severity_number, "value", lr.severity_number)


# --- mapping ---------------------------------------------------------------


def test_web_vital_maps_to_semconv_event():
    wt, _, log_exp = _build()
    assert wt.emit(_ctx(), [_rec(_vital())]) == 1
    (lr,) = _flush_all(wt, log_exp)
    attrs = dict(lr.attributes)
    assert lr.event_name == "browser.web_vital"
    assert _sev(lr) == 9
    assert lr.timestamp == NOW_MS * 1_000_000
    assert lr.observed_timestamp == RECEIVED_NS
    assert lr.trace_id == 0  # never correlated to the relay's request span
    assert attrs["browser.web_vital.name"] == "inp"
    assert attrs["browser.web_vital.value"] == 143.0
    assert attrs["browser.web_vital.id"] == "v4-1712345678901-1234567890123"
    assert attrs["browser.web_vital.rating"] == "good"
    assert attrs["browser.web_vital.navigation_type"] == "navigate"
    assert attrs["librerun.scope"] == "ux"
    assert attrs["librerun.telemetry.source"] == "browser_untrusted"
    assert attrs["librerun.ui.owner"] == "agent"
    assert attrs["librerun.agent.id"] == "vita-v1"
    assert attrs["librerun.run.id"] == RUN_ID


def test_exception_maps_to_stable_fields_and_never_a_message():
    wt, _, log_exp = _build()
    wt.emit(_ctx(), [_rec(_exception())])
    (lr,) = _flush_all(wt, log_exp)
    attrs = dict(lr.attributes)
    assert lr.event_name == "exception"
    assert _sev(lr) == 17
    assert attrs["exception.type"] == "SyntaxError"
    assert attrs["librerun.exception.mechanism"] == "window.error"
    assert attrs["librerun.error.fingerprint"] == "deadbeef"
    # The free-text exception fields must be structurally absent.
    assert "exception.message" not in attrs
    assert "exception.stacktrace" not in attrs


def test_route_change_with_duration_becomes_root_span():
    wt, span_exp, _ = _build()
    rec = _rec(
        {
            "type": "route_change",
            "occurred_at_ms": NOW_MS,
            "page_id": PAGE_ID,
            "route": "run.detail",
            "ui_owner": "platform",
            "from_route": "dashboard",
            "trigger": "link",
            "duration_ms": 420,
            "navigation_id": str(uuid.uuid4()),
        }
    )
    wt.emit(_ctx(), [rec])
    wt.force_flush()
    (span,) = span_exp.get_finished_spans()
    assert span.name == "browser.route.commit"
    assert span.parent is None  # fresh root, not the relay's request
    assert span.end_time - span.start_time == 420 * 1_000_000
    attrs = dict(span.attributes)
    assert attrs["librerun.navigation.from_route"] == "dashboard"
    assert attrs["librerun.navigation.trigger"] == "link"
    assert attrs["librerun.scope"] == "ux"
    assert span.resource.attributes["service.name"] == "librerun-web"
    assert span.resource.attributes["librerun.semconv.version"] == SEMCONV_VERSION
    assert span.resource.attributes["service.version"] == __version__


def test_route_change_without_duration_is_an_event_not_a_fake_span():
    wt, span_exp, log_exp = _build()
    rec = _rec(
        {
            "type": "route_change",
            "occurred_at_ms": NOW_MS,
            "page_id": PAGE_ID,
            "route": "run.detail",
            "ui_owner": "platform",
            "from_route": "dashboard",
            "trigger": "unknown",
        }
    )
    wt.emit(_ctx(), [rec])
    logs = _flush_all(wt, log_exp)
    assert span_exp.get_finished_spans() == ()
    assert logs[0].event_name == "librerun.route_change"


def test_page_view_event():
    wt, _, log_exp = _build()
    rec = _rec(
        {
            "type": "page_view",
            "occurred_at_ms": NOW_MS,
            "page_id": PAGE_ID,
            "route": "dashboard",
            "ui_owner": "platform",
            "navigation_kind": "hard",
        }
    )
    wt.emit(_ctx(), [rec])
    wt.force_flush()
    (entry,) = log_exp.get_finished_logs()
    lr = entry.log_record
    assert lr.event_name == "librerun.page_view"
    assert dict(lr.attributes)["librerun.navigation.kind"] == "hard"
    # Resource identity rides the export entry, not the record itself.
    assert entry.resource.attributes["service.namespace"] == "librerun"
    assert entry.resource.attributes["service.name"] == "librerun-web"


def test_disabled_telemetry_counts_zero_and_never_raises():
    wt = WebTelemetry(None, None)
    assert wt.enabled is False
    assert wt.emit(_ctx(), [_rec(_vital())]) == 0
    wt.force_flush()  # no-op, no crash


# --- attribute-key snapshot: the schema cannot grow silently ----------------

EXPECTED_KEYS = {
    "browser.web_vital": {
        "librerun.scope",
        "librerun.telemetry.source",
        "session.id",
        "librerun.page.id",
        "librerun.route.template",
        "librerun.ui.owner",
        "librerun.tenant.id",
        "librerun.user.pseudonym",
        "librerun.agent.id",
        "librerun.run.id",
        "librerun.app.version",
        "browser.web_vital.name",
        "browser.web_vital.value",
        "browser.web_vital.id",
        "browser.web_vital.rating",
        "browser.web_vital.navigation_type",
    },
    "exception": {
        "librerun.scope",
        "librerun.telemetry.source",
        "session.id",
        "librerun.page.id",
        "librerun.route.template",
        "librerun.ui.owner",
        "librerun.tenant.id",
        "librerun.user.pseudonym",
        "librerun.app.version",
        "exception.type",
        "librerun.exception.mechanism",
        "librerun.error.fingerprint",
        "librerun.error.bundle_module",
    },
}


def test_emitted_attribute_keys_match_snapshot_exactly():
    """A NEW attribute key is a schema change and must fail here first —
    silently widening the emitted surface is how telemetry leaks start."""
    wt, _, log_exp = _build()
    wt.emit(_ctx(), [_rec(_vital()), _rec(_exception())])
    logs = _flush_all(wt, log_exp)
    by_event = {lr.event_name: set(dict(lr.attributes)) for lr in logs}
    assert by_event == EXPECTED_KEYS


# --- the canary suite -------------------------------------------------------


def _scan_exported(span_exp, log_exp) -> str:
    """Serialize EVERYTHING that left the translator, canary-huntably."""
    blob: list[str] = []
    for span in span_exp.get_finished_spans():
        blob.append(span.name)
        blob.append(json.dumps(dict(span.attributes), default=str))
    for entry in log_exp.get_finished_logs():
        lr = entry.log_record
        blob.append(str(lr.event_name))
        blob.append(str(lr.body))
        blob.append(json.dumps(dict(lr.attributes), default=str))
    return "\n".join(blob)


def _contains_canary(exported: str) -> list[str]:
    return [c for c in CANARIES if c in exported]


def test_canaries_cannot_pass_record_validation():
    """Every free-string slot rejects canary-shaped content — the schema is
    where unrepresentability is enforced, so prove it field by field."""
    canary = CANARIES[0] + "=hunter2 with spaces"
    attempts = [
        _vital(metric_id=canary),
        _vital(route=canary),
        # Charset-VALID free text is rejected too: routes are a closed
        # set, bundle modules require the /_next/static/ prefix, and
        # metric ids must be the digits-only web-vitals generated shape
        # (both smuggle strings below matched the old identifier charset).
        _vital(route="customer-acme-production-secret"),
        _vital(metric_id="customer-acme-production-secret"),
        _vital(metric_id="LIBRERUN_SECRET_CANARY_9f31"),
        _exception(bundle_module="customer-secret"),
        _exception(error_type=f"Error: {canary}"),
        _exception(fingerprint=canary),
        _exception(bundle_module=f"/_next/x.js?leak={canary}"),
        {**_exception(), "message": canary},  # unknown field
    ]
    for payload in attempts:
        with pytest.raises(Exception):
            record_adapter.validate_python(payload)


def test_healthy_translator_output_carries_no_canary():
    wt, span_exp, log_exp = _build()
    wt.emit(
        _ctx(),
        [
            _rec(_vital()),
            _rec(_exception()),
            _rec(
                {
                    "type": "route_change",
                    "occurred_at_ms": NOW_MS,
                    "page_id": PAGE_ID,
                    "route": "run.detail",
                    "ui_owner": "platform",
                    "from_route": "dashboard",
                    "trigger": "link",
                    "duration_ms": 100,
                }
            ),
        ],
    )
    wt.force_flush()
    assert _contains_canary(_scan_exported(span_exp, log_exp)) == []


def test_canary_detector_catches_a_planted_leak():
    """Guard-the-guard: sabotage the exception mapping to leak a canary the
    way real code would (an exception.message attribute) and require the
    SAME detector to flag it. If this test ever passes with an empty
    result, the sweep above is blind and proves nothing."""
    wt, span_exp, log_exp = _build()

    original = WebTelemetry._emit_exception

    def leaky(self, ctx, rec):
        self._emit_log(
            ctx,
            rec,
            "exception",
            17,
            rec.error_type,
            {
                "exception.type": rec.error_type,
                "exception.message": f"Could not parse user log: {CANARIES[0]}",
            },
        )

    WebTelemetry._emit_exception = leaky
    try:
        wt.emit(_ctx(), [_rec(_exception())])
    finally:
        WebTelemetry._emit_exception = original
    wt.force_flush()
    assert _contains_canary(_scan_exported(span_exp, log_exp)) == [CANARIES[0]]


def test_both_telemetry_planes_report_the_same_version():
    """The ux plane and the backend plane must agree on the build.

    They are separate OTel resources under different service names, and the
    ux plane carried no service.version at all until one constant fed both.
    Without this, correlating a browser trace with the backend trace of the
    same deployment is guesswork. Asserting equality rather than a literal is
    the point: a release bump must move both or fail here.
    """
    from fastapi import FastAPI

    from app.observability.web_telemetry import _web_resource

    ux_version = _web_resource().attributes["service.version"]
    backend_version = FastAPI(title="LibreRun", version=__version__).version

    assert ux_version == backend_version == __version__
