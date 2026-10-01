"""Blueprint B13: the run-scoped MCP server, driven by a container agent.

The accept gate's second half: a container agent reads/writes
``run_store`` **via MCP** during its run, authenticated by the same Run
Contract bearer the chassis minted. A test-local contract server (an
echo variant) makes real HTTP calls back to the chassis MCP endpoint
mid-run; direct endpoint tests pin the JSON-RPC surface, the 401, and
the grant gate.

Real Redis is used (the sandbox/service one) — the token registry and
run_store are Redis-backed by design.
"""
from __future__ import annotations

import json
import threading
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.agents import registry
from app.agents.container import ContainerAgent
from app.agents.manifest import load_manifest
from app.services import agent_runner
from tests.test_agent_runner import (
    _FakeRun,
    _FakeSession,
    patch_runner,  # noqa: F401  (fixture)
)
from tests.test_container_runner import _write_container_dir

from app.main import app as chassis_app


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


@pytest.fixture(scope="module")
def chassis_client():
    # No lifespan needed — the MCP endpoint is stateless and the token
    # registry talks straight to Redis.
    return TestClient(chassis_app)


def test_compose_advertises_a_container_routable_mcp_url():
    """The MCP url is advertised to agent CONTAINERS, so the compose
    deployment must pass ``LIBRERUN_PUBLIC_URL`` through with a
    container-routable default — a localhost value would send every
    container agent to ITSELF instead of the chassis. (The backend image
    builds from ``./backend``, so the repo-root .env cannot supply it.)"""
    import yaml

    repo_root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((repo_root / "compose.yaml").read_text())
    env = compose["services"]["backend"]["environment"]
    assert "LIBRERUN_PUBLIC_URL" in env, (
        "compose must pass LIBRERUN_PUBLIC_URL into the backend container"
    )
    value = str(env["LIBRERUN_PUBLIC_URL"])
    default = value.split(":-", 1)[1].rstrip("}") if ":-" in value else ""
    assert default, "LIBRERUN_PUBLIC_URL needs a default; blank is unusable"
    assert not any(
        host in default for host in ("localhost", "127.0.0.1", "0.0.0.0")
    ), f"default {default!r} is not reachable from another container"


def _rpc(client, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/api/v1/mcp", json=payload, headers=headers)


# --------------------------- endpoint surface --------------------------------


def test_initialize_needs_no_token(chassis_client):
    r = _rpc(chassis_client, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["serverInfo"]["name"] == "librerun"
    assert "tools" in result["capabilities"]


def test_initialize_reports_the_platform_version(chassis_client, monkeypatch):
    """``initialize`` names the platform's version, read from
    ``app.version`` when it answers (K blueprint B1b, D38): it once
    answered a literal ``"1.0.0"`` that no version check read. A probe
    value proves the read, since a literal equal to VERSION would pass the
    first assertion alone."""
    from app import version

    r = _rpc(chassis_client, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 200
    assert r.json()["result"]["serverInfo"]["version"] == version.__version__

    monkeypatch.setattr(version, "__version__", "9.9.9-probe.1")
    r = _rpc(chassis_client, {"jsonrpc": "2.0", "id": 2, "method": "initialize"})
    assert r.json()["result"]["serverInfo"]["version"] == "9.9.9-probe.1"


def test_tools_need_a_live_run_token(chassis_client):
    r = _rpc(chassis_client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert r.status_code == 401
    assert "run token" in r.json()["error"]["message"]


async def _register_token(grants):
    import redis.asyncio as aioredis

    from app.config import settings

    token = f"test-{uuid.uuid4().hex}"
    run = {
        "run_id": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "agent_id": "mcp-test",
        "grants": grants,
    }
    async with aioredis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True) as r:
        await r.set(f"run_token:{token}", json.dumps(run), ex=300)
    return token, run


@pytest.mark.asyncio
async def test_tools_list_is_grant_filtered(chassis_client):
    token, _ = await _register_token(["run_store"])
    r = _rpc(
        chassis_client,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        token=token,
    )
    names = {t["name"] for t in r.json()["result"]["tools"]}
    # `config_get` needs no grant (K5a): the run's own configuration; nor
    # does `secret_get` (K8a): the run's own tool secrets.
    assert names == {"run_store_get", "run_store_set", "config_get", "secret_get"}


@pytest.mark.asyncio
async def test_run_store_roundtrip_over_mcp(chassis_client):
    token, run = await _register_token(["run_store"])
    r = _rpc(
        chassis_client,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "run_store_set",
                "arguments": {"key": "note", "value": {"x": 1}},
            },
        },
        token=token,
    )
    assert r.status_code == 200 and "error" not in r.json()
    r = _rpc(
        chassis_client,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "run_store_get", "arguments": {"key": "note"}},
        },
        token=token,
    )
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["value"] == {"x": 1}


