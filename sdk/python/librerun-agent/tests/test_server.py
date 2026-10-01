"""The four endpoints, token binding, the events stream, the output
fallback, failures and the deadline, and the MCP-backed context."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from librerun_agent import CapabilityNotGranted, PiiRefused, RunContext, serve
from librerun_agent.testing import serve_in_thread

from _fakes import FakeMCP, start

TRACEPARENT = "00-4bf92f3577b34da6a2ce929d0e0e4736-00f067aa0ba902b7-01"


def _events(client, url, invocation_id, token):
    with client.stream("GET", f"{url}/v1/runs/{invocation_id}/events", headers={"Authorization": f"Bearer {token}"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events, name, data = [], None, []
        for line in r.iter_lines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
            elif line == "":
                if name:
                    events.append((name, json.loads("\n".join(data) or "{}")))
                name, data = None, []
        return events


def _post(client, url, token="tok-1", **overrides):
    body = {
        "contract": "v1", "agent_id": "t", "phase": "echo",
        "run": {"id": "run-1", "tenant_id": "ten-1", "rerun": False, "mcp": {"url": overrides.pop("mcp_url", None)}},
        "input": {"message": "hi"}, "prior_output": None, "user_edits": None, "deadline_seconds": 30,
    }
    body.update(overrides.pop("body", {}))
    if not body["run"]["mcp"]["url"]:
        body["run"].pop("mcp")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers.update(overrides.pop("headers", {}))
    return client.post(f"{url}/v1/runs", json=body, headers=headers)


@pytest.fixture(scope="module")
def mcp():
    server, url = start(FakeMCP)
    yield url
    server.shutdown()


@pytest.fixture(scope="module")
def app_url():
    seen: dict = {}

    async def handler(ctx: RunContext) -> dict:
        seen["ctx"] = ctx
        ctx.progress("running", step="echo", label="Echoing")
        ctx.log("echoing the input back")
        mode = ctx.input.get("mode")
        if mode == "boom":
            raise ValueError("boom for run " + ctx.run_id)
        if mode == "slow":
            await asyncio.sleep(5)
        if mode == "not-a-dict":
            return "nope"  # type: ignore[return-value]
        if mode == "capabilities":
            ok = await ctx.capabilities.audit_log("asked", {"n": 1})
            clean = await ctx.pii.redact("mail me at a@b.co")
            hits = await ctx.capabilities.kb_search("q", top_k=2)
            await ctx.capabilities.run_store_set("k", {"v": 1})
            got = await ctx.capabilities.run_store_get("k")
            return {"ok": ok, "clean": clean, "hits": hits, "got": got}
        if mode == "not-granted":
            await ctx.capabilities.kb_search("q")
        if mode == "pii":
            await ctx.capabilities.run_store_set("bad", 1)
        ctx.progress("completed", step="echo")
        return {"echo": ctx.input, "phase": ctx.phase, "deadline_left": ctx.seconds_left, "tp": ctx.traceparent}

    app = serve(handler, name="test-agent", capture_stdio=False)
    handle = serve_in_thread(app)
    yield handle.url, seen
    handle.stop()


def test_healthz_and_unknown_paths(app_url):
    url, _ = app_url
    r = httpx.get(f"{url}/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert httpx.get(f"{url}/nope").status_code == 404


def test_the_full_exchange(app_url):
    url, seen = app_url
    with httpx.Client(timeout=10) as client:
        r = _post(client, url, headers={"traceparent": TRACEPARENT, "tracestate": "vendor=abc"})
        assert r.status_code == 201, r.text
        invocation_id = r.json()["invocation_id"]
        assert r.json()["run_id"] == invocation_id  # the one-release alias
        events = _events(client, url, invocation_id, "tok-1")
        names = [e[0] for e in events]
        assert names == ["progress", "log", "progress", "completed"]
        assert events[0][1] == {"step_id": "echo", "status": "running", "detail": None, "label": "Echoing"}
        assert events[1][1] == {"level": "info", "message": "echoing the input back"}
        output = events[-1][1]["output"]
        assert output["echo"] == {"message": "hi"} and output["phase"] == "echo"
        assert 0 < output["deadline_left"] <= 30
        assert output["tp"] == TRACEPARENT
        assert seen["ctx"].tracestate == "vendor=abc"
        assert seen["ctx"].run_id == "run-1" and seen["ctx"].tenant_id == "ten-1"
        assert seen["ctx"].deadline is not None
        r = client.get(f"{url}/v1/runs/{invocation_id}/output", headers={"Authorization": "Bearer tok-1"})
        assert r.status_code == 200 and r.json()["output"] == output
        # A replay of the stream sees the same events again.
        assert [e[0] for e in _events(client, url, invocation_id, "tok-1")] == names


def test_token_binding(app_url):
    url, _ = app_url
    with httpx.Client(timeout=10) as client:
        assert _post(client, url, token=None).status_code == 401
        r = _post(client, url, token="tok-A")
        invocation_id = r.json()["invocation_id"]
        assert client.get(f"{url}/v1/runs/{invocation_id}/events", headers={"Authorization": "Bearer tok-B"}).status_code == 401
        assert client.get(f"{url}/v1/runs/{invocation_id}/output").status_code == 401
        assert client.get(f"{url}/v1/runs/unknown/events", headers={"Authorization": "Bearer tok-A"}).status_code == 404


def test_bad_requests(app_url):
    url, _ = app_url
    with httpx.Client(timeout=10) as client:
        assert _post(client, url, body={"contract": "v2"}).status_code == 400
        assert _post(client, url, body={"phase": ""}).status_code == 400
        assert _post(client, url, body={"deadline_seconds": 0}).status_code == 400
        r = client.post(f"{url}/v1/runs", content=b"not json", headers={"Authorization": "Bearer t"})
        assert r.status_code == 400


def test_a_failing_handler_reports_failed_and_the_output_endpoint_409s(app_url):
    url, _ = app_url
    with httpx.Client(timeout=10) as client:
        invocation_id = _post(client, url, body={"input": {"mode": "boom"}}).json()["invocation_id"]
        events = _events(client, url, invocation_id, "tok-1")
        assert events[-1][0] == "failed"
        assert "ValueError: boom for run run-1" in events[-1][1]["error"]
        r = client.get(f"{url}/v1/runs/{invocation_id}/output", headers={"Authorization": "Bearer tok-1"})
        assert r.status_code == 409
        invocation_id = _post(client, url, body={"input": {"mode": "not-a-dict"}}).json()["invocation_id"]
        assert "must return a dict" in _events(client, url, invocation_id, "tok-1")[-1][1]["error"]


def test_the_deadline_fails_a_slow_handler(app_url):
    url, _ = app_url
    with httpx.Client(timeout=10) as client:
        invocation_id = _post(client, url, body={"input": {"mode": "slow"}, "deadline_seconds": 1}).json()["invocation_id"]
        events = _events(client, url, invocation_id, "tok-1")
        assert events[-1] == ("failed", {"error": "deadline of 1s exceeded"})


def test_the_output_endpoint_404s_before_completion(app_url):
    url, _ = app_url
    with httpx.Client(timeout=10) as client:
        invocation_id = _post(client, url, body={"input": {"mode": "slow"}, "deadline_seconds": 2}).json()["invocation_id"]
        r = client.get(f"{url}/v1/runs/{invocation_id}/output", headers={"Authorization": "Bearer tok-1"})
        assert r.status_code == 404


def test_capabilities_and_pii_go_through_the_mcp_server_with_the_token(app_url, mcp):
    url, _ = app_url
    FakeMCP.calls.clear()
    FakeMCP.responses = {
        "audit_log": {"ok": True},
        "redact": {"text": "mail me at [REDACTED_EMAIL_ADDRESS_1]"},
        "kb_search": {"results": [{"title": "t", "url": "u", "snippet": "s", "relevance_score": 0.9}]},
        "run_store_set": {"ok": True, "key": "k"},
        "run_store_get": {"key": "k", "value": {"v": 1}},
    }
    with httpx.Client(timeout=10) as client:
        r = _post(client, url, token="tok-cap", mcp_url=mcp, body={"input": {"mode": "capabilities"}},
                  headers={"traceparent": TRACEPARENT})
        invocation_id = r.json()["invocation_id"]
        events = _events(client, url, invocation_id, "tok-cap")
    assert events[-1][0] == "completed", events[-1]
    out = events[-1][1]["output"]
    assert out == {"ok": True, "clean": "mail me at [REDACTED_EMAIL_ADDRESS_1]",
                   "hits": [{"title": "t", "url": "u", "snippet": "s", "relevance_score": 0.9}], "got": {"v": 1}}
    names = [c["name"] for c in FakeMCP.calls]
    assert names == ["audit_log", "redact", "kb_search", "run_store_set", "run_store_get"]
    assert all(c["authorization"] == "Bearer tok-cap" for c in FakeMCP.calls)
    # Without tracing, the chassis phase span's traceparent is forwarded.
    assert all(c["traceparent"] == TRACEPARENT for c in FakeMCP.calls)
    assert FakeMCP.calls[1]["arguments"] == {"text": "mail me at a@b.co"}
    assert FakeMCP.calls[2]["arguments"] == {"queries": ["q"], "top_k": 2}


def test_capability_errors_are_typed(app_url, mcp):
    url, _ = app_url
    FakeMCP.responses = {
        "kb_search": ("error", -32002, "capability 'kb' is not granted"),
        "run_store_set": ("error", -32003, "pii_in_store: key at run_store.key (identifier flagged as EMAIL_ADDRESS)"),
    }
    with httpx.Client(timeout=10) as client:
        invocation_id = _post(client, url, token="t", mcp_url=mcp, body={"input": {"mode": "not-granted"}}).json()["invocation_id"]
        assert "CapabilityNotGranted" in _events(client, url, invocation_id, "t")[-1][1]["error"]
        invocation_id = _post(client, url, token="t", mcp_url=mcp, body={"input": {"mode": "pii"}}).json()["invocation_id"]
        error = _events(client, url, invocation_id, "t")[-1][1]["error"]
        assert "PiiRefused" in error and "pii_in_store" in error
    assert issubclass(CapabilityNotGranted, Exception) and issubclass(PiiRefused, Exception)


def test_without_an_mcp_url_capabilities_explain_themselves():
    async def handler(ctx: RunContext) -> dict:
        try:
            await ctx.capabilities.kb_search("q")
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        return {}

    from librerun_agent._context import Invocation

    inv = Invocation(id="i", token="t", run_id="r", tenant_id=None, phase="p", input={}, prior_output=None,
                     user_edits=None, rerun=False, deadline_seconds=None, mcp_url=None, traceparent=None,
                     tracestate=None, trace_id=None)
    out = asyncio.run(handler(RunContext(inv)))
    assert "run.mcp.url" in out["error"]
