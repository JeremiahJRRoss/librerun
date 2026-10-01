"""Blueprint B12a: the container runner — Run Contract v1 end to end.

The reference echo agent (``agents/_examples/echo_container``) runs as a
real HTTP server on a thread; the chassis side is exercised at three
levels:

1. **Discovery** — a ``runtime: container`` directory registers a
   ``ContainerAgent`` proxy (env-expanded url, schema file checked), the
   failure modes are logged skips, and entry points still refuse
   container manifests.
2. **The proxy itself** — ``run_phase`` drives healthz → POST → SSE →
   result mapping, including the output-endpoint fallback and the
   ``failed``-event and unreachable-container error paths.
3. **The generic lifecycle** — ``start_run``/``resume_run`` walk a
   container agent exactly like an in-process one: ungated single phase
   completes into ``structured_data``; a gated second phase parks in
   ``awaiting_approval`` and resumes with the prior phase's output
   threaded through as ``prior_output``.
"""
from __future__ import annotations

import importlib.util
import logging
import json
import yaml
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.agents import registry
from app.agents.container import ContainerAgent, ContainerAgentError
from app.agents.manifest import load_manifest
from app.agents.protocol import AgentInput
from app.services import agent_runner
from tests.test_agent_runner import (
    _FakeRun,
    _FakeSession,
    patch_runner,  # noqa: F401  (fixture)
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
ECHO_DIR = BACKEND_DIR / "agents" / "_examples" / "echo_container"


def _load_echo_module():
    spec = importlib.util.spec_from_file_location(
        "echo_container_reference", ECHO_DIR / "echo_agent.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


@pytest.fixture(scope="module")
def echo_url():
    """The reference echo agent — on the agent SDK since blueprint S4 —
    live on an ephemeral local port under uvicorn."""
    from librerun_agent.testing import serve_in_thread

    echo = _load_echo_module()
    handle = serve_in_thread(echo.app)
    yield handle.url
    handle.stop()


def _write_container_dir(
    root: Path,
    *,
    agent_id: str = "echo-test-v1",
    url: str = "${TEST_ECHO_URL}",
    phases: list[dict] | None = None,
    with_schema: bool = True,
) -> Path:
    d = root / agent_id
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "manifest_version": 1,
                "id": agent_id,
                "name": "Echo Test",
                "runtime": "container",
                "container": {"url": url},
                "input_schema": "input_schema.json",
                "phases": phases or [{"name": "echo"}],
                "output": {"mode": "structured"},
            }
        )
    )
    if with_schema:
        (d / "input_schema.json").write_text(
            json.dumps(
                {
                    "type": "object",
                    "required": ["message"],
                    "properties": {"message": {"type": "string"}},
                }
            )
        )
    return d


def _agent_input(**overrides) -> AgentInput:
    defaults = dict(
        run_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_inputs={"message": "hello"},
        prior_analysis=None,
        user_edits=None,
    )
    defaults.update(overrides)
    return AgentInput(**defaults)


class _ProgressCollector:
    def __init__(self):
        self.events = []

    async def __call__(self, p):
        self.events.append((p.step_id, p.status))


# --------------------------- discovery ---------------------------------------


def test_container_directory_registers_a_proxy(tmp_path, monkeypatch, echo_url):
    d = _write_container_dir(tmp_path)
    monkeypatch.setenv("TEST_ECHO_URL", echo_url)
    monkeypatch.setattr(registry, "_agent_entry_points", lambda: [])

    count = registry.discover_agents(tmp_path, include_entry_points=True)

    assert count == 1
    agent = registry.get_agent("echo-test-v1")
    assert isinstance(agent, ContainerAgent)
    assert registry.get_agent_dir("echo-test-v1") == d
    schema = agent.input_schema()
    assert schema["required"] == ["message"]


def test_container_skipped_when_url_env_unset(tmp_path, monkeypatch, caplog):
    _write_container_dir(tmp_path)
    monkeypatch.delenv("TEST_ECHO_URL", raising=False)

    assert registry.discover_agents(tmp_path) == 0
    assert any(
        "agent_container_url_unresolved" in r.getMessage() for r in caplog.records
    )


