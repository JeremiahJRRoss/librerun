"""What a container actually exports, through the real relay (blueprint
S4 Accept; gaps H9, J3).

Every other test of the walk builds the protobuf by hand. That proves
the walker, not the path: an agent's telemetry only reaches an operator
if its SDK encodes it, its export arrives at the chassis under a run
token, and the relay walks what it finds. Any of those three could be
wrong while the walker is perfect.

So this drives the shipped echo agent — the same module the container
image runs — against a live chassis. The agent's ``instrument`` switch
puts the fixture into the eight positions the S4 acceptance names: a
span name, a string attribute, an int64 attribute, a span event and its
attribute, a span link attribute, a log record body, and a ``bytes``
attribute carrying it encoded. Nothing here calls ``ctx.log()``: this is
the agent's own instrumentation, the part the chassis does not write.

Real Redis for the token registry (that is how run tokens work); the
forward to Vector is a recorder, so the assertions are on exactly the
bytes that would have left the box.
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
import uuid
from pathlib import Path

import httpx
import pytest

from app.main import app as chassis_app
from app.observability import otlp_walk
from app.routers import otlp_relay

FIXTURE_EMAIL = "pii.fixture@example.com"
PLACEHOLDER = "[REDACTED_EMAIL_ADDRESS_1]"
CARD = 4111111111111111  # Luhn-valid: flags the span that carries it
ECHO_DIR = Path(__file__).resolve().parents[1] / "agents" / "_examples" / "echo_container"


class _Recorder:
    """Where Vector would be."""

    enabled = True

    def __init__(self):
        self.traces = []
        self.logs = []

    def send_traces(self, request):
        self.traces.append(request)
        return True

    def send_logs(self, request):
        self.logs.append(request)
        return True

    def dump(self) -> bytes:
        return b"".join(
            r.SerializeToString() for r in (*self.traces, *self.logs)
        )

    def spans(self):
        return [
            s
            for r in self.traces
            for rs in r.resource_spans
            for ss in rs.scope_spans
            for s in ss.spans
        ]

    def resources(self):
        return [rs.resource for r in self.traces for rs in r.resource_spans]

    def records(self):
        return [
            rec
            for r in self.logs
            for rl in r.resource_logs
            for sl in rl.scope_logs
            for rec in sl.log_records
        ]


@pytest.fixture(scope="module")
def chassis():
    """The real chassis under uvicorn — the relay has to be reachable
    over HTTP, because the agent's exporter speaks HTTP and nothing
    else. A live server runs the lifespan, which refuses to boot on the
    default secret (blueprint S3), so the fixture supplies one."""
    from librerun_agent.testing import serve_in_thread

    from app.config import settings

    previous = settings.APP_SECRET_KEY
    settings.APP_SECRET_KEY = "test-secret-for-the-relay-fixture"
    try:
        handle = serve_in_thread(chassis_app)
        yield handle.url
        handle.stop()
    finally:
        settings.APP_SECRET_KEY = previous


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(otlp_relay, "_senders", rec)
    return rec


async def _mint(trace_hex: str, *, with_record: bool = False):
    import redis.asyncio as aioredis

    from app.config import settings

    token = f"echo-{uuid.uuid4().hex}"
    record = {
        "run_id": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "agent_id": "echo-v1",
        "grants": ["run_store", "pii", "audit"],
        "run_number": "RUN-2026",
        "trace_id": trace_hex,
        "traceparent": f"00-{trace_hex}-00f067aa0ba902b7-01",
        "state": "active",
    }
    async with aioredis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True) as r:
        await r.set(f"run_token:{token}", json.dumps(record), ex=300)
    return (token, record) if with_record else token


@pytest.fixture(scope="module")
def echo(chassis):
    """The echo agent exporting into the chassis relay, as the container
    does: ``OTEL_EXPORTER_OTLP_ENDPOINT`` at ``/api/v1/_o/otlp``."""
    from librerun_agent import _capture, _otel
    from librerun_agent.testing import serve_in_thread

    _otel._reset_for_tests()
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{chassis}/api/v1/_o/otlp"
    # The suite turns the SDK's stdio capture off process-wide (pytest's
    # streams are pytest's). This module is the one that needs it on,
    # because a print() with nowhere to go is the whole point of
    # `docker logs` being empty. A CapturingWriter with no invocation
    # bound falls through to the original stream, so pytest keeps its
    # own output either way.
    previous_capture = os.environ.get("LIBRERUN_AGENT_CAPTURE_STDIO")
    os.environ["LIBRERUN_AGENT_CAPTURE_STDIO"] = "1"
    try:
        spec = importlib.util.spec_from_file_location("echo_instrumented", ECHO_DIR / "echo_agent.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handle = serve_in_thread(module.app)
        yield handle.url
        handle.stop()
    finally:
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        if previous_capture is None:
            os.environ.pop("LIBRERUN_AGENT_CAPTURE_STDIO", None)
        else:
            os.environ["LIBRERUN_AGENT_CAPTURE_STDIO"] = previous_capture
        if _capture.capture_installed():
            _capture.uninstall_stdio_capture()
        _otel._reset_for_tests()


async def _invoke(url: str, token: str, trace_hex: str, payload: dict) -> dict:
    """One phase, driven the way the chassis drives a container."""
    headers = {
        "Authorization": f"Bearer {token}",
        "traceparent": f"00-{trace_hex}-00f067aa0ba902b7-01",
    }
    body = {
        "contract": "v1",
        "agent_id": "echo-v1",
        "phase": "echo",
        "deadline_seconds": 60,
        "run": {"id": "run-1", "tenant_id": "t-1", "rerun": False},
        "input": payload,
        "prior_output": None,
        "user_edits": None,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        started = await client.post(f"{url}/v1/runs", json=body, headers=headers)
        assert started.status_code in (200, 201), started.text
        invocation = started.json()["invocation_id"]
        async with client.stream(
            "GET", f"{url}/v1/runs/{invocation}/events",
            headers={**headers, "Accept": "text/event-stream"},
        ) as stream:
            text = "".join([chunk async for chunk in stream.aiter_text()])
    events = {}
    name = None
    for line in text.splitlines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:") and name:
            events.setdefault(name, []).append(json.loads(line[5:].strip() or "{}"))
    assert "completed" in events, text[:400]
    return events["completed"][0].get("output") or {}


def _settle(recorder, *, want=1, seconds=8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and len(recorder.traces) < want:
        time.sleep(0.1)
    time.sleep(0.4)


def _attrs(kvs):
    out = {}
    for kv in kvs:
        which = kv.value.WhichOneof("value")
        out[kv.key] = getattr(kv.value, which) if which else None
    return out


@pytest.mark.asyncio
async def test_the_containers_own_instrumentation_reaches_the_forwarder_walked(echo, recorder):
    """The whole path, once: the agent's own span, event, link, log
    record and bytes attribute, exported by its SDK, arriving under the
    token, walked before the forward."""
    trace_hex = uuid.uuid4().hex
    token = await _mint(trace_hex)

    output = await _invoke(echo, token, trace_hex, {"instrument": FIXTURE_EMAIL, "message": "hi"})
    assert output["echo"]["message"] == "hi"

    from librerun_agent import _otel

    _otel.flush()
    _settle(recorder)

    dump = recorder.dump()
    assert dump, "the container exported nothing to the relay"
    assert FIXTURE_EMAIL.encode() not in dump, "the fixture reached the forwarder"

    spans = recorder.spans()
    assert spans, "no span was forwarded"
    assert all(s.trace_id.hex() == trace_hex for s in spans), "a span carried another trace id"

    custom = next(s for s in spans if s.name.startswith("custom "))
    assert custom.name == f"custom {PLACEHOLDER}"
    attributes = _attrs(custom.attributes)
    assert attributes["note"] == PLACEHOLDER
    # The agent set `blob` to the address ENCODED. It arrives as a string
    # anyway: opentelemetry-sdk decodes a bytes attribute before the OTLP
    # encoder sees it (`_clean_attribute`), so a Python agent cannot
    # reach the protobuf's `bytes_value` position through the tracing
    # API at all. Which is fine — it means the value is walked as content
    # and comes out as the placeholder rather than being dropped. The
    # `bytes_value` position itself is another language's to produce, and
    # test_otlp_walk.py exercises it on the protobuf directly.
    assert attributes["blob"] == PLACEHOLDER
    assert custom.events[0].name == f"event {PLACEHOLDER}"
    assert _attrs(custom.events[0].attributes)["who"] == PLACEHOLDER
    assert _attrs(custom.links[0].attributes)["why"] == PLACEHOLDER

    # The identity the relay stamps from the token, not from the agent.
    resource = _attrs(recorder.resources()[0].attributes)
    assert resource["agent.id"] == "echo-v1"
    assert resource["librerun.scope"] == "run"
    assert resource[otlp_relay.SOURCE_ATTRIBUTE]

    bodies = [r.body.string_value for r in recorder.records()]
    assert any(PLACEHOLDER in b for b in bodies), bodies


@pytest.mark.asyncio
async def test_a_flagged_number_in_the_containers_own_span_strips_it_to_identity(echo, recorder):
    """An int64 attribute is the position a redactor cannot fix: there is
    no placeholder for a number, so the span is stripped to its identity
    and the number never leaves the box."""
    trace_hex = uuid.uuid4().hex
    token = await _mint(trace_hex)

    await _invoke(echo, token, trace_hex, {"instrument": "a note", "instrument_number": CARD})

    from librerun_agent import _otel

    _otel.flush()
    _settle(recorder)

    dump = recorder.dump()
    assert str(CARD).encode() not in dump, "the card number reached the forwarder"
    names = [s.name for s in recorder.spans()]
    assert otlp_walk.STRIPPED_SPAN_NAME in names, names
    stripped = next(s for s in recorder.spans() if s.name == otlp_walk.STRIPPED_SPAN_NAME)
    kept = _attrs(stripped.attributes)
    assert "instrument_number" not in kept and "note" not in kept
    assert kept[otlp_walk.REDACTED_ATTRIBUTE] is True
    assert stripped.trace_id.hex() == trace_hex
    assert len(stripped.events) == 0


@pytest.mark.asyncio
async def test_a_print_inside_the_container_reaches_the_run_plane_walked(echo, recorder):
    """``docker logs`` is empty by construction, so a ``print()`` is only
    ever seen if the SDK turns it into a record of that invocation. The
    line is the agent's, the walk is the chassis's."""
    from librerun_agent.testing import capture_stdio

    trace_hex = uuid.uuid4().hex
    token = await _mint(trace_hex)

    # pytest reassigns sys.stdout between tests, which leaves the writer
    # the agent's server installed at startup unreachable. Re-install it
    # around the invocation; the container has no such problem.
    with capture_stdio():
        await _invoke(echo, token, trace_hex, {"print": f"printed {FIXTURE_EMAIL}", "message": "x"})

    from librerun_agent import _otel

    _otel.flush()
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and not recorder.logs:
        time.sleep(0.1)
    time.sleep(0.4)

    assert FIXTURE_EMAIL.encode() not in recorder.dump()
    records = recorder.records()
    assert records, "the printed line never reached the relay"
    printed = next(r for r in records if r.body.string_value.startswith("printed "))
    assert printed.body.string_value == f"printed {PLACEHOLDER}"
    assert printed.trace_id.hex() == trace_hex


@pytest.mark.asyncio
async def test_an_export_carrying_another_trace_id_lands_nothing(echo, recorder):
    """Negative: the agent is instrumented and exporting, but its token
    is bound to a different trace. Nothing it sends may be forwarded —
    otherwise the checks above would pass on any container that merely
    exports something."""
    bound = uuid.uuid4().hex
    token = await _mint(bound)
    other = uuid.uuid4().hex  # what the invocation actually traces under

    await _invoke(echo, token, other, {"instrument": FIXTURE_EMAIL})

    from librerun_agent import _otel

    _otel.flush()
    time.sleep(1.0)

    assert recorder.traces == [] and recorder.logs == [], (
        "a container exporting under a trace its token does not own was forwarded"
    )


@pytest.mark.asyncio
async def test_threads_and_pool_jobs_print_under_the_invocations_trace(echo, recorder):
    """A handler that spawns a thread and submits a pool job: both lines
    land as records of that invocation's trace.

    Context propagation is the part an agent author never thinks about
    and the platform cannot recover from afterwards — a line with no
    trace is a line nobody finds.
    """
    from librerun_agent.testing import capture_stdio

    trace_hex = uuid.uuid4().hex
    token = await _mint(trace_hex)

    with capture_stdio():
        await _invoke(echo, token, trace_hex, {"threads": FIXTURE_EMAIL, "message": "x"})

    from librerun_agent import _otel

    _otel.flush()
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and len(recorder.records()) < 3:
        time.sleep(0.1)
    time.sleep(0.4)

    assert FIXTURE_EMAIL.encode() not in recorder.dump()
    bodies = {r.body.string_value for r in recorder.records()}
    for prefix in ("thread ", "job ", "callback "):
        line = next((b for b in bodies if b.startswith(prefix)), None)
        assert line == f"{prefix}{PLACEHOLDER}", f"{prefix!r} missing from {sorted(bodies)}"
    assert all(r.trace_id.hex() == trace_hex for r in recorder.records())


@pytest.mark.asyncio
async def test_the_agent_asks_the_chassis_to_redact_and_gets_the_intake_pipeline(echo, chassis, recorder):
    """``ctx.pii.redact`` over MCP, from the agent, against the real
    chassis: the answer is the intake pipeline's own redaction of the
    same text, not an approximation of it."""
    from app.services import pii_service

    trace_hex = uuid.uuid4().hex
    token = await _mint(trace_hex)
    text = f"mail me at {FIXTURE_EMAIL}"

    headers = {
        "Authorization": f"Bearer {token}",
        "traceparent": f"00-{trace_hex}-00f067aa0ba902b7-01",
    }
    body = {
        "contract": "v1",
        "agent_id": "echo-v1",
        "phase": "echo",
        "deadline_seconds": 60,
        "run": {
            "id": "run-1",
            "tenant_id": "t-1",
            "rerun": False,
            "mcp": {"url": f"{chassis}/api/v1/mcp"},
        },
        "input": {"redact": text},
        "prior_output": None,
        "user_edits": None,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        started = await client.post(f"{echo}/v1/runs", json=body, headers=headers)
        assert started.status_code in (200, 201), started.text
        invocation = started.json()["invocation_id"]
        async with client.stream(
            "GET", f"{echo}/v1/runs/{invocation}/events",
            headers={**headers, "Accept": "text/event-stream"},
        ) as stream:
            text_stream = "".join([chunk async for chunk in stream.aiter_text()])

    completed = None
    name = None
    for line in text_stream.splitlines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:") and name == "completed":
            completed = json.loads(line[5:].strip() or "{}")
    assert completed is not None, text_stream[:400]
    output = completed.get("output") or {}
    assert output["redacted"] == pii_service.redact(text)[0]
    assert FIXTURE_EMAIL not in output["redacted"]


@pytest.mark.asyncio
async def test_text_the_agent_fetched_itself_is_walked_on_every_way_out(echo, recorder):
    """The case intake cannot help with (gap J3).

    Everything else here sends the fixture IN. An `x-pii` input reaches
    the handler already replaced by the placeholder, so a check driven
    that way proves intake redaction and says nothing about the export
    walk. `fetch_and_emit` makes the agent use an address it holds
    itself — a ticket it fetched, a document it read — which the chassis
    has never seen and cannot have redacted on the way in.

    Every way out is then the walk's: the span, the log record, the
    printed line, and the output the chassis would persist.
    """
    from librerun_agent.testing import capture_stdio

    trace_hex = uuid.uuid4().hex
    token = await _mint(trace_hex)

    with capture_stdio():
        output = await _invoke(echo, token, trace_hex, {"message": "boundary", "fetch_and_emit": True})

    # What the agent returns still carries it: the agent is not the one
    # who redacts, which is exactly why the chassis walks the output.
    assert FIXTURE_EMAIL in output["fetched"]

    from app.services import pii_service

    walked = pii_service.walk(output, quiet=True)
    assert not walked.refused
    assert FIXTURE_EMAIL not in json.dumps(walked.value)
    assert PLACEHOLDER in walked.value["fetched"]

    from librerun_agent import _otel

    _otel.flush()
    _settle(recorder)

    dump = recorder.dump()
    assert dump, "the container exported nothing"
    assert FIXTURE_EMAIL.encode() not in dump, "the address the agent fetched reached the forwarder"
    assert PLACEHOLDER.encode() in dump, "nothing carrying it was exported at all"


@pytest.mark.asyncio
async def test_two_runs_under_one_upstream_trace_keep_their_own_identities(echo, recorder):
    """Sharing a trace id must not share a token.

    A caller can submit several runs under one upstream W3C trace — of
    different tenants, even — and the SDK used to key its token registry
    by trace id. The second registration replaced the first's token, so
    the first invocation's spans left under the second's token and the
    relay stamped them as the second run: one tenant's telemetry filed
    under another's.
    """
    trace_hex = uuid.uuid4().hex          # the SAME upstream trace
    first, first_run = await _mint(trace_hex, with_record=True)
    second, second_run = await _mint(trace_hex, with_record=True)
    assert first != second and first_run["run_id"] != second_run["run_id"]

    await _invoke(echo, first, trace_hex, {"instrument": "first run", "message": "a"})
    await _invoke(echo, second, trace_hex, {"instrument": "second run", "message": "b"})

    from librerun_agent import _otel

    _otel.flush()
    _settle(recorder, want=2)

    # The relay stamps `run.id` from the token it was presented with, so
    # the run each span is filed under IS the token it left under.
    named = {s.name: _attrs(s.attributes) for s in recorder.spans()}
    assert "custom first run" in named, sorted(named)
    assert "custom second run" in named, sorted(named)
    assert named["custom first run"]["run.id"] == first_run["run_id"]
    assert named["custom second run"]["run.id"] == second_run["run_id"], (
        "the second invocation's spans were filed under the first run"
    )