@pytest.mark.asyncio
async def test_ungranted_tool_is_refused(chassis_client):
    token, _ = await _register_token(["run_store"])
    r = _rpc(
        chassis_client,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "kb_search", "arguments": {"queries": ["q"]}},
        },
        token=token,
    )
    err = r.json()["error"]
    assert err["code"] == -32002 and "not granted" in err["message"]


@pytest.mark.asyncio
async def test_config_get_is_listed_and_served_with_no_grant(chassis_client, monkeypatch):
    """K5-04: the run's own configuration needs no grant.

    Before K5a `config_get` was listed only under `llm` and served without
    it, while the battery refused it — three answers to one question. An
    agent whose token grants nothing at all is now offered `config_get`
    and served its steps and its settings, in its run's tenant, while
    every other tool stays behind its grant. The reads themselves are
    stubbed here (Gate T, `test_gate_t.py`, holds them to the database);
    what is under test is the grant and the shape.
    """
    import contextlib

    import app.database
    from app.agents.manifest import AgentManifest
    from app.agents.protocol import AgentProtocol
    from app.services import agent_settings_service, agent_step_config_service

    class _Agent(AgentProtocol):
        agent_id = "mcp-test"
        display_name = "MCP test"
        description = "declares one setting and grants nothing"

    manifest = AgentManifest.model_validate(
        {
            "id": "mcp-test",
            "name": "MCP test",
            "runtime": "python-package",
            "phases": [{"name": "work"}],
            "output": {"mode": "structured"},
            "settings": [
                {
                    "key": "depth",
                    "type": "enum",
                    "options": ["basic", "advanced"],
                    "default": "advanced",
                },
                {"key": "limit", "type": "int", "default": 3},
            ],
        }
    )
    registry.register(_Agent(), manifest)
    token, run = await _register_token([])
    reads: list[tuple[str, str]] = []

    @contextlib.asynccontextmanager
    async def _session():
        yield object()

    async def _values_for(db, tenant_id, agent_id):
        reads.append((str(tenant_id), agent_id))
        return {"depth": "basic"}

    async def _overrides_for(db, tenant_id, agent_id):
        return {}

    monkeypatch.setattr(app.database, "async_session", _session)
    monkeypatch.setattr(agent_settings_service, "values_for", _values_for)
    monkeypatch.setattr(agent_step_config_service, "overrides_for", _overrides_for)

    r = _rpc(chassis_client, {"jsonrpc": "2.0", "id": 40, "method": "tools/list"}, token=token)
    # The two tools that need no grant: the run's own configuration and,
    # since K8a, its own tool secrets.
    assert [t["name"] for t in r.json()["result"]["tools"]] == ["config_get", "secret_get"]

    r = _call(chassis_client, token, "config_get", {}, id_=41)
    assert "error" not in r.json(), r.json()
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload == {
        "steps": [],
        "settings": [{"key": "depth", "value": "basic"}, {"key": "limit", "value": 3}],
    }
    # Read for the token's run: its tenant and its agent, nobody else's.
    assert reads == [(run["tenant_id"], "mcp-test")]

    # And every other tool is still behind its grant.
    r = _call(chassis_client, token, "run_store_get", {"key": "k"}, id_=42)
    assert r.json()["error"]["code"] == -32002