def test_container_skipped_when_schema_file_missing(tmp_path, monkeypatch, caplog):
    _write_container_dir(tmp_path, with_schema=False)
    monkeypatch.setenv("TEST_ECHO_URL", "http://127.0.0.1:1")

    assert registry.discover_agents(tmp_path) == 0
    assert any(
        "agent_container_schema_missing" in r.getMessage() for r in caplog.records
    )


def test_entry_points_still_refuse_container_manifests(tmp_path, monkeypatch, caplog):
    """An entry point IS a python package; container manifests register
    from the agents directory only."""
    from types import SimpleNamespace

    pkg = tmp_path / f"epc_{tmp_path.name}"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    _ = _write_container_dir(tmp_path, agent_id="unused")  # noqa: F841
    # Reuse the container manifest shape inside the EP package dir:
    (pkg / "agent.yaml").write_text(
        (tmp_path / "unused" / "agent.yaml").read_text().replace("id: unused", "id: epc-v1")
    )
    (pkg / "input_schema.json").write_text("{}")
    monkeypatch.setenv("TEST_ECHO_URL", "http://127.0.0.1:1")

    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module(pkg.name)
    finally:
        sys.path.remove(str(tmp_path))
    ep = SimpleNamespace(name="epc-v1", value=pkg.name, load=lambda: module)
    monkeypatch.setattr(registry, "_agent_entry_points", lambda: [ep])

    empty = tmp_path / "empty-agents"
    empty.mkdir()
    assert registry.discover_agents(empty, include_entry_points=True) == 0
    assert any(
        "agent_runtime_unsupported" in r.getMessage() for r in caplog.records
    )


def test_shipped_echo_example_manifest_is_valid(monkeypatch):
    """The reference directory must always parse; with the env var set its
    url resolves and the schema file exists."""
    manifest = load_manifest(ECHO_DIR)
    assert manifest.runtime == "container"
    assert manifest.container.url == "${ECHO_AGENT_URL}"
    assert (ECHO_DIR / manifest.input_schema).is_file()
    scenario = json.loads(
        (ECHO_DIR / "scenarios" / "demo-echo.json").read_text()
    )
    assert "message" in scenario["user_inputs"]


# --------------------------- the proxy ---------------------------------------


@pytest.mark.asyncio
async def test_run_phase_completes_against_live_echo(tmp_path, monkeypatch, echo_url):
    d = _write_container_dir(tmp_path)
    manifest = load_manifest(d)
    agent = ContainerAgent(manifest, echo_url, d)
    progress = _ProgressCollector()

    result = await agent.run_phase("echo", _agent_input(), progress)

    assert result.status == "complete"  # single phase == final
    assert result.structured["echo"] == {"message": "hello"}
    assert result.structured["phase"] == "echo"
    assert result.report_html is None
    assert ("echo", "running") in progress.events
    # The wire's "completed" maps onto the chassis progress vocabulary
    # ("complete") before hitting the callback — the progress endpoint's
    # response schema only speaks pending|running|complete|skipped|error.
    assert ("echo", "complete") in progress.events
    assert all(
        status in {"pending", "running", "complete", "skipped", "error"}
        for _, status in progress.events
    )


@pytest.mark.asyncio
async def test_run_phase_fails_fast_when_container_unreachable(tmp_path):
    d = _write_container_dir(tmp_path, url="http://127.0.0.1:9")
    manifest = load_manifest(d)
    agent = ContainerAgent(manifest, "http://127.0.0.1:9", d)

    with pytest.raises(ContainerAgentError, match="unreachable"):
        await agent.run_phase("echo", _agent_input(), _ProgressCollector())


