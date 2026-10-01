"""The container battery (blueprint S4): against the SDK-based echo agent
served in-process it passes — schema-valid output, progress, completed,
and the container's spans under the minted token with the trace id the
battery sent; against a scripted bad agent it fails with the check
named; and a container that exports nothing reports the span check as
skipped, never as passed (or as failed when spans were expected)."""
from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import pytest

from adapter_kit.run_contract import RelayRecorder, run_contract_battery
from app.agents.manifest import load_manifest

ECHO_DIR = Path(__file__).resolve().parents[1] / "agents" / "_examples" / "echo_container"


def _two_phase(manifest):
    """The manifest with a second phase appended.

    The binding probe picks its second invocation from the manifest: with
    a later phase it invokes that phase under the SAME run id, which is
    the case a run-scoped token pool leaks through. A single-phase manifest
    has no such transition — the chassis invokes that phase once per run —
    so the probe uses a second run instead and the same-run case cannot
    arise (Codex round 6).
    """
    from app.agents.manifest import PhaseSpec

    return manifest.model_copy(
        update={"phases": [manifest.phases[0], PhaseSpec(name="second")]}
    )


def _three_phase_ungated(manifest):
    """Three phases, NO gate — so production runs all three in ONE run.

    `agent_runner.py:589-591` parks a run only when `phases[i+1].approval`
    is set; with none set, `:640`'s `i += 1` carries the loop straight
    through to the last phase with no human in between. Every one of
    those invocations is a request the chassis really makes, so every one
    of them is the battery's to probe (Codex round 25).
    """
    from app.agents.manifest import PhaseSpec

    return manifest.model_copy(update={"phases": [
        manifest.phases[0], PhaseSpec(name="second"), PhaseSpec(name="third"),
    ]})


def _gated_two_phase(manifest):
    """Two phases, the second GATED — so the first can also be rerun.

    `approval: true` parks the run before the second phase, and an edit
    during that park reruns the first. Both shipped multi-phase manifests
    (vita_v1, langgraph_triage) are this shape, so this is the case their
    first phase actually gets.
    """
    from app.agents.manifest import PhaseSpec

    return manifest.model_copy(
        update={"phases": [manifest.phases[0], PhaseSpec(name="second", approval=True)]}
    )


@contextlib.contextmanager
def _collector_paused():
    """No garbage-collector pass inside a timed window — `timeit`'s rule.

    A probe's ceiling is wall clock in the battery's OWN process, so any
    pause of that process is charged to the agent. Under the full suite
    that process is pytest two thousand tests in, and one full collection
    over what they leave behind stops every thread for hundreds of
    milliseconds — measured locally at 576ms in the file after this one,
    and longer on a slower runner. That is more than the 0.4s the tests
    below give a conforming agent: CI answered "output on the first
    invocation without a token never answered at all: no answer within
    the 0.4s a binding probe is given" (run 36661203696) for a fixture
    that answers in a millisecond, while the same test alone passed
    fifteen times in fifteen. The pause was the suite's, not the agent's
    or the battery's, and `timeit` turns the collector off for the same
    reason. Every test that shrinks `_PROBE_CEILING_SECONDS` times its
    battery inside this
    (`test_every_shrunk_probe_ceiling_is_timed_without_the_collector`).
    """
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


@pytest.fixture(scope="module")
def relay():
    recorder = RelayRecorder(0, host="127.0.0.1")
    yield recorder
    recorder.stop()