# --------------------------- container agent over MCP ------------------------


class _McpEchoHandler(BaseHTTPRequestHandler):
    """A contract server that, during its run, calls the chassis MCP
    endpoint (run_store set + get) with its own bearer token and echoes
    the roundtrip in its output — the accept gate's proof."""

    chassis_url: str = ""  # set by the fixture

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _mcp_call(self, token, method, params, msg_id):
        req = urllib.request.Request(
            self.chassis_url,
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "method": method,
                    "params": params,
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        if self.path.endswith("/events"):
            run_id = self.path.split("/")[3]
            token = self.server.runs[run_id]["token"]  # type: ignore[attr-defined]
            mcp_url = self.server.runs[run_id]["mcp_url"]  # type: ignore[attr-defined]
            type(self).chassis_url = mcp_url
            # The agent uses its run bearer against the chassis MCP.
            self._mcp_call(
                token,
                "tools/call",
                {
                    "name": "run_store_set",
                    "arguments": {"key": "from_agent", "value": "hello-mcp"},
                },
                10,
            )
            read_back = self._mcp_call(
                token,
                "tools/call",
                {"name": "run_store_get", "arguments": {"key": "from_agent"}},
                11,
            )
            value = json.loads(read_back["result"]["content"][0]["text"])["value"]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            frame = (
                "event: completed\n"
                + "data: "
                + json.dumps({"output": {"mcp_roundtrip": value}})
                + "\n\n"
            )
            self.wfile.write(frame.encode())
            self.wfile.flush()
            return
        self._json(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length))
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        run_id = uuid.uuid4().hex
        if not hasattr(self.server, "runs"):
            self.server.runs = {}  # type: ignore[attr-defined]
        self.server.runs[run_id] = {  # type: ignore[attr-defined]
            "token": token,
            "mcp_url": payload["run"]["mcp"]["url"],
        }
        self._json(201, {"run_id": run_id})