class _ScriptedHandler(BaseHTTPRequestHandler):
    """Minimal contract server for the paths echo doesn't exercise:
    a ``failed`` terminal event, and ``completed`` without an inline
    output (forcing the output-endpoint fallback). Behavior is selected
    by the POSTed input's ``mode``."""

    runs: dict = {}

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        if self.path.endswith("/events"):
            run_id = self.path.split("/")[3]
            mode = self.runs[run_id]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if mode == "fail":
                self.wfile.write(
                    b'event: failed\ndata: {"error": "scripted failure"}\n\n'
                )
            else:  # lazy: terminal event omits the output
                self.wfile.write(b"event: completed\ndata: {}\n\n")
            self.wfile.flush()
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"came_from": "output-endpoint"}})
        self._json(404, {})

    def do_POST(self):
        from urllib.parse import quote

        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length))
        # The headers and body the chassis sent, for the propagation and
        # deadline tests.
        type(self).last_headers = {k.lower(): v for k, v in self.headers.items()}
        type(self).last_body = payload
        mode = payload["input"].get("mode", "lazy")
        if mode == "weird-id":
            # An opaque id full of reserved characters: the chassis must
            # percent-encode it as one path segment, which is exactly the
            # form this server keys the run under.
            run_id = "weird/run?id#1"
            self.runs[quote(run_id, safe="")] = "lazy"
            self._json(201, {"run_id": run_id})
            return
        run_id = uuid.uuid4().hex
        self.runs[run_id] = mode
        # What the chassis sent about the run, for the vocabulary test.
        type(self).last_run_object = payload.get("run")
        if mode == "invocation-id-only":
            # A post-S1 agent that speaks only the new spelling.
            self.runs[run_id] = "lazy"
            self._json(201, {"invocation_id": run_id})
            return
        self._json(201, {"run_id": run_id})


@pytest.fixture(scope="module")
def scripted_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.mark.asyncio
async def test_failed_event_raises(tmp_path, scripted_url):
    d = _write_container_dir(tmp_path)
    agent = ContainerAgent(load_manifest(d), scripted_url, d)

    with pytest.raises(ContainerAgentError, match="scripted failure"):
        await agent.run_phase(
            "echo", _agent_input(user_inputs={"mode": "fail"}), _ProgressCollector()
        )


@pytest.mark.asyncio
async def test_completed_without_output_falls_back_to_output_endpoint(
    tmp_path, scripted_url
):
    d = _write_container_dir(tmp_path)
    agent = ContainerAgent(load_manifest(d), scripted_url, d)

    result = await agent.run_phase(
        "echo", _agent_input(user_inputs={"mode": "lazy"}), _ProgressCollector()
    )
    assert result.structured == {"came_from": "output-endpoint"}


@pytest.mark.asyncio
async def test_opaque_run_ids_are_encoded_as_one_path_segment(
    tmp_path, scripted_url
):
    """A run_id containing /, ? and # must still address the run it
    created — the scripted server keys the run under the percent-encoded
    form, so a chassis interpolating the raw id would miss it."""
    d = _write_container_dir(tmp_path)
    agent = ContainerAgent(load_manifest(d), scripted_url, d)

    result = await agent.run_phase(
        "echo", _agent_input(user_inputs={"mode": "weird-id"}), _ProgressCollector()
    )
    assert result.structured == {"came_from": "output-endpoint"}


@pytest.mark.asyncio
async def test_progress_statuses_map_onto_the_chassis_vocabulary():
    """Direct unit check of the wire→chassis mapping, including the
    degrade-to-running rule for unknown values."""
    from app.agents.manifest import AgentManifest

    manifest = AgentManifest.model_validate(
        {
            "id": "map-v1",
            "name": "m",
            "runtime": "container",
            "container": {"url": "http://unused:1"},
            "input_schema": "input_schema.json",
            "phases": [{"name": "echo"}],
            "output": {"mode": "structured"},
        }
    )
    agent = ContainerAgent(manifest, "http://unused:1", Path("/nonexistent"))
    collector = _ProgressCollector()

    for wire, expected in [
        ("running", "running"),
        ("completed", "complete"),
        ("failed", "error"),
        ("no-such-status", "running"),
    ]:
        await agent._handle_event(
            "progress", {"step_id": "s", "status": wire}, collector
        )
        assert collector.events[-1] == ("s", expected)