@pytest.fixture(scope="module")
def echo(relay):
    """The echo agent exporting to the recorder (the otel extra is present
    in the chassis environment)."""
    import importlib.util

    from librerun_agent import _otel
    from librerun_agent.testing import serve_in_thread

    _otel._reset_for_tests()
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{relay.port}"
    try:
        spec = importlib.util.spec_from_file_location("echo_for_battery", ECHO_DIR / "echo_agent.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handle = serve_in_thread(module.app)
        yield handle.url
        handle.stop()
    finally:
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        _otel._reset_for_tests()


@pytest.mark.asyncio
async def test_the_echo_agent_passes_the_container_battery(echo, relay):
    manifest = load_manifest(ECHO_DIR)
    result = await run_contract_battery(
        echo, manifest=manifest, scenario={"message": "battery"}, relay=relay, expect_spans=True
    )
    assert result.passed, result.summary()
    assert result.checks == {
        "healthz": "pass", "token_binding": "pass", "completed": "pass",
        # The headerless invocation and the capability endpoint.
        # `mcp` is `skip` because this call serves no recorder,
        # and `skip` is never rendered as `pass`.
        "traceparent_optional": "pass", "mcp": "skip",
        "progress": "pass", "output": "pass", "spans": "pass",
    }
    assert result.output["echo"] == {"message": "battery"}
    # EACH span on ITS invocation's trace, not all on one: the battery
    # sends a trace per run and a parent span per invocation, the way the
    # chassis does, so the second run's spans carry the second run's trace
    # (Codex round 22). `result.traces` is the mapping it sent.
    assert all(s["trace_id"] == result.traces[s["token"]] for s in result.spans)
    assert any(s["trace_id"] == result.trace_id for s in result.spans), result.spans
    assert any(s["name"].startswith("invocation") for s in result.spans)


class _BadAgent(BaseHTTPRequestHandler):
    """Completes with a number where an object should be, streams no
    progress, and lets anyone read the events stream."""

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
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'event: completed\ndata: {"output": {"phone": 2125551234}}\n\n')
            return
        self._json(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._json(201, {"invocation_id": "bad-1"})


@pytest.mark.asyncio
async def test_a_bad_agent_fails_with_the_checks_named():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BadAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(url, manifest=manifest, scenario={"message": "x"}, expect_spans=True)
    finally:
        server.shutdown()
    assert not result.passed
    assert result.checks["token_binding"] == "fail"
    assert result.checks["progress"] == "fail"
    assert result.checks["output"] == "fail"
    assert result.checks["spans"] == "skip"
    text = result.summary()
    assert "no progress event" in text
    assert "pii_in_output" in text and "$.phone" in text
    assert "spans expected but no relay recorder" in text
    assert "2125551234" not in text


@pytest.mark.asyncio
async def test_no_export_is_skipped_not_passed(echo, relay):
    """A recorder that receives nothing (the container exports to another
    endpoint) reports skip without --expect-spans and fail with it — and
    a second recorder in the same process never reads the first's spans,
    or a silent container would pass the check on someone else's export."""
    quiet = RelayRecorder(0, host="127.0.0.1")
    try:
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(
            echo, manifest=manifest, scenario={"message": "x"}, relay=quiet, settle_seconds=0.3
        )
        assert quiet.spans == [] and relay.spans
        assert result.checks["spans"] == "skip" and result.passed
        result = await run_contract_battery(
            echo, manifest=manifest, scenario={"message": "x"}, relay=quiet, expect_spans=True, settle_seconds=0.3
        )
        assert result.checks["spans"] == "fail" and not result.passed
    finally:
        quiet.stop()


class _LaxAgent(BaseHTTPRequestHandler):
    """Correct in every respect except the one that matters.

    It refuses a request with NO bearer, which is all the battery used to
    probe, and accepts ANY bearer on the per-invocation leaves — so its
    events and output are readable by anyone who invents a token. That is
    not a hypothetical shape: "reject when the header is absent" is the
    natural thing to write, and it passes a check that only ever omits
    the header.
    """

    OUTPUT = {"echo": {"message": "x"}}

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _has_any_bearer(self):
        # The defect, stated in one line: presence, not match.
        return (self.headers.get("Authorization") or "").startswith("Bearer ")

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        if not self._has_any_bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": self.OUTPUT})
        self._json(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._json(201, {"invocation_id": "lax-1"})


@pytest.mark.asyncio
async def test_an_agent_that_takes_any_token_fails_the_binding_check():
    """The contract names four obligations; the battery probed one.

    `docs/authoring/Run_Contract_v1.md`: the agent MUST reject (401) any
    events/output request carrying a DIFFERENT **or** missing token —
    {events, output} x {foreign, missing}. The battery tested
    events x missing, which only proves the agent reads the header at
    all. Binding is the foreign cases, and both were unexamined.

    Measured before the widening, against this same fixture:

        checks   {'healthz': 'pass', 'token_binding': 'pass',
                  'completed': 'pass', 'progress': 'pass',
                  'output': 'pass', 'spans': 'skip'}
        failures []
        passed   True

    An agent whose leaves are readable by anyone who invents a token
    passed clean. This is not a latent worry: S5 adds two example agents
    that are NOT served by the Python SDK, so they inherit no binding
    check of their own, and S6's templates derive from them.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LaxAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(url, manifest=manifest, scenario={"message": "x"})
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "fail", result.summary()
    assert not result.passed

    # The failure must name WHICH of the four, or a report that says
    # "token_binding: fail" leaves the author guessing at three cases
    # that are fine.
    text = result.summary()
    assert ("events on the first invocation with a foreign token answered 200, "
            "not 401") in text, text
    assert ("output on the first invocation with a foreign token answered 200, "
            "not 401") in text, text

    # And it must NOT accuse the two cases this agent gets right —
    # asserting the silence as explicitly as the catch (§12 158(d)).
    assert "without a token" not in text, text

    # Nothing else may fail, or a green run of this test could be
    # explained by some other defect in the fixture rather than by the
    # binding (§12 155(b)).
    assert {k: v for k, v in result.checks.items() if k != "token_binding"} == {
        "healthz": "pass", "completed": "pass", "progress": "pass",
        "output": "pass", "spans": "skip",
        # This agent is lax about tokens, not about trace context or
        # capabilities, so both new checks must be clean here too —
        # which is what keeps "nothing else may fail" exhaustive.
        "traceparent_optional": "pass", "mcp": "skip",
    }, result.checks


class _AllowlistAgent(BaseHTTPRequestHandler):
    """Rejects tokens it never issued — and accepts any that it did.

    The shape that separates BINDING from an allowlist, and the reason an
    invented token is not a sufficient probe (Codex, PR #56). This agent
    refuses a random bearer, so it answers 401 to every "missing" and
    "foreign" case the battery used to ask about, and passes them all.
    One run's token still reads another run's output.
    """

    ISSUED: set[str] = set()
    OUTPUT = {"echo": {"message": "x"}}

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        token = self._bearer()
        # The defect, in one line: membership, not binding.
        if token is None or token not in self.ISSUED:
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": self.OUTPUT})
        self._json(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        token = self._bearer()
        if token:
            self.ISSUED.add(token)
        self._json(201, {"invocation_id": f"inv-{len(self.ISSUED)}"})


@pytest.mark.asyncio
async def test_an_allowlist_is_not_a_binding():
    """An invented token proves nothing about per-invocation scope.

    `docs/authoring/Run_Contract_v1.md`: the agent MUST bind the token seen on
    `POST /v1/runs` **to the returned invocation_id**. An agent that
    collects every token it has issued and accepts any of them anywhere
    satisfies "reject what I never issued" while violating that, and the
    battery's four probes could not tell the difference — the foreign
    token they send is random, so it is refused, and all four pass.

    Measured against this fixture before the cross-invocation probe
    existed::

        checks  : {'healthz': 'pass', 'token_binding': 'pass', ...}
        failures: []
        passed  : True

    and then invocation B's own valid token fetched invocation A's
    output with **200** and the body. The probe has to be a token the
    agent itself accepted, for a different invocation.
    """
    _AllowlistAgent.ISSUED.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AllowlistAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(url, manifest=manifest, scenario={"message": "x"})
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "fail", result.summary()
    text = result.summary()
    assert ("events with another invocation's token on the first invocation "
            "answered 200, not 401") in text, text
    assert ("output with another invocation's token on the first invocation "
            "answered 200, not 401") in text, text

    # And the four cases this agent gets RIGHT must not be accused. It
    # refuses a missing bearer and an invented one; saying otherwise
    # would send an author hunting for a defect that is not there.
    assert "without a token" not in text, text
    assert "with a foreign token" not in text, text

    # Nothing else may fail, or a green run of this test could be
    # explained by some other defect in the fixture.
    assert {k: v for k, v in result.checks.items() if k != "token_binding"} == {
        "healthz": "pass", "completed": "pass", "progress": "pass",
        "output": "pass", "spans": "skip",
        # This agent is lax about tokens, not about trace context or
        # capabilities, so both new checks must be clean here too —
        # which is what keeps "nothing else may fail" exhaustive.
        "traceparent_optional": "pass", "mcp": "skip",
    }, result.checks


class _SnapshotAgent(BaseHTTPRequestHandler):
    """Per-invocation allowlist, snapshotted at start. The leak is one-way.

    Each invocation is handed the set of tokens issued SO FAR, itself
    included: A sees ``{A}``, B sees ``{A, B}``. So B's token on A is
    refused — the direction the battery probed — while A's token on B is
    accepted. A cross-invocation probe that runs in one direction only
    certified exactly this agent clean (Codex round 2): ``token_binding:
    pass, failures: [], passed: True``, and then A's own token fetched
    B's output with ``200`` and the body.
    """

    ISSUED: list[str] = []
    ALLOWED: dict[str, set] = {}

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        token = self._bearer()
        if token is None or token not in self.ALLOWED.get(invocation, set()):
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        token = self._bearer()
        if token:
            self.ISSUED.append(token)
        invocation = f"inv-{len(self.ISSUED)}"
        self.ALLOWED[invocation] = set(self.ISSUED)  # the snapshot
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_one_way_cross_probe_misses_a_snapshot_allowlist():
    """Cross-use is DIRECTIONAL, so the battery has to ask both ways.

    The battery mints a second invocation and must probe B's token
    against A **and** A's token against B — which means reading B's
    ``invocation_id`` back, not just its status code.
    """
    _SnapshotAgent.ISSUED.clear()
    _SnapshotAgent.ALLOWED.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SnapshotAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "fail", result.summary()
    text = result.summary()
    # The case names the TRANSITION, so two transitions cannot overwrite
    # each other's result (Codex round 10).
    assert ("events with the first invocation's token on another invocation "
            "answered 200, not 401") in text, text
    assert ("output with the first invocation's token on another invocation "
            "answered 200, not 401") in text, text
    # And the silence, as explicitly as the catch: this agent DOES refuse
    # B's token on A, so naming that direction would be an accusation it
    # has not earned.
    assert "another invocation's token" not in text, text


class _NeverSecondAgent(BaseHTTPRequestHandler):
    """Serves exactly one invocation, ever, and refuses every later POST.

    The battery then has no token the agent issued to cross-use, so
    per-invocation binding is UNPROVEN. It used to report that as
    ``token_binding: pass`` with a note — and ``passed`` reads only
    ``failures``, so automation exited 0 on a check that never ran
    (Codex round 2).
    """

    TOKEN: list[str] = []

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        if not self.TOKEN or self._bearer() != self.TOKEN[0]:
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.TOKEN:
            return self._json(409, {"error": "one invocation per process"})
        self.TOKEN.append(self._bearer())
        self._json(201, {"invocation_id": "inv-1"})


@pytest.mark.asyncio
async def test_an_unprobed_binding_is_not_a_passed_one():
    """A probe that did not run is not a probe that succeeded.

    This agent's four single-invocation answers are all correct, so
    nothing here is an accusation of leaking — the point is that the
    battery could not REACH the case that separates binding from an
    allowlist, and says so instead of certifying.
    """
    _NeverSecondAgent.TOKEN.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NeverSecondAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "fail", result.summary()
    assert not result.passed, result.summary()
    text = result.summary()
    assert "per-invocation binding is UNPROVEN" in text, text
    # It must not ALSO accuse the agent of leaking: every case it could
    # reach, it answered 401.
    assert "not 401" not in text, text


class _HangingEventsAgent(BaseHTTPRequestHandler):
    """Accepts any bearer on ``/events`` and holds the stream open.

    The agent a binding probe exists to catch is the one that accepts a
    foreign bearer — and what it hands back is an SSE stream that stays
    open for the life of the invocation. ``AsyncClient.get()`` reads the
    body before returning, so the probe waited out the invocation it had
    already caught: measured at 4x the hold to reach a verdict, and an
    uncaught ``ReadTimeout`` out of the battery when the client timeout
    was shorter than the invocation (Codex round 2).
    """

    RELEASE = threading.Event()
    BOUND: list[str] = []

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        if not self.path.endswith("/events"):
            return self._json(404, {})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if self._bearer() in self.BOUND:
            # The battery's own stream: a normal, prompt invocation, so
            # the elapsed time below measures the PROBES and nothing else.
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        # A foreign bearer, accepted — and then held, the way a long
        # invocation's stream is held.
        self.wfile.write(b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n')
        self.wfile.flush()
        self.RELEASE.wait(60)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        token = self._bearer()
        if token:
            self.BOUND.append(token)
        self._json(201, {"invocation_id": f"inv-{len(self.BOUND)}"})


@pytest.mark.asyncio
async def test_a_held_events_stream_does_not_stall_the_binding_probe():
    """The probe reads the status line and closes; it never drains a body.

    Every ``/events`` probe here is answered ``200`` and then held for a
    minute. A probe that consumes the body waits out all of them — or
    raises ``ReadTimeout`` out of the battery, which is not a recorded
    failure at all. So this asserts the verdict AND that it arrived
    promptly: the whole point is that the status line was enough.
    """
    _HangingEventsAgent.RELEASE.clear()
    _HangingEventsAgent.BOUND.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HangingEventsAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        started = time.monotonic()
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"},
            settle_seconds=0.1, timeout=45.0,
        )
        elapsed = time.monotonic() - started
    finally:
        _HangingEventsAgent.RELEASE.set()
        server.shutdown()

    assert result.checks["token_binding"] == "fail", result.summary()
    text = result.summary()
    assert ("events on the first invocation with a foreign token answered 200, "
            "not 401") in text, text
    assert ("events on the first invocation without a token answered 200, "
            "not 401") in text, text
    # Three held streams at 60s each, against a 45s client timeout: a
    # body-consuming probe cannot reach here in single digits, and would
    # raise rather than return. The bound token's own stream is prompt,
    # so this measures the probes.
    assert elapsed < 10, f"the binding probes took {elapsed:.1f}s — a body was consumed"


class _NeverFinishingAgent(BaseHTTPRequestHandler):
    """Binds correctly, but its events stream never reaches a terminal.

    The battery gives up on the stream and returns early — BEFORE the
    cross-invocation probe, which only runs once the first invocation has
    finished. What it must not do is carry away the verdict the first
    four probes suggested: those four cannot tell a binding from an
    allowlist, which is the entire finding this check exists for.
    """

    RELEASE = threading.Event()
    BOUND: list[str] = []

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        if self._bearer() not in self.BOUND:
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        if not self.path.endswith("/events"):
            return self._json(404, {})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n')
        self.wfile.flush()
        self.RELEASE.wait(60)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        token = self._bearer()
        if token:
            self.BOUND.append(token)
        self._json(201, {"invocation_id": f"inv-{len(self.BOUND)}"})


@pytest.mark.asyncio
async def test_a_battery_that_bails_out_early_reports_no_binding_verdict():
    """An open check is not a passed one, on every path out of the run."""
    _NeverFinishingAgent.RELEASE.clear()
    _NeverFinishingAgent.BOUND.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NeverFinishingAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"},
            settle_seconds=0.1, timeout=3.0,
        )
    finally:
        _NeverFinishingAgent.RELEASE.set()
        server.shutdown()

    assert result.checks.get("token_binding") == "incomplete", result.summary()
    assert not result.passed, result.summary()
    text = result.summary()
    assert "the cross-invocation probe never ran" in text, text
    # The early return is the reason, and it is reported as its own
    # failure rather than being folded into the binding one. Asserted as
    # TWO distinct failures rather than by matching one message: the
    # invariant is that the bail-out is reported separately, and pinning
    # the old wording made the test go red when the reason got MORE
    # accurate. This agent never finishes, so what it does is outrun the
    # budget it was advertised; before the wall-clock bound existed that
    # surfaced as whichever transport symptom happened to fire first
    # (`events stream failed: ReadTimeout`), which named the battery's
    # HTTP client rather than the agent's defect.
    assert len(result.failures) == 2, result.failures
    binding = [f for f in result.failures if "cross-invocation probe" in f]
    bailout = [f for f in result.failures if f not in binding]
    assert len(binding) == 1 and len(bailout) == 1, result.failures
    assert "outran the 3s deadline" in bailout[0], bailout
    assert result.checks["completed"] == "fail", result.checks


class _UnnamedSecondAgent(BaseHTTPRequestHandler):
    """Starts a second invocation and never says which one it is.

    The other way to reach UNPROVEN, and an author cannot act on the
    wrong diagnosis: this agent did not refuse anything, so telling them
    it "would not start a second invocation" would send them looking for
    a refusal that is not there.
    """

    ISSUED: list[str] = []

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        if not self.ISSUED or self._bearer() != self.ISSUED[0]:
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        token = self._bearer()
        if self.ISSUED:
            return self._json(201, {"started": True})  # accepted, unnamed
        if token:
            self.ISSUED.append(token)
        self._json(201, {"invocation_id": "inv-1"})


@pytest.mark.asyncio
async def test_an_unnamed_second_invocation_is_diagnosed_as_itself():
    _UnnamedSecondAgent.ISSUED.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UnnamedSecondAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "fail", result.summary()
    text = result.summary()
    assert "per-invocation binding is UNPROVEN" in text, text
    assert "named no invocation_id" in text, text
    # NOT the other diagnosis: nothing was refused here.
    assert "would not start a second invocation" not in text, text


class _RunScopedAgent(BaseHTTPRequestHandler):
    """Pools accepted tokens by ``run.id`` instead of by invocation.

    Invocation-scoped is what the contract requires. This agent is
    run-scoped, so two invocations OF THE SAME RUN share a token pool —
    and the same run id on two invocations is exactly what the chassis
    sends for a two-phase run or a rerun.

    It is invisible to a battery that starts its second invocation under
    a DIFFERENT run: two run ids give two disjoint pools, both cross-uses
    are refused, and the agent passes (Codex round 3). Measured that way:
    ``token_binding: pass, failures: [], passed: True``, and then a
    same-run second invocation's token read the first's output with
    ``200``.
    """

    BY_RUN: dict[str, set] = {}
    INVOCATION_RUN: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        run = self.INVOCATION_RUN.get(invocation)
        token = self._bearer()
        # The defect, in one line: the pool is the RUN's, not this
        # invocation's.
        if run is None or token is None or token not in self.BY_RUN.get(run, set()):
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            run_id = (json.loads(raw or b"{}").get("run") or {}).get("id") or "?"
        except ValueError:
            run_id = "?"
        token = self._bearer()
        self.BY_RUN.setdefault(run_id, set())
        if token:
            self.BY_RUN[run_id].add(token)
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.INVOCATION_RUN[invocation] = run_id
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_run_scoped_token_pool_is_not_a_binding():
    """The second invocation has to be one the chassis would really make.

    Both invocations under one ``run.id``, as a rerun — otherwise the
    probe manufactures an isolation the platform never provides and the
    agent passes on the strength of the battery's own choice of ids.
    """
    _RunScopedAgent.BY_RUN.clear()
    _RunScopedAgent.INVOCATION_RUN.clear()
    _RunScopedAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RunScopedAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = _two_phase(load_manifest(ECHO_DIR))
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "fail", result.summary()
    text = result.summary()
    for case in (
        "events with the second phase's token on the first invocation",
        "output with the second phase's token on the first invocation",
        "events with the first invocation's token on the second phase",
        "output with the first invocation's token on the second phase",
    ):
        assert f"{case} answered 200, not 401" in text, text
    # The consequence, not a proxy: the run under test issued TWO tokens
    # into ONE pool, which is what the cross-use above rides on. Round 32
    # added a same-phase second run, so `battery-run-2` now has a pool of
    # its own — a different pool, and not the one the leak is in.
    assert list(_RunScopedAgent.BY_RUN) == [
        "battery-run", "battery-run-2", "battery-run-headerless"], (
        _RunScopedAgent.BY_RUN)  # +1 run: the headerless traceparent probe
    assert len(_RunScopedAgent.BY_RUN["battery-run"]) == 2, _RunScopedAgent.BY_RUN


class _DisconnectingAgent(BaseHTTPRequestHandler):
    """Serves one invocation, then drops the connection on the next POST.

    The battery's own first ``POST`` has always turned a transport
    failure into a result. Until round 3 the second one did not, so this
    agent aborted ``run_contract_battery`` with a ``RemoteProtocolError``
    and the CLI printed a traceback instead of the promised UNPROVEN
    verdict — an unexecuted cross-probe escaping reporting altogether, on
    exactly the path where the agent is least well behaved.
    """

    TOKEN: list[str] = []

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        if not self.TOKEN or self._bearer() != self.TOKEN[0]:
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.TOKEN:
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return
        self.TOKEN.append(self._bearer())
        self._json(201, {"invocation_id": "inv-1"})


@pytest.mark.asyncio
async def test_a_transport_failure_on_the_second_post_is_reported_not_raised():
    """A battery that raises has not reported anything at all."""
    _DisconnectingAgent.TOKEN.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DisconnectingAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "fail", result.summary()
    assert not result.passed, result.summary()
    text = result.summary()
    assert "per-invocation binding is UNPROVEN" in text, text
    assert "failed at the transport" in text, text


class _ReplacingAgent(BaseHTTPRequestHandler):
    """Treats the rerun as REPLACING the first invocation: same id back.

    Then there is no second invocation to cross-use, and a ``200`` from
    the one invocation that exists would be correct rather than a leak.
    The battery must say UNPROVEN and say why — not accuse it.
    """

    TOKENS: list[str] = []

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        # Bound to the CURRENT token only — a correct binding for an agent
        # that only ever has one invocation alive.
        if not self.TOKENS or self._bearer() != self.TOKENS[-1]:
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        token = self._bearer()
        if token:
            self.TOKENS.append(token)
        self._json(201, {"invocation_id": "the-only-one"})


@pytest.mark.asyncio
async def test_a_replaced_invocation_is_unproven_not_accused():
    _ReplacingAgent.TOKENS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ReplacingAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "fail", result.summary()
    text = result.summary()
    assert "per-invocation binding is UNPROVEN" in text, text
    assert "SAME invocation_id" in text, text
    # Never accused of a leak it did not commit: the battery reached no
    # cross-use case at all here.
    assert "not 401" not in text, text


class _SilentOnUnauthorizedAgent(BaseHTTPRequestHandler):
    """Drops the connection instead of answering an unauthorized probe.

    The contract says the agent MUST reject with ``401``; hanging up is
    not that, so it is a failure. What it must not be is a traceback:
    every ``_probe_status`` call used to let an ``httpx.HTTPError``
    escape, so the battery produced no result at all — the same defect
    the second ``POST`` carried a round earlier (Codex round 4).
    """

    ISSUED: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.ISSUED.get(invocation) != self._bearer():
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.ISSUED[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_probe_that_never_answers_is_reported_not_raised():
    _SilentOnUnauthorizedAgent.ISSUED.clear()
    _SilentOnUnauthorizedAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SilentOnUnauthorizedAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        # The assertion that matters most is that this returns at all.
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "fail", result.summary()
    text = result.summary()
    assert "never answered at all" in text, text
    # Named, not swallowed: an author cannot act on "something went wrong".
    assert "RemoteProtocolError" in text, text


class _LazyStartAgent(BaseHTTPRequestHandler):
    """Starts work only when the events stream is opened, and records
    which invocations were drained.

    The contract permits that, so an undrained second invocation either
    never runs or runs on past the battery's exit — and a later
    single-flight check collides with work this run abandoned.
    """

    ISSUED: dict[str, str] = {}
    DRAINED: list[str] = []
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.ISSUED.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.DRAINED.append(invocation)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.ISSUED[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_the_binding_invocation_is_drained_before_returning():
    """The consequence, not a proxy: every invocation the battery starts
    is one it also finishes."""
    _LazyStartAgent.ISSUED.clear()
    _LazyStartAgent.DRAINED.clear()
    _LazyStartAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LazyStartAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "pass", result.summary()
    started = [f"inv-{i}" for i in range(1, _LazyStartAgent.COUNT[0] + 1)]
    assert _LazyStartAgent.COUNT[0] == 3, started  # +1: the headerless traceparent probe
    assert sorted(_LazyStartAgent.DRAINED) == sorted(started), (
        f"started {started}, drained {_LazyStartAgent.DRAINED}"
    )


def _otlp_span(trace_id_hex: str, name: str) -> bytes:
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    req = trace_service_pb2.ExportTraceServiceRequest()
    span = req.resource_spans.add().scope_spans.add().spans.add()
    span.name = name
    span.trace_id = bytes.fromhex(trace_id_hex)
    span.span_id = bytes.fromhex("aabbccddeeff0011")
    return req.SerializeToString()


class _TelemetryOnlyOnTheSecondAgent(BaseHTTPRequestHandler):
    """Exports nothing for the invocation under test and something for the
    battery's own binding invocation — so the only telemetry the relay
    ever sees belongs to the probe, not to the run being certified."""

    RELAY: list[str] = [""]
    ISSUED: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.ISSUED.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        json.loads(raw or b"{}")
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        token = self._bearer()
        self.ISSUED[invocation] = token
        if self.COUNT[0] > 1:  # the battery's own binding invocation
            import urllib.request

            traceparent = self.headers.get("traceparent") or ""
            trace_id = traceparent.split("-")[1] if traceparent.count("-") >= 3 else "0" * 32
            try:
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"{self.RELAY[0]}/v1/traces",
                        data=_otlp_span(trace_id, "rerun-only-span"),
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/x-protobuf"},
                    ),
                    timeout=5,
                ).read()
            except Exception:  # noqa: BLE001 — the assertion below is the check
                pass
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_the_binding_invocation_is_not_telemetry_evidence():
    """Spans from the battery's own second invocation are not evidence
    that the invocation under test was instrumented."""
    recorder = RelayRecorder(0, host="127.0.0.1")
    _TelemetryOnlyOnTheSecondAgent.RELAY[0] = f"http://127.0.0.1:{recorder.port}"
    _TelemetryOnlyOnTheSecondAgent.ISSUED.clear()
    _TelemetryOnlyOnTheSecondAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TelemetryOnlyOnTheSecondAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"},
            relay=recorder, expect_spans=True, settle_seconds=1.0,
        )
    finally:
        server.shutdown()
        recorder.stop()

    # The rerun's span really did arrive — the fixture is not a no-op.
    assert any(s["name"] == "rerun-only-span" for s in result.spans), result.spans
    assert result.checks["spans"] == "fail", result.summary()
    assert "exported no spans" in result.summary(), result.summary()


class _RefusesAnyRerunAgent(BaseHTTPRequestHandler):
    """Refuses every rerun, which for its manifest is CONFORMANT.

    `runs.py:466` accepts an edit only while a run is
    `awaiting_approval`/`refining`, and a run parks only when a later
    phase declares `approval: true` — the flag gates the PREVIOUS phase
    and the first phase cannot be gated. A single-phase, ungated manifest
    therefore can never be reruns at all, so an agent that rejects one is
    right and the battery that sent it was wrong (Codex round 6).
    """

    ISSUED: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.ISSUED.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        payload = json.loads(raw or b"{}")
        if (payload.get("run") or {}).get("rerun"):
            return self._json(400, {"error": "this phase is not gated; it cannot be rerun"})
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.ISSUED[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_no_rerun_is_sent_to_a_phase_that_cannot_be_rerun():
    _RefusesAnyRerunAgent.ISSUED.clear()
    _RefusesAnyRerunAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RefusesAnyRerunAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)  # single phase, ungated
        assert len(manifest.phases) == 1, manifest.phases
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "pass", result.summary()
    assert "UNPROVEN" not in result.summary(), result.summary()
    # And the report says which transition it used, so an author is not
    # left guessing what was certified.
    assert "a second run" in result.summary(), result.summary()


class _RecordingPhaseAgent(BaseHTTPRequestHandler):
    """Records every POST body so the test can assert the transition."""

    BODIES: list[dict] = []
    ISSUED: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.ISSUED.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.BODIES.append(json.loads(raw or b"{}"))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.ISSUED[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_the_second_invocation_is_the_next_phase_of_the_same_run():
    """With a later phase, the probe uses the transition the chassis makes."""
    _RecordingPhaseAgent.BODIES.clear()
    _RecordingPhaseAgent.ISSUED.clear()
    _RecordingPhaseAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingPhaseAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = _two_phase(load_manifest(ECHO_DIR))
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "pass", result.summary()
    assert len(_RecordingPhaseAgent.BODIES) == 4, _RecordingPhaseAgent.BODIES  # +1: round 32's same-phase second run; +1: the headerless traceparent probe
    first, second, _second_run, _headerless = _RecordingPhaseAgent.BODIES
    assert second["phase"] == "second", second["phase"]
    # The SAME run, which is the whole point of the same-run case.
    assert second["run"]["id"] == first["run"]["id"], (first["run"], second["run"])
    # Not a rerun, and carrying the prior phase's output, as the chassis does.
    assert second["run"].get("rerun") is False, second["run"]
    assert isinstance(second["prior_output"], dict), second["prior_output"]


class _RefusesItsOwnSecondTokenAgent(BaseHTTPRequestHandler):
    """Issues a second invocation and then will not let its own bearer
    read it. Every cross-use is correctly refused, so only this can fail
    the check — and it must, because the chassis could never consume that
    invocation (Codex round 6)."""

    ISSUED: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if invocation != "inv-1":  # the second invocation: nothing works
            return self._json(401, {"error": "unauthorized"})
        if self.ISSUED.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.ISSUED[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_an_invocation_its_own_token_cannot_read_is_a_failure():
    _RefusesItsOwnSecondTokenAgent.ISSUED.clear()
    _RefusesItsOwnSecondTokenAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RefusesItsOwnSecondTokenAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "fail", result.summary()
    assert not result.passed, result.summary()
    text = result.summary()
    assert "own token was refused by its own events stream" in text, text
    # Not accused of leaking: every cross-use it was asked, it refused.
    assert "not 401" not in text, text


class _SecondInvocationOnItsOwnTraceAgent(BaseHTTPRequestHandler):
    """Instruments both invocations, but the second ignores its
    ``traceparent`` and exports onto a trace of its own.

    Evidence that the invocation under test was instrumented is the first
    token's telemetry — but trace integrity is a claim about everything
    the agent exported, and the real relay refuses a mismatched trace
    outright (`403 trace_mismatch`). Filtering the verdict down to the
    first token alone hid this (Codex round 6).
    """

    RELAY: list[str] = [""]
    ISSUED: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.ISSUED.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        import urllib.request

        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        token = self._bearer()
        self.ISSUED[invocation] = token
        traceparent = self.headers.get("traceparent") or ""
        real = traceparent.split("-")[1] if traceparent.count("-") >= 3 else "0" * 32
        second = self.COUNT[0] > 1
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{self.RELAY[0]}/v1/traces",
                    data=_otlp_span("ff" * 16 if second else real,
                                    "second-span" if second else "first-span"),
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/x-protobuf"},
                ),
                timeout=5,
            ).read()
        except Exception:  # noqa: BLE001 — the assertions below are the check
            pass
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_the_second_invocations_trace_id_is_checked_too():
    recorder = RelayRecorder(0, host="127.0.0.1")
    _SecondInvocationOnItsOwnTraceAgent.RELAY[0] = f"http://127.0.0.1:{recorder.port}"
    _SecondInvocationOnItsOwnTraceAgent.ISSUED.clear()
    _SecondInvocationOnItsOwnTraceAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SecondInvocationOnItsOwnTraceAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"},
            relay=recorder, expect_spans=True, settle_seconds=1.0,
        )
    finally:
        server.shutdown()
        recorder.stop()

    # The first invocation WAS instrumented correctly, so the evidence
    # test passes and only the integrity test can fail here.
    assert any(s["name"] == "first-span" and s["trace_id"] == result.trace_id
               for s in result.spans), result.spans
    assert any(s["name"] == "second-span" for s in result.spans), result.spans
    assert result.checks["spans"] == "fail", result.summary()
    assert "carry another trace id" in result.summary(), result.summary()


class _SlowFirstFlushAgent(BaseHTTPRequestHandler):
    """Both invocations export; the second promptly, the first after a
    delay — ordinary asynchronous flush.

    The settle loop waited for ANY span, so the binding probe's own export
    satisfied it and the loop skipped the window it was given. Measured
    before the fix: told to wait 20s it returned in 5.68s and reported
    "the container exported no spans" about an agent whose span arrived at
    12s (Codex round 6; found independently by auditing what the probe
    touches).
    """

    RELAY: list[str] = [""]
    DELAY = 2.0
    ISSUED: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def _export(self, token, trace_id, name):
        import urllib.request

        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{self.RELAY[0]}/v1/traces",
                    data=_otlp_span(trace_id, name),
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/x-protobuf"},
                ),
                timeout=5,
            ).read()
        except Exception:  # noqa: BLE001
            pass

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.ISSUED.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        token = self._bearer()
        self.ISSUED[invocation] = token
        traceparent = self.headers.get("traceparent") or ""
        trace_id = traceparent.split("-")[1] if traceparent.count("-") >= 3 else "0" * 32
        if self.COUNT[0] > 1:
            self._export(token, trace_id, "second-span")            # prompt
        else:
            def later(tok=token, tid=trace_id):
                time.sleep(self.DELAY)
                self._export(tok, tid, "first-span")
            threading.Thread(target=later, daemon=True).start()      # delayed
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_the_settle_wait_is_for_the_invocation_under_test():
    recorder = RelayRecorder(0, host="127.0.0.1")
    _SlowFirstFlushAgent.RELAY[0] = f"http://127.0.0.1:{recorder.port}"
    _SlowFirstFlushAgent.ISSUED.clear()
    _SlowFirstFlushAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowFirstFlushAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        started = time.monotonic()
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"},
            relay=recorder, expect_spans=True, settle_seconds=10.0,
        )
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        recorder.stop()

    assert result.checks["spans"] == "pass", result.summary()
    assert any(s["name"] == "first-span" for s in result.spans), result.spans
    # The consequence: it WAITED, rather than settling for the probe's
    # own prompt export.
    assert elapsed >= _SlowFirstFlushAgent.DELAY, (
        f"returned in {elapsed:.2f}s without waiting for the first "
        f"invocation's {_SlowFirstFlushAgent.DELAY}s flush"
    )


class _PhaseScopedPoolAgent(BaseHTTPRequestHandler):
    """Pools accepted tokens by ``(run.id, phase)`` rather than by
    invocation.

    Invisible to a next-phase probe — two phases, two buckets, both
    cross-uses refused — and wide open to a rerun, which holds the phase
    constant as well as the run. Measured before the rerun transition
    existed: `token_binding: pass`, buckets
    ``[(('battery-run','echo'),1), (('battery-run','second'),1)]``, while
    the rerun-shaped request leaked `200` (Codex round 7).
    """

    BY_KEY: dict[tuple, set] = {}
    INVOCATION_KEY: dict[str, tuple] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        key = self.INVOCATION_KEY.get(invocation)
        token = self._bearer()
        if key is None or token is None or token not in self.BY_KEY.get(key, set()):
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        payload = json.loads(raw or b"{}")
        key = ((payload.get("run") or {}).get("id") or "?", payload.get("phase") or "?")
        token = self._bearer()
        self.BY_KEY.setdefault(key, set())
        if token:
            self.BY_KEY[key].add(token)
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.INVOCATION_KEY[invocation] = key
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_gated_phase_is_probed_with_a_rerun_as_well():
    """A gated phase has TWO producible transitions, and the rerun is the
    one that holds the phase constant."""
    _PhaseScopedPoolAgent.BY_KEY.clear()
    _PhaseScopedPoolAgent.INVOCATION_KEY.clear()
    _PhaseScopedPoolAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PhaseScopedPoolAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = _gated_two_phase(load_manifest(ECHO_DIR))
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    text = result.summary()
    # Both transitions were used, and the report says so.
    assert "the next phase (second) of the same run" in text, text
    assert "a rerun of echo" in text, text
    # The rerun is what catches this agent; the next phase cannot.
    assert result.checks["token_binding"] == "fail", text
    assert ("with the echo rerun's token on the first invocation answered 200, "
            "not 401") in text, text
    # Three invocations: the one under test, the next phase, the rerun.
    assert _PhaseScopedPoolAgent.COUNT[0] == 5, _PhaseScopedPoolAgent.COUNT[0]  # +1: round 32's same-phase second run; +1: the headerless traceparent probe


class _SilentFirstWrongSecondAgent(BaseHTTPRequestHandler):
    """Exports nothing for the invocation under test and a wrong-trace span
    for the battery's binding invocation.

    With `expect_spans` off, `own` is empty and the check went straight to
    `skip` — returning PASS over telemetry the production relay refuses as
    `trace_mismatch` (Codex round 7). Evidence and integrity are separate
    questions.
    """

    RELAY: list[str] = [""]
    ISSUED: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.ISSUED.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        import urllib.request

        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        token = self._bearer()
        self.ISSUED[invocation] = token
        if self.COUNT[0] > 1:  # only the binding invocation exports, wrongly
            try:
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"{self.RELAY[0]}/v1/traces",
                        data=_otlp_span("ff" * 16, "wrong-trace-span"),
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/x-protobuf"},
                    ),
                    timeout=5,
                ).read()
            except Exception:  # noqa: BLE001
                pass
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_integrity_is_checked_without_first_invocation_evidence():
    recorder = RelayRecorder(0, host="127.0.0.1")
    _SilentFirstWrongSecondAgent.RELAY[0] = f"http://127.0.0.1:{recorder.port}"
    _SilentFirstWrongSecondAgent.ISSUED.clear()
    _SilentFirstWrongSecondAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SilentFirstWrongSecondAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        # expect_spans OFF: the path that used to return skip, and PASS.
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"},
            relay=recorder, settle_seconds=1.0,
        )
    finally:
        server.shutdown()
        recorder.stop()

    assert any(s["name"] == "wrong-trace-span" for s in result.spans), result.spans
    assert not any(s["token"] == "" for s in result.spans)
    assert result.checks["spans"] == "fail", result.summary()
    assert "carry another trace id" in result.summary(), result.summary()
    assert not result.passed, result.summary()


class _DelayedWrongSecondSpanAgent(BaseHTTPRequestHandler):
    """The invocation under test exports promptly and correctly; the
    binding invocation exports a wrong-trace span AFTER its stream is
    drained — an ordinary batch exporter straddling ``completed``.

    A settle window that waits only for the first invocation's telemetry
    ends before that batch arrives, and the wrong trace is missed
    (Codex round 7).
    """

    RELAY: list[str] = [""]
    DELAY = 1.5
    ISSUED: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def _export(self, token, trace_id, name):
        import urllib.request

        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{self.RELAY[0]}/v1/traces",
                    data=_otlp_span(trace_id, name),
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/x-protobuf"},
                ),
                timeout=5,
            ).read()
        except Exception:  # noqa: BLE001
            pass

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.ISSUED.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        token = self._bearer()
        self.ISSUED[invocation] = token
        traceparent = self.headers.get("traceparent") or ""
        real = traceparent.split("-")[1] if traceparent.count("-") >= 3 else "0" * 32
        if self.COUNT[0] == 1:
            self._export(token, real, "first-span")                 # prompt, correct
        else:
            def later(tok=token):
                time.sleep(self.DELAY)
                self._export(tok, "ff" * 16, "late-wrong-span")     # delayed, wrong
            threading.Thread(target=later, daemon=True).start()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_the_settle_window_covers_the_binding_invocation_too():
    recorder = RelayRecorder(0, host="127.0.0.1")
    _DelayedWrongSecondSpanAgent.RELAY[0] = f"http://127.0.0.1:{recorder.port}"
    _DelayedWrongSecondSpanAgent.ISSUED.clear()
    _DelayedWrongSecondSpanAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DelayedWrongSecondSpanAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"},
            relay=recorder, expect_spans=True, settle_seconds=8.0,
        )
    finally:
        server.shutdown()
        recorder.stop()

    # The first invocation was instrumented correctly, so only the late
    # wrong-trace span from the binding invocation can fail this.
    assert any(s["name"] == "first-span" and s["trace_id"] == result.trace_id
               for s in result.spans), result.spans
    assert any(s["name"] == "late-wrong-span" for s in result.spans), (
        "the battery returned before the binding invocation's batch arrived"
    )
    assert result.checks["spans"] == "fail", result.summary()
    assert "carry another trace id" in result.summary(), result.summary()


class _StatefulGateAgent(BaseHTTPRequestHandler):
    """Correct on every axis, and it models the run's own lifecycle.

    Tokens are bound to their invocation — no pool of any kind — and a
    rerun is accepted exactly while the run is parked. Completing the
    final phase sets ``run.status = "complete"``
    (``agent_runner.py:568-577``) and ``runs.py:466`` accepts an edit
    only in ``awaiting_approval``/``refining``, so a rerun that arrives
    AFTER the next phase has run is one the chassis could not have sent,
    and answering it ``400`` is conformant.

    The battery used to advance through the gate first and ask for the
    rerun second, which is that impossible order. Measured against this
    agent before the fix: ``token_binding: fail``, "per-invocation
    binding is UNPROVEN: the agent would not start a rerun of echo ...
    (400)" (Codex round 8).
    """

    BY_INVOCATION: dict[str, str] = {}
    ADVANCED: set = set()
    COUNT: list[int] = [0]
    REFUSED: list[str] = []
    LAST_PHASE = "second"

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INVOCATION.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        run = parsed.get("run") or {}
        run_id, phase = run.get("id") or "?", parsed.get("phase")
        if run.get("rerun"):
            if run_id in self.ADVANCED:
                self.REFUSED.append(f"rerun of {phase} after the final phase ran")
                return self._json(400, {"error": "run is complete; cannot rerun"})
        elif phase == self.LAST_PHASE:
            self.ADVANCED.add(run_id)
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_the_rerun_is_probed_before_the_gate_is_passed():
    """The SET of transitions was right and the ORDER was not."""
    _StatefulGateAgent.BY_INVOCATION.clear()
    _StatefulGateAgent.ADVANCED.clear()
    _StatefulGateAgent.COUNT[0] = 0
    _StatefulGateAgent.REFUSED.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StatefulGateAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = _gated_two_phase(load_manifest(ECHO_DIR))
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    text = result.summary()
    assert _StatefulGateAgent.REFUSED == [], _StatefulGateAgent.REFUSED
    assert result.checks["token_binding"] == "pass", text
    assert "UNPROVEN" not in text, text
    # Three invocations: the one under test, its rerun, then the next phase.
    assert _StatefulGateAgent.COUNT[0] == 5, _StatefulGateAgent.COUNT[0]  # +1: round 32's same-phase second run; +1: the headerless traceparent probe
    # And the report's order is the producible one, so an author reading it
    # sees the sequence the chassis would really send.
    rerun_at = text.index("a rerun of echo")
    next_at = text.index("the next phase (second) of the same run")
    assert rerun_at < next_at, text


class _PriorOutputRecordingAgent(BaseHTTPRequestHandler):
    """Returns a DIFFERENT output per invocation and records every body.

    After a rerun the chassis hands the next phase the RERUN's analysis —
    ``agent_runner.py:485-489`` reads the snapshot the rerun just wrote —
    so a battery that carries the original invocation's output forward is
    sending a `prior_output` the chassis would not have sent.
    """

    BODIES: list[dict] = []
    BY_INVOCATION: dict[str, str] = {}
    COUNT: list[int] = [0]
    OUTPUT_OF: dict[str, dict] = {}

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INVOCATION.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        out = self.OUTPUT_OF.get(invocation, {})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                + b'event: completed\ndata: '
                + json.dumps({"output": out}).encode() + b"\n\n"
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": out})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        self.BODIES.append(parsed)
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        self.OUTPUT_OF[invocation] = {"echo": {"from": invocation}}
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_the_next_phase_carries_the_reruns_output_not_the_originals():
    _PriorOutputRecordingAgent.BODIES.clear()
    _PriorOutputRecordingAgent.BY_INVOCATION.clear()
    _PriorOutputRecordingAgent.OUTPUT_OF.clear()
    _PriorOutputRecordingAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PriorOutputRecordingAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = _gated_two_phase(load_manifest(ECHO_DIR))
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    bodies = _PriorOutputRecordingAgent.BODIES
    assert len(bodies) == 5, [b.get("phase") for b in bodies]  # +1: round 32's same-phase second run; +1: the headerless traceparent probe
    first, rerun, nxt, _second_run, _headerless = bodies
    assert rerun["run"]["rerun"] is True and rerun["phase"] == first["phase"], rerun
    assert nxt["phase"] == "second" and nxt["run"]["rerun"] is False, nxt
    # The rerun sees the FIRST invocation's output...
    assert rerun["prior_output"] == {"echo": {"from": "inv-1"}}, rerun["prior_output"]
    # ...and the next phase sees the RERUN's, not the first's.
    assert nxt["prior_output"] == {"echo": {"from": "inv-2"}}, nxt["prior_output"]
    assert result.checks["token_binding"] == "pass", result.summary()


class _PromptThenLateWrongAgent(BaseHTTPRequestHandler):
    """Every invocation exports a CORRECT span at once; the binding
    invocation exports a wrong-trace span later, inside the window.

    ``_DelayedWrongSecondSpanAgent`` exports NOTHING until late, which the
    round-7 predicate ("every minted token has produced a span") waits
    for. This one satisfies that predicate immediately with a correct
    batch and then misbehaves — measured before the fix: told to settle
    for 8s the battery returned in 5.39s with ``spans: pass`` about a
    wrong-trace span that arrived at 7.2s (Codex round 8).
    """

    RELAY: list[str] = [""]
    TRACE: list[str] = [""]
    DELAY = 1.5
    ISSUED: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def _export(self, token, trace_id, name):
        import urllib.request

        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{self.RELAY[0]}/v1/traces",
                    data=_otlp_span(trace_id, name),
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/x-protobuf"},
                ),
                timeout=5,
            ).read()
        except Exception:  # noqa: BLE001
            pass

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.ISSUED.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        token = self._bearer()
        parts = (self.headers.get("traceparent") or "").split("-")
        if len(parts) == 4 and not self.TRACE[0]:
            self.TRACE[0] = parts[1]
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.ISSUED[invocation] = token
        # Correct AND prompt: this is what satisfied the old predicate.
        self._export(token, self.TRACE[0], f"prompt-span-{self.COUNT[0]}")
        if self.COUNT[0] == 2:
            def later(token=token):
                time.sleep(self.DELAY)
                self._export(token, "ff" * 16, "late-wrong-span")

            threading.Thread(target=later, daemon=True).start()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_prompt_correct_batch_does_not_close_the_settle_window():
    recorder = RelayRecorder(0, host="127.0.0.1")
    _PromptThenLateWrongAgent.RELAY[0] = f"http://127.0.0.1:{recorder.port}"
    _PromptThenLateWrongAgent.TRACE[0] = ""
    _PromptThenLateWrongAgent.ISSUED.clear()
    _PromptThenLateWrongAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PromptThenLateWrongAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=load_manifest(ECHO_DIR), scenario={"message": "x"},
            relay=recorder, expect_spans=True, settle_seconds=2.0,
        )
    finally:
        server.shutdown()
        recorder.stop()

    # Both prompt batches arrived, so the OLD predicate was satisfied at
    # once — the window stayed open only because it is measured from the
    # latest arrival.
    assert {s["name"] for s in result.spans} >= {"prompt-span-1", "prompt-span-2"}, result.spans
    assert any(s["name"] == "late-wrong-span" for s in result.spans), (
        "the window closed on the first correct batch per token"
    )
    assert result.checks["spans"] == "fail", result.summary()
    assert "carry another trace id" in result.summary(), result.summary()


class _NeverStopsExportingAgent(BaseHTTPRequestHandler):
    """Exports a correct span every 0.1s until told to stop.

    The settle window restarts on every arrival, so this agent would hold
    the battery open for as long as it keeps exporting. The cap is for
    it — and reaching the cap while telemetry is still arriving is
    RECORDED, because a verdict taken over a moving target is not the
    same as one taken over a finished one.
    """

    RELAY: list[str] = [""]
    TRACE: list[str] = [""]
    STOP: list[bool] = [False]
    ISSUED: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def _export(self, token, trace_id, name):
        import urllib.request

        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{self.RELAY[0]}/v1/traces",
                    data=_otlp_span(trace_id, name),
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/x-protobuf"},
                ),
                timeout=5,
            ).read()
        except Exception:  # noqa: BLE001
            pass

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.ISSUED.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        token = self._bearer()
        parts = (self.headers.get("traceparent") or "").split("-")
        if len(parts) == 4 and not self.TRACE[0]:
            self.TRACE[0] = parts[1]
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.ISSUED[invocation] = token
        if self.COUNT[0] == 1:
            def forever(token=token):
                n = 0
                while not self.STOP[0]:
                    n += 1
                    self._export(token, self.TRACE[0], f"span-{n}")
                    time.sleep(0.1)

            threading.Thread(target=forever, daemon=True).start()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_never_quiet_exporter_hits_the_cap_and_the_report_says_so():
    recorder = RelayRecorder(0, host="127.0.0.1")
    _NeverStopsExportingAgent.RELAY[0] = f"http://127.0.0.1:{recorder.port}"
    _NeverStopsExportingAgent.TRACE[0] = ""
    _NeverStopsExportingAgent.STOP[0] = False
    _NeverStopsExportingAgent.ISSUED.clear()
    _NeverStopsExportingAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NeverStopsExportingAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settle = 0.5
    started = time.monotonic()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=load_manifest(ECHO_DIR), scenario={"message": "x"},
            relay=recorder, expect_spans=True, settle_seconds=settle,
        )
    finally:
        elapsed = time.monotonic() - started
        _NeverStopsExportingAgent.STOP[0] = True
        server.shutdown()
        recorder.stop()

    # It returned at all — the rolling window is bounded.
    assert elapsed < settle * 8 + 20, elapsed
    # And it does NOT certify the agent. When I added the cap I made this
    # a note, and a note leaves `passed` true: every span this agent sent
    # was correct, so the battery said "taken over a moving target" in its
    # own report and certified anyway — while a wrong-trace batch arriving
    # one tick after the cap would never have been looked at (Codex round
    # 9). An open check is a failing check.
    assert result.checks["spans"] == "incomplete", result.summary()
    assert result.passed is False, result.summary()
    assert "never concluded" in result.summary(), result.summary()
    # The failure names the remedy, because an author cannot act on "no".
    assert "raise settle_seconds" in result.summary(), result.summary()


class _FailingRerunAgent(BaseHTTPRequestHandler):
    """Correct on every axis, and its rerun terminates ``failed``.

    ``agent_runner.py:574-577``: for a non-final phase, ``if not ok:
    run.status = "error"``. A phase invocation that fails ends the run, so
    the chassis never follows it with the next phase — and this agent
    refuses that advance with ``409``, which is conformant.

    Round 8 fixed the ORDER of the transitions and left the CONDITION on
    advancing unexamined. Measured before this check existed:
    ``token_binding: fail``, "per-invocation binding is UNPROVEN: the
    agent would not start the next phase (second) of the same run (409)"
    (Codex round 9).
    """

    BY_INVOCATION: dict[str, str] = {}
    KIND: dict[str, str] = {}
    ERRORED: set = set()
    COUNT: list[int] = [0]
    REFUSED: list[str] = []

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INVOCATION.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        failed = self.KIND.get(invocation) == "rerun"
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
            )
            self.wfile.write(
                b'event: failed\ndata: {"error": "the rerun could not complete"}\n\n'
                if failed else
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            if failed:
                return self._json(409, {"error": "invocation failed"})
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        run = parsed.get("run") or {}
        run_id = run.get("id") or "?"
        rerun = bool(run.get("rerun"))
        if not rerun and run_id in self.ERRORED:
            self.REFUSED.append(f"{parsed.get('phase')} after the run errored")
            return self._json(409, {"error": "run is in error; cannot advance"})
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        if rerun:
            self.KIND[invocation] = "rerun"
            self.ERRORED.add(run_id)
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_failed_transition_re_anchors_on_a_fresh_run():
    _FailingRerunAgent.BY_INVOCATION.clear()
    _FailingRerunAgent.KIND.clear()
    _FailingRerunAgent.ERRORED.clear()
    _FailingRerunAgent.COUNT[0] = 0
    _FailingRerunAgent.REFUSED.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FailingRerunAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = _gated_two_phase(load_manifest(ECHO_DIR))
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    text = result.summary()
    # The errored run was never advanced, so the agent never had to refuse
    # a transition the chassis could not have made.
    assert _FailingRerunAgent.REFUSED == [], _FailingRerunAgent.REFUSED
    # Four invocations: the one under test, its rerun, a FRESH run's phase
    # under test, and the next phase of that run. Round 9 stopped at two
    # and thereby skipped the next-phase path altogether — which a user
    # reaches by approving rather than editing, so an agent whose reruns
    # are correct and whose ordinary invocations pool by `run.id` passed
    # over a real leak (Codex round 10).
    assert _FailingRerunAgent.COUNT[0] == 6, _FailingRerunAgent.COUNT[0]  # +1: round 32's same-phase second run; +1: the headerless traceparent probe
    # A legal `failed` is still not the agent's defect.
    assert result.checks["token_binding"] == "pass", text
    assert "UNPROVEN" not in text, text
    # And the report says what it did, in a sentence that reads.
    assert "the echo rerun ended `failed`" in text, text
    assert "probed against a fresh run" in text, text


class _BudgetRecordingAgent(BaseHTTPRequestHandler):
    """Records the ``deadline_seconds`` of every POST it receives."""

    BUDGETS: list = []
    PHASES: list = []
    BY_INVOCATION: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INVOCATION.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        self.BUDGETS.append(parsed.get("deadline_seconds"))
        self.PHASES.append(parsed.get("phase"))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_each_invocation_carries_its_own_phases_budget():
    """``agent_runner.py:413`` computes the deadline INSIDE the phase loop.

    The battery sent ``int(timeout)`` — 120 — to every phase of every
    manifest instead, a budget the chassis cannot produce. Measured
    against a manifest declaring 30s: the chassis would send ``[30, 30]``
    and the battery sent ``120``, which an agent enforcing its own
    published deadline answered ``400`` at the first POST, so no check ran
    at all.

    The two phases declare DIFFERENT budgets on purpose: a single shared
    value — the battery's timeout, the first phase's, the ceiling — cannot
    satisfy this by accident.
    """
    from app.agents.manifest import PhaseSpec
    from app.services.agent_runner import phase_deadline

    _BudgetRecordingAgent.BUDGETS.clear()
    _BudgetRecordingAgent.PHASES.clear()
    _BudgetRecordingAgent.BY_INVOCATION.clear()
    _BudgetRecordingAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BudgetRecordingAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        base = load_manifest(ECHO_DIR)
        manifest = base.model_copy(update={"phases": [
            PhaseSpec(name=base.phases[0].name, deadline_seconds=30),
            PhaseSpec(name="second", approval=True, deadline_seconds=45),
        ]})
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"}, settle_seconds=0.1
        )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "pass", result.summary()
    # The invocation under test, its rerun (same phase), then the next.
    # +1: round 32's same-phase second run, appended last.
    # +1: the headerless traceparent probe, which invokes the phase under test
    # on a run of its own and is therefore appended last.
    assert _BudgetRecordingAgent.PHASES == [
        base.phases[0].name, base.phases[0].name, "second",
        base.phases[0].name,
        base.phases[0].name
    ], _BudgetRecordingAgent.PHASES
    # The fourth is 30 and that is the point, not bookkeeping: round 32's
    # second run holds the PHASE constant, so it must be advertised that
    # phase's own budget and not the last one the walk reached. The
    # fifth is the headerless traceparent probe, which makes the same claim
    # from a second direction: it too invokes the phase under test, on a
    # run of its own, and must carry that phase's budget rather than
    # `second`'s 45 — stripping the trace headers changes the headers
    # and nothing else about the invocation.
    assert _BudgetRecordingAgent.BUDGETS == [30, 30, 45, 30, 30], (
        _BudgetRecordingAgent.BUDGETS)
    # And it is the chassis's own rule producing those numbers, not a
    # constant that happens to match today.
    assert [phase_deadline(p) for p in manifest.phases] == [30, 45]


@pytest.mark.asyncio
async def test_the_battery_timeout_is_the_ceiling_it_can_RAISE(monkeypatch):
    """``--timeout`` is this battery's ``LIBRERUN_MAX_PHASE_SECONDS``.

    That sentence was false in the direction that matters. The battery
    composed the chassis rule with a ``min`` of its own, so
    ``phase_deadline(spec)`` had already clamped to whatever ceiling the
    IMPORTING process carried (3600 by default) and ``--timeout`` could
    only ever lower the advertised budget. An agent whose phases
    legitimately run longer than this process's ceiling was therefore
    untestable: a manifest declaring 5000s under ``--timeout 5000``
    advertised 3600, and an agent enforcing its own published deadline
    answers ``400`` to a budget it never asked for.

    The process ceiling is pinned BELOW the declared value on purpose —
    a battery that merely passed the number through would pass this test
    too, so the assertion is that the CLI's ceiling, not this process's,
    is the one in force.
    """
    from app.agents.manifest import PhaseSpec
    from app.services import agent_runner

    monkeypatch.setattr(
        agent_runner._settings, "LIBRERUN_MAX_PHASE_SECONDS", 3600
    )

    _BudgetRecordingAgent.BUDGETS.clear()
    _BudgetRecordingAgent.PHASES.clear()
    _BudgetRecordingAgent.BY_INVOCATION.clear()
    _BudgetRecordingAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BudgetRecordingAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        base = load_manifest(ECHO_DIR)
        manifest = base.model_copy(update={"phases": [
            PhaseSpec(name=base.phases[0].name, deadline_seconds=5000),
        ]})
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"},
            settle_seconds=0.1, timeout=5000.0,
        )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "pass", result.summary()
    assert _BudgetRecordingAgent.BUDGETS == [5000, 5000, 5000], (  # +1: the headerless traceparent probe
        _BudgetRecordingAgent.BUDGETS
    )


@pytest.mark.asyncio
async def test_the_battery_timeout_is_also_the_ceiling_it_can_LOWER():
    """The same parameter, the other direction.

    A ceiling that can only be raised is not a ceiling. ``--timeout 3``
    against a phase declaring 30 must advertise 3, because the battery
    will only WAIT 3 — advertising a budget it will not honour is the
    defect that made it send a deadline and then abandon the agent
    inside it (round 11).
    """
    from app.agents.manifest import PhaseSpec

    _BudgetRecordingAgent.BUDGETS.clear()
    _BudgetRecordingAgent.PHASES.clear()
    _BudgetRecordingAgent.BY_INVOCATION.clear()
    _BudgetRecordingAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BudgetRecordingAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        base = load_manifest(ECHO_DIR)
        manifest = base.model_copy(update={"phases": [
            PhaseSpec(name=base.phases[0].name, deadline_seconds=30),
        ]})
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"},
            settle_seconds=0.1, timeout=3.0,
        )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "pass", result.summary()
    assert _BudgetRecordingAgent.BUDGETS == [3, 3, 3], _BudgetRecordingAgent.BUDGETS  # +1: the headerless traceparent probe


class _ChattyNeverTerminatingAgent(BaseHTTPRequestHandler):
    """Emits a progress chunk every 0.2s for ``CHUNK_SECONDS``, then
    completes — never idle long enough for a read timeout to fire."""

    CHUNK_SECONDS = 12.0
    BY_INVOCATION: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INVOCATION.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            end = time.monotonic() + self.CHUNK_SECONDS
            try:
                while time.monotonic() < end:
                    self.wfile.write(
                        b'event: progress\ndata: '
                        b'{"step_id": "only", "status": "running"}\n\n'
                    )
                    self.wfile.flush()
                    time.sleep(0.2)
                self.wfile.write(
                    b'event: completed\ndata: '
                    b'{"output": {"echo": {"message": "x"}}}\n\n'
                )
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_an_invocation_that_outruns_its_advertised_deadline_FAILS():
    """The battery is exactly as patient as the deadline it advertised.

    ``httpx``'s timeout bounds the gap BETWEEN reads, so an agent that
    keeps chunking never trips it. Production has no such hole — the
    runner wraps the whole invocation in ``asyncio.timeout(deadline)``
    (``agent_runner.py:513``) and fails the run
    ``PhaseDeadlineExceeded``. So a battery that advertised
    ``deadline_seconds`` and then waited arbitrarily long was not a
    lenient chassis, it was a different one, and an agent that overruns
    every budget it is handed passed clean. Measured before the fix: an
    invocation advertised 3s and chunking for 20s was consumed in full,
    ``completed: pass``, no failure, 40.1s to a verdict.

    The wall clock is asserted, not just the verdict: a battery that
    waited out the full 12s and THEN complained would satisfy an
    assertion on ``failures`` alone while leaving the defect — the
    waiting — exactly where it was.
    """
    from app.agents.manifest import PhaseSpec

    _ChattyNeverTerminatingAgent.BY_INVOCATION.clear()
    _ChattyNeverTerminatingAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChattyNeverTerminatingAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    started = time.monotonic()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        base = load_manifest(ECHO_DIR)
        manifest = base.model_copy(update={"phases": [
            PhaseSpec(name=base.phases[0].name, deadline_seconds=2),
        ]})
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"},
            settle_seconds=0.1, timeout=2.0,
        )
    finally:
        elapsed = time.monotonic() - started
        server.shutdown()

    assert result.passed is False, result.summary()
    assert result.checks["completed"] == "fail", result.checks
    assert any("outran the 2s deadline" in f for f in result.failures), (
        result.failures
    )
    assert elapsed < 8.0, f"waited {elapsed:.1f}s on an invocation it gave 2s"


class _SlowSecondPhaseAgent(BaseHTTPRequestHandler):
    """Phase one answers instantly; phase two works for ``SLOW_SECONDS``.

    Entirely conformant: each invocation finishes well inside the budget
    ITS OWN phase declared.
    """

    SLOW_SECONDS = 4.0
    BY_INVOCATION: dict[str, str] = {}
    PHASE_OF: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INVOCATION.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                if self.PHASE_OF.get(invocation) == "second":
                    end = time.monotonic() + self.SLOW_SECONDS
                    while time.monotonic() < end:
                        self.wfile.write(
                            b'event: progress\ndata: '
                            b'{"step_id": "only", "status": "running"}\n\n'
                        )
                        self.wfile.flush()
                        time.sleep(0.2)
                else:
                    self.wfile.write(
                        b'event: progress\ndata: '
                        b'{"step_id": "only", "status": "running"}\n\n'
                    )
                self.wfile.write(
                    b'event: completed\ndata: '
                    b'{"output": {"echo": {"message": "x"}}}\n\n'
                )
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            phase = (json.loads(raw or b"{}") or {}).get("phase")
        except ValueError:
            phase = None
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        self.PHASE_OF[invocation] = phase
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_each_drained_invocation_waits_on_ITS_OWN_phases_budget():
    """The battery advertises a per-phase budget and must wait per-phase too.

    The wall-clock bound is a rule about EVERY invocation, and there are
    three: the one under test, the fresh anchor, and each transition the
    binding check drains. Applying the first invocation's budget to a
    drained next-phase invocation reintroduces the round-10 defect in a
    new place — the battery would send phase two its own 12s and then
    abandon it at phase one's 2s, failing an agent that did exactly what
    it was asked.

    The two phases declare deliberately different budgets, and the second
    takes longer than the FIRST's, so a single shared number cannot
    satisfy this by accident.
    """
    from app.agents.manifest import PhaseSpec

    _SlowSecondPhaseAgent.BY_INVOCATION.clear()
    _SlowSecondPhaseAgent.PHASE_OF.clear()
    _SlowSecondPhaseAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowSecondPhaseAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        base = load_manifest(ECHO_DIR)
        manifest = base.model_copy(update={"phases": [
            PhaseSpec(name=base.phases[0].name, deadline_seconds=2),
            PhaseSpec(name="second", approval=True, deadline_seconds=12),
        ]})
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"},
            settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    assert result.passed is True, result.summary()
    assert result.checks["token_binding"] == "pass", result.summary()
    assert not any("outran" in f for f in result.failures), result.failures


def _otlp_log(trace_id_hex: str, body: str) -> bytes:
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

    req = logs_service_pb2.ExportLogsServiceRequest()
    record = req.resource_logs.add().scope_logs.add().log_records.add()
    record.trace_id = bytes.fromhex(trace_id_hex)
    record.body.string_value = body
    return req.SerializeToString()


class _CorrectRerunsPoolingOrdinaryAgent(BaseHTTPRequestHandler):
    """Its RERUNS are invocation-scoped and they fail; its ORDINARY
    invocations pool accepted tokens by ``run.id``.

    The rerun probe finds nothing (that path is correct), the rerun ends
    ``failed``, and a battery that stopped there never reached the
    next-phase path — where the pool is. But the later phase is reachable
    without any edit at all: the user approves the original invocation.
    Measured with the stop in place: ``token_binding: pass`` over a real
    leak (Codex round 10).
    """

    BY_RUN: dict = {}
    BY_INVOCATION: dict[str, str] = {}
    INVOCATION_RUN: dict[str, str] = {}
    IS_RERUN: dict[str, bool] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        token = self._bearer()
        if self.IS_RERUN.get(invocation):
            allowed = self.BY_INVOCATION.get(invocation) == token
        else:
            run = self.INVOCATION_RUN.get(invocation)
            allowed = run is not None and token in self.BY_RUN.get(run, set())
        if not allowed:
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
            )
            self.wfile.write(
                b'event: failed\ndata: {"error": "no"}\n\n'
                if self.IS_RERUN.get(invocation) else
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        run = parsed.get("run") or {}
        run_id, rerun = run.get("id") or "?", bool(run.get("rerun"))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        token = self._bearer()
        self.BY_INVOCATION[invocation] = token
        self.INVOCATION_RUN[invocation] = run_id
        if rerun:
            self.IS_RERUN[invocation] = True
        else:
            self.BY_RUN.setdefault(run_id, set()).add(token)
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_pool_on_the_ordinary_path_survives_a_failed_rerun():
    for store in (_CorrectRerunsPoolingOrdinaryAgent.BY_RUN,
                  _CorrectRerunsPoolingOrdinaryAgent.BY_INVOCATION,
                  _CorrectRerunsPoolingOrdinaryAgent.INVOCATION_RUN,
                  _CorrectRerunsPoolingOrdinaryAgent.IS_RERUN):
        store.clear()
    _CorrectRerunsPoolingOrdinaryAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CorrectRerunsPoolingOrdinaryAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=_gated_two_phase(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    assert result.checks["token_binding"] == "fail", text
    # The consequence, not a proxy: the fresh run's two invocations share
    # one pool, and that is what leaks.
    assert "the second phase's token" in text, text
    assert "probed against a fresh run" in text, text
    pooled = [run for run, tokens in
              _CorrectRerunsPoolingOrdinaryAgent.BY_RUN.items() if len(tokens) > 1]
    assert pooled, _CorrectRerunsPoolingOrdinaryAgent.BY_RUN


class _LeaksOnTheRerunOnlyAgent(BaseHTTPRequestHandler):
    """Accepts the first invocation's token on the RERUN's leaves and
    refuses it on the next phase's.

    Both iterations used to write the same ``bindings`` key, so the next
    phase's ``401`` overwrote the rerun's ``200`` and the agent passed
    (Codex round 10).
    """

    BY_INVOCATION: dict[str, str] = {}
    IS_RERUN: dict[str, bool] = {}
    FIRST: list[str] = [""]
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        token = self._bearer()
        allowed = self.BY_INVOCATION.get(invocation) == token
        if (not allowed and self.IS_RERUN.get(invocation)
                and token == self.FIRST[0]):
            allowed = True                      # the leak, on the rerun only
        if not allowed:
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        token = self._bearer()
        self.BY_INVOCATION[invocation] = token
        if self.COUNT[0] == 1:
            self.FIRST[0] = token
        if (parsed.get("run") or {}).get("rerun"):
            self.IS_RERUN[invocation] = True
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_leak_on_the_rerun_is_not_overwritten_by_the_next_phase():
    _LeaksOnTheRerunOnlyAgent.BY_INVOCATION.clear()
    _LeaksOnTheRerunOnlyAgent.IS_RERUN.clear()
    _LeaksOnTheRerunOnlyAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LeaksOnTheRerunOnlyAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=_gated_two_phase(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    assert result.checks["token_binding"] == "fail", text
    # Named by TRANSITION, so the later iteration cannot erase it...
    assert ("events with the first invocation's token on the echo rerun "
            "answered 200, not 401") in text, text
    assert ("output with the first invocation's token on the echo rerun "
            "answered 200, not 401") in text, text
    # ...and the silence is as explicit as the catch: the next phase
    # refuses that token, so accusing it would be unearned.
    assert "on the second phase answered" not in text, text


class _ForeignLogRecordAgent(BaseHTTPRequestHandler):
    """A valid span under its own token, and a log record smuggled under
    an invented one carrying the right trace id.

    ``foreign`` counted spans only, so the relay's own rule — a bearer it
    minted, or nothing — went unchecked for logs (Codex round 10).
    """

    RELAY: list[str] = [""]
    TRACE: list[str] = [""]
    BY_INVOCATION: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def _post(self, path, data, token):
        import urllib.request

        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{self.RELAY[0]}{path}", data=data,
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/x-protobuf"},
                ),
                timeout=5,
            ).read()
        except Exception:  # noqa: BLE001
            pass

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INVOCATION.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        token = self._bearer()
        parts = (self.headers.get("traceparent") or "").split("-")
        if len(parts) == 4 and not self.TRACE[0]:
            self.TRACE[0] = parts[1]
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = token
        if self.COUNT[0] == 1:
            self._post("/v1/traces", _otlp_span(self.TRACE[0], "ok-span"), token)
            self._post("/v1/logs", _otlp_log(self.TRACE[0], "smuggled"),
                       "an-invented-token")
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_log_record_under_a_foreign_token_is_caught():
    recorder = RelayRecorder(0, host="127.0.0.1")
    _ForeignLogRecordAgent.RELAY[0] = f"http://127.0.0.1:{recorder.port}"
    _ForeignLogRecordAgent.TRACE[0] = ""
    _ForeignLogRecordAgent.BY_INVOCATION.clear()
    _ForeignLogRecordAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ForeignLogRecordAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=load_manifest(ECHO_DIR), scenario={"message": "x"},
            relay=recorder, expect_spans=True, settle_seconds=1.0,
        )
    finally:
        server.shutdown()
        recorder.stop()

    text = result.summary()
    # The span itself is fine, so only the record can fail this.
    assert any(s["name"] == "ok-span" for s in result.spans), result.spans
    assert [r["token"] for r in result.log_records] == ["an-invented-token"], (
        result.log_records
    )
    assert result.checks["spans"] == "fail", text
    assert "record(s) arrived under another token" in text, text


class _RefusesAFreshRunAgent(BaseHTTPRequestHandler):
    """Its rerun fails, and it will not start a fresh run either.

    The re-anchor is how the battery reaches a transition the errored run
    can no longer carry. When the agent refuses one, the binding check has
    NOT concluded for the remaining transitions — and an open check is a
    failing check, with the reason named so an author can act on the right
    one.
    """

    BY_INVOCATION: dict[str, str] = {}
    IS_RERUN: dict[str, bool] = {}
    COUNT: list[int] = [0]
    REFUSED: list[str] = []

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INVOCATION.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
            )
            self.wfile.write(
                b'event: failed\ndata: {"error": "no"}\n\n'
                if self.IS_RERUN.get(invocation) else
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        run = parsed.get("run") or {}
        run_id = run.get("id") or "?"
        if run_id.startswith("battery-run-fresh"):
            self.REFUSED.append(run_id)
            return self._json(409, {"error": "no new runs"})
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        if run.get("rerun"):
            self.IS_RERUN[invocation] = True
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_refused_fresh_run_leaves_the_binding_check_unconcluded():
    _RefusesAFreshRunAgent.BY_INVOCATION.clear()
    _RefusesAFreshRunAgent.IS_RERUN.clear()
    _RefusesAFreshRunAgent.COUNT[0] = 0
    _RefusesAFreshRunAgent.REFUSED.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RefusesAFreshRunAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=_gated_two_phase(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    # It really was asked, and really refused.
    assert _RefusesAFreshRunAgent.REFUSED, _RefusesAFreshRunAgent.REFUSED
    # An open check is a failing check: one of the two transitions was
    # never reached, so the verdict is not a pass.
    assert result.checks["token_binding"] == "fail", text
    # Against `failures`, NOT against `summary()`. The summary prints
    # notes and failures alike, so asserting on it cannot tell a verdict
    # from an aside — and `passed` reads only `failures`. Demoting this
    # to a note left `passed` true while the check said "fail", and the
    # first version of this test could not see it (the injection reported
    # not-caught and was right).
    assert result.passed is False, text
    assert any("would not start a fresh run" in f for f in result.failures), (
        result.failures
    )
    # And the reason is named, not merely the fact.
    assert any("could not conclude" in f for f in result.failures), result.failures


class _NextPhaseAcceptsTheRerunsToken(BaseHTTPRequestHandler):
    """Per-invocation everywhere EXCEPT that the next-phase invocation
    also accepts the token of the RERUN immediately before it.

    That is the one ordered pair an anchored cross-use never asks about:
    every probe involving the first invocation returns 401, so the agent
    passed while the rerun's token could read the next phase's events and
    output (Codex round 11).
    """

    BY_INVOCATION: dict[str, str] = {}
    PREVIOUS: dict[str, str] = {}
    LEAKS: dict[str, bool] = {}
    ORDER: list[str] = []
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        token = self._bearer()
        allowed = self.BY_INVOCATION.get(invocation) == token or (
            self.LEAKS.get(invocation) and self.PREVIOUS.get(invocation) == token
        )
        if not allowed:
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        if self.ORDER:
            self.PREVIOUS[invocation] = self.BY_INVOCATION[self.ORDER[-1]]
        self.ORDER.append(invocation)
        # ONLY the third invocation — the next phase — leaks, and only to
        # the rerun before it. Letting every invocation accept its
        # predecessor would include (first -> rerun), a pair that IS
        # probed, and the fixture would then measure a different agent.
        if self.COUNT[0] == 3:
            self.LEAKS[invocation] = True
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_every_ordered_pair_of_invocations_is_cross_used():
    for store in (_NextPhaseAcceptsTheRerunsToken.BY_INVOCATION,
                  _NextPhaseAcceptsTheRerunsToken.PREVIOUS,
                  _NextPhaseAcceptsTheRerunsToken.LEAKS):
        store.clear()
    _NextPhaseAcceptsTheRerunsToken.ORDER.clear()
    _NextPhaseAcceptsTheRerunsToken.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NextPhaseAcceptsTheRerunsToken)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=_gated_two_phase(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    assert _NextPhaseAcceptsTheRerunsToken.COUNT[0] == 5, (  # +1: round 32's same-phase second run; +1: the headerless traceparent probe
        _NextPhaseAcceptsTheRerunsToken.COUNT[0]
    )
    assert result.checks["token_binding"] == "fail", text
    # The pair that leaks, named at both ends.
    assert ("events with the echo rerun's token on the second phase "
            "answered 200, not 401") in text, text
    assert ("output with the echo rerun's token on the second phase "
            "answered 200, not 401") in text, text
    # And the silence: every pair involving the first invocation is clean,
    # so naming one of those would be an accusation the agent has not
    # earned — which is exactly why the anchored loop saw nothing.
    assert "on the first invocation answered" not in text, text
    assert "the first invocation's token on" not in text.split("\n  ?")[0], text


class _OutputOnlyAtTheEndpointAgent(BaseHTTPRequestHandler):
    """``completed`` carries no inline output; the result lives at
    ``/output``, which the contract permits and ``container.py:324-328``
    fetches. The next phase rejects a ``prior_output`` that is not its
    predecessor's, which is how the stale value becomes visible.
    """

    BY_INVOCATION: dict[str, str] = {}
    OUTPUT_OF: dict[str, dict] = {}
    LAST_FIRST_PHASE: list[str] = [""]
    SEEN_PRIOR: list = []
    REJECTED: list[str] = []
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INVOCATION.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": self.OUTPUT_OF.get(invocation, {})})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        if parsed.get("phase") == "second":
            prior = parsed.get("prior_output")
            self.SEEN_PRIOR.append(prior)
            expected = self.OUTPUT_OF.get(self.LAST_FIRST_PHASE[0])
            if prior != expected:
                self.REJECTED.append(f"{prior} != {expected}")
                return self._json(400, {"error": "prior_output is not my last result"})
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        self.OUTPUT_OF[invocation] = {"echo": {"from": invocation}}
        if parsed.get("phase") != "second":
            self.LAST_FIRST_PHASE[0] = invocation
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_the_next_phase_carries_a_fallback_output_not_a_stale_one():
    for store in (_OutputOnlyAtTheEndpointAgent.BY_INVOCATION,
                  _OutputOnlyAtTheEndpointAgent.OUTPUT_OF):
        store.clear()
    _OutputOnlyAtTheEndpointAgent.SEEN_PRIOR.clear()
    _OutputOnlyAtTheEndpointAgent.REJECTED.clear()
    _OutputOnlyAtTheEndpointAgent.LAST_FIRST_PHASE[0] = ""
    _OutputOnlyAtTheEndpointAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OutputOnlyAtTheEndpointAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=_gated_two_phase(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    assert _OutputOnlyAtTheEndpointAgent.REJECTED == [], (
        _OutputOnlyAtTheEndpointAgent.REJECTED
    )
    # The RERUN's output, fetched from /output because `completed` carried
    # none — not the original invocation's.
    assert _OutputOnlyAtTheEndpointAgent.SEEN_PRIOR == [
        {"echo": {"from": "inv-2"}}
    ], _OutputOnlyAtTheEndpointAgent.SEEN_PRIOR
    assert result.checks["token_binding"] == "pass", result.summary()


@pytest.mark.asyncio
async def test_the_battery_never_advertises_more_than_it_will_wait_for():
    """``timeout`` is this battery's ``LIBRERUN_MAX_PHASE_SECONDS``.

    Both directions are asserted, because a clamp that ALWAYS returned the
    ceiling would satisfy the first on its own.
    """
    async def budgets(timeout, declared):
        from app.agents.manifest import PhaseSpec

        _BudgetRecordingAgent.BUDGETS.clear()
        _BudgetRecordingAgent.PHASES.clear()
        _BudgetRecordingAgent.BY_INVOCATION.clear()
        _BudgetRecordingAgent.COUNT[0] = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), _BudgetRecordingAgent)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = load_manifest(ECHO_DIR)
            manifest = base.model_copy(update={"phases": [
                PhaseSpec(name=base.phases[0].name, deadline_seconds=declared),
                PhaseSpec(name="second", approval=True, deadline_seconds=declared),
            ]})
            await run_contract_battery(
                f"http://127.0.0.1:{server.server_address[1]}", manifest=manifest,
                scenario={"message": "x"}, settle_seconds=0.1, timeout=timeout,
            )
        finally:
            server.shutdown()
        return list(_BudgetRecordingAgent.BUDGETS)

    # A ceiling BELOW the declared budget clamps to the ceiling: the
    # battery never promises time it will not give.
    tight = await budgets(timeout=5.0, declared=300)
    assert tight and all(b == 5 for b in tight), tight

    # A ceiling ABOVE it leaves the manifest's own number alone, so the
    # clamp is a clamp and not a constant.
    loose = await budgets(timeout=600.0, declared=30)
    assert loose and all(b == 30 for b in loose), loose


class _LeaksIntoTheFreshAnchorAgent(BaseHTTPRequestHandler):
    """Its rerun fails; everything is per-invocation EXCEPT the fresh
    anchor, which also accepts the ORIGINAL invocation's token.

    The fresh anchor used to be appended to the issued list without being
    cross-probed at all, so that pair was never asked about and the agent
    passed — while the report claimed every pair (Codex round 12).
    """

    BY_INVOCATION: dict[str, str] = {}
    IS_RERUN: dict[str, bool] = {}
    IS_FRESH: dict[str, bool] = {}
    FIRST: list[str] = [""]
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        token = self._bearer()
        allowed = self.BY_INVOCATION.get(invocation) == token
        if not allowed and self.IS_FRESH.get(invocation) and token == self.FIRST[0]:
            allowed = True
        if not allowed:
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
            )
            self.wfile.write(
                b'event: failed\ndata: {"error": "no"}\n\n'
                if self.IS_RERUN.get(invocation) else
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        if self.COUNT[0] == 1:
            self.FIRST[0] = self._bearer()
        if (parsed.get("run") or {}).get("rerun"):
            self.IS_RERUN[invocation] = True
        # The fresh anchor ITSELF, not everything sharing its run id — the
        # next-phase invocation is built on that same run, and that pair
        # IS probed, so keying on the run id would put the leak somewhere
        # already covered and measure a different agent.
        if self.COUNT[0] == 3:
            self.IS_FRESH[invocation] = True
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_the_fresh_anchor_is_cross_probed_like_any_other_invocation():
    for store in (_LeaksIntoTheFreshAnchorAgent.BY_INVOCATION,
                  _LeaksIntoTheFreshAnchorAgent.IS_RERUN,
                  _LeaksIntoTheFreshAnchorAgent.IS_FRESH):
        store.clear()
    _LeaksIntoTheFreshAnchorAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LeaksIntoTheFreshAnchorAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=_gated_two_phase(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    assert _LeaksIntoTheFreshAnchorAgent.COUNT[0] == 6, (  # +1: round 32's same-phase second run; +1: the headerless traceparent probe
        _LeaksIntoTheFreshAnchorAgent.COUNT[0]
    )
    assert result.checks["token_binding"] == "fail", text
    assert ("events with the first invocation's token on fresh run 1's echo "
            "answered 200, not 401") in text, text
    assert ("output with the first invocation's token on fresh run 1's echo "
            "answered 200, not 401") in text, text


class _LegacyRunIdAgent(BaseHTTPRequestHandler):
    """Correct, and names its invocations with the pre-v1.1 ``run_id``.

    Its rerun fails, so the battery must start a fresh run to reach the
    remaining transition. ``_fresh_anchor`` read only ``invocation_id``
    while the first POST, the ordinary transition parser and
    ``container.py:302-308`` all accept either — so this agent was told it
    "would not start a fresh run" (Codex round 12).
    """

    BY_INVOCATION: dict[str, str] = {}
    IS_RERUN: dict[str, bool] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INVOCATION.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
            )
            self.wfile.write(
                b'event: failed\ndata: {"error": "no"}\n\n'
                if self.IS_RERUN.get(invocation) else
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        if (parsed.get("run") or {}).get("rerun"):
            self.IS_RERUN[invocation] = True
        self._json(201, {"run_id": invocation})      # the legacy spelling ONLY


@pytest.mark.asyncio
async def test_a_fresh_anchor_named_with_the_legacy_run_id_is_accepted():
    _LegacyRunIdAgent.BY_INVOCATION.clear()
    _LegacyRunIdAgent.IS_RERUN.clear()
    _LegacyRunIdAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LegacyRunIdAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=_gated_two_phase(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    assert "would not start a fresh run" not in text, text
    assert result.checks["token_binding"] == "pass", text
    # Four invocations: the original, its failed rerun, the fresh anchor,
    # and the next phase on that fresh run.
    assert _LegacyRunIdAgent.COUNT[0] == 6, _LegacyRunIdAgent.COUNT[0]  # +1: round 32's same-phase second run; +1: the headerless traceparent probe


def test_the_cli_exposes_the_timeout_the_authoring_guide_names():
    """`Container_Agents.md` tells authors to raise `--timeout`.

    It said so while argparse rejected the flag, which is an instruction
    that exits with an error (Codex round 12). Asserted against the parser
    itself rather than against the prose, so the two cannot drift apart
    without this failing.
    """
    import subprocess
    import sys as _sys

    guide = (Path(__file__).resolve().parents[2]
             / "docs" / "authoring" / "Container_Agents.md").read_text()
    assert "--timeout" in guide, "the guide no longer names the flag"

    # Against the CLI a user would actually type, not against private
    # shape: the parser is built inside main().
    out = subprocess.run(
        [_sys.executable, "-m", "adapter_kit.run_contract", "--help"],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=120,
    )
    assert "--timeout" in out.stdout, out.stdout + out.stderr


class _PacedAgent(BaseHTTPRequestHandler):
    """Conformant, but slow in one configurable leg of the exchange.

    One fixture with three dials rather than three near-identical agents:
    the defect under test is about WHICH legs the deadline covers, so the
    legs need to be moved independently, and three copies of a fixture
    drift exactly the way three copies of a rule do.
    """

    POST_DELAY = 0.0
    STREAM_DELAY = 0.0
    OUTPUT_DELAY = 0.0
    PROBE_DELAY = 0.0          # how slowly it answers an UNAUTHORIZED request
    HEALTHZ_DELAY = 0.0
    HEALTHZ_HITS: list[int] = [0]
    INLINE_OUTPUT = True
    BY_INVOCATION: dict[str, str] = {}
    COUNT: list[int] = [0]

    @classmethod
    def reset(cls, **dials):
        cls.POST_DELAY = cls.STREAM_DELAY = cls.OUTPUT_DELAY = 0.0
        cls.PROBE_DELAY = 0.0
        cls.HEALTHZ_DELAY = 0.0
        cls.HEALTHZ_HITS[0] = 0
        cls.INLINE_OUTPUT = True
        cls.BY_INVOCATION.clear()
        cls.COUNT[0] = 0
        for k, v in dials.items():
            setattr(cls, k, v)

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            self.HEALTHZ_HITS[0] += 1
            time.sleep(self.HEALTHZ_DELAY)
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INVOCATION.get(invocation) != self._bearer():
            time.sleep(self.PROBE_DELAY)
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            time.sleep(self.STREAM_DELAY)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            done = ({"output": {"echo": {"message": "x"}}}
                    if self.INLINE_OUTPUT else {})
            try:
                self.wfile.write(
                    b'event: progress\ndata: '
                    b'{"step_id": "only", "status": "running"}\n\n'
                    + b"event: completed\ndata: "
                    + json.dumps(done).encode() + b"\n\n"
                )
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path.endswith("/output"):
            time.sleep(self.OUTPUT_DELAY)
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(self.POST_DELAY)
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


async def _paced(deadline, **dials):
    """Run the battery against `_PacedAgent`, returning (result, elapsed)."""
    from app.agents.manifest import PhaseSpec

    _PacedAgent.reset(**dials)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PacedAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    started = time.monotonic()
    try:
        base = load_manifest(ECHO_DIR)
        manifest = base.model_copy(update={"phases": [
            PhaseSpec(name=base.phases[0].name, deadline_seconds=deadline),
        ]})
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}",
            manifest=manifest, scenario={"message": "x"},
            settle_seconds=0.1, timeout=30.0,
        )
    finally:
        elapsed = time.monotonic() - started
        server.shutdown()
    return result, elapsed


@pytest.mark.asyncio
async def test_a_slow_POST_spends_the_invocations_deadline():
    """The deadline covers the POST, which production wraps too.

    Round 13 started the guard inside the events read, so `POST /v1/runs`
    ran before it and the fallback after it, each bounded only by the
    client's global timeout. Measured: a 2s advertised deadline against an
    agent sleeping 8s in the POST, and the battery returned `completed:
    pass` after **24.8s** with no deadline failure at all (Codex round
    14) — three invocations, each spending eight unbudgeted seconds.

    The wall clock is asserted as well as the verdict: a battery that
    waited out all 8s and then complained would satisfy an assertion on
    `failures` while leaving the defect exactly where it was.
    """
    result, elapsed = await _paced(2, POST_DELAY=8.0)
    assert result.passed is False, result.summary()
    assert any("outran the 2s deadline" in f for f in result.failures), result.failures
    assert any("answering POST" in f for f in result.failures), result.failures
    assert elapsed < 6.0, f"waited {elapsed:.1f}s on a 2s budget"


@pytest.mark.asyncio
async def test_a_slow_OUTPUT_fetch_spends_the_invocations_deadline():
    """The other end of the exchange, and the other side of the guard.

    `completed` without an inline output is conformant — the contract puts
    the result at `/output` — so the fallback fetch is part of the
    invocation the chassis times, not an extra the battery does for free.
    """
    result, elapsed = await _paced(2, INLINE_OUTPUT=False, OUTPUT_DELAY=8.0)
    assert result.passed is False, result.summary()
    assert any("serving its output" in f for f in result.failures), result.failures
    assert elapsed < 6.0, f"waited {elapsed:.1f}s on a 2s budget"


@pytest.mark.asyncio
async def test_the_budget_is_CARRIED_across_the_legs_not_restarted():
    """One deadline for the exchange, not one deadline per leg.

    This is the wrong fix the finding invites: give the POST its own
    `asyncio.timeout(deadline)`, the stream another, the fallback a third.
    Every leg then passes on its own while the invocation as a whole runs
    to three times its budget — and production, which wraps the lot in a
    single `asyncio.timeout`, would have killed it.

    1.2s in the POST and 1.2s in the stream is 2.4s against a 2s budget:
    neither leg exceeds it alone, so only a carried budget fails this.
    """
    result, elapsed = await _paced(2, POST_DELAY=1.2, STREAM_DELAY=1.2)
    assert result.passed is False, result.summary()
    assert any("outran the 2s deadline" in f for f in result.failures), result.failures
    assert elapsed < 5.0, f"waited {elapsed:.1f}s on a 2s budget"


@pytest.mark.asyncio
async def test_the_batterys_own_probes_do_not_spend_the_agents_budget():
    """What the budget must NOT cover.

    The binding probes run BESIDE the events stream (round 16 moved them
    off the POST->stream seam, and this docstring said otherwise for three
    rounds) and production never makes them, so charging the agent for
    them would fail a conformant agent for the battery's own curiosity.

    The probes have to be SLOW for this to mean anything. The first
    version of this test left them instant, so the injection that charges
    one to the budget cost roughly nothing and the case came back
    `not-caught` — correctly: the property was real and the test could
    not see it. Here a single charged probe (1.0s) on top of the agent's
    own 0.3s would exceed the 1s budget, while the agent left alone
    spends less than a third of it.
    """
    result, _ = await _paced(
        1, POST_DELAY=0.15, STREAM_DELAY=0.15, PROBE_DELAY=1.0)
    assert result.passed is True, result.summary()
    assert not any("outran" in f for f in result.failures), result.failures


@pytest.mark.asyncio
async def test_a_subsecond_ceiling_is_refused_rather_than_rounded():
    """Advertised and enforced must not diverge, in either direction.

    `deadline_seconds` is a positive integer on the wire, so `--timeout
    0.5` truncates to a ceiling of 0, `phase_deadline` floors it to 1, and
    the battery promises a second while the client cuts the exchange off
    at half of one. Measured: an agent answering at 0.75s, inside the
    budget it was given, failed `events stream failed: ReadTimeout`
    (Codex round 14). Refused, not rounded — silently treating 0.5 as 1
    enforces a budget the caller never asked for.
    """
    from app.agents.manifest import PhaseSpec

    base = load_manifest(ECHO_DIR)
    manifest = base.model_copy(update={"phases": [
        PhaseSpec(name=base.phases[0].name, deadline_seconds=30)]})
    for bad in (0.5, 0.0, 0.999):
        with pytest.raises(ValueError, match="at least 1 second"):
            await run_contract_battery(
                "http://127.0.0.1:1", manifest=manifest,
                scenario={"message": "x"}, timeout=bad,
            )
    # And one second is accepted: the boundary is inclusive, or the
    # smallest budget the wire can express would be unusable.
    from adapter_kit.run_contract import phase_deadline
    assert phase_deadline(manifest.phases[0], ceiling=int(1.0)) == 1


@pytest.mark.asyncio
async def test_a_slow_healthz_spends_the_invocations_deadline():
    """Production charges `/healthz` to the phase, so the battery must.

    `ContainerAgent.run_phase` probes health before the POST
    (`container.py:261`) and the runner wraps all of `run_phase` in
    `asyncio.timeout(deadline)` (`agent_runner.py:513`). Probing it once,
    before any budget existed, let a 2s health endpoint pass a 1s phase
    that the chassis would have failed `PhaseDeadlineExceeded` (Codex
    round 15).
    """
    result, elapsed = await _paced(1, HEALTHZ_DELAY=2.0)
    assert result.passed is False, result.summary()
    assert any("answering /healthz" in f for f in result.failures), result.failures
    assert elapsed < 5.0, f"waited {elapsed:.1f}s on a 1s budget"


@pytest.mark.asyncio
async def test_health_is_probed_once_per_invocation_not_once_per_run():
    """The count, not just the charge.

    Production asks after the agent's health on every invocation it
    makes. The battery drove three and asked once, so a container healthy
    at the first probe and sick by the third was never noticed — the
    synthetic invocations skipped the check production never skips.
    """
    from app.agents.manifest import PhaseSpec

    _PacedAgent.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PacedAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = load_manifest(ECHO_DIR)
        manifest = base.model_copy(update={"phases": [
            PhaseSpec(name=base.phases[0].name, deadline_seconds=30),
            PhaseSpec(name="second", approval=True, deadline_seconds=30),
        ]})
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}",
            manifest=manifest, scenario={"message": "x"},
            settle_seconds=0.1, timeout=30.0,
        )
    finally:
        server.shutdown()

    assert result.passed is True, result.summary()
    invocations = _PacedAgent.COUNT[0]
    assert invocations >= 3, invocations
    assert _PacedAgent.HEALTHZ_HITS[0] == invocations, (
        f"{invocations} invocations but {_PacedAgent.HEALTHZ_HITS[0]} health probes"
    )


@pytest.mark.asyncio
async def test_an_unreachable_agent_still_reports_healthz_rather_than_a_deadline():
    """Moving the probe inside the budget must not blur the diagnosis.

    A container that is not listening fails at the transport instantly,
    so it is still `healthz unreachable` and `healthz: fail` — not a
    deadline overrun, which would send an author to look at their phase
    budget instead of at whether their agent is running.
    """
    from app.agents.manifest import PhaseSpec

    base = load_manifest(ECHO_DIR)
    manifest = base.model_copy(update={"phases": [
        PhaseSpec(name=base.phases[0].name, deadline_seconds=30)]})
    result = await run_contract_battery(
        "http://127.0.0.1:9", manifest=manifest,
        scenario={"message": "x"}, settle_seconds=0.1, timeout=30.0,
    )
    assert result.checks.get("healthz") == "fail", result.checks
    assert any("healthz unreachable" in f for f in result.failures), result.failures
    assert not any("outran" in f for f in result.failures), result.failures


class _BackgroundWorker(BaseHTTPRequestHandler):
    """Begins work when its POST returns; finishes WORK_SECONDS later.

    The completion time is fixed at the POST, not at the moment the stream
    is opened, which is what "background execution" means and what makes a
    paused clock visible.
    """

    WORK_SECONDS = 5.0
    # Small ON PURPOSE since round 31. The probes no longer overlap the
    # invocation — nothing is sent until its last leg closes — so this
    # delay is a pure TAIL on the battery's wall clock rather than part
    # of what the test below measures. At 0.6s it added ~5s of tail and
    # the elapsed bound stopped discriminating "stopped at the deadline"
    # from "waited out the agent's 5s of work"; the two totals came
    # within 2s of each other. Shrinking the irrelevant term is what
    # keeps that bound sharp — the property is asserted by
    # WORK_SECONDS (5.0) against the 3s deadline, which is untouched:
    # a battery that waited the agent's work out still takes >= 5s and
    # still trips the 4.5s bound, which is not moved.
    PROBE_DELAY = 0.0
    STARTED_AT: dict[str, float] = {}
    BY_INVOCATION: dict[str, str] = {}
    COUNT: list[int] = [0]

    @classmethod
    def reset(cls):
        cls.STARTED_AT.clear()
        cls.BY_INVOCATION.clear()
        cls.COUNT[0] = 0

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INVOCATION.get(invocation) != self._bearer():
            time.sleep(self.PROBE_DELAY)
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(
                    b'event: progress\ndata: '
                    b'{"step_id": "only", "status": "running"}\n\n')
                self.wfile.flush()
                left = (self.STARTED_AT.get(invocation, time.monotonic())
                        + self.WORK_SECONDS) - time.monotonic()
                if left > 0:
                    time.sleep(left)
                self.wfile.write(
                    b'event: completed\ndata: '
                    b'{"output": {"echo": {"message": "x"}}}\n\n')
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        self.STARTED_AT[invocation] = time.monotonic()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_background_work_stays_on_the_deadline_clock():
    """The clock does not pause while the agent keeps working.

    Round 14's budget charged only the time spent INSIDE its segments, so
    the interval holding the battery's binding probes was free. An agent
    that begins work when its POST returns goes on working through that
    interval, and measured: a 5s invocation passed a 3s deadline with
    `completed: pass` (Codex round 16). Production's
    `asyncio.timeout(deadline)` never pauses.

    The wall clock is asserted too: the point is that the battery stops
    at the deadline, not that it complains after waiting the full 5s.
    """
    from app.agents.manifest import PhaseSpec

    _BackgroundWorker.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BackgroundWorker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    started = time.monotonic()
    try:
        base = load_manifest(ECHO_DIR)
        manifest = base.model_copy(update={"phases": [
            PhaseSpec(name=base.phases[0].name, deadline_seconds=3)]})
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}",
            manifest=manifest, scenario={"message": "x"},
            settle_seconds=0.1, timeout=60.0,
        )
    finally:
        elapsed = time.monotonic() - started
        server.shutdown()

    assert result.passed is False, result.summary()
    assert any("outran the 3s deadline" in f for f in result.failures), result.failures
    assert elapsed < 4.5, f"waited {elapsed:.1f}s on a 3s deadline"


@pytest.mark.asyncio
async def test_a_cancelled_probe_batch_cannot_pass_the_binding_check(monkeypatch):
    """A probe that never ran must not vanish from the verdict.

    `unbound` is a comprehension over the keys that EXIST in `bindings`,
    so before this fix a probe cancelled by the collection window wrote
    no key, was therefore not unbound, and disappeared. Measured against
    an agent answering four correct-but-slow 401s under a 3s window:
    `token_binding: pass`, `passed: True`, **no failures at all**, on a
    check that ran on half of what it claims to cover (Codex round 17,
    P1) — a gate reporting success by not looking, which is the one thing
    CLAUDE.md forbids, introduced by round 16's own concurrency change.

    The agent here NEVER answers an unauthorized request, so every probe
    is cut at its own ceiling. It used to be a CONFORMANT agent that
    answered 401 only slowly — and round 29 established that such an
    agent must NOT be cut at all, because `run_probes` is serial and the
    batch was bounded as though it were one request. So the fixture moved
    to an agent that really does fail the obligation, and the property
    under test is unchanged: nothing a probe was meant to check may
    vanish from the verdict.

    The ceiling is monkeypatched down. It is a transport ceiling, not a
    check — the same kind of number as `_HEALTHZ_TIMEOUT` — and twelve
    serial probes at the real five seconds would put a minute on this
    file for nothing. The mechanism is identical at either value.
    """
    from app.agents.manifest import PhaseSpec
    import adapter_kit.run_contract as _rc

    monkeypatch.setattr(_rc, "_PROBE_CEILING_SECONDS", 0.4)
    _PacedAgent.reset(PROBE_DELAY=30.0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PacedAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = load_manifest(ECHO_DIR)
        manifest = base.model_copy(update={"phases": [
            PhaseSpec(name=base.phases[0].name, deadline_seconds=30)]})
        with _collector_paused():
            result = await run_contract_battery(
                f"http://127.0.0.1:{server.server_address[1]}",
                manifest=manifest, scenario={"message": "x"},
                settle_seconds=0.1, timeout=3.0,
            )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] != "pass", result.summary()
    assert result.passed is False, result.summary()
    # Both halves of the remedy, and both SEEDING SITES, asserted
    # separately. A single `any("never answered")` over every failure was
    # satisfied by whichever seeding survived, so removing either one
    # alone went uncaught — the injections said so and were right. The
    # main probes and the cross-use probes are seeded in different
    # places; a test that cannot tell them apart cannot guard them.
    unanswered = [f for f in result.failures if "never answered at all" in f]
    # EVERY ONE SAYS WHY. The batch used to be cancelled wholesale and the
    # probes recorded only as "cancelled before the agent answered"; now
    # each is cut at its own ceiling and names that, which is strictly
    # more than the line it replaces. A probe that vanished entirely
    # would appear in neither form, which is what round 17 was about.
    assert unanswered, result.failures
    assert all("a binding probe is given" in f for f in unanswered), unanswered
    # PARTITIONED BY SEEDING SITE, and the partition has to be exact.
    # Matching "without a token" anywhere used to identify the first
    # invocation's own set — and round 18 gave the LATER invocations that
    # same pair, so the test would have been satisfied by a cross-use
    # seeding that survived while the first invocation's vanished. A
    # partition that stops discriminating is a test that stopped
    # testing; the name it matches on is now the invocation's.
    own = [f for f in unanswered
           if f.startswith(("events on the first invocation ",
                            "output on the first invocation "))]
    crossed = [f for f in unanswered if "token on" in f]
    later = [f for f in unanswered
             if f.startswith(("events on another invocation ",
                              "output on another invocation "))]
    assert own, f"no unanswered probe from the invocation's own set: {result.failures}"
    assert crossed, f"no unanswered probe from the cross-use set: {result.failures}"
    assert later, (
        f"no unanswered tokenless probe from the second invocation: "
        f"{result.failures}"
    )


class _LaxOnLaterInvocationsAgent(BaseHTTPRequestHandler):
    """Strict on its first invocation, `if header and header != expected:
    401` on every one after it — so a LATER invocation's events and
    output are readable with no Authorization header at all.

    Every bearer this battery can show a later invocation is a token the
    agent itself minted for some other invocation, and this agent refuses
    all of them: the cross-use probes all answer 401. The two probes that
    catch it need no issued token, and they used to be asked of the first
    invocation only (Codex round 18). Measured before the fix:
    `token_binding: pass`, `passed: True`, no failures, while a tokenless
    GET of the second invocation's events and output each returned 200.

    Its rerun FAILS, so the battery re-anchors and the later invocations
    are a rerun, a fresh anchor, and that fresh run's next phase — every
    kind this battery starts.
    """

    BY_INVOCATION: dict[str, str] = {}
    IS_RERUN: dict[str, bool] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def _allowed(self, invocation):
        expected, token = self.BY_INVOCATION.get(invocation), self._bearer()
        if invocation == "inv-1":
            return token is not None and token == expected
        # The defect, and a plausible one: a guard that validates the
        # bearer it was given without checking that it was given one.
        return token is None or token == expected

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if not self._allowed(invocation):
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
            )
            self.wfile.write(
                b'event: failed\ndata: {"error": "no"}\n\n'
                if self.IS_RERUN.get(invocation) else
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        if (parsed.get("run") or {}).get("rerun"):
            self.IS_RERUN[invocation] = True
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_every_started_invocation_is_probed_without_a_token():
    """The tokenless pair is asked of every invocation, not just the first.

    A rerun, a fresh anchor and a next phase are all started here, and
    each must be named. The agent is CORRECT on every probe that carries
    a token — which is every probe the battery had for a later
    invocation before round 18 — so a report that says nothing here is a
    report of a check that never asked.
    """
    for store in (_LaxOnLaterInvocationsAgent.BY_INVOCATION,
                  _LaxOnLaterInvocationsAgent.IS_RERUN):
        store.clear()
    _LaxOnLaterInvocationsAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LaxOnLaterInvocationsAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=_gated_two_phase(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    assert _LaxOnLaterInvocationsAgent.COUNT[0] == 6, (  # +1: round 32's same-phase second run; +1: the headerless traceparent probe
        _LaxOnLaterInvocationsAgent.COUNT[0]
    )
    assert result.checks["token_binding"] == "fail", text
    # EVERY KIND, named separately: fixing the instance in front of me is
    # how the next round gets its finding (§12 181).
    for noun in ("the echo rerun", "fresh run 1's echo", "the second phase"):
        for leaf in ("events", "output"):
            assert f"{leaf} on {noun} without a token answered 200, not 401" in text, text
    # Pinned to the missing bearer, not to a blanket refusal to answer:
    # this agent rejects every token it did not issue, so a foreign-token
    # failure here would mean the fixture, not the battery, is what the
    # assertions above are measuring.
    assert "with a foreign token answered" not in text, text


@pytest.mark.asyncio
async def test_the_probes_do_not_eat_the_OUTPUT_fetchs_budget():
    """The same rule, on the leg that comes after the probes are collected.

    `completed` without inline output is conformant — the contract puts
    the result at `/output` — so the battery fetches it, inside the
    invocation's budget, because production does. The probes were
    collected between the stream and that fetch, and the budget is
    absolute wall clock, so their wait was charged to the agent.
    Measured: an agent whose POST, stream and `/output` together took
    1.0s against a 5s deadline, failed with *the invocation outran the 5s
    deadline it was advertised while serving its output* — for a request
    the battery never made, because four 1.5s probes had spent the
    budget first (Codex round 19).

    The sibling test above leaves `INLINE_OUTPUT` on, so it exercises the
    same rule on a path with no fallback fetch at all; that is why it
    stayed green through this defect. The probes must be SLOW here for
    the same reason they are there: an instant probe charges nothing.
    """
    result, _ = await _paced(3, PROBE_DELAY=1.2, INLINE_OUTPUT=False)
    assert result.checks["completed"] == "pass", result.summary()
    assert not any("outran" in f for f in result.failures), result.failures
    # The output was actually recovered, not merely un-failed: a battery
    # that skipped the fetch would also report no deadline failure.
    assert result.output == {"echo": {"message": "x"}}, result.output


class _NoUsableOutputOnTheSecondAgent(BaseHTTPRequestHandler):
    """Correct on its first invocation and on every token; its SECOND says
    `completed` with no inline output and then cannot serve `/output`.

    Production raises `ContainerAgentError` for a non-200, a non-JSON body
    and a non-object `output` (`container.py:506-531`), so the chassis
    could never have consumed such an invocation. The battery counted it
    drained the moment its events stream answered 200 and swallowed every
    way the fetch could fail: measured `token_binding: pass`, `passed:
    True`, no failures (Codex round 20).
    """

    MODE = "status"          # status | nonjson | nonobject
    BY_INVOCATION: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, code, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        token = self._bearer()
        if token is None or self.BY_INVOCATION.get(invocation) != token:
            return self._json(401, {"error": "unauthorized"})
        first = invocation == "inv-1"
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            done = {"output": {"echo": {"message": "x"}}} if first else {}
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                + b"event: completed\ndata: " + json.dumps(done).encode() + b"\n\n"
            )
            return
        if self.path.endswith("/output"):
            if first:
                return self._json(200, {"output": {"echo": {"message": "x"}}})
            if self.MODE == "status":
                return self._json(503, {"error": "not ready"})
            if self.MODE == "nonjson":
                return self._raw(200, b"<html>not json</html>")
            return self._json(200, {"output": "a string, not an object"})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, named",
    [("status", "`/output` answered 503"),
     ("nonjson", "a body that is not JSON"),
     ("nonobject", "`output` is str, not a JSON object")],
)
async def test_an_invocation_that_cannot_supply_its_output_is_not_drained(mode, named):
    """Every way the fallback fetch can fail, named — not swallowed.

    All three are `ContainerAgentError` in production. The agent here is
    correct on its first invocation and on every token probe, so a
    battery that counts the second invocation drained reports an overall
    PASS on an agent the chassis could not have run.
    """
    _NoUsableOutputOnTheSecondAgent.BY_INVOCATION.clear()
    _NoUsableOutputOnTheSecondAgent.COUNT[0] = 0
    _NoUsableOutputOnTheSecondAgent.MODE = mode
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NoUsableOutputOnTheSecondAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}",
            manifest=load_manifest(ECHO_DIR), scenario={"message": "x"},
            settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    assert _NoUsableOutputOnTheSecondAgent.COUNT[0] == 3, (  # +1: the headerless traceparent probe
        _NoUsableOutputOnTheSecondAgent.COUNT[0]
    )
    assert result.passed is False, text
    # NOT drained: `drained != len(transitions)` is what records that the
    # binding check did not conclude over every producible transition.
    assert result.checks["token_binding"] == "fail", text
    assert "could not have consumed that invocation" in text, text
    assert named in text, text


class _FreshAnchorWithoutOutputAgent(BaseHTTPRequestHandler):
    """Its rerun fails, and the fresh run the battery starts to recover
    says `completed` with no output and cannot serve `/output`.

    The same defect one instance over: the anchor used to carry `{}`
    forward silently, which made the next transition's `prior_output` a
    fiction as well. Codex named the drain; this is the other site
    (§12 181 — enumerate the instances).
    """

    MODE = "output503"       # output503 | noterminal | nonobject
    BY_INVOCATION: dict[str, str] = {}
    IS_RERUN: dict[str, bool] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        token = self._bearer()
        if token is None or self.BY_INVOCATION.get(invocation) != token:
            return self._json(401, {"error": "unauthorized"})
        fresh = invocation == "inv-3"
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
            )
            if self.IS_RERUN.get(invocation):
                self.wfile.write(b'event: failed\ndata: {"error": "no"}\n\n')
            elif fresh:
                if self.MODE == "noterminal":
                    return
                done = {"output": []} if self.MODE == "nonobject" else {}
                self.wfile.write(
                    b"event: completed\ndata: " + json.dumps(done).encode() + b"\n\n")
            else:
                self.wfile.write(
                    b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
                )
            return
        if self.path.endswith("/output"):
            if fresh and self.MODE == "output503":
                return self._json(503, {"error": "not ready"})
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        if (parsed.get("run") or {}).get("rerun"):
            self.IS_RERUN[invocation] = True
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, named",
    [("output503", "`completed` carried no output and `/output` answered 503"),
     ("noterminal", "its events stream ended without a terminal event"),
     ("nonobject",
      "`completed` carried an `output` that is list, not a JSON object")],
)
async def test_a_fresh_anchor_that_cannot_supply_its_output_is_no_anchor(mode, named):
    """And the report says which of the two happened.

    "The agent would not start a fresh run" is the right sentence for a
    refusal and the wrong one for a run that started, streamed and then
    could not produce a result — an author sent after the wrong defect
    is a report that cost them the round.
    """
    _FreshAnchorWithoutOutputAgent.BY_INVOCATION.clear()
    _FreshAnchorWithoutOutputAgent.IS_RERUN.clear()
    _FreshAnchorWithoutOutputAgent.COUNT[0] = 0
    _FreshAnchorWithoutOutputAgent.MODE = mode
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FreshAnchorWithoutOutputAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}",
            manifest=_gated_two_phase(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    assert result.passed is False, text
    assert result.checks["token_binding"] == "fail", text
    assert "the fresh run meant to reach the remaining transition(s)" in text, text
    assert named in text, text
    assert "would not start a fresh run" not in text, text


class _BadTerminalOnTheSecondAgent(BaseHTTPRequestHandler):
    """Correct on its first invocation; its second breaks the contract in
    one of two ways that the battery used to read as "no inline output".

    `noterminal` — the stream ends with neither `completed` nor `failed`.
    Production raises "events stream ... ended without a terminal event"
    (`container.py:410-414`); the battery counted it drained.

    `nonobject` — `completed` carries `output: []`. Production falls back
    to `/output` ONLY when the event's output is `None`
    (`container.py:324-328`) and `_to_result` rejects any other
    non-object; the battery's shape filter made `[]` indistinguishable
    from absent, so it fetched a valid object from `/output` and accepted
    the invocation (Codex round 21).
    """

    MODE = "noterminal"
    BY_INVOCATION: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        token = self._bearer()
        if token is None or self.BY_INVOCATION.get(invocation) != token:
            return self._json(401, {"error": "unauthorized"})
        first = invocation == "inv-1"
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
            )
            if not first and self.MODE == "noterminal":
                return
            done = ({"output": {"echo": {"message": "x"}}} if first
                    else {"output": []})
            self.wfile.write(
                b"event: completed\ndata: " + json.dumps(done).encode() + b"\n\n"
            )
            return
        if self.path.endswith("/output"):
            # Valid here on purpose: the point is that the battery must
            # not come looking.
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, named",
    [("noterminal", "its events stream ended without a terminal event"),
     ("nonobject", "`completed` carried an `output` that is list, not a JSON object")],
)
async def test_a_transition_the_chassis_could_not_consume_is_not_drained(mode, named):
    """Two contract rules the drain did not apply, both PASSING before.

    The agent is correct on its first invocation and on every token
    probe, so nothing else in the battery objects; `/output` is valid,
    which is what made the `nonobject` case pass — the battery went
    looking where production would not have.
    """
    _BadTerminalOnTheSecondAgent.BY_INVOCATION.clear()
    _BadTerminalOnTheSecondAgent.COUNT[0] = 0
    _BadTerminalOnTheSecondAgent.MODE = mode
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BadTerminalOnTheSecondAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}",
            manifest=load_manifest(ECHO_DIR), scenario={"message": "x"},
            settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    assert result.passed is False, text
    assert result.checks["token_binding"] == "fail", text
    assert named in text, text
    assert "could not have consumed that invocation" in text, text


class _NeverAnswersUnauthorizedAgent(BaseHTTPRequestHandler):
    """Accepts the connection for an unauthorized request and never
    answers it. Its own legs are fast, and its rerun fails, so the
    battery takes the re-anchor path.

    `cross_use` runs its plan sequentially, so an unbounded batch costs
    the client timeout PER PROBE — twelve of them once two invocations
    exist to cross-use. Measured at 84s against a 2s timeout, which at a
    production timeout is hours of hanging instead of the failing verdict
    the battery is supposed to produce (Codex round 21).
    """

    HANG_SECONDS = 30.0
    BY_INVOCATION: dict[str, str] = {}
    IS_RERUN: dict[str, bool] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        token = self._bearer()
        if token is None or self.BY_INVOCATION.get(invocation) != token:
            time.sleep(self.HANG_SECONDS)
            return
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
            )
            self.wfile.write(
                b'event: failed\ndata: {"error": "no"}\n\n'
                if self.IS_RERUN.get(invocation) else
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        if (parsed.get("run") or {}).get("rerun"):
            self.IS_RERUN[invocation] = True
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_the_fresh_anchors_probe_batch_is_bounded_like_the_others(monkeypatch):
    """Every probe batch is bounded, including the third one.

    Asserted on what distinguishes bounded from unbounded: every probe of
    the third batch is recorded with the reason it did not answer, rather
    than each having run to the client timeout. Round 29 moved the bound
    from the batch to the probe — the batch bound is now a backstop with
    headroom, so a hanging agent is caught by the per-probe ceiling and
    every probe says so by name instead of a whole batch collapsing into
    one line. That is more information, not less; the thing being
    guarded, that this third batch is bounded at all, is the same.
    """
    import adapter_kit.run_contract as _rc

    monkeypatch.setattr(_rc, "_PROBE_CEILING_SECONDS", 0.4)
    _NeverAnswersUnauthorizedAgent.BY_INVOCATION.clear()
    _NeverAnswersUnauthorizedAgent.IS_RERUN.clear()
    _NeverAnswersUnauthorizedAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NeverAnswersUnauthorizedAgent)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with _collector_paused():
            result = await run_contract_battery(
                f"http://127.0.0.1:{server.server_address[1]}",
                manifest=_gated_two_phase(load_manifest(ECHO_DIR)),
                scenario={"message": "x"}, settle_seconds=0.1, timeout=2.0,
            )
    finally:
        server.shutdown()

    anchor = [f for f in result.failures
              if "fresh run 1's echo" in f and "never answered" in f]
    assert anchor, result.failures
    assert all("a binding probe is given" in f for f in anchor), anchor
    assert result.passed is False, result.summary()


class _RecordingTraceAgent(BaseHTTPRequestHandler):
    """Conformant, and records the run id and `traceparent` of every POST.

    Production opens a new phase span around every `run_phase`
    (`agent_runner.py:439-440`) and injects whatever span is current
    (`container.py:121-125`), so invocations of one run share its trace id
    under a fresh parent span, and a second run — being a different run —
    is a different trace (S4-B). The battery replayed one traceparent byte
    for byte, so a stateful agent that refuses to be handed the same
    parent twice answered 400 and was reported "per-invocation binding is
    UNPROVEN" for a request shape the chassis never sends (Codex round 22).
    """

    SEEN: list[tuple[str, str]] = []
    BY_INVOCATION: dict[str, str] = {}
    IS_RERUN: dict[str, bool] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        token = self._bearer()
        if token is None or self.BY_INVOCATION.get(invocation) != token:
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        run = parsed.get("run") or {}
        self.SEEN.append((run.get("id"), self.headers.get("traceparent") or ""))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        if run.get("rerun"):
            self.IS_RERUN[invocation] = True
        self._json(201, {"invocation_id": invocation})


async def _traced(manifest):
    for store in (_RecordingTraceAgent.SEEN, _RecordingTraceAgent.BY_INVOCATION,
                  _RecordingTraceAgent.IS_RERUN):
        store.clear()
    _RecordingTraceAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingTraceAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}", manifest=manifest,
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()
    # THE HEADERLESS PROBE IS EXCLUDED HERE AND ASSERTED INSTEAD. It
    # carries no `traceparent` by design, so there is no trace id
    # or parent span to read off it — and a helper that silently dropped
    # it would let the probe stop being headerless without anything
    # noticing, which is the failure mode the probe exists to prevent.
    headerless = [e for e in _RecordingTraceAgent.SEEN if not e[1]]
    assert len(headerless) == 1, _RecordingTraceAgent.SEEN
    assert headerless[0][0] == "battery-run-headerless", headerless
    seen = [(run_id, tp.split("-")[1], tp.split("-")[2])
            for run_id, tp in _RecordingTraceAgent.SEEN if tp]
    return result, seen


@pytest.mark.asyncio
async def test_each_invocation_of_one_run_shares_its_trace_under_a_new_parent():
    """One trace per run, one parent span per invocation.

    A gated manifest gives three invocations of the SAME run — the phase,
    its rerun, and the next phase — so all three carry one trace id, and
    each must carry its own parent span.
    """
    result, seen = await _traced(_gated_two_phase(load_manifest(ECHO_DIR)))
    assert result.passed is True, result.summary()
    # Round 32 added a same-phase SECOND RUN to every plan, so `seen`
    # carries a fourth invocation belonging to `battery-run-2`. The
    # property here is about ONE run, so it is read over that run's
    # invocations — and the extra one is then asserted to be a different
    # run with a different trace, which is the same claim from the other
    # side and more than the test used to make.
    assert len(seen) == 4, seen
    of_run = [e for e in seen if e[0] == "battery-run"]
    assert len(of_run) == 3, seen
    assert len({t for _r, t, _s in of_run}) == 1, f"one run, one trace: {seen}"
    assert len({s for _r, _t, s in of_run}) == 3, f"three parents wanted: {seen}"
    other = [e for e in seen if e[0] != "battery-run"]
    assert [e[0] for e in other] == ["battery-run-2"], seen
    assert other[0][1] != of_run[0][1], (
        f"a different run must be a different trace: {seen}")


@pytest.mark.asyncio
async def test_a_second_run_is_a_second_trace():
    """A different run is a different trace, as it is in production.

    The single-phase manifest's only producible transition is a second
    run, and the battery used to hand it the first run's trace id — the
    one thing S4-B says never happens.
    """
    result, seen = await _traced(load_manifest(ECHO_DIR))
    assert result.passed is True, result.summary()
    assert len(seen) == 2, seen
    assert [run_id for run_id, _t, _s in seen] == ["battery-run", "battery-run-2"], seen
    assert len({t for _r, t, _s in seen}) == 2, f"two runs, two traces: {seen}"
    assert len({s for _r, _t, s in seen}) == 2, seen
    # And the battery still knows which trace belongs to which invocation.
    assert set(result.traces.values()) >= {t for _r, t, _s in seen}, result.traces


class _PiiOnALaterInvocationAgent(BaseHTTPRequestHandler):
    """Clean on its first invocation; a later one returns an output the
    chassis walk refuses — a Luhn-valid card number as a JSON number.

    `agent_runner.py:556-562` walks EVERY phase's terminal output before
    persisting it, and a refusal ends that run `error` with
    `pii_in_output`. The battery walked `result.output` — the invocation
    under test — and nothing else, so a second run or a fresh anchor could
    return one the chassis would have refused and still be counted
    drained, and the anchor's was then carried into the next phase as
    `prior_output` (Codex round 23).
    """

    WHERE = "second"         # second (inv-2) | anchor (inv-3)
    BY_INVOCATION: dict[str, str] = {}
    IS_RERUN: dict[str, bool] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        token = self._bearer()
        if token is None or self.BY_INVOCATION.get(invocation) != token:
            return self._json(401, {"error": "unauthorized"})
        dirty = invocation == ("inv-2" if self.WHERE == "second" else "inv-3")
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
            )
            if self.IS_RERUN.get(invocation):
                self.wfile.write(b'event: failed\ndata: {"error": "no"}\n\n')
                return
            done = ({"output": {"echo": {"card": 4111111111111111}}} if dirty
                    else {"output": {"echo": {"message": "x"}}})
            self.wfile.write(
                b"event: completed\ndata: " + json.dumps(done).encode() + b"\n\n")
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        if (parsed.get("run") or {}).get("rerun"):
            self.IS_RERUN[invocation] = True
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
@pytest.mark.parametrize("where, gated", [("second", False), ("anchor", True)])
async def test_every_invocations_output_is_boundary_walked(where, gated):
    """The walk production runs over every phase, run over every invocation.

    The invocation under test is clean, so nothing else in the battery
    objects — the old single-walk check passed both of these.
    """
    _PiiOnALaterInvocationAgent.BY_INVOCATION.clear()
    _PiiOnALaterInvocationAgent.IS_RERUN.clear()
    _PiiOnALaterInvocationAgent.COUNT[0] = 0
    _PiiOnALaterInvocationAgent.WHERE = where
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PiiOnALaterInvocationAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}",
            manifest=_gated_two_phase(manifest) if gated else manifest,
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    assert result.passed is False, text
    # The invocation under test is clean, so THIS check must still pass —
    # a battery that failed everything would satisfy a weaker assertion.
    assert result.checks["output"] == "pass", text
    assert result.checks["token_binding"] == "fail", text
    assert "`pii_in_output`: number at $.echo.card (CREDIT_CARD)" in text, text
    # Never the value itself, in any failure the battery writes.
    assert "4111111111111111" not in text, text
    if where == "second":
        # And the consequence named is production's for THIS defect.
        assert "ends that run `error` with `pii_in_output`" in text, text
        assert "raises ContainerAgentError" not in text, text


@pytest.mark.asyncio
async def test_the_battery_does_not_block_the_loop_its_probes_run_on(
    monkeypatch, request,
):
    """The battery's own work must not starve its own requests.

    `pii_service.walk` loads models on first use — 3.42s measured, then
    4ms. Round 23 moved the first walk out of the end of the battery and
    into the transition loop, where the cross-use probe batch is in
    flight, and blocked the loop for 3.52s: CI answered `events with
    another invocation's token on the first invocation never answered at
    all: ConnectTimeout('')` with every check otherwise passing. Local
    tests were green, because nothing here measured the loop.

    The fix is two rules in `run_contract.py`: the walker is warmed in a
    worker thread before the battery sends anything, and every walk
    after that runs in a worker thread too. Each is asserted as what it
    is:

    * STRUCTURE, with no clock in it. Every `pii_service.walk` call is
      recorded with its thread and its place among the agent's requests.
      Every walk must run on a thread other than the loop's, and the
      walks that pay the load — the warm-up, and any walk that finds the
      walker cold — must return before the agent receives its first
      request. So a warm-up moved onto the loop is caught, and so is a
      warm 4ms walk moved onto it, which no heartbeat bound can see.
    * TIMING, over the probe window only. A heartbeat ticking every 50ms
      records how long the loop went without running it, counted from
      the moment the window opens: the warm-up returning or the agent's
      first request, whichever comes first. The bound is deliberately
      loose — this is a "seconds, not milliseconds" regression, and a
      tight bound on a shared CI runner would be noise.

    THE WINDOW IS WHAT CHANGED (#159). The heartbeat used to span the
    whole run, warm-up included, and deserializing spaCy's vocabulary
    holds the GIL in the worker thread: the loop thread stalls there
    while nothing is waiting on it. Measured on a four-core sandbox:
    0.59-0.73s alone, 1.505s in a full-suite run that failed this bound,
    and 0.05s — one tick — after the warm-up had returned, in every run.
    Every local failure on record, 1.50s to 1.71s, is that stall's size,
    and the one reproduced was that stall: charged to probes that did not
    exist yet.
    """
    # A COLD WALKER, or this test cannot see the thing it is named for.
    # The analyzer is process-global and lazily built, so by the time this
    # test runs in the full file an earlier test has already paid the 3.4s
    # load and an injected on-loop walk costs 4ms — which is exactly what
    # the injection reported: `not-caught`, and right about the test.
    # Dropping the handle puts the load back inside the battery run, where
    # the property lives (§12 190(f)). The thread assertion no longer
    # needs it; the ordering and the timing still do, because both are
    # about where the LOAD is paid.
    #
    # THROUGH MONKEYPATCH, which raises if the handle is gone. A plain
    # assignment to a renamed `_analyzer` makes a stray attribute that
    # stays None, so every walk reads as cold and the ordering assertion
    # blames a mid-run load that never happened — a failure pointing the
    # wrong way. The handle is put back afterwards, too, as
    # `test_pii_fail_closed.py` puts back every module global it touches.
    from app.services import pii_service as _pii
    monkeypatch.setattr(_pii, "_analyzer", None)

    _RecordingTraceAgent.SEEN.clear()
    _RecordingTraceAgent.BY_INVOCATION.clear()
    _RecordingTraceAgent.IS_RERUN.clear()
    _RecordingTraceAgent.COUNT[0] = 0

    # ONE LEDGER, in the order things happened, appended under one lock
    # by the walker's threads and the agent's. "Returned before the first
    # request" is read off the order of two entries, not off two clocks;
    # the times are for the window alone.
    ledger: list[tuple[str, float]] = []
    walks: list[dict] = []
    lock = threading.Lock()
    loop_thread = threading.get_ident()
    real_walk = _pii.walk

    def walk(value, *args, **kwargs):
        cold = _pii._analyzer is None
        try:
            return real_walk(value, *args, **kwargs)
        finally:
            with lock:
                walks.append({
                    "warm_up": value == {"warm": "up"}, "cold": cold,
                    "on_loop": threading.get_ident() == loop_thread,
                    "returned": len(ledger),
                })
                ledger.append(("walk", time.monotonic()))

    monkeypatch.setattr(_pii, "walk", walk)

    class _LedgeredAgent(_RecordingTraceAgent):
        def _arrived(self):
            with lock:
                ledger.append(("request", time.monotonic()))

        def do_GET(self):
            self._arrived()
            super().do_GET()

        def do_POST(self):
            self._arrived()
            super().do_POST()

    ticks = [time.monotonic()]

    async def heartbeat():
        while True:
            await asyncio.sleep(0.05)
            ticks.append(time.monotonic())

    beat = asyncio.create_task(heartbeat())
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LedgeredAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}",
            manifest=load_manifest(ECHO_DIR), scenario={"message": "x"},
            settle_seconds=0.1,
        )
        closed = time.monotonic()
        # ONE MORE TICK before the heartbeat goes. A block in the run's
        # last moments ends no tick until the battery has returned, and
        # cancelling here would throw away the one sample that saw it.
        # Timers run in deadline order, so the heartbeat's — due within
        # 50ms — runs before this one.
        await asyncio.sleep(0.1)
    finally:
        beat.cancel()
        server.shutdown()

    arrivals = [i for i, (kind, _) in enumerate(ledger) if kind == "request"]
    warm_ups = [w for w in walks if w["warm_up"]]
    mid_run = [w for w in walks if not w["warm_up"]]

    # THE PROBE WINDOW opens at the first of: the warm-up returning, the
    # agent receiving a request. Nothing can be waiting on this loop
    # before that — the battery sends its first request (`/healthz`) only
    # once the warm-up has returned, and the relay and MCP recorders it
    # may be handed serve from threads of their own — so a stall there
    # starves nothing. It closes when the battery returns. A gap that
    # straddles the opening counts only the part inside.
    opened = min(
        [ledger[w["returned"]][1] for w in warm_ups]
        + [ledger[i][1] for i in arrivals[:1]],
        default=None,
    )
    gaps = list(zip(ticks, ticks[1:]))
    samples = ([(a, b) for a, b in gaps if b > opened and a < closed]
               if opened is not None else [])
    worst = max((min(b, closed) - max(a, opened) for a, b in samples),
                default=0.0)
    # In the JUnit report: what this run measured, and what the whole-run
    # heartbeat would have charged it.
    request.node.user_properties.append(
        ("probe_window_worst_gap_s", round(worst, 3)))
    request.node.user_properties.append(
        ("whole_run_worst_gap_s",
         round(max((b - a for a, b in gaps), default=0.0), 3)))

    problems = []
    if not result.passed:
        problems.append(f"the battery did not pass:\n{result.summary()}")
    # NOTHING BELOW MAY PASS BY MEASURING NOTHING. Each of these names
    # what an assertion after it is about, and fails when it is absent.
    if len(warm_ups) != 1 or not mid_run:
        problems.append(
            f"the walk wrapper saw {len(warm_ups)} warm-up call(s) and "
            f"{len(mid_run)} mid-run walk(s), not exactly one and at least "
            f"one: a walk it cannot see is one the thread assertion cannot "
            f"check")
    if not arrivals:
        problems.append(
            "the agent received no requests, so no probe was ever in flight "
            "and nothing was ordered against one")
    if not any(w["cold"] for w in walks):
        problems.append(
            "no walk the wrapper saw found the walker cold: the load was "
            "paid somewhere this test cannot see, so neither the ordering "
            "nor the timing below is about it")
    if not samples:
        problems.append(
            "the heartbeat took no samples inside the probe window, so the "
            "timing assertion measured nothing")

    def named(ws):
        return ", ".join(
            "the warm-up" if w["warm_up"]
            else "a mid-run walk that found the walker cold" if w["cold"]
            else "a mid-run walk" for w in ws)

    on_loop = [w for w in walks if w["on_loop"]]
    if on_loop:
        problems.append(
            f"{len(on_loop)} walk(s) ran on the event loop's own thread "
            f"({named(on_loop)}): the walker belongs in a worker thread, "
            f"warm or cold")
    late = [w for w in walks if (w["warm_up"] or w["cold"])
            and arrivals and w["returned"] > arrivals[0]]
    if late:
        problems.append(
            f"{len(late)} walk(s) that should have paid the load returned "
            f"after the agent's first request ({named(late)}): the load was "
            f"paid while requests were waiting on the loop")
    if worst >= 1.5:
        problems.append(
            f"the loop was blocked for {worst:.2f}s while probes were in "
            f"flight; the walker belongs off the loop and warmed")
    assert not problems, "\n".join(problems)


class _RedactableOutputAgent(BaseHTTPRequestHandler):
    """Conformant, and its output contains a string the walk REDACTS.

    `agent_runner.py:556-562` replaces the phase's output with the walked
    value before persisting it, so the next phase's `prior_output` carries
    placeholders. The battery walked only to decide pass/fail and threw
    `.value` away, handing every later invocation the original — a
    `prior_output` the chassis never sends (Codex round 24).
    """

    EMAIL = "ada@example.com"
    PRIOR: list = []
    BY_INVOCATION: dict[str, str] = {}
    COUNT: list[int] = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        token = self._bearer()
        if token is None or self.BY_INVOCATION.get(invocation) != token:
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            done = {"output": {"echo": {"who": self.EMAIL}}}
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                + b"event: completed\ndata: " + json.dumps(done).encode() + b"\n\n")
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"who": self.EMAIL}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        self.PRIOR.append(parsed.get("prior_output"))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INVOCATION[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_later_invocations_receive_the_walked_prior_output():
    """What the chassis would persist is what the next phase receives."""
    _RedactableOutputAgent.PRIOR.clear()
    _RedactableOutputAgent.BY_INVOCATION.clear()
    _RedactableOutputAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedactableOutputAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}",
            manifest=_gated_two_phase(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    assert result.passed is True, result.summary()
    handed = [p for p in _RedactableOutputAgent.PRIOR if p]
    assert handed, _RedactableOutputAgent.PRIOR
    for prior in handed:
        assert "ada@example.com" not in json.dumps(prior), prior
        assert "[REDACTED_EMAIL_ADDRESS_1]" in json.dumps(prior), prior
    # And the report shows what would have been persisted, not what the
    # agent said — the two must be the same value.
    assert result.output == {"echo": {"who": "[REDACTED_EMAIL_ADDRESS_1]"}}, result.output


class _TailArrivalRelay:
    """A relay whose one batch lands just after the quiet window closes.

    The append is scheduled on the FIRST read — that read is the settle
    loop's first poll, so `settle + 0.05` puts the arrival a hair after
    the break: inside the 0.2s tail the battery used to sleep through
    without observing, and after the snapshot now that the tail is gone.
    A batch admitted by that tail was never given the quiet window the
    loop exists to measure (Codex round 24).
    """

    def __init__(self, settle: float):
        self._settle = settle
        self._spans: list[dict] = []
        self._records: list[dict] = []
        self._armed = False
        self.late = {"name": "arrived-in-the-tail", "trace_id": "0" * 32,
                     "token": "not-a-minted-token"}
        # Set when the late batch lands, so the test can await its own
        # precondition instead of racing the timer.
        self.delivered = threading.Event()

    def _deliver(self):
        self._spans.append(self.late)
        self.delivered.set()

    def _arm(self):
        if self._armed:
            return
        self._armed = True
        threading.Timer(self._settle + 0.05, self._deliver).start()

    @property
    def spans(self) -> list[dict]:
        self._arm()
        return list(self._spans)

    @property
    def log_records(self) -> list[dict]:
        return list(self._records)


@pytest.mark.asyncio
async def test_nothing_enters_the_snapshot_after_the_quiet_window():
    """The snapshot is taken at the verified quiet point.

    Asserted on the arrival the battery must NOT have: one that landed
    after the window closed. Before this fix the unconditional 0.2s sleep
    admitted it to the snapshot without the window ever being observed
    after it — and a wrong-trace batch following it, inside
    `settle_seconds` of it, was then missed entirely.
    """
    _RecordingTraceAgent.SEEN.clear()
    _RecordingTraceAgent.BY_INVOCATION.clear()
    _RecordingTraceAgent.IS_RERUN.clear()
    _RecordingTraceAgent.COUNT[0] = 0
    relay = _TailArrivalRelay(settle=1.0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingTraceAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}",
            manifest=load_manifest(ECHO_DIR), scenario={"message": "x"},
            relay=relay, settle_seconds=1.0,
        )
    finally:
        server.shutdown()

    # The battery returns about 40ms before the timer fires, and how soon
    # this line runs after that depends on when the fake agent's
    # serve_forever next polls (every 0.5s) to see its shutdown — so the
    # delivery is awaited, not raced (the K blueprint's §11, T1 item 14).
    assert relay.delivered.wait(5) and relay.late in relay.spans, (
        "the fixture never delivered its late batch, so this test proved "
        "nothing about the snapshot"
    )
    assert relay.late not in result.spans, (
        f"a batch that arrived after the quiet window is in the snapshot: "
        f"{result.spans}"
    )


class _PoolsFromTheThirdAgent(BaseHTTPRequestHandler):
    """Bound per invocation for the first two; a run-scoped pool after.

    An agent can be written correctly and then drift: the third phase
    reaches for "any token this run issued" because by then the author
    is thinking in runs, not invocations. A battery that stops at the
    second invocation never sees it.
    """

    BY_INV: dict[str, str] = {}
    POOL: set[str] = set()
    COUNT = [0]
    PHASES: list[str | None] = []

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def _authorized(self, invocation):
        token = self._bearer()
        if token is None:
            return False
        tail = invocation.split("-")[-1]
        if tail.isdigit() and int(tail) >= 3:
            return token in self.POOL          # the leak
        return self.BY_INV.get(invocation) == token

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if not self._authorized(invocation):
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            done = {"output": {"echo": {"inv": invocation}}}
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                + b"event: completed\ndata: " + json.dumps(done).encode() + b"\n\n"
            )
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"inv": invocation}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        self.PHASES.append(parsed.get("phase"))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        token = self._bearer()
        self.BY_INV[invocation] = token
        if token:
            self.POOL.add(token)
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_every_reachable_later_phase_is_probed():
    """The walk runs to the end of the sequence, not one step into it.

    Production continues through phases until a gate, a failure or the
    last one (`agent_runner.py:382`, `:626`, `:640`), so on an ungated
    three-phase manifest it makes THREE invocations with no human in
    between. The battery planned `index + 1` and stopped, and this agent
    — correct on its first two invocations, a run-scoped pool from the
    third — was reported `token_binding: pass`, `passed: True`, with no
    failures at all (Codex round 25).
    """
    _PoolsFromTheThirdAgent.BY_INV.clear()
    _PoolsFromTheThirdAgent.POOL.clear()
    _PoolsFromTheThirdAgent.COUNT[0] = 0
    _PoolsFromTheThirdAgent.PHASES.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PoolsFromTheThirdAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=_three_phase_ungated(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    # THE THIRD PHASE WAS ACTUALLY INVOKED. Asserting only on the verdict
    # would pass for a battery that failed the agent for some unrelated
    # reason and still never walked this far.
    assert _PoolsFromTheThirdAgent.PHASES == ["echo", "second", "third",
                                              "echo", "echo"], (  # +1: round 32's same-phase second run; +1: the headerless traceparent probe
        _PoolsFromTheThirdAgent.PHASES
    )
    assert result.checks["token_binding"] == "fail", text
    # Named at both ends, because the leak is directional and the report
    # has to tell an author which pair to go and look at.
    for holder in ("the first invocation", "the second phase"):
        for leaf in ("events", "output"):
            assert (f"{leaf} with {holder}'s token on the third phase "
                    f"answered 200, not 401") in text, text


def _four_phase_ungated(manifest):
    """Four ungated phases — long enough to leave a fresh run behind.

    A replacement run reaches only the phase under test, so with a plan
    this long a failure partway along it cannot supply what comes next.
    """
    from app.agents.manifest import PhaseSpec

    return manifest.model_copy(update={"phases": [
        manifest.phases[0], PhaseSpec(name="second"),
        PhaseSpec(name="third"), PhaseSpec(name="fourth"),
    ]})


class _SecondPhaseFailsAgent(BaseHTTPRequestHandler):
    """Correct on every token; its `second` phase ends `failed`.

    It also models its run's lifecycle — a phase runs only once its
    predecessor has completed IN THAT RUN — which is what makes it able
    to notice a transition asked for out of order.
    """

    ORDER = ["echo", "second", "third", "fourth"]
    BY_INV: dict[str, str] = {}
    PHASE: dict[str, str] = {}
    DONE_BY_RUN: dict[str, set] = {}
    COUNT = [0]
    REFUSED: list[tuple] = []
    # Every (run, phase) it ACCEPTED, in order — the premise half of the
    # test below, which asserts the replay was attempted and not merely
    # described.
    ASKED: list[tuple] = []

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INV.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "s", "status": "running"}\n\n')
            if self.PHASE.get(invocation) == "second":
                self.wfile.write(
                    b'event: failed\ndata: {"error": "battery fixture"}\n\n')
            else:
                done = {"output": {"echo": {"inv": invocation}}}
                self.wfile.write(
                    b"event: completed\ndata: " + json.dumps(done).encode() + b"\n\n")
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"inv": invocation}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        phase = parsed.get("phase")
        run = (parsed.get("run") or {}).get("id", "")
        rerun = bool((parsed.get("run") or {}).get("rerun"))
        done = self.DONE_BY_RUN.setdefault(run, set())
        at = self.ORDER.index(phase) if phase in self.ORDER else -1
        if not (rerun or at == 0 or (at > 0 and self.ORDER[at - 1] in done)):
            self.REFUSED.append((run, phase, sorted(done)))
            return self._json(409, {"error": f"{phase} cannot run yet"})
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INV[invocation] = self._bearer()
        self.PHASE[invocation] = phase
        self.ASKED.append((run, phase))
        if phase != "second":
            done.add(phase)
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_failed_transition_does_not_ask_a_fresh_run_for_a_later_phase():
    """A replacement run is DRIVEN as far as the agent lets it, and no
    further.

    Measured before this guard, on this manifest: the battery asked
    `battery-run-fresh-1` for `third` and then `fourth` and reported BOTH
    `UNPROVEN` — failing an agent for refusing a sequence production
    cannot produce (the round-8/9 defect, reintroduced by widening the
    plan).

    Round 34 changed WHY it stops, and the difference is the whole point.
    It used to stop because `_fresh_anchor` drove one phase and the rest
    of the plan started further along than that — the helper's reach
    standing in for production's, which certified an agent that leaks
    from the third phase on. Now it drives the replacement run through
    the prefix the plan needs, and stops here only because THIS agent's
    `second` fails on that run too, so `third` never becomes producible
    in it. That is the agent putting the rest of the sequence out of
    reach, established by trying rather than assumed.

    The run really did end, so the verdict is taken over what was
    reachable: a note, a shorter plan, and no penalty.
    """
    _SecondPhaseFailsAgent.BY_INV.clear()
    _SecondPhaseFailsAgent.PHASE.clear()
    _SecondPhaseFailsAgent.DONE_BY_RUN.clear()
    _SecondPhaseFailsAgent.REFUSED.clear()
    _SecondPhaseFailsAgent.ASKED.clear()
    _SecondPhaseFailsAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SecondPhaseFailsAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=_four_phase_ungated(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    # NOTHING WAS ASKED OUT OF ORDER — the property, read off the agent
    # rather than off the report.
    assert _SecondPhaseFailsAgent.REFUSED == [], _SecondPhaseFailsAgent.REFUSED
    # ...and the legal stop is not charged to the agent.
    assert "UNPROVEN" not in text, text
    assert result.checks["token_binding"] == "pass", text
    # THE REPLAY WAS ATTEMPTED, read off the agent: a replacement run was
    # started and asked for `second`, which is the leg that has to fail
    # before anything may be subtracted. Asserting only the note would
    # let a battery that skipped the attempt keep this test green by
    # printing the same sentence.
    fresh = [(run, ph) for run, ph in _SecondPhaseFailsAgent.ASKED
             if run.startswith("battery-run-fresh")]
    assert ("battery-run-fresh-1", "echo") in fresh, _SecondPhaseFailsAgent.ASKED
    assert ("battery-run-fresh-1", "second") in fresh, _SecondPhaseFailsAgent.ASKED
    # ...and it was never asked for a phase whose predecessor had not run
    # in it, which `REFUSED` above already proves from the other side.
    assert ("battery-run-fresh-1", "third") not in fresh, _SecondPhaseFailsAgent.ASKED
    # The report says the attempt happened and how far it got, rather
    # than implying the battery declined to make it.
    assert "the replacement run could not be driven far enough" in text, text
    assert "it reached echo and then its second ended `failed`" in text, text


class _DoubleTerminalOnTheSecondAgent(BaseHTTPRequestHandler):
    """Its `second` phase sends `completed` and then a stray `failed`.

    Production returns at the `completed` (`container.py:388-392`) and
    carries the run on to `third`; it never reads the second frame.
    """

    BY_INV: dict[str, str] = {}
    PHASE: dict[str, str] = {}
    COUNT = [0]
    PHASES: list[str | None] = []

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INV.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "s", "status": "running"}\n\n')
            done = {"output": {"echo": {"inv": invocation}}}
            self.wfile.write(
                b"event: completed\ndata: " + json.dumps(done).encode() + b"\n\n")
            if self.PHASE.get(invocation) == "second":
                self.wfile.write(
                    b'event: failed\ndata: {"error": "never read by the chassis"}\n\n')
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"inv": invocation}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INV[invocation] = self._bearer()
        self.PHASE[invocation] = parsed.get("phase")
        self.PHASES.append(parsed.get("phase"))
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_second_terminal_event_is_reported_without_ending_the_walk():
    """Two properties, and the fix is wrong if either is missing.

    The battery read terminals with
    `next((v for k, v in events if k == "completed"), None)` plus
    `any(k == "failed" ...)`, which ignores ORDER and MULTIPLICITY. So a
    `completed` followed by a stray `failed` was read as a phase that
    succeeded AND died: the battery re-anchored, the round-25
    reachability stop then ended the walk, and it still reported
    `token_binding: pass`, `passed: True`. Measured on this fixture
    before the fix: `['echo', 'second']`, stopped with "the second phase
    ended `failed`" — about a run the chassis carries straight through
    (Codex round 26).

    So: the extra terminal must be REPORTED, and the walk must still
    follow the FIRST terminal the way production does. A fix that only
    reported it would leave the truncation in place; one that only
    followed the first would leave a contract violation nobody mentions.
    """
    _DoubleTerminalOnTheSecondAgent.BY_INV.clear()
    _DoubleTerminalOnTheSecondAgent.PHASE.clear()
    _DoubleTerminalOnTheSecondAgent.PHASES.clear()
    _DoubleTerminalOnTheSecondAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DoubleTerminalOnTheSecondAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=_three_phase_ungated(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    # 1. THE WALK FOLLOWED THE FIRST TERMINAL, as the chassis does.
    assert _DoubleTerminalOnTheSecondAgent.PHASES == ["echo", "second",
                                                     "third", "echo",
                                                     "echo"], (  # +1: round 32's same-phase second run; +1: the headerless traceparent probe
        _DoubleTerminalOnTheSecondAgent.PHASES
    )
    assert "ended `failed`" not in text, text
    # 2. ...AND THE VIOLATION IS NAMED, with the consequence production
    # really has. Saying it "raises ContainerAgentError" would send an
    # author hunting an exception that is never thrown: the chassis
    # returns at the first terminal and consumes this invocation.
    assert "carried 2 terminal events (completed, failed)" in text, text
    assert "the chassis returns at the first" in text, text
    assert "ContainerAgentError" not in text, text
    assert result.passed is False, text
    # The invocation WAS consumable, so the binding verdict is not the
    # thing that failed here.
    assert result.checks["token_binding"] == "pass", text


class _MalformedFrameAgent(BaseHTTPRequestHandler):
    """Its transition streams a malformed frame, and serves a good `/output`.

    MODE picks the shape: `nonjson` is data that is not JSON at all,
    `nonobject` is JSON that is not an object, `progress` puts the bad
    data on a non-terminal frame. `container.py:418-434` raises
    `ContainerAgentError` for every one of them, while PARSING, so
    production never reaches the terminal or the fallback.
    """

    MODE = "nonjson"          # nonjson | nonobject | progress
    # Whether the INVOCATION UNDER TEST is malformed too. Off by default so
    # the shape tests exercise the transition path; on for the
    # report-once test, whose subject is the first invocation's stream.
    ALL = [False]
    BY_INV: dict[str, str] = {}
    COUNT = [0]

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INV.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        tail = invocation.split("-")[-1]
        first = tail.isdigit() and int(tail) == 1
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            good = json.dumps({"output": {"echo": {"inv": invocation}}}).encode()
            if first and not self.ALL[0]:
                self.wfile.write(
                    b'event: progress\ndata: {"step_id": "s", "status": "running"}\n\n'
                    b"event: completed\ndata: " + good + b"\n\n")
                return
            if self.MODE == "progress":
                self.wfile.write(b"event: progress\ndata: not json\n\n")
                self.wfile.write(b"event: completed\ndata: " + good + b"\n\n")
            else:
                self.wfile.write(
                    b'event: progress\ndata: {"step_id": "s", "status": "running"}\n\n')
                self.wfile.write(
                    b"event: completed\ndata: not json at all\n\n"
                    if self.MODE == "nonjson" else
                    b"event: completed\ndata: 42\n\n")
            return
        if self.path.endswith("/output"):
            # VALID, and that is the point: production raised before here.
            return self._json(200, {"output": {"echo": {"inv": invocation}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INV[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.parametrize(
    ("mode", "fragment"),
    [("nonjson", "`completed` event carried data that is not JSON"),
     ("nonobject", "`completed` event carried data that is JSON but not an object"),
     ("progress", "`progress` event carried data that is not JSON")],
)
@pytest.mark.asyncio
async def test_a_frame_the_chassis_dies_on_is_not_a_drained_invocation(mode, fragment):
    """`_parse_sse` used to fake a dict, or hand back an int.

    Non-JSON data became `{"_unparseable": raw}` — a dict, so
    `.get("output")` was None and a `completed` frame fell through to
    `/output` and was ACCEPTED. JSON that is not an object was passed
    through as whatever `json.loads` returned, against this function's own
    `-> list[tuple[str, dict]]`, and `data: 42` raised
    `AttributeError: 'int' object has no attribute 'get'` OUT of
    `run_contract_battery` — a crash where a verdict was due. And a
    malformed NON-terminal frame was never looked at, though `_parse_data`
    runs on every event. Measured: all three passed or crashed (Codex
    round 27).
    """
    _MalformedFrameAgent.MODE = mode
    _MalformedFrameAgent.ALL[0] = False
    _MalformedFrameAgent.BY_INV.clear()
    _MalformedFrameAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MalformedFrameAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=_two_phase(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    assert result.checks["token_binding"] == "fail", text
    assert fragment in text, text
    # The consequence, and the reason the valid `/output` is no defence.
    assert "while PARSING that frame" in text, text
    assert "never reaches the terminal, the output, or the fallback" in text, text
    assert result.passed is False, text


@pytest.mark.asyncio
async def test_a_malformed_frame_is_reported_once_for_its_invocation():
    """A correct verdict repeated three times is still a defect (§12 173(g)).

    `_terminal_of` scans every event of a stream, and the progress check
    reads the same events. Both appending meant one bad `progress` line
    produced three failures for the invocation under test.
    """
    _MalformedFrameAgent.MODE = "progress"
    _MalformedFrameAgent.ALL[0] = True
    _MalformedFrameAgent.BY_INV.clear()
    _MalformedFrameAgent.COUNT[0] = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MalformedFrameAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        result = await run_contract_battery(
            url, manifest=_two_phase(load_manifest(ECHO_DIR)),
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()
        _MalformedFrameAgent.ALL[0] = False

    # The invocation under test's failures carry no transition label; a
    # transition's are prefixed with its own. Only a repeat WITHIN one
    # invocation is duplication.
    about = [f for f in result.failures if "while PARSING that frame" in f]
    under_test = [f for f in about if f.startswith("its `")]
    assert len(under_test) == 1, about
    # ...and grading a stream production never got through is refused
    # rather than passed.
    assert result.checks["progress"] == "fail", result.summary()


class _NonObjectJsonAgent(BaseHTTPRequestHandler):
    """Answers some boundary with JSON that decodes to a non-object.

    `httpx`'s `.json()` succeeds for `[1]`, `42` and `"x"` exactly as it
    does for an object, and every caller then reaches for `.get()`. The
    bodies here are all TRUTHY on purpose: `[]` is falsy and `x or {}`
    rescues it, so a probe using `[]` reports sites safe that are not.
    """

    MODE = "first-post"
    BY_INV: dict[str, str] = {}
    RUNS: dict[str, str] = {}
    COUNT = [0]

    def log_message(self, *a):
        pass

    def _raw(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._raw(code, json.dumps(obj).encode())

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INV.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        tail = invocation.split("-")[-1]
        n = int(tail) if tail.isdigit() else 0
        fresh = self.RUNS.get(invocation, "").startswith("battery-run-fresh")
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if self.MODE == "data-only" and n > 1:
                self.wfile.write(b"data: not-json\n\n")   # no `event:` line
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "s", "status": "running"}\n\n')
            if self.MODE in ("anchor-post", "anchor-output") and n == 2:
                self.wfile.write(
                    b'event: failed\ndata: {"error": "to force a re-anchor"}\n\n')
                return
            # `startswith`, because `first-output-nonjson` must ALSO omit
            # the inline output or `/output` is never fetched and the
            # non-JSON body is never reached — the test would pass while
            # measuring nothing.
            omit = ((self.MODE.startswith("first-output") and n == 1)
                    or (self.MODE == "transition-output" and n > 1)
                    or (self.MODE == "anchor-output" and fresh))
            done = {} if omit else {"output": {"echo": {"inv": invocation}}}
            self.wfile.write(
                b"event: completed\ndata: " + json.dumps(done).encode() + b"\n\n")
            return
        if self.path.endswith("/output"):
            if self.MODE == "first-output" and n == 1:
                return self._raw(200, b"42")
            if self.MODE == "first-output-nonjson" and n == 1:
                return self._raw(200, b"<html>nope</html>", "text/html")
            if self.MODE == "transition-output" and n > 1:
                return self._raw(200, b'[{"output": 1}]')
            if self.MODE == "anchor-output" and fresh:
                return self._raw(200, b'[{"output": 1}]')
            return self._json(200, {"output": {"echo": {"inv": invocation}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        run = (parsed.get("run") or {}).get("id", "")
        self.COUNT[0] += 1
        n = self.COUNT[0]
        invocation = f"inv-{n}"
        self.BY_INV[invocation] = self._bearer()
        self.RUNS[invocation] = run
        if self.MODE == "first-post" and n == 1:
            return self._raw(201, b'[{"invocation_id": "x"}]')
        if self.MODE == "transition-post" and n > 1:
            return self._raw(201, b"42")
        if self.MODE == "anchor-post" and run.startswith("battery-run-fresh"):
            return self._raw(201, b'[{"invocation_id": "x"}]')
        self._json(201, {"invocation_id": invocation})


@pytest.mark.parametrize(
    "mode",
    ["first-post", "first-output", "first-output-nonjson", "transition-post",
     "transition-output", "anchor-post", "anchor-output", "data-only"],
)
@pytest.mark.asyncio
async def test_no_json_boundary_crashes_instead_of_reporting(mode):
    """Eight shapes, six `.json()` sites, and none may raise.

    Round 27 fixed this assumption inside `_parse_sse` and swept no
    further. `grep -n '.json()'` finds six of them, and every one raised
    out of `run_contract_battery` rather than producing a verdict —
    `AttributeError: 'list' object has no attribute 'get'` and a bare
    `JSONDecodeError`. A crash is not a verdict, so an agent that answers
    `42` where an object was due could not be reported at all rather than
    being reported non-conformant (Codex round 28).

    `data-only` is the seventh shape from the same review: a frame with
    `data:` and no `event:` is FLUSHED in production
    (`container.py:383-388`) and dies on its data; the battery dropped it
    on `if name` and passed the stream.
    """
    _NonObjectJsonAgent.MODE = mode
    _NonObjectJsonAgent.BY_INV.clear()
    _NonObjectJsonAgent.RUNS.clear()
    _NonObjectJsonAgent.COUNT[0] = 0
    gated = mode in ("anchor-post", "anchor-output")
    base = load_manifest(ECHO_DIR)
    manifest = (_gated_two_phase(base) if gated else _two_phase(base))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NonObjectJsonAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        # The assertion is that this RETURNS. Before the fix it raised.
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"},
            settle_seconds=0.1, timeout=25.0,
        )
    finally:
        server.shutdown()

    assert result.passed is False, result.summary()
    assert result.failures, result.summary()


@pytest.mark.asyncio
async def test_a_slow_but_conforming_agent_is_not_failed_by_a_long_plan(monkeypatch):
    """`run_probes` is serial; the bound must not be one request's worth.

    `collect` bounded a WHOLE plan at `timeout` while `run_probes` ran it
    with `for ...: await ...`, so N probes each answering in t needed
    N*t < timeout. Measured against an agent that answers every refusal
    correctly in 0.3s, under `--timeout 2`: `token_binding: fail`,
    `passed: False`, "the cross-use probes did not finish within 2s" —
    twelve serial probes simply do not fit one window. Plans grow by four
    requests for every prior invocation, so the bigger the manifest the
    likelier that false failure (Codex round 29).

    The agent here is CONFORMANT and the assertion is that it passes.
    """
    import adapter_kit.run_contract as _rc

    # Each probe well inside its own ceiling; the PLAN far outside the
    # single window the batch used to get.
    monkeypatch.setattr(_rc, "_PROBE_CEILING_SECONDS", 1.0)
    _PacedAgent.reset(PROBE_DELAY=0.25)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PacedAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with _collector_paused():
            result = await run_contract_battery(
                f"http://127.0.0.1:{server.server_address[1]}",
                manifest=_two_phase(load_manifest(ECHO_DIR)),
                scenario={"message": "x"}, settle_seconds=0.1, timeout=1.0,
            )
    finally:
        server.shutdown()

    assert result.checks["token_binding"] == "pass", result.summary()
    assert not [f for f in result.failures if "stopped answering" in f], result.failures
    # ...and nothing vanished: every probe recorded an answer.
    assert not [f for f in result.failures if "never answered at all" in f], (
        result.failures
    )


class _SerializedAgent(BaseHTTPRequestHandler):
    """Answers one request at a time, and its phase takes a while.

    The Run Contract permits this and `Container_Agents.md` promises it
    costs nothing: "if your agent serializes requests, the probes simply
    wait their turn behind the stream". Served by a plain ``HTTPServer``
    rather than the threading one every other fixture here uses —
    THAT is the property under test, so it cannot be a detail.
    """

    HOLD = 1.2
    ISSUED: dict[str, str] = {}
    COUNT = [0]
    STREAM_ENDED: list[float] = []
    REFUSED: list[float] = []

    protocol_version = "HTTP/1.1"

    @classmethod
    def reset(cls, hold: float) -> None:
        cls.HOLD = hold
        cls.ISSUED = {}
        cls.COUNT = [0]
        cls.STREAM_ENDED = []
        cls.REFUSED = []

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.ISSUED.get(invocation) != self._bearer():
            # Correct binding, every time — the token must be the one
            # THIS invocation was handed.
            self.REFUSED.append(time.monotonic())
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            time.sleep(self.HOLD)
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
            )
            self.STREAM_ENDED.append(time.monotonic())
            self.close_connection = True
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.ISSUED[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_serialized_agent_is_not_failed_for_answering_after_its_stream(
    monkeypatch,
):
    """The probe ceiling times the AGENT, not the queue in front of it.

    Round 29 gave each probe a flat ceiling measured from the moment it
    was SENT. Against a serialized agent that is the wrong clock: the
    agent cannot answer until its stream closes, so its refusals arrive
    correctly and late, and the battery recorded them as never answered.
    Measured before the fix, against a conforming agent holding an 8s
    stream at the real 5s ceiling: two probes "never answered at all",
    `token_binding: fail`, while the agent's `401`s were served at
    15.65s (Codex round 30).

    The ceiling is monkeypatched down for the same reason the two
    cancellation tests above do it — the mechanism is identical at
    either value and the real one would put seconds on this file.
    """
    import adapter_kit.run_contract as _rc

    monkeypatch.setattr(_rc, "_PROBE_CEILING_SECONDS", 0.4)
    # Three times the ceiling: long enough that a probe sent when the
    # stream opens cannot survive a clock started at the send.
    _SerializedAgent.reset(hold=1.2)
    server = HTTPServer(("127.0.0.1", 0), _SerializedAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        with _collector_paused():
            result = await run_contract_battery(
                url, manifest=manifest, scenario={"message": "x"},
                settle_seconds=0.1,
            )
    finally:
        server.shutdown()

    # THE PREMISE, asserted rather than assumed. A threading server would
    # answer the probes DURING the stream and this test would pass while
    # exercising nothing — the conclusion holding for the wrong reason.
    # Every refusal must have been served after a stream closed.
    assert _SerializedAgent.STREAM_ENDED, "no stream ever completed"
    assert _SerializedAgent.REFUSED, "no probe was ever refused"
    first_close = _SerializedAgent.STREAM_ENDED[0]
    assert min(_SerializedAgent.REFUSED) >= first_close, (
        "a refusal was served while the stream was still open, so this "
        "agent was not serialized and the queuing under test never happened"
    )

    # THE CONCLUSION: a conforming agent is reported conforming.
    assert result.checks["token_binding"] == "pass", result.summary()
    assert result.passed, result.summary()
    assert not [f for f in result.failures if "never answered at all" in f], (
        result.failures
    )


def test_every_shrunk_probe_ceiling_is_timed_without_the_collector():
    """A test that shrinks the probe ceiling times its battery inside
    `_collector_paused()`.

    Shrinking the ceiling is how these tests stay fast, and it is also
    what brings it under the pause a full collection costs in a long
    suite (`_collector_paused`, run 36661203696). Found by the AST rather
    than by name, in every spelling that shrinks it — `setattr` on the
    module or on its dotted path, `mock.patch`, `patch.object`, a plain
    assignment — so a test added later is held to the same rule; and
    counted, so the rule cannot pass by finding nothing to hold.
    """
    import ast

    def name_of(call):
        return getattr(call.func, "id", getattr(call.func, "attr", None))

    def calls(node, name):
        return [c for c in ast.walk(node)
                if isinstance(c, ast.Call) and name_of(c) == name]

    def shrinks(node):
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                    and name_of(sub) in ("setattr", "patch", "object")
                    and any(isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                            and arg.value.endswith("_PROBE_CEILING_SECONDS")
                            for arg in sub.args)):
                return True
            if (isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Store)
                    and sub.attr == "_PROBE_CEILING_SECONDS"):
                return True
        return False

    shrinking, unpaused = [], []
    for node in ast.parse(Path(__file__).read_text()).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not shrinks(node):
            continue
        shrinking.append(node.name)
        timed = [block for block in ast.walk(node)
                 if isinstance(block, (ast.With, ast.AsyncWith))
                 and any(isinstance(item.context_expr, ast.Call)
                         and getattr(item.context_expr.func, "id", None) == "_collector_paused"
                         for item in block.items)
                 and calls(block, "run_contract_battery")]
        if not timed or len(calls(node, "run_contract_battery")) != len(
                [c for block in timed for c in calls(block, "run_contract_battery")]):
            unpaused.append(node.name)

    assert len(shrinking) >= 4, shrinking
    assert not unpaused, (
        f"these shrink _PROBE_CEILING_SECONDS and time a battery with the "
        f"collector running: {unpaused}")


class _FifoNoInlineOutputAgent(BaseHTTPRequestHandler):
    """One request at a time, and `completed` carries no inline output.

    That combination is what exposes a probe the battery merely SENT
    early: the `/output` fallback cannot be sent until the stream has
    closed, so anything already in this agent's accept queue is served
    ahead of it — and that fetch is inside the invocation's budget.
    """

    HOLD = 0.3
    REFUSE = 0.2
    ISSUED: dict[str, str] = {}
    COUNT = [0]
    SERVED: list[tuple[str, int]] = []

    protocol_version = "HTTP/1.1"

    @classmethod
    def reset(cls) -> None:
        cls.ISSUED = {}
        cls.COUNT = [0]
        cls.SERVED = []

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.SERVED.append((self.path, code))

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.ISSUED.get(invocation) != self._bearer():
            time.sleep(self.REFUSE)      # slow, but entirely CORRECT
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.SERVED.append((self.path, 200))
            time.sleep(self.HOLD)
            # No inline output, so the battery MUST fetch `/output`.
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {}\n\n'
            )
            self.close_connection = True
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.ISSUED[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_no_binding_probe_is_sent_before_the_invocations_last_leg():
    """The battery's own requests stay out of the agent's QUEUE, not just
    out of its clock.

    Round 30 deferred when a probe's ceiling starts and left the request
    in flight from the moment the stream opened. Against a FIFO agent
    that request sits ahead of the `/output` fallback, which the battery
    cannot send until the stream has closed — so the agent's own fallback
    is served behind it, inside the invocation's budget. Measured before
    the fix, refusing in 8s under a 10s deadline: "the invocation outran
    the 10s deadline it was advertised while serving its output",
    `completed: fail`, for time the battery itself spent (Codex round
    31). That is round 19's defect by another route.

    The assertion is ORDERING rather than elapsed time: for a FIFO server
    the order it serves requests in is exactly the order they arrived, so
    it decides the question without depending on any timing margin.
    """
    _FifoNoInlineOutputAgent.reset()
    server = HTTPServer(("127.0.0.1", 0), _FifoNoInlineOutputAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        manifest = load_manifest(ECHO_DIR)
        result = await run_contract_battery(
            url, manifest=manifest, scenario={"message": "x"},
            settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    served = list(_FifoNoInlineOutputAgent.SERVED)

    # THE PREMISE: the fallback really was needed, so there really was a
    # leg after the stream for a probe to jump ahead of.
    fallback = [i for i, (path, code) in enumerate(served)
                if path.endswith("/output") and code == 200]
    assert fallback, f"no `/output` fallback was ever fetched: {served}"

    # THE PROPERTY: no refusal was served before the first invocation's
    # last leg. A probe sent during the stream would appear here first.
    refusals = [i for i, (_path, code) in enumerate(served) if code == 401]
    assert refusals, f"no probe was ever refused: {served}"
    assert min(refusals) > fallback[0], (
        f"a binding probe was served before the `/output` fallback, so it "
        f"was sent while the invocation still held the agent: {served}"
    )

    # THE CONSEQUENCE: the agent is not blamed for the battery's queue.
    assert result.checks["completed"] == "pass", result.summary()
    assert not [f for f in result.failures if "outran the" in f], result.failures
    assert result.passed, result.summary()


class _DropsTheOutputFetchAgent(BaseHTTPRequestHandler):
    """Answers everything, then drops the connection on `/output`.

    `completed` carries no inline result, so the battery must fetch
    `/output` — and that fetch meets a transport failure rather than a
    slow answer.
    """

    ISSUED: dict[str, str] = {}
    COUNT = [0]
    protocol_version = "HTTP/1.1"

    @classmethod
    def reset(cls):
        cls.ISSUED = {}
        cls.COUNT = [0]

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.ISSUED.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                b'event: completed\ndata: {}\n\n'
            )
            self.close_connection = True
            return
        if self.path.endswith("/output"):
            try:
                self.connection.close()      # answered nothing at all
            except OSError:
                pass
            self.close_connection = True
            return
        self._json(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.ISSUED[invocation] = self._bearer()
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_transport_failure_on_the_output_fetch_is_reported_not_raised():
    """REPORTED, NEVER RAISED — the third site to need it.

    The first invocation's `/output` fallback caught only `TimeoutError`,
    so an agent that reset the connection took the exception straight out
    of `run_contract_battery` and the CLI printed a traceback instead of
    a verdict. Measured before the fix: `httpx.RemoteProtocolError: Server
    disconnected without sending a response` escaping the call (Codex
    round 32). The two sibling fallbacks already caught both.

    The assertion is that the battery RETURNS — a raise fails the test by
    propagating, which is the behaviour under test rather than a proxy
    for it.
    """
    _DropsTheOutputFetchAgent.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DropsTheOutputFetchAgent)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}",
            manifest=load_manifest(ECHO_DIR), scenario={"message": "x"},
            settle_seconds=0.1, timeout=20,
        )
    finally:
        server.shutdown()

    text = result.summary()
    assert result.checks["completed"] == "fail", text
    assert any("`/output` could not be read" in f for f in result.failures), text
    # NOT the deadline verdict: the agent was not slow, and saying it was
    # sends an author to the wrong place.
    assert not [f for f in result.failures if "outran the" in f], result.failures


class _PhaseKeyedPoolAgent(BaseHTTPRequestHandler):
    """Pools every token it issues BY PHASE, and honours that pool.

    Every cross-use the battery used to probe on a multi-phase manifest
    crossed a phase boundary, so this agent refused all of them — while a
    second run of the SAME phase could read the first run's invocation.
    """

    POOL: dict[str, set] = {}
    PHASE_OF: dict[str, str] = {}
    COUNT = [0]
    # When set, that phase's invocation ends `failed` — a LEGAL terminal
    # that ends the run, used by the round-33 test below.
    FAIL_PHASE: str | None = None
    protocol_version = "HTTP/1.1"

    @classmethod
    def reset(cls, fail_phase=None):
        cls.POOL = {}
        cls.PHASE_OF = {}
        cls.COUNT = [0]
        cls.FAIL_PHASE = fail_phase

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def _accepted(self, invocation):
        phase = self.PHASE_OF.get(invocation)
        return phase is not None and self._bearer() in self.POOL.get(phase, set())

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if not self._accepted(invocation):
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.PHASE_OF.get(invocation) == self.FAIL_PHASE:
                self.wfile.write(
                    b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                    b'event: failed\ndata: {"error": "legitimately failed"}\n\n'
                )
            else:
                self.wfile.write(
                    b'event: progress\ndata: {"step_id": "only", "status": "running"}\n\n'
                    b'event: completed\ndata: {"output": {"echo": {"message": "x"}}}\n\n'
                )
            self.close_connection = True
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"message": "x"}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        phase = (json.loads(raw or "{}").get("phase")) or "?"
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.PHASE_OF[invocation] = phase
        self.POOL.setdefault(phase, set()).add(self._bearer())
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_phase_scoped_token_pool_is_not_a_binding():
    """A pool keyed by PHASE is still a pool.

    The second-run transition used to be the `else` of the phase walk,
    reached only when the manifest had no later phase. So on a
    multi-phase manifest every pair the battery crossed changed the
    phase, and this agent refused all of them correctly. Measured before
    the fix on a two-phase ungated manifest: `token_binding: pass`,
    `passed: True`, no failures — while a real second run of the first
    phase read the first run's `/output` with `200` (Codex round 32).

    That is the round-2 defect keyed differently: membership in a pool is
    not binding to an invocation, whatever the pool is keyed by.
    """
    from app.agents.manifest import PhaseSpec

    _PhaseKeyedPoolAgent.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PhaseKeyedPoolAgent)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = load_manifest(ECHO_DIR)
        first = base.phases[0].name
        manifest = base.model_copy(update={"phases": [
            PhaseSpec(name=first), PhaseSpec(name="second")]})
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}",
            manifest=manifest, scenario={"message": "x"},
            settle_seconds=0.1, timeout=30,
        )
    finally:
        server.shutdown()

    text = result.summary()
    # THE PREMISE: a later phase existed, so the walk really did run and
    # this really is the branch that used to skip the second run.
    phases = list(_PhaseKeyedPoolAgent.PHASE_OF.values())
    assert "second" in phases, phases
    # ...and a SAME-PHASE invocation was started anyway.
    assert phases.count(first) >= 2, phases

    # THE CONSEQUENCE: the pool is named, in both directions.
    assert result.checks["token_binding"] == "fail", text
    assert ("with another invocation's token on the first invocation answered "
            "200, not 401") in text, text
    assert ("with the first invocation's token on another invocation answered "
            "200, not 401") in text, text


def test_no_two_fixtures_in_this_file_share_a_name():
    """A redefined fixture silently repoints the tests above it.

    Adding `_PhaseScopedPoolAgent` a second time at the end of this file
    did not fail anything at import: Python simply kept the later class,
    so `test_a_gated_phase_is_probed_with_a_rerun_as_well` started
    exercising an agent written for a different test — and said nothing
    until a count happened to disagree (Codex round 32's own follow-up).

    The file is parsed rather than grepped, because a name inside a
    docstring or a comment is not a definition and a rule that cannot
    tell them apart would fail on its own prose.
    """
    import ast as _ast
    from collections import Counter

    source = Path(__file__).read_text()
    tree = _ast.parse(source)
    names = [n.name for n in tree.body if isinstance(n, _ast.ClassDef)]
    repeated = sorted(n for n, c in Counter(names).items() if c > 1)
    assert not repeated, (
        f"these classes are defined more than once in this file, so the "
        f"later definition silently replaces the earlier one for every "
        f"test that names it: {repeated}"
    )
    # The functions too, and for the same reason — a redefined test is a
    # test that never runs.
    functions = [n.name for n in tree.body
                 if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))]
    repeated_fns = sorted(n for n, c in Counter(functions).items() if c > 1)
    assert not repeated_fns, (
        f"these top-level functions are defined more than once, so the "
        f"earlier ones never run: {repeated_fns}"
    )


@pytest.mark.asyncio
async def test_a_legal_failure_in_the_walk_does_not_cancel_the_second_run():
    """The second run is RUN-SCOPED, so the walk ending cannot skip it.

    The same-phase second run was appended to the transition list, and the
    reachability stop — which exists so a `failed` run is not asked for
    phases it can no longer produce — counted it among "the remaining
    transitions" and subtracted it. So an agent pooling tokens per phase
    had only to answer `failed` for the later phase, which is LEGAL, to
    have the one probe that catches it skipped: measured
    `token_binding: pass`, `passed: True`, no failures, no second run
    started, with the leak then live at `200` (Codex round 33).

    I had written that off as a narrow accepted cost when placing the
    transition last. A legal terminal is not a narrow case, and a check
    an agent can switch off by behaving legally is not a check.
    """
    from app.agents.manifest import PhaseSpec

    _PhaseKeyedPoolAgent.reset(fail_phase="second")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PhaseKeyedPoolAgent)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = load_manifest(ECHO_DIR)
        first = base.phases[0].name
        manifest = base.model_copy(update={"phases": [
            PhaseSpec(name=first), PhaseSpec(name="second")]})
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}",
            manifest=manifest, scenario={"message": "x"},
            settle_seconds=0.1, timeout=30,
        )
    finally:
        server.shutdown()

    text = result.summary()
    phases = list(_PhaseKeyedPoolAgent.PHASE_OF.values())
    # THE PREMISE: the later phase really did run and really did fail, so
    # this is the path where the walk is abandoned.
    assert "second" in phases, phases
    assert any("ended `failed`" in n for n in result.notes), result.notes
    # ...and the second run was started ANYWAY.
    assert phases.count(first) >= 2, (
        f"the walk ended, and the run-scoped second run went with it: {phases}")

    # THE CONSEQUENCE: the pool is still caught.
    assert result.checks["token_binding"] == "fail", text
    assert ("with another invocation's token on the first invocation answered "
            "200, not 401") in text, text


class _BindsEarlyLeaksLateAgent(BaseHTTPRequestHandler):
    """Bound per invocation for every phase the battery used to reach;
    a run-scoped pool from the THIRD phase on. Its reruns end `failed`.

    Every request the old plan made, this agent answers correctly. The
    leak sits behind a failed rerun, where the plan used to stop.
    """

    LEAKY = {"third", "fourth"}
    OWNER: dict[str, str] = {}
    PHASE_OF: dict[str, str] = {}
    RUN_OF: dict[str, str] = {}
    POOL: dict[str, set] = {}
    RERUN: set = set()
    ASKED: list[tuple] = []
    COUNT = [0]

    @classmethod
    def reset(cls):
        cls.OWNER = {}
        cls.PHASE_OF = {}
        cls.RUN_OF = {}
        cls.POOL = {}
        cls.RERUN = set()
        cls.ASKED = []
        cls.COUNT = [0]

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def _ok(self, invocation):
        phase = self.PHASE_OF.get(invocation)
        if phase is None:
            return False
        if phase in self.LEAKY:
            # THE DEFECT: any token this run ever issued.
            return self._bearer() in self.POOL.get(self.RUN_OF[invocation], set())
        return self._bearer() == self.OWNER[invocation]

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if not self._ok(invocation):
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "s", "status": "running"}\n\n')
            if invocation in self.RERUN:
                # A LEGAL terminal, and the only thing this agent does
                # that is not perfect conformance.
                self.wfile.write(
                    b'event: failed\ndata: {"error": "the edit did not apply"}\n\n')
            else:
                done = {"output": {"echo": {"inv": invocation}}}
                self.wfile.write(
                    b"event: completed\ndata: " + json.dumps(done).encode() + b"\n\n")
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"inv": invocation}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        phase = parsed.get("phase")
        run = (parsed.get("run") or {}).get("id", "")
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.OWNER[invocation] = self._bearer()
        self.PHASE_OF[invocation] = phase
        self.RUN_OF[invocation] = run
        self.POOL.setdefault(run, set()).add(self._bearer())
        self.ASKED.append((run, phase))
        if (parsed.get("run") or {}).get("rerun"):
            self.RERUN.add(invocation)
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_a_failed_rerun_does_not_subtract_the_phases_a_replay_reaches():
    """The prefix is replayed on the replacement run, so a `failed`
    rerun mid-walk costs the plan nothing it can still produce.

    `p1 -> p2 -> gated p3 -> p4`, the p2 rerun answering a legal
    `failed`. Measured before the fix, against this agent:
    `token_binding: pass`, no failures at all, and `third` and `fourth`
    never invoked — while a real replacement run that replayed the
    prefix, approved rather than edited and reached `third` read p1's
    token into its `/output` with `200`.

    A user whose edit fails does exactly that. Treating the helper's
    one-phase reach as production's is what made the leak invisible.
    """
    from app.agents.manifest import PhaseSpec

    _BindsEarlyLeaksLateAgent.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BindsEarlyLeaksLateAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = load_manifest(ECHO_DIR)
        manifest = base.model_copy(update={"phases": [
            base.phases[0],
            PhaseSpec(name="second"),
            PhaseSpec(name="third", approval=True),
            PhaseSpec(name="fourth"),
        ]})
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}", manifest=manifest,
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    asked = _BindsEarlyLeaksLateAgent.ASKED
    # THE PREMISE, read off the agent: a replacement run was started and
    # driven THROUGH the prefix, so the phases behind the failed rerun
    # became producible. Without this the conclusion below could be
    # reached by some other route and the test would not be about the
    # replay at all.
    fresh = [(run, ph) for run, ph in asked if run.startswith("battery-run-fresh")]
    assert ("battery-run-fresh-1", "second") in fresh, asked
    assert any(ph == "third" for _run, ph in asked), asked
    assert any(ph == "fourth" for _run, ph in asked), asked
    # THE CONSEQUENCE: the pool behind the failed rerun is caught, in
    # both directions and on both leaves.
    assert result.checks["token_binding"] == "fail", text
    assert "the third phase" in text, text
    assert "the fourth phase" in text, text
    # ...and the report says the replay happened rather than leaving an
    # author to infer it.
    assert "replayed under it to reach them" in text, text
    # EVERY LEG OF THE REPLACEMENT RUN IS NAMED APART, by which run and
    # which phase of it. The round-10 property, which the replay makes
    # plural for the first time: two legs sharing a noun would overwrite
    # each other's `bindings` entries, and a report that cannot say which
    # invocation leaked is one an author cannot act on.
    assert "fresh run 1's echo's token on the third phase" in text, text
    assert "fresh run 1's second's token on the third phase" in text, text


class _TwoGateLifecycleAgent(BaseHTTPRequestHandler):
    """Models its run's lifecycle, on a manifest with TWO gates.

    A phase runs only once its predecessor has completed IN THAT RUN, so
    any request the chassis could not have produced shows up as a refusal
    rather than being answered — which is what makes this fixture able to
    judge the replay rather than merely survive it. Its `second` RERUN
    ends `failed`, which is the transition with the most `needs` values
    live behind it.
    """

    ORDER = ["echo", "second", "third", "fourth"]
    BY_INV: dict[str, str] = {}
    PHASE: dict[str, str] = {}
    DONE_BY_RUN: dict[str, set] = {}
    RERUN: set = set()
    REFUSED: list[tuple] = []
    ASKED: list[tuple] = []
    COUNT = [0]

    @classmethod
    def reset(cls):
        cls.BY_INV = {}
        cls.PHASE = {}
        cls.DONE_BY_RUN = {}
        cls.RERUN = set()
        cls.REFUSED = []
        cls.ASKED = []
        cls.COUNT = [0]

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        if self.BY_INV.get(invocation) != self._bearer():
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "s", "status": "running"}\n\n')
            if invocation in self.RERUN and self.PHASE.get(invocation) == "second":
                self.wfile.write(
                    b'event: failed\ndata: {"error": "the edit did not apply"}\n\n')
            else:
                done = {"output": {"echo": {"inv": invocation}}}
                self.wfile.write(
                    b"event: completed\ndata: " + json.dumps(done).encode() + b"\n\n")
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"inv": invocation}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        phase = parsed.get("phase")
        run = (parsed.get("run") or {}).get("id", "")
        rerun = bool((parsed.get("run") or {}).get("rerun"))
        done = self.DONE_BY_RUN.setdefault(run, set())
        at = self.ORDER.index(phase) if phase in self.ORDER else -1
        if not (rerun or at == 0 or (at > 0 and self.ORDER[at - 1] in done)):
            self.REFUSED.append((run, phase, sorted(done)))
            return self._json(409, {"error": f"{phase} cannot run yet"})
        # ...AND NEVER THE SAME PHASE TWICE IN A RUN unless it is a rerun.
        # The predicate above only asks whether the PREDECESSOR ran, so a
        # replacement run driven one phase too deep starts `third`, the
        # resumed walk starts `third` again on that same run, and both
        # were accepted — a request production cannot make, waved
        # through by the fixture written to notice exactly that (Codex
        # round 35).
        if not rerun and phase in done:
            self.REFUSED.append((run, phase, sorted(done)))
            return self._json(409, {"error": f"{phase} already ran in {run}"})
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.BY_INV[invocation] = self._bearer()
        self.PHASE[invocation] = phase
        self.ASKED.append((run, phase))
        if rerun:
            self.RERUN.add(invocation)
        if not (rerun and phase == "second"):
            done.add(phase)
        self._json(201, {"invocation_id": invocation})


@pytest.mark.asyncio
async def test_the_replay_reaches_every_phase_behind_two_adjacent_gates():
    """`needs` is right for the shape with the most values of it live.

    Two ADJACENT gates put five transitions in the walk — a rerun of the
    phase under test, the next phase, a rerun of THAT, and two more
    phases — with `needs` taking three different values. The `second`
    rerun fails, so everything behind it has to be reached on a
    replacement run driven to the right depth: too shallow and this
    agent refuses the request as one its run cannot produce, too deep and
    the battery invokes a phase production would not have reached yet.

    Neither happens: nothing is refused, nothing is UNPROVEN, and the
    walk runs to the end.
    """
    from app.agents.manifest import PhaseSpec

    _TwoGateLifecycleAgent.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TwoGateLifecycleAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = load_manifest(ECHO_DIR)
        manifest = base.model_copy(update={"phases": [
            base.phases[0],
            PhaseSpec(name="second", approval=True),
            PhaseSpec(name="third", approval=True),
            PhaseSpec(name="fourth"),
        ]})
        result = await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}", manifest=manifest,
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()

    text = result.summary()
    # NOTHING ASKED OUT OF ORDER — read off the agent, not off the report.
    assert _TwoGateLifecycleAgent.REFUSED == [], _TwoGateLifecycleAgent.REFUSED
    assert "UNPROVEN" not in text, text
    assert result.checks["token_binding"] == "pass", text
    # THE REPLAY WENT DEEP ENOUGH: the replacement run replayed the phase
    # under test AND `second`, then carried the walk to its end on that
    # run. A shallower drive would have left `third` and `fourth` behind.
    asked = _TwoGateLifecycleAgent.ASKED
    fresh = [ph for run, ph in asked if run.startswith("battery-run-fresh")]
    # EXACTLY THIS SEQUENCE. "`third` appears somewhere" is satisfied by a
    # replay that drives one phase too deep and then has the resumed walk
    # ask for that phase again — measured: with `needs + 2` the earlier
    # form of this assertion still passed (Codex round 35).
    assert fresh == ["echo", "second", "third", "fourth"], asked


class _ReplayLegAgent(BaseHTTPRequestHandler):
    """`p1 -> p2 -> p3`, where `second` always ends the run.

    The walk's next-phase transition fails, so a replacement run is
    needed to reach `third` — and that run's `second` leg is where both
    round-35 findings live. `MODE` picks which:

    * `"500"` — the leg answers 500 where its stream should be. NOT a
      terminal: the chassis can produce that invocation and could not
      consume it, so the check did not conclude.
    * `"leak"` — the leg ends on a valid `failed` AND answers any token
      the agent ever issued.
    * `"noterminal"` — the leg's stream sends progress and then stops.
    * `"twoterminals"` — the leg sends `completed` and then `failed`.

    The last two are contract violations the chassis dies on, so neither
    may quietly shorten the plan the way a valid `failed` does.
    """

    MODE = "500"
    OWNER: dict[str, str] = {}
    PHASE: dict[str, str] = {}
    RUN: dict[str, str] = {}
    EVERY_TOKEN: set = set()
    ASKED: list[tuple] = []
    COUNT = [0]

    @classmethod
    def reset(cls, mode):
        cls.MODE = mode
        cls.OWNER = {}
        cls.PHASE = {}
        cls.RUN = {}
        cls.EVERY_TOKEN = set()
        cls.ASKED = []
        cls.COUNT = [0]

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else None

    def _is_replay_leg(self, invocation):
        return (self.PHASE.get(invocation) == "second"
                and self.RUN.get(invocation, "").startswith("battery-run-fresh"))

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        invocation = self.path.split("/v1/runs/")[-1].split("/")[0]
        leaky = self.MODE == "leak" and self._is_replay_leg(invocation)
        ok = (self._bearer() in self.EVERY_TOKEN if leaky
              else self.OWNER.get(invocation) == self._bearer())
        if not ok:
            return self._json(401, {"error": "unauthorized"})
        if self.path.endswith("/events"):
            if self.MODE == "500" and self._is_replay_leg(invocation):
                return self._json(500, {"error": "boom"})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"step_id": "s", "status": "running"}\n\n')
            done = {"output": {"echo": {"inv": invocation}}}
            if self._is_replay_leg(invocation) and self.MODE == "noterminal":
                # The stream just stops: no terminal at all.
                return
            if self._is_replay_leg(invocation) and self.MODE == "twoterminals":
                self.wfile.write(
                    b"event: completed\ndata: " + json.dumps(done).encode() + b"\n\n")
                self.wfile.write(
                    b'event: failed\ndata: {"error": "and again"}\n\n')
                return
            if self.PHASE.get(invocation) == "second":
                self.wfile.write(
                    b'event: failed\ndata: {"error": "battery fixture"}\n\n')
            else:
                self.wfile.write(
                    b"event: completed\ndata: " + json.dumps(done).encode() + b"\n\n")
            return
        if self.path.endswith("/output"):
            return self._json(200, {"output": {"echo": {"inv": invocation}}})
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        self.COUNT[0] += 1
        invocation = f"inv-{self.COUNT[0]}"
        self.OWNER[invocation] = self._bearer()
        self.PHASE[invocation] = parsed.get("phase")
        self.RUN[invocation] = (parsed.get("run") or {}).get("id", "")
        self.EVERY_TOKEN.add(self._bearer())
        self.ASKED.append((self.RUN[invocation], self.PHASE[invocation]))
        self._json(201, {"invocation_id": invocation})


async def _three_phase_replay(mode):
    from app.agents.manifest import PhaseSpec

    _ReplayLegAgent.reset(mode)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ReplayLegAgent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = load_manifest(ECHO_DIR)
        manifest = base.model_copy(update={"phases": [
            base.phases[0], PhaseSpec(name="second"), PhaseSpec(name="third")]})
        return await run_contract_battery(
            f"http://127.0.0.1:{server.server_address[1]}", manifest=manifest,
            scenario={"message": "x"}, settle_seconds=0.1,
        )
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_a_replay_leg_that_cannot_be_consumed_is_not_a_shorter_plan():
    """Only a valid `failed` terminal may shorten the walk.

    A replay leg answering 500 is an invocation the chassis can PRODUCE
    and cannot CONSUME, so the binding check did not conclude — it is not
    the agent exercising its prerogative to fail a phase. Measured before
    this guard: `token_binding: pass`, `passed: True`, no failures, and a
    report reading "it reached echo and then None" because the path that
    returned never said why.
    """
    result = await _three_phase_replay("500")
    text = result.summary()
    assert result.checks["token_binding"] == "fail", text
    assert result.passed is False, text
    # The reason is NAMED, not left as a `None` in the sentence.
    assert "answered 500 to its own events stream" in text, text
    assert "and then None" not in text, text
    # ...and the second run of the phase under test, which starts a run
    # of its own, is not cancelled by any of it.
    assert ("battery-run-2", "echo") in _ReplayLegAgent.ASKED, _ReplayLegAgent.ASKED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,why",
    [("noterminal", "a stream that never terminated"),
     ("twoterminals", "a second terminal event")],
)
async def test_only_a_valid_failed_terminal_shortens_the_replay(mode, why):
    """The two contract violations that are not a `failed` either.

    `leg_ended_failed` reads THREE things — the terminal is `failed`, the
    stream terminated at all, and it terminated once. A flag set on the
    kind alone lets a leg that never terminated, or that sent two
    terminals, shorten the walk exactly as a legal `failed` does; the
    chassis dies on both, so neither is the agent exercising a
    prerogative.

    FOUND BY THE INJECTION HARNESS, not by review: the two tests written
    for this round both happen to exercise paths that return before the
    flag is ever assigned, so `leg_ended_failed = True` came back
    `not-caught` (§12 204(g)).
    """
    result = await _three_phase_replay(mode)
    text = result.summary()
    assert result.checks["token_binding"] == "fail", (why, text)
    assert result.passed is False, (why, text)
    # NOT the sentence a legal `failed` earns.
    assert "could not be driven far enough" not in text, (why, text)
    assert "could not conclude" in text, (why, text)


@pytest.mark.asyncio
async def test_a_replay_leg_that_ends_failed_is_still_cross_probed():
    """An invocation the battery started is probed however it ended.

    The leg is allowed to end `failed` — that shortens the plan and is
    not charged to the agent. What is NOT allowed is for the battery to
    remember that invocation without testing it: measured before this
    guard, an agent whose failed replay leg answered every token it had
    ever issued took `token_binding: pass` with no failures at all.
    """
    result = await _three_phase_replay("leak")
    text = result.summary()
    assert result.checks["token_binding"] == "fail", text
    # Both leaves, and both directions — the leg is a full participant.
    assert ("events with the first invocation's token on fresh run 1's second "
            "answered 200, not 401") in text, text
    assert ("output with the first invocation's token on fresh run 1's second "
            "answered 200, not 401") in text, text
    # The legal `failed` still shortens the plan and still is not charged.
    assert "could not be driven far enough" in text, text
