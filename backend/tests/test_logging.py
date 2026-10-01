"""Tests for structured JSON file logging.

These tests drive real writes to a temp file via the ``log_file``
fixture and assert on the rendered NDJSON. Presidio's heavy NER is
mocked where the test isn't specifically exercising it, because loading
``en_core_web_lg`` in CI is too slow.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog


def _drain(path: Path) -> list[dict]:
    """Flush handlers and return parsed JSON lines from the log file."""
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_json_output_shape(log_file):
    log = structlog.get_logger("test.shape")
    log.info("hello_event", foo="bar", count=3)
    records = _drain(log_file)
    assert len(records) == 1
    rec = records[0]
    assert rec["event"] == "hello_event"
    assert rec["foo"] == "bar"
    assert rec["count"] == 3
    assert rec["level"] == "info"
    assert rec["logger"] == "test.shape"
    assert rec["stream"] == "app"
    assert "timestamp" in rec


def test_timestamp_is_utc_iso(log_file):
    log = structlog.get_logger("test.ts")
    log.info("ts_event")
    rec = _drain(log_file)[0]
    # TimeStamper(fmt="iso", utc=True) produces e.g. 2026-04-21T15:30:12.123456Z
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$", rec["timestamp"]), rec["timestamp"]


def test_user_content_redacted_when_enabled(log_file):
    from app.logging_pii import user_content

    log = structlog.get_logger("test.redact")
    # Mock Presidio to be deterministic regardless of model availability.
    from app.services import pii_service

    def fake_redact(text, threshold=None):
        return text.replace("123-45-6789", "[REDACTED_SSN_1]"), []

    with patch.object(pii_service, "redact", side_effect=fake_redact):
        log.info("leak_test", body=user_content("my SSN is 123-45-6789"))
    rec = _drain(log_file)[0]
    assert rec["body"] == "my SSN is [REDACTED_SSN_1]"
    assert "123-45-6789" not in json.dumps(rec)


def test_user_content_unwrapped_when_redaction_disabled(log_file, monkeypatch):
    import app.logging_pii as lp

    monkeypatch.setattr(lp.settings, "LOG_REDACT_PII", False)
    log = structlog.get_logger("test.unwrap")
    log.info("noredact", body=lp.user_content("123-45-6789"))
    rec = _drain(log_file)[0]
    assert rec["body"] == "123-45-6789"


def test_non_user_content_fields_not_redacted(log_file):
    """run_id that happens to match a PII pattern must not be redacted."""
    from app.services import pii_service

    called = {"n": 0}

    def fake_redact(text, threshold=None):
        called["n"] += 1
        return "REDACTED", []

    log = structlog.get_logger("test.plain")
    with patch.object(pii_service, "redact", side_effect=fake_redact):
        log.info("run_created", run_id="123-45-6789")
    rec = _drain(log_file)[0]
    assert rec["run_id"] == "123-45-6789"
    assert called["n"] == 0  # redactor never invoked


def test_contextvars_propagate(log_file):
    from app.logging_context import bind_run_context

    bind_run_context("abc-123")
    log = structlog.get_logger("test.ctx")
    log.info("in_scope")
    rec = _drain(log_file)[0]
    assert rec["run_id"] == "abc-123"


def test_otel_ids_added_when_span_active(log_file):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    # Set up a minimal tracer provider so spans are recording.
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")
    log = structlog.get_logger("test.otel")
    with tracer.start_as_current_span("test-span"):
        log.info("in_span")
    # Find this test's own record rather than assuming it is the first
    # line in the file. Setting a tracer provider is process-global: if
    # anything earlier in the session installed one, OTEL logs an
    # "Overriding of current TracerProvider is not allowed" warning,
    # and that warning is itself a log record landing ahead of ours.
    # The assertion is about our record, so select it.
    rec = next(r for r in _drain(log_file) if r.get("event") == "in_span")
    assert "trace_id" in rec and len(rec["trace_id"]) == 32
    assert "span_id" in rec and len(rec["span_id"]) == 16


def test_otel_ids_omitted_when_no_span(log_file):
    log = structlog.get_logger("test.nospan")
    # No active span — processor must not attach trace_id/span_id.
    log.info("outside_span")
    rec = _drain(log_file)[0]
    # Either the keys are absent, or they're the "invalid" all-zeros values
    # that OTEL reports when no span is active. The add_otel_ids processor
    # guards against that via is_valid.
    assert rec.get("trace_id") in (None,) or rec["trace_id"] != "0" * 32


def test_app_stream_tag(log_file):
    log = structlog.get_logger("app.services.agent_runner")
    log.info("app_event")
    rec = _drain(log_file)[0]
    assert rec["stream"] == "app"


def test_access_stream_tag_via_uvicorn_logger(log_file):
    """uvicorn.access stdlib records must come out as stream=access with
    fields promoted from record.args."""
    uvlog = logging.getLogger("uvicorn.access")
    # Uvicorn's access record: (client_addr, method, full_path, http_version, status_code)
    uvlog.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5555", "GET", "/health?token=abc", "1.1", 200)
    rec = _drain(log_file)[0]
    assert rec["stream"] == "access"
    assert rec["event"] == "http_request"
    assert rec["method"] == "GET"
    assert rec["path"] == "/health"
    assert rec["has_query"] is True
    assert rec["status_code"] == 200
    assert rec["client_addr"] == "127.0.0.1:5555"
    # Query string contents must not leak.
    assert "abc" not in json.dumps(rec)


def test_truncation(log_file):
    from app.logging_pii import user_content, MAX_FIELD_LEN
    from app.services import pii_service

    # With Presidio mocked out, truncation alone determines final length.
    def fake_redact(text, threshold=None):
        return text, []

    log = structlog.get_logger("test.trunc")
    big = "a" * 10_000
    with patch.object(pii_service, "redact", side_effect=fake_redact):
        log.info("big_event", blob=user_content(big))
    rec = _drain(log_file)[0]
    assert len(rec["blob"]) == MAX_FIELD_LEN


def test_user_content_redaction_failure_fails_open(log_file):
    """If pii_service.redact raises, the field must be replaced with
    ``[REDACTION_FAILED]`` — never the raw value — and the log line must
    still be emitted."""
    from app.logging_pii import user_content
    from app.services import pii_service

    def boom(text, threshold=None):
        raise RuntimeError("presidio exploded")

    log = structlog.get_logger("test.failopen")
    with patch.object(pii_service, "redact", side_effect=boom):
        log.info("boom_event", body=user_content("sensitive-value-xyz"))
    rec = _drain(log_file)[0]
    assert rec["body"] == "[REDACTION_FAILED]"
    assert "sensitive-value-xyz" not in json.dumps(rec)


def test_unwritable_log_file_falls_back_to_stderr(tmp_path, monkeypatch, capsys):
    """A log path that can't be opened (root-owned bind mount, read-only fs)
    must NOT abort configure_logging — the app degrades to stderr-only and
    announces it. Regression: Docker creates a missing ``./data/logs`` bind
    mount root-owned, and the resulting PermissionError inside dictConfig
    crash-looped the backend.

    The unwritable path is simulated with a parent that is a regular file,
    so the probe fails with an OSError even when the suite runs as root.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    monkeypatch.setenv("LOG_FILE_PATH", str(blocker / "logs" / "backend.jsonl"))
    monkeypatch.setenv("LOG_STDERR_ENABLED", "false")  # must be overridden by the fallback

    from app.config import Settings
    from app import logging_config as _lc

    new_settings = Settings()
    monkeypatch.setattr("app.config.settings", new_settings)
    monkeypatch.setattr(_lc, "settings", new_settings)
    monkeypatch.setattr(_lc, "_CONFIGURED", False)

    _lc.configure_logging()  # must not raise

    root = logging.getLogger()
    handler_types = [type(h).__name__ for h in root.handlers]
    assert "TimedRotatingFileHandler" not in handler_types
    assert "StreamHandler" in handler_types, handler_types

    err = capsys.readouterr().err
    assert "log_file_unwritable" in err


