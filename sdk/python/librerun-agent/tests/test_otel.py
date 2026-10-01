"""Export to the relay, one request per run token (the ``otel`` extra):
overlapping invocations never mix in one request, a span whose trace the
SDK does not own is dropped, the handler span is a child of the chassis
phase span, a captured print() is a log record under the invocation's
trace, the flush precedes ``completed``, and the token map outlives the
invocation by a grace only."""
from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest

pytest.importorskip("opentelemetry.sdk")

from librerun_agent import RunContext, _otel, serve
from librerun_agent.testing import serve_in_thread

from _fakes import FakeRelay, start

TRACE_A = "4bf92f3577b34da6a2ce929d0e0e4736"
TRACE_B = "0af7651916cd43dd8448eb211c80319c"


def _tp(trace_hex: str) -> str:
    return f"00-{trace_hex}-00f067aa0ba902b7-01"


@pytest.fixture(scope="module")
def relay():
    server, url = start(FakeRelay)
    yield url
    server.shutdown()


@pytest.fixture(scope="module")
def app_url(relay):
    _otel._reset_for_tests()
    gate = threading.Barrier(2, timeout=10)

    async def handler(ctx: RunContext) -> dict:
        from opentelemetry import trace

        tracer = trace.get_tracer("agent.echo")
        with tracer.start_as_current_span("custom work", attributes={"who": ctx.run_id}):
            print(f"printed by {ctx.run_id}")
            if ctx.input.get("pair"):
                await asyncio.to_thread(gate.wait)
            await asyncio.sleep(0.05)
        return {"ok": True}

    app = serve(handler, name="otel-test-agent", capture_stdio=True, otel_endpoint=relay)
    handle = serve_in_thread(app)
    yield handle.url
    handle.stop()
    _otel._reset_for_tests()
    from librerun_agent import _capture

    _capture.uninstall_stdio_capture()
    _capture.uninstall_thread_context()


def _run(client, url, token, run_id, trace_hex, pair=False):
    r = client.post(
        f"{url}/v1/runs",
        json={"contract": "v1", "phase": "p", "run": {"id": run_id}, "input": {"pair": pair}},
        headers={"Authorization": f"Bearer {token}", "traceparent": _tp(trace_hex)},
    )
    assert r.status_code == 201, r.text
    return r.json()["invocation_id"]


def _wait_terminal(client, url, invocation_id, token) -> str:
    with client.stream("GET", f"{url}/v1/runs/{invocation_id}/events", headers={"Authorization": f"Bearer {token}"}) as s:
        text = "".join(s.iter_text())
    return text


def test_overlapping_invocations_export_one_request_per_token_with_only_their_trace(app_url):
    FakeRelay.requests.clear()
    with httpx.Client(timeout=30) as client:
        a = _run(client, app_url, "tok-A", "run-A", TRACE_A, pair=True)
        b = _run(client, app_url, "tok-B", "run-B", TRACE_B, pair=True)
        ta = _wait_terminal(client, app_url, a, "tok-A")
        tb = _wait_terminal(client, app_url, b, "tok-B")
    assert "event: completed" in ta and "event: completed" in tb
    time.sleep(1.0)
    by_token: dict[str, dict] = {}
    for req in FakeRelay.requests:
        entry = by_token.setdefault(req["token"], {"spans": [], "records": []})
        entry["spans"] += req.get("spans", [])
        entry["records"] += req.get("records", [])
        assert req["content_type"] == "application/x-protobuf"
    assert set(by_token) == {"tok-A", "tok-B"}
    for token, trace_hex in (("tok-A", TRACE_A), ("tok-B", TRACE_B)):
        spans = by_token[token]["spans"]
        assert spans and all(s["trace_id"] == trace_hex for s in spans)
        names = {s["name"] for s in spans}
        assert "custom work" in names and "invocation p" in names
        invocation_span = next(s for s in spans if s["name"] == "invocation p")
        assert invocation_span["parent"] == "00f067aa0ba902b7"  # the chassis phase span
        records = by_token[token]["records"]
        assert records and all(r["trace_id"] == trace_hex for r in records)
        assert any(r["body"] == f"printed by run-{token[-1]}" for r in records)


def test_the_flush_precedes_completed(app_url):
    FakeRelay.requests.clear()
    with httpx.Client(timeout=30) as client:
        a = _run(client, app_url, "tok-F", "run-F", "4bf92f3577b34da6a2ce929d0e0e4700")
        text = _wait_terminal(client, app_url, a, "tok-F")
    assert "event: completed" in text
    # By the time completed was delivered the relay had the spans.
    assert any(req["token"] == "tok-F" and req.get("spans") for req in FakeRelay.requests)