@pytest.fixture()
def mcp_echo_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _McpEchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.mark.asyncio
async def test_container_agent_uses_run_store_via_mcp(
    tmp_path, monkeypatch, mcp_echo_url, patch_runner  # noqa: F811
):
    """The accept gate: a container agent reads/writes run_store via the
    chassis MCP endpoint mid-run, using its Run Contract bearer. The
    chassis MCP is served in-process (TestClient via a live uvicorn-free
    port is impossible here), so we point the advertised mcp url at a
    thread-served copy of the app."""
    from uvicorn.config import Config  # noqa: F401 — presence check only

    # Serve the real chassis app on a local port for the agent to call.
    import uvicorn

    config = uvicorn.Config(
        chassis_app, host="127.0.0.1", port=0, log_level="error", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    import time

    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    monkeypatch.setattr(
        "app.config.settings.LIBRERUN_PUBLIC_URL", f"http://127.0.0.1:{port}"
    )

    d = _write_container_dir(tmp_path, agent_id="mcp-echo-v1")
    # Grant run_store to the container agent.
    manifest_text = (d / "agent.yaml").read_text()
    (d / "agent.yaml").write_text(
        manifest_text + "capabilities:\n  - run_store\n"
    )
    manifest = load_manifest(d)
    agent = ContainerAgent(manifest, mcp_echo_url, d)
    registry.register(agent, manifest, agent_dir=d)

    run = _FakeRun(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    run.agent_id = "mcp-echo-v1"
    session = _FakeSession(run)
    patch_runner(session)

    await agent_runner.start_run(run.id, run.tenant_id, "mcp-echo-v1")

    server.should_exit = True
    assert run.status == "complete"
    assert session.snapshot.structured_data == {"mcp_roundtrip": "hello-mcp"}


# --------------------------- untrusted argument handling ---------------------


@pytest.mark.asyncio
async def test_positional_params_get_invalid_params_not_500(chassis_client):
    """JSON-RPC allows an array `params`; this server only accepts the
    object form and must say so rather than crashing on .get()."""
    token, _ = await _register_token(["run_store"])
    r = _rpc(
        chassis_client,
        {"jsonrpc": "2.0", "id": 20, "method": "tools/call", "params": ["a"]},
        token=token,
    )
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_non_object_arguments_get_invalid_params(chassis_client):
    token, _ = await _register_token(["kb"])
    r = _rpc(
        chassis_client,
        {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {"name": "kb_search", "arguments": ["not", "an", "object"]},
        },
        token=token,
    )
    assert r.json()["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_kb_search_arguments_are_validated(chassis_client):
    """A string where an array is declared would otherwise fan out into
    one embedding + vector query PER CHARACTER."""
    token, _ = await _register_token(["kb"])
    for bad in ("a string", [], ["ok"] * 50, [1, 2]):
        r = _rpc(
            chassis_client,
            {
                "jsonrpc": "2.0",
                "id": 22,
                "method": "tools/call",
                "params": {"name": "kb_search", "arguments": {"queries": bad}},
            },
            token=token,
        )
        assert r.json()["error"]["code"] == -32602, bad


@pytest.mark.asyncio
async def test_audit_log_reports_whether_the_row_persisted(chassis_client, monkeypatch):
    """The capability swallows write failures so a run's fate never hinges
    on an audit row — so the tool must not answer 'ok' for a dropped
    write, and must reject an action_type the column cannot store."""
    token, _ = await _register_token(["audit"])

    r = _rpc(
        chassis_client,
        {
            "jsonrpc": "2.0",
            "id": 23,
            "method": "tools/call",
            "params": {
                "name": "audit_log",
                "arguments": {"action_type": "x" * 40, "detail": {}},
            },
        },
        token=token,
    )
    assert r.json()["error"]["code"] == -32602

    # With no database reachable the write is swallowed → ok must be False.
    r = _rpc(
        chassis_client,
        {
            "jsonrpc": "2.0",
            "id": 24,
            "method": "tools/call",
            "params": {
                "name": "audit_log",
                "arguments": {"action_type": "demo_event", "detail": {"a": 1}},
            },
        },
        token=token,
    )
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert isinstance(payload["ok"], bool)


@pytest.mark.asyncio
async def test_tool_failures_do_not_leak_internals(chassis_client, monkeypatch):
    """-32603 messages cross into third-party agent code, so exception
    text (which carries hosts, ports, and credentials in URLs) stays in
    the chassis log."""
    from app import capabilities as caps_mod

    class _Boom:
        async def get(self, key):
            raise RuntimeError("Error 111 connecting to redis://user:pw@10.0.0.5:6379")

    class _Facade:
        run_store = _Boom()

    monkeypatch.setattr(caps_mod, "for_run", lambda **_: _Facade())
    token, _ = await _register_token(["run_store"])
    r = _rpc(
        chassis_client,
        {
            "jsonrpc": "2.0",
            "id": 25,
            "method": "tools/call",
            "params": {"name": "run_store_get", "arguments": {"key": "k"}},
        },
        token=token,
    )
    err = r.json()["error"]
    assert err["code"] == -32603
    assert "10.0.0.5" not in err["message"] and "pw" not in err["message"]


@pytest.mark.asyncio
async def test_pre_s1_tool_names_and_grant_spelling_still_work_for_one_release(chassis_client):
    """Blueprint S1 (L18): a run token minted with the ``case_store`` grant
    lists the run_store tools, and ``tools/call`` still answers to
    ``case_store_set`` / ``case_store_get``. Removed at v1.1."""
    token, _ = await _register_token(["case_store"])
    r = _rpc(chassis_client, {"jsonrpc": "2.0", "id": 30, "method": "tools/list"}, token=token)
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {"run_store_get", "run_store_set", "config_get", "secret_get"}, (
        "old names are not advertised"
    )
    r = _rpc(
        chassis_client,
        {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {"name": "case_store_set", "arguments": {"key": "k", "value": [1, 2]}},
        },
        token=token,
    )
    assert r.status_code == 200 and "error" not in r.json(), r.text
    r = _rpc(
        chassis_client,
        {
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": {"name": "case_store_get", "arguments": {"key": "k"}},
        },
        token=token,
    )
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["value"] == [1, 2]


# --------------------------------------------------------------------------
# S4: the redact tool under the pii grant, declared arguments only, and the
# walk at the MCP write paths
# --------------------------------------------------------------------------

FIXTURE_EMAIL = "pii.fixture@example.com"


def _call(client, token, name, arguments, id_=90):
    return _rpc(
        client,
        {
            "jsonrpc": "2.0",
            "id": id_,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        token=token,
    )


@pytest.mark.asyncio
async def test_redact_is_listed_and_callable_only_under_the_pii_grant(chassis_client):
    from app.services import pii_service

    token, _ = await _register_token(["pii"])
    r = _rpc(chassis_client, {"jsonrpc": "2.0", "id": 80, "method": "tools/list"}, token=token)
    assert {t["name"] for t in r.json()["result"]["tools"]} == {
        "redact",
        "config_get",
        "secret_get",
    }
    text = f"mail me at {FIXTURE_EMAIL}"
    r = _call(chassis_client, token, "redact", {"text": text})
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["text"] == pii_service.redact(text)[0]
    assert FIXTURE_EMAIL not in payload["text"]

    token, _ = await _register_token(["run_store", "audit"])
    r = _rpc(chassis_client, {"jsonrpc": "2.0", "id": 81, "method": "tools/list"}, token=token)
    assert "redact" not in {t["name"] for t in r.json()["result"]["tools"]}
    r = _call(chassis_client, token, "redact", {"text": "x"})
    assert r.json()["error"]["code"] == -32002


@pytest.mark.asyncio
async def test_tools_refuse_arguments_they_do_not_declare(chassis_client):
    token, _ = await _register_token(["audit", "run_store", "pii"])
    r = _call(
        chassis_client, token, "audit_log",
        {"action_type": "demo", "detail": {}, "user_email": FIXTURE_EMAIL},
    )
    assert r.json()["error"]["code"] == -32602
    assert "user_email" in r.json()["error"]["message"]
    r = _call(chassis_client, token, "run_store_set", {"key": "k", "value": 1, "ttl": 5})
    assert r.json()["error"]["code"] == -32602
    r = _call(chassis_client, token, "redact", {"text": "x", "mode": "all"})
    assert r.json()["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_run_store_set_over_mcp_is_walked_and_a_flagged_key_is_refused(chassis_client):
    import redis.asyncio as aioredis

    from app.config import settings

    token, run = await _register_token(["run_store"])
    r = _call(chassis_client, token, "run_store_set", {"key": "note", "value": {"t": f"see {FIXTURE_EMAIL}"}})
    assert "error" not in r.json(), r.json()
    r = _call(chassis_client, token, "run_store_set", {"key": FIXTURE_EMAIL, "value": 1})
    assert r.json()["error"]["code"] == -32003
    assert "pii_in_store" in r.json()["error"]["message"]
    assert FIXTURE_EMAIL not in r.json()["error"]["message"]
    r = _call(chassis_client, token, "run_store_set", {"key": "contact", "value": {"phone": 2125551234}})
    assert r.json()["error"]["code"] == -32003
    assert "$.phone" in r.json()["error"]["message"]
    async with aioredis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True) as rd:
        stored = await rd.hgetall(f"run:{run['run_id']}:kv")
    assert set(stored) == {"note"}
    assert FIXTURE_EMAIL not in json.dumps(stored)


@pytest.mark.asyncio
async def test_audit_log_over_mcp_refuses_a_flagged_action_type_or_detail(chassis_client):
    token, _ = await _register_token(["audit"])
    r = _call(chassis_client, token, "audit_log", {"action_type": FIXTURE_EMAIL[:30], "detail": {}})
    assert r.json()["error"]["code"] == -32003
    assert "pii_in_audit" in r.json()["error"]["message"]
    r = _call(chassis_client, token, "audit_log", {"action_type": "demo", "detail": {FIXTURE_EMAIL: 1}})
    assert r.json()["error"]["code"] == -32003