# --------------------------- generic lifecycle -------------------------------


def _register_container(tmp_path, echo_url, *, phases: list[dict], agent_id="echo-test-v1"):
    d = _write_container_dir(tmp_path, agent_id=agent_id, phases=phases)
    manifest = load_manifest(d)
    agent = ContainerAgent(manifest, echo_url, d)
    registry.register(agent, manifest, agent_dir=d)
    return agent


@pytest.mark.asyncio
async def test_single_phase_container_run_completes(
    tmp_path, echo_url, patch_runner  # noqa: F811
):
    _register_container(tmp_path, echo_url, phases=[{"name": "echo"}])
    run = _FakeRun(
        run_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_inputs={"message": "through the runner"},
    )
    run.agent_id = "echo-test-v1"
    session = _FakeSession(run)
    patch_runner(session)

    await agent_runner.start_run(run.id, run.tenant_id, "echo-test-v1")

    assert run.status == "complete"
    assert session.snapshot.structured_data["echo"] == {
        "message": "through the runner"
    }
    assert session.snapshot.report_html is None


@pytest.mark.asyncio
async def test_gated_container_run_parks_and_resumes(
    tmp_path, echo_url, patch_runner  # noqa: F811
):
    """Approval gates are enforced chassis-side between contract runs:
    phase 1 parks, resume threads phase 1's output into phase 2 as
    prior_output."""
    _register_container(
        tmp_path,
        echo_url,
        phases=[{"name": "echo"}, {"name": "confirm", "approval": True}],
    )
    run = _FakeRun(
        run_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_inputs={"message": "gate me"},
    )
    run.agent_id = "echo-test-v1"
    session = _FakeSession(run)
    patch_runner(session)

    await agent_runner.start_run(run.id, run.tenant_id, "echo-test-v1")

    assert run.status == "awaiting_approval"
    assert run.current_phase == "echo"
    assert session.snapshot.analysis["phase"] == "echo"
    assert session.snapshot.structured_data is None

    await agent_runner.resume_run(run.id, run.tenant_id, "echo-test-v1")

    assert run.status == "complete"
    final = session.snapshot.structured_data
    assert final["phase"] == "confirm"
    # The echo agent reports the keys of the prior_output it received —
    # proof the chassis threaded phase 1's output into phase 2.
    assert final["prior_output_keys"] == sorted(
        session.snapshot.analysis.keys()
    )