def test_every_line_carries_librerun_scope(log_file):
    """The plane stamp is derived per line, not bound at call sites: a line
    emitted outside any context is ``platform``, a line emitted while an
    agent run's context is bound is ``run``, and after the context exits
    the stamp reverts — both directions, so the processor can neither
    over- nor under-claim the run plane."""
    import structlog as _structlog

    from app.logging_context import log_context

    log = _structlog.get_logger("test.scope")
    log.info("platform_event")
    with log_context(agent_id="vita-v1", phase="phase1"):
        log.info("run_event")
    log.info("platform_again")

    records = {r["event"]: r for r in _drain(log_file)}
    assert records["platform_event"]["librerun_scope"] == "platform"
    assert records["run_event"]["librerun_scope"] == "run"
    assert records["platform_again"]["librerun_scope"] == "platform"


def test_run_ids_are_duplicated_under_the_pre_s1_names_for_one_release(log_file):
    """Blueprint S1 (L18): ``run_id`` / ``run_number`` are the log fields;
    ``case_id`` / ``case_number`` carry the same values for one release
    (saved log queries) and go at v1.1. An explicit old-spelling field is
    never overwritten."""
    log = structlog.get_logger("test.s1")
    log.info("with_ids", run_id="r-1", run_number="RUN-1000")
    log.info("explicit_old", run_id="r-2", case_id="kept")
    log.info("no_ids")
    recs = {r["event"]: r for r in _drain(log_file)}
    assert recs["with_ids"]["case_id"] == "r-1"
    assert recs["with_ids"]["case_number"] == "RUN-1000"
    assert recs["explicit_old"]["case_id"] == "kept"
    assert "case_id" not in recs["no_ids"] and "case_number" not in recs["no_ids"]
