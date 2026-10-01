"""Tests for LoggingContextMiddleware.

Covers: X-Request-ID header, uvicorn access reshape + query-string
stripping, and contextvars bridging via ``bind_request_context``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import structlog
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.middleware_logging import LoggingContextMiddleware


def _drain(path: Path) -> list[dict]:
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(LoggingContextMiddleware)

    @app.get("/ping")
    async def ping():
        log = structlog.get_logger("test.handler")
        log.info("handler_hit")
        return {"ok": True}

    return app


def test_request_id_header_present(log_file):
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/ping")
    assert resp.status_code == 200
    rid = resp.headers.get("X-Request-ID")
    assert rid and len(rid) == 32


def test_request_id_propagates_to_handler_log(log_file):
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/ping")
    rid = resp.headers["X-Request-ID"]
    records = _drain(log_file)
    handler_lines = [r for r in records if r.get("event") == "handler_hit"]
    assert handler_lines, "handler never logged"
    assert handler_lines[0]["request_id"] == rid
    assert handler_lines[0]["method"] == "GET"
    assert handler_lines[0]["path"] == "/ping"


def test_access_log_strips_query_string(log_file):
    """Simulate the uvicorn.access record that would fire for this request.

    TestClient doesn't run uvicorn's access logger, so we emit the record
    directly — that's the seam we care about: our formatter stripping the
    query string. The middleware's contribution (request_id via contextvars)
    is covered in the propagate test above.
    """
    uvlog = logging.getLogger("uvicorn.access")
    uvlog.info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:5555",
        "GET",
        "/api/v1/runs?search=secret-token-foo",
        "1.1",
        200,
    )
    records = _drain(log_file)
    assert len(records) == 1
    rec = records[0]
    assert rec["path"] == "/api/v1/runs"
    assert rec["has_query"] is True
    full = json.dumps(rec)
    assert "secret-token-foo" not in full


def test_unauthenticated_access_has_no_tenant_id(log_file):
    app = _make_app()
    client = TestClient(app)
    client.get("/ping")
    records = _drain(log_file)
    # None of the app-stream records emitted during an unauthenticated
    # request should carry tenant_id.
    app_lines = [r for r in records if r.get("stream") == "app"]
    assert app_lines
    assert all("tenant_id" not in r for r in app_lines), app_lines