def test_a_span_outside_any_invocation_is_dropped(app_url):
    from opentelemetry import trace

    FakeRelay.requests.clear()
    before = _otel.registry.dropped
    with trace.get_tracer("stray").start_as_current_span("stray span"):
        pass
    _otel.flush()
    time.sleep(0.5)
    assert _otel.registry.dropped > before
    assert not any("stray span" in json.dumps(req.get("spans", [])) for req in FakeRelay.requests)


def test_the_token_map_outlives_the_invocation_by_a_grace_only(monkeypatch):
    registry = _otel._TokenRegistry()
    registry.register("inv-1", "tok")
    assert registry.token_for("inv-1") == "tok"
    registry.end("inv-1")
    assert registry.token_for("inv-1") == "tok"  # inside the grace
    monkeypatch.setattr(_otel, "TOKEN_GRACE_SECONDS", 0.0)
    time.sleep(0.01)
    assert registry.token_for("inv-1") is None
    assert registry.token_for("never") is None


def test_two_invocations_under_one_trace_id_keep_their_own_tokens():
    """The registry is keyed by invocation, not by trace.

    Two runs can share an upstream W3C trace — a caller may submit
    several under one, of different tenants even. Keyed by trace id, the
    second registration replaced the first's token: every span of the
    first then left under the second's token and the relay stamped them
    as the second run, filing one tenant's telemetry under another's.
    The first's ``end()`` also started the grace clock on the live one.
    """
    registry = _otel._TokenRegistry()
    registry.register("inv-a", "token-a")
    registry.register("inv-b", "token-b")   # same trace, different run
    assert registry.token_for("inv-a") == "token-a"
    assert registry.token_for("inv-b") == "token-b"

    registry.end("inv-a")                    # the first finishes
    assert registry.token_for("inv-b") == "token-b", "the live invocation lost its token"


def test_the_registry_does_not_grow_with_every_finished_invocation(monkeypatch):
    """Expiry has to happen without a later export asking.

    A finished invocation flushes its telemetry BEFORE ``end()``, so
    nothing calls ``token_for`` for it again — and expiry lived only in
    ``token_for``. Every completed run's entry and bearer token stayed
    for the life of the process. A long-running agent is exactly the
    case that matters.
    """
    registry = _otel._TokenRegistry()
    monkeypatch.setattr(_otel, "TOKEN_GRACE_SECONDS", 0.0)
    for n in range(50):
        registry.register(f"inv-{n}", f"token-{n}")
        registry.end(f"inv-{n}")          # as the server does at completion
        time.sleep(0.001)
    assert registry.size() <= 2, f"the registry kept {registry.size()} finished invocations"

    # And a live one is never pruned out from under itself.
    monkeypatch.setattr(_otel, "TOKEN_GRACE_SECONDS", 60.0)
    registry.register("live", "token-live")
    registry.register("other", "token-other")
    registry.end("other")
    assert registry.token_for("live") == "token-live"


def test_an_agent_cannot_claim_another_invocations_spans():
    """Ownership lives beside the span, not in its attributes.

    Attributes are a mutable collection the agent shares, so ownership
    written there is ownership the agent can rewrite: handler code in
    invocation A could set `librerun.invocation_id` to B's, and since
    the two share an upstream trace the relay's trace check passes and
    A's telemetry is filed as B's run — and B's tenant.
    """
    from librerun_agent import _otel as otel

    owners = otel._SpanOwners()
    owners.record(trace_id=7, span_id=11, invocation_id="inv-A")

    class _Span:
        def __init__(self, trace_id, span_id, attributes):
            self.attributes = attributes
            self._ctx = type("C", (), {"trace_id": trace_id, "span_id": span_id})()

        def get_span_context(self):
            return self._ctx

    # The agent relabels the span with another invocation's id.
    span = _Span(7, 11, {otel.INVOCATION_ATTRIBUTE: "inv-B"})
    previous, otel.span_owners = otel.span_owners, owners
    try:
        assert otel._span_owner(span) == "inv-A", "the attribute decided ownership"
        # And taking it is once: a replayed span has no owner and is dropped.
        assert otel._span_owner(span) is None
    finally:
        otel.span_owners = previous


def test_the_owner_store_is_bounded():
    """A span created and never exported leaves its entry behind, so the
    store has to forget. Losing an owner drops the span, which is the
    safe direction — it never sends one under the wrong token."""
    from librerun_agent import _otel as otel

    owners = otel._SpanOwners(limit=10)
    for n in range(50):
        owners.record(trace_id=1, span_id=n, invocation_id=f"inv-{n}")
    assert owners.take(1, 49) == "inv-49"       # the newest is kept
    assert owners.take(1, 0) is None            # the oldest was forgotten