@pytest.mark.asyncio
async def test_unreachable_container_marks_run_errored(
    tmp_path, patch_runner  # noqa: F811
):
    _register_container(
        tmp_path, "http://127.0.0.1:9", phases=[{"name": "echo"}]
    )
    run = _FakeRun(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    run.agent_id = "echo-test-v1"
    session = _FakeSession(run)
    patch_runner(session)

    await agent_runner.start_run(run.id, run.tenant_id, "echo-test-v1")

    assert run.status == "error"


# --------------------------- agent-supplied field hygiene --------------------


@pytest.mark.asyncio
async def test_progress_detail_from_the_container_is_coerced(tmp_path):
    """Every progress field is agent-supplied. A non-string ``detail``
    persisted verbatim would permanently 500 the run's progress endpoint
    on read (its response schema types detail as str|None) — the same
    class of defect as an unmapped status."""
    from app.schemas.run import StepProgress as ProgressSchema

    d = _write_container_dir(tmp_path)
    agent = ContainerAgent(load_manifest(d), "http://unused:1", d)
    collected = []

    async def _on_progress(p):
        collected.append(p)

    await agent._handle_event(
        "progress",
        {"step_id": "s", "status": "running", "detail": {"nested": "object"}},
        _on_progress,
    )
    await agent._handle_event(
        "progress",
        {"step_id": "s", "status": "running", "detail": "x" * 5000},
        _on_progress,
    )

    for p in collected:
        assert isinstance(p.detail, str)
        assert len(p.detail) <= 500
        # The value must survive the schema the progress endpoint uses.
        ProgressSchema(step_id=p.step_id, status=p.status, detail=p.detail)
    assert "nested" in collected[0].detail


class _NoBlankLineHandler(BaseHTTPRequestHandler):
    """Terminal SSE frame written without a trailing blank line, then the
    socket closes — legal SSE, and it used to lose the whole run."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/healthz":
            body = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(
            b'event: completed\ndata: {"output": {"terminal": "no-blank-line"}}\n'
        )
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = b'{"run_id": "r1"}'
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.asyncio
async def test_terminal_frame_without_trailing_blank_line_is_honoured(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NoBlankLineHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        d = _write_container_dir(tmp_path)
        agent = ContainerAgent(load_manifest(d), url, d)
        result = await agent.run_phase("echo", _agent_input(), _ProgressCollector())
        assert result.structured == {"terminal": "no-blank-line"}
    finally:
        server.shutdown()


class _NeverTerminatesHandler(BaseHTTPRequestHandler):
    """A container that keeps emitting progress and never sends a terminal
    event — the per-read timeout never trips, so only a wall-clock
    deadline ends it."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/healthz":
            body = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            while True:
                self.wfile.write(
                    b'event: progress\ndata: {"step_id": "s", "status": "running"}\n\n'
                )
                self.wfile.flush()
                time.sleep(0.05)
        except Exception:  # client went away when the deadline fired
            pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = b'{"run_id": "forever"}'
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.asyncio
async def test_non_terminating_container_hits_the_phase_deadline(
    tmp_path, patch_runner, caplog  # noqa: F811
):
    """Without a deadline this run never ends: the runner holds a DB
    session across the phase, so a chatty container would pin a
    connection and a background task forever. The manifest phase's
    ``deadline_seconds`` (blueprint S4) bounds it — enforced by the
    runner, named in the error, and revoking the token on the way out."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NeverTerminatesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    revoked: list[str] = []
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        agent = _register_container(
            tmp_path, url, phases=[{"name": "echo", "deadline_seconds": 1}]
        )

        async def _register(token, inp, trace_headers=None, deadline=None):
            assert deadline == 1

        async def _end(token, inp, trace_headers=None, deadline=None):
            revoked.append(token)

        agent._register_run_token = _register
        agent._end_run_token = _end
        run = _FakeRun(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
        run.agent_id = "echo-test-v1"
        patch_runner(_FakeSession(run))

        with caplog.at_level(logging.ERROR):
            await agent_runner.start_run(run.id, run.tenant_id, "echo-test-v1")

        assert run.status == "error"
        assert "exceeded its deadline of 1s" in caplog.text
        assert len(revoked) == 1
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_post_body_carries_the_deadline_and_the_token_ttl_follows_it(
    tmp_path, scripted_url, monkeypatch
):
    """Blueprint §8: ``deadline_seconds`` in the ``POST /v1/runs`` body is
    the value the runner resolved; the run-token record carries it and
    the token expires a minute after it at most."""
    d = _write_container_dir(tmp_path)
    agent = ContainerAgent(load_manifest(d), scripted_url, d)
    records: list[tuple[dict, int]] = []

    async def _register(token, inp, trace_headers=None, deadline=None):
        records.append((agent._run_token_record(inp, trace_headers or {}, deadline), deadline))

    monkeypatch.setattr(agent, "_register_run_token", _register)

    await agent.run_phase(
        "echo",
        _agent_input(user_inputs={"mode": "lazy"}, deadline_seconds=300),
        _ProgressCollector(),
    )
    assert _ScriptedHandler.last_body["deadline_seconds"] == 300
    ((record, deadline),) = records
    assert deadline == 300 and record["deadline_seconds"] == 300
    assert agent._token_ttl_seconds(300) == 360

    # A direct caller that resolved no deadline gets the platform ceiling.
    from app.config import settings

    monkeypatch.setattr(settings, "LIBRERUN_MAX_PHASE_SECONDS", 900)
    records.clear()
    await agent.run_phase(
        "echo", _agent_input(user_inputs={"mode": "lazy"}), _ProgressCollector()
    )
    assert _ScriptedHandler.last_body["deadline_seconds"] == 900
    assert records[0][1] == 900


@pytest.mark.asyncio
async def test_run_contract_speaks_run_id_and_invocation_id_with_one_release_aliases(
    tmp_path, scripted_url
):
    """Blueprint S1 (L18): the chassis sends ``run.id`` with ``run.case_id``
    as the deprecated duplicate, and accepts either ``invocation_id`` or
    the pre-S1 ``run_id`` in the start response. Both aliases go at v1.1."""
    d = _write_container_dir(tmp_path)
    agent = ContainerAgent(load_manifest(d), scripted_url, d)
    inp = _agent_input(user_inputs={"mode": "invocation-id-only"})

    result = await agent.run_phase("echo", inp, _ProgressCollector())
    assert result.structured == {"came_from": "output-endpoint"}
    sent = _ScriptedHandler.last_run_object
    assert sent["id"] == sent["case_id"] == str(inp.run_id)

    # The pre-S1 response spelling is still accepted (every other scripted
    # mode answers {"run_id": ...}).
    result = await agent.run_phase(
        "echo", _agent_input(user_inputs={"mode": "lazy"}), _ProgressCollector()
    )
    assert result.structured == {"came_from": "output-endpoint"}


# --------------------------------------------------------------------------
# S4: the phase span's W3C context travels on every request of an invocation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invocation_requests_carry_the_phase_spans_traceparent_and_tracestate(
    tmp_path, scripted_url, monkeypatch
):
    """Blueprint §8: ``POST /v1/runs`` (and the requests that follow it)
    carry ``traceparent`` / ``tracestate`` from the chassis phase span —
    here a span started under a persisted root that carries vendor
    state, exactly as the runner starts it — and the run-token record
    names the trace the token authorizes."""
    from opentelemetry.sdk.trace import TracerProvider

    from app.observability import run_trace

    provider = TracerProvider(sampler=run_trace.RunRootSampler())
    tracer = provider.get_tracer("test")
    row = type("Row", (), {})()
    row.id = uuid.uuid4()
    row.tenant_id = uuid.uuid4()
    row.run_number = "RUN-1"
    row.trace_id = row.root_traceparent = row.root_tracestate = None
    upstream = run_trace.upstream_context(
        {
            "traceparent": "00-4bf92f3577b34da6a2ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": "vendor=abc",
        }
    )
    monkeypatch.setattr(run_trace, "_tracer", tracer)
    with run_trace.root_span(
        upstream=upstream, run_id=row.id, run_number="RUN-1", agent_id="echo-test-v1", tenant_id=row.tenant_id
    ) as root:
        run_trace.persist_root(row, root)
    root_ctx = run_trace.restore_root(row)

    d = _write_container_dir(tmp_path)
    agent = ContainerAgent(load_manifest(d), scripted_url, d)
    records: list[dict] = []

    async def _register(token, inp, trace_headers=None, deadline=None):
        records.append(agent._run_token_record(inp, trace_headers or {}, deadline))

    monkeypatch.setattr(agent, "_register_run_token", _register)

    with tracer.start_as_current_span("echo", context=root_ctx) as phase:
        await agent.run_phase(
            "echo",
            _agent_input(user_inputs={"mode": "lazy"}, run_id=row.id, tenant_id=row.tenant_id),
            _ProgressCollector(),
        )
        phase_sc = phase.get_span_context()

    sent = _ScriptedHandler.last_headers
    assert sent["traceparent"] == (
        f"00-4bf92f3577b34da6a2ce929d0e0e4736-{format(phase_sc.span_id, '016x')}-01"
    )
    assert sent["tracestate"] == "vendor=abc"
    assert sent["authorization"].startswith("Bearer ")
    (record,) = records
    assert record["trace_id"] == "4bf92f3577b34da6a2ce929d0e0e4736"
    assert record["traceparent"] == sent["traceparent"]
    assert record["run_id"] == str(row.id) and record["agent_id"] == "echo-test-v1"
    provider.shutdown()


@pytest.mark.asyncio
async def test_requests_outside_any_span_carry_no_trace_headers(
    tmp_path, scripted_url, monkeypatch
):
    """Tracing off (no span active): the headers are simply absent —
    an agent must never require them."""
    d = _write_container_dir(tmp_path)
    agent = ContainerAgent(load_manifest(d), scripted_url, d)

    async def _register(token, inp, trace_headers=None, deadline=None):
        assert trace_headers == {}

    monkeypatch.setattr(agent, "_register_run_token", _register)
    _ScriptedHandler.last_headers = {}
    await agent.run_phase(
        "echo", _agent_input(user_inputs={"mode": "lazy"}), _ProgressCollector()
    )
    assert "traceparent" not in _ScriptedHandler.last_headers
    assert "tracestate" not in _ScriptedHandler.last_headers


@pytest.mark.asyncio
async def test_a_tracestate_carrying_the_fixture_never_reaches_the_container(
    tmp_path, scripted_url, monkeypatch
):
    """A dropped vendor state has to stay dropped all the way out.

    ``tracestate`` is caller-controlled, so a submission carrying the
    fixture in it is refused the vendor state and the run proceeds with
    ``root_tracestate`` null. That much is asserted at the router. What
    nobody was asserting is the other end: the container's own headers,
    which are built from the phase span's context and would carry the
    address straight out of the box if the drop were only cosmetic.
    """
    from opentelemetry.sdk.trace import TracerProvider

    from app.observability import run_trace

    fixture = "pii.fixture@example.com"
    provider = TracerProvider(sampler=run_trace.RunRootSampler())
    tracer = provider.get_tracer("test")
    row = type("Row", (), {})()
    row.id = uuid.uuid4()
    row.tenant_id = uuid.uuid4()
    row.run_number = "RUN-2"
    row.trace_id = row.root_traceparent = row.root_tracestate = None

    upstream = run_trace.upstream_context(
        {
            "traceparent": "00-4bf92f3577b34da6a2ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": f"vendor={fixture}",
        }
    )
    monkeypatch.setattr(run_trace, "_tracer", tracer)
    with run_trace.root_span(
        upstream=upstream, run_id=row.id, run_number="RUN-2", agent_id="echo-test-v1",
        tenant_id=row.tenant_id,
    ) as root:
        run_trace.persist_root(row, root)

    # The state was dropped before the row was written; the trace itself
    # continues, because losing vendor routing is not losing the run.
    # (An address is not legal in a tracestate value either, so this is
    # refused whether the grammar check or the identifier walk sees it
    # first — both ends of the header's validation, one outcome.)
    assert row.root_tracestate is None
    assert row.trace_id == "4bf92f3577b34da6a2ce929d0e0e4736"

    root_ctx = run_trace.restore_root(row)
    d = _write_container_dir(tmp_path)
    agent = ContainerAgent(load_manifest(d), scripted_url, d)
    records: list[dict] = []

    async def _register(token, inp, trace_headers=None, deadline=None):
        records.append(agent._run_token_record(inp, trace_headers or {}, deadline))

    monkeypatch.setattr(agent, "_register_run_token", _register)

    with tracer.start_as_current_span("echo", context=root_ctx):
        await agent.run_phase(
            "echo",
            _agent_input(user_inputs={"mode": "lazy"}, run_id=row.id, tenant_id=row.tenant_id),
            _ProgressCollector(),
        )

    sent = _ScriptedHandler.last_headers
    assert "tracestate" not in sent or not sent["tracestate"]
    assert fixture not in json.dumps(dict(sent))
    assert "pii.fixture" not in json.dumps(dict(sent))
    assert fixture not in json.dumps(records)
    provider.shutdown()
