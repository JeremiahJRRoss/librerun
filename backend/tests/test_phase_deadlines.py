"""Per-phase deadlines (blueprint S4, §8 and §10).

``phases[].deadline_seconds`` is one invocation's wall-clock budget under
the platform ceiling ``LIBRERUN_MAX_PHASE_SECONDS``: the runner resolves
it (unset = the ceiling, above = clamped and logged), enforces it around
``run_phase`` for every runtime — a slow handler fails the run with the
deadline named in the error — hands it to the agent in ``AgentInput``,
and the container runtime forwards it in the ``POST /v1/runs`` body and
bounds the run token with it.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

import pytest
from pydantic import ValidationError

from app.agents import registry
from app.agents.manifest import AgentManifest, PhaseSpec
from app.agents.protocol import AgentProtocol, AnalysisResult, InvestigationResult
from app.services import agent_runner

from tests.test_agent_runner import (  # noqa: E402
    _FakeRun,
    _FakeSession,
    _manifest,
    patch_runner,  # noqa: F401  (fixture)
)


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


# ------------------------------------------------------------- manifest --


def test_manifest_accepts_a_positive_deadline_and_defaults_to_none():
    m = AgentManifest.model_validate(
        {
            "id": "a-v1", "name": "a", "runtime": "python-package",
            "phases": [{"name": "one", "deadline_seconds": 5}, {"name": "two", "approval": True}],
            "output": {"mode": "structured"},
        }
    )
    assert m.phases[0].deadline_seconds == 5
    assert m.phases[1].deadline_seconds is None


@pytest.mark.parametrize("value", [0, -1, "5", 1.5, True])
def test_manifest_rejects_a_deadline_that_is_not_a_positive_integer(value):
    with pytest.raises(ValidationError):
        PhaseSpec.model_validate({"name": "one", "deadline_seconds": value})


# ----------------------------------------------------------- resolution --


def test_unset_deadline_is_the_ceiling(monkeypatch):
    monkeypatch.setattr(agent_runner._settings, "LIBRERUN_MAX_PHASE_SECONDS", 1234)
    assert agent_runner.phase_deadline(PhaseSpec(name="p")) == 1234


def test_declared_deadline_under_the_ceiling_is_itself(monkeypatch):
    monkeypatch.setattr(agent_runner._settings, "LIBRERUN_MAX_PHASE_SECONDS", 3600)
    assert agent_runner.phase_deadline(PhaseSpec(name="p", deadline_seconds=5)) == 5


def test_declared_deadline_above_the_ceiling_is_clamped_and_logged(monkeypatch, caplog):
    monkeypatch.setattr(agent_runner._settings, "LIBRERUN_MAX_PHASE_SECONDS", 100)
    with caplog.at_level(logging.WARNING):
        assert agent_runner.phase_deadline(PhaseSpec(name="p", deadline_seconds=500)) == 100
    assert "phase_deadline_clamped" in caplog.text


def test_a_caller_may_supply_its_own_ceiling_ABOVE_the_processs(monkeypatch):
    """The parameter exists to RAISE, which is the direction a second
    clamp cannot reach.

    The conformance battery is the operator for the invocations it drives
    and its ``--timeout`` is that operator's ceiling; before it could say
    so it composed this function with a ``min`` of its own, which read
    whatever ``LIBRERUN_MAX_PHASE_SECONDS`` the importing process
    happened to have. So a manifest declaring 5000s under ``--timeout
    5000`` advertised 3600 and an agent whose phases legitimately run
    longer than this process's ceiling was untestable.
    """
    monkeypatch.setattr(agent_runner._settings, "LIBRERUN_MAX_PHASE_SECONDS", 3600)
    spec = PhaseSpec(name="p", deadline_seconds=5000)
    assert agent_runner.phase_deadline(spec) == 3600
    assert agent_runner.phase_deadline(spec, ceiling=5000) == 5000


def test_a_callers_own_ceiling_still_clamps_and_logs(monkeypatch, caplog):
    """Supplying a ceiling replaces the operator, it does not remove one.

    The failing direction matters as much as the raising one: a caller
    that passes a ceiling and is then handed a larger declared value must
    get the ceiling back, with the same warning, or `--timeout 3` would
    silently advertise 30.
    """
    monkeypatch.setattr(agent_runner._settings, "LIBRERUN_MAX_PHASE_SECONDS", 3600)
    with caplog.at_level(logging.WARNING):
        assert agent_runner.phase_deadline(
            PhaseSpec(name="p", deadline_seconds=30), ceiling=3
        ) == 3
    assert "phase_deadline_clamped" in caplog.text


def test_a_callers_own_ceiling_covers_an_undeclared_deadline(monkeypatch):
    monkeypatch.setattr(agent_runner._settings, "LIBRERUN_MAX_PHASE_SECONDS", 3600)
    assert agent_runner.phase_deadline(PhaseSpec(name="p"), ceiling=7) == 7


def test_default_ceiling_is_an_hour():
    from app.config import Settings

    assert Settings().LIBRERUN_MAX_PHASE_SECONDS == 3600


# ---------------------------------------------------------- enforcement --


class _SlowAgent(AgentProtocol):
    agent_id = "slow-v1"
    display_name = "slow"
    description = "d"

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.received: list = []

    async def analyze(self, inp, on_progress):  # noqa: ARG002
        self.received.append(inp)
        await asyncio.sleep(self.seconds)
        return AnalysisResult(display={}, structured={"ok": True}, status="complete")


@pytest.mark.asyncio
async def test_a_slow_in_process_phase_fails_the_run_with_the_deadline_named(
    patch_runner, caplog
):
    registry.register(
        _SlowAgent(seconds=5),
        _manifest("slow-v1", [{"name": "analyze", "deadline_seconds": 1}], mode="structured"),
    )
    run = _FakeRun(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    run.agent_id = "slow-v1"
    session = _FakeSession(run)
    patch_runner(session)

    with caplog.at_level(logging.ERROR):
        await agent_runner.start_run(run.id, run.tenant_id, "slow-v1")

    assert run.status == "error"
    assert "exceeded its deadline of 1s" in caplog.text
    assert "run_failed" in caplog.text
    # Blueprint S7: the deadline is its own kind of failure on the row,
    # with the named deadline in the operator detail.
    assert run.error_code == "deadline_exceeded"
    assert run.error_detail.startswith("PhaseDeadlineExceeded: phase 'analyze' exceeded its deadline of 1s")


@pytest.mark.asyncio
async def test_a_phase_within_its_deadline_completes_and_sees_the_resolved_value(
    patch_runner, monkeypatch
):
    monkeypatch.setattr(agent_runner._settings, "LIBRERUN_MAX_PHASE_SECONDS", 7)
    agent = _SlowAgent(seconds=0)
    registry.register(
        agent, _manifest("slow-v1", [{"name": "analyze"}], mode="structured")
    )
    run = _FakeRun(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    run.agent_id = "slow-v1"
    session = _FakeSession(run)
    patch_runner(session)

    await agent_runner.start_run(run.id, run.tenant_id, "slow-v1")

    assert run.status == "complete"
    assert agent.received[0].deadline_seconds == 7


@pytest.mark.asyncio
async def test_the_deadline_is_a_phase_span_attribute(patch_runner, monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(agent_runner, "_tracer", provider.get_tracer("t"))
    registry.register(
        _SlowAgent(seconds=0),
        _manifest("slow-v1", [{"name": "analyze", "deadline_seconds": 42}], mode="structured"),
    )
    run = _FakeRun(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    run.agent_id = "slow-v1"
    patch_runner(_FakeSession(run))

    await agent_runner.start_run(run.id, run.tenant_id, "slow-v1")

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "analyze"]
    assert span.attributes["phase.deadline_seconds"] == 42
    provider.shutdown()


def test_the_container_transport_ceiling_follows_the_invocations_deadline():
    """The read timeout is the deadline, not a constant beside it.

    It was `httpx.Timeout(600.0)` while a phase's deadline is
    configurable to `LIBRERUN_MAX_PHASE_SECONDS` (3600), so an agent that
    emitted one progress event and then worked silently for longer than
    ten minutes — legal inside a deadline that allows it — died at the
    transport, and the run named a read timeout rather than the phase.
    Measured at 1/600 scale (ceiling 1s, deadline 5s, one quiet stretch
    of 2s): `httpx.ReadTimeout` with the constant, `completed` with the
    ceiling taken from the deadline (Codex round 36).

    `agent_runner.py` already states this rule for model calls; the
    container client was the one place that had not adopted it.
    """
    from app.agents.container import _run_timeout

    for deadline in (1, 30, 600, 3600):
        ceiling = _run_timeout(deadline)
        assert ceiling.read == float(deadline), (deadline, ceiling.read)
        # ...and the connect timeout stays short, because a dead URL
        # should fail fast whatever the phase is allowed to take.
        assert ceiling.connect == 10.0, (deadline, ceiling.connect)


@pytest.mark.asyncio
async def test_a_quiet_stretch_inside_the_deadline_is_not_cut_off():
    """The consequence, end to end at 1/600 scale.

    The agent sends one progress event, goes quiet for longer than the
    OLD constant would have allowed, and finishes — all inside the
    deadline it was advertised. Nothing may cut it off but the runner.
    """
    import json
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import httpx

    from app.agents import container as container_module

    quiet_seconds = 0.6
    deadline = 5

    class QuietThenFinishes(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "s", "status": "running"}\n\n')
            self.wfile.flush()
            time.sleep(quiet_seconds)
            done = {"output": {"echo": {"m": "x"}}}
            self.wfile.write(
                b"event: completed\ndata: " + json.dumps(done).encode() + b"\n\n")
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), QuietThenFinishes)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        # THE CONSTANT, shrunk to the same ratio: it cuts the phase off.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(0.3, connect=10.0)
        ) as client:
            with pytest.raises(httpx.HTTPError):
                async with client.stream("GET", f"{url}/v1/runs/inv-1/events") as r:
                    async for _chunk in r.aiter_bytes():
                        pass
        # THE DEADLINE'S OWN CEILING: it does not.
        async with httpx.AsyncClient(
            timeout=container_module._run_timeout(deadline)
        ) as client:
            body = b""
            async with client.stream("GET", f"{url}/v1/runs/inv-1/events") as r:
                async for chunk in r.aiter_bytes():
                    body += chunk
        assert b"completed" in body, body
    finally:
        server.shutdown()
