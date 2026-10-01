"""The chassis OTLP relay for containers (blueprint S4; gap H9).

Authenticated by the run token: an active record admits a request whose
every span and log record carries the token's trace id, stamped with
the run identity and walked before it is forwarded; a request carrying
another trace id is refused ``403 trace_mismatch`` and nothing from it
lands; an ``ended`` record is honoured by the relay inside its grace
and by nothing else — an MCP ``tools/call`` with the same token is
refused — and refused by the relay once the grace has passed; the
walk strips the fixture from a container's own instrumentation and
drops its bytes.

Real Redis (the token registry is Redis-backed by design); the OTLP
senders are replaced by recorders.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from starlette.testclient import TestClient

from app.main import app as chassis_app
from app.observability import otlp_walk
from app.routers import otlp_relay

FIXTURE_EMAIL = "pii.fixture@example.com"
CARD = 4111111111111111
TRACE_HEX = "4bf92f3577b34da6a2ce929d0e0e4736"
OTHER_HEX = "0af7651916cd43dd8448eb211c80319c"


@pytest.fixture(scope="module")
def client():
    return TestClient(chassis_app)


class _Recorder:
    def __init__(self):
        self.traces = []
        self.logs = []

    def send_traces(self, request):
        self.traces.append(request)
        return True

    def send_logs(self, request):
        self.logs.append(request)
        return True

    enabled = True


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(otlp_relay, "_senders", rec)
    return rec


async def _register(state="active", *, trace_id=TRACE_HEX, ttl=300, **extra):
    import redis.asyncio as aioredis

    from app.config import settings

    token = f"relay-{uuid.uuid4().hex}"
    record = {
        "run_id": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "agent_id": "echo-v1",
        "grants": ["run_store", "pii"],
        "run_number": "RUN-1042",
        "trace_id": trace_id,
        "traceparent": f"00-{trace_id}-00f067aa0ba902b7-01" if trace_id else None,
        "state": state,
        **extra,
    }
    async with aioredis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True) as r:
        await r.set(f"run_token:{token}", json.dumps(record), ex=ttl)
    return token, record


def _trace_request(trace_hex=TRACE_HEX, *, name="agent work", attrs=None, resource=None):
    req = trace_service_pb2.ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    for k, v in (resource or {"service.name": "my-echo"}).items():
        kv = rs.resource.attributes.add()
        kv.key = k
        kv.value.string_value = v
    ss = rs.scope_spans.add()
    ss.scope.name = "agent.echo"
    span = ss.spans.add()
    span.trace_id = bytes.fromhex(trace_hex)
    span.span_id = os.urandom(8)
    span.name = name
    span.start_time_unix_nano = 1757534400000000000
    span.end_time_unix_nano = 1757534400001000000
    for k, v in (attrs or {}).items():
        kv = span.attributes.add()
        kv.key = k
        if isinstance(v, bytes):
            kv.value.bytes_value = v
        elif isinstance(v, int):
            kv.value.int_value = v
        else:
            kv.value.string_value = v
    return req


def _logs_request(trace_hex=TRACE_HEX, *, body="hello"):
    req = logs_service_pb2.ExportLogsServiceRequest()
    rl = req.resource_logs.add()
    sl = rl.scope_logs.add()
    sl.scope.name = "agent.echo"
    rec = sl.log_records.add()
    rec.trace_id = bytes.fromhex(trace_hex)
    rec.span_id = os.urandom(8)
    rec.body.string_value = body
    rec.time_unix_nano = 1757534400000000000
    return req


def _post(client, path, token, payload, content_type="application/x-protobuf"):
    headers = {"Content-Type": content_type}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(f"/api/v1/_o/otlp{path}", content=payload, headers=headers)


def _attrs(kvs):
    out = {}
    for kv in kvs:
        which = kv.value.WhichOneof("value")
        out[kv.key] = getattr(kv.value, which) if which else None
    return out


# ------------------------------------------------------------ the happy path


@pytest.mark.asyncio
async def test_a_container_trace_is_stamped_walked_and_forwarded(client, recorder):
    token, record = await _register()
    req = _trace_request(
        name=f"custom {FIXTURE_EMAIL}",
        attrs={"note": f"see {FIXTURE_EMAIL}", "blob": FIXTURE_EMAIL.encode()},
    )
    r = _post(client, "/v1/traces", token, req.SerializeToString())
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["accepted"] == 1 and body["forwarded"] is True
    assert body["walked"]["bytes_dropped"] == 1
    (forwarded,) = recorder.traces
    dump = forwarded.SerializeToString()
    assert FIXTURE_EMAIL.encode() not in dump
    span = forwarded.resource_spans[0].scope_spans[0].spans[0]
    assert span.name == "custom [REDACTED_EMAIL_ADDRESS_1]"
    attrs = _attrs(span.attributes)
    assert attrs["librerun.scope"] == "run"
    assert attrs["agent.id"] == "echo-v1"
    assert attrs["run.id"] == record["run_id"]
    assert attrs["tenant.id"] == record["tenant_id"]
    assert attrs["run.number"] == "RUN-1042"
    assert attrs[otlp_relay.SOURCE_ATTRIBUTE] == "container"
    assert attrs["note"] == "see [REDACTED_EMAIL_ADDRESS_1]"
    assert "blob" not in attrs and attrs[otlp_walk.BYTES_DROPPED_ATTRIBUTE] == 1
    resource = _attrs(forwarded.resource_spans[0].resource.attributes)
    assert resource["service.name"] == "my-echo"
    assert resource["agent.id"] == "echo-v1" and resource["librerun.scope"] == "run"


@pytest.mark.asyncio
async def test_a_container_log_record_is_stamped_walked_and_forwarded(client, recorder):
    token, record = await _register()
    r = _post(client, "/v1/logs", token, _logs_request(body=f"mail {FIXTURE_EMAIL}").SerializeToString())
    assert r.status_code == 202, r.text
    (forwarded,) = recorder.logs
    rec = forwarded.resource_logs[0].scope_logs[0].log_records[0]
    assert rec.body.string_value == "mail [REDACTED_EMAIL_ADDRESS_1]"
    attrs = _attrs(rec.attributes)
    assert attrs["agent.id"] == "echo-v1" and attrs["run.id"] == record["run_id"]


@pytest.mark.asyncio
async def test_a_container_never_masquerades_as_the_chassis(client, recorder):
    token, _ = await _register()
    for name in ("librerun-backend", ""):
        req = _trace_request(resource={"service.name": name})
        r = _post(client, "/v1/traces", token, req.SerializeToString())
        assert r.status_code == 202
    for forwarded in recorder.traces:
        assert _attrs(forwarded.resource_spans[0].resource.attributes)["service.name"] == "echo-v1"


@pytest.mark.asyncio
async def test_a_flagged_attribute_key_strips_the_span_before_it_is_forwarded(client, recorder):
    token, _ = await _register()
    req = _trace_request(attrs={FIXTURE_EMAIL: "x", "n": CARD})
    r = _post(client, "/v1/traces", token, req.SerializeToString())
    assert r.status_code == 202
    assert r.json()["walked"]["spans_stripped"] == 1
    span = recorder.traces[0].resource_spans[0].scope_spans[0].spans[0]
    assert span.name == otlp_walk.STRIPPED_SPAN_NAME
    assert _attrs(span.attributes)["agent.id"] == "echo-v1"
    assert FIXTURE_EMAIL.encode() not in recorder.traces[0].SerializeToString()


# ------------------------------------------------------------ one token, one trace


@pytest.mark.asyncio
async def test_another_trace_id_is_refused_and_nothing_lands(client, recorder):
    token, _ = await _register()
    req = _trace_request(OTHER_HEX)
    r = _post(client, "/v1/traces", token, req.SerializeToString())
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "trace_mismatch"
    # A mixed batch is refused whole.
    mixed = _trace_request(TRACE_HEX)
    other = mixed.resource_spans[0].scope_spans[0].spans.add()
    other.trace_id = bytes.fromhex(OTHER_HEX)
    other.span_id = os.urandom(8)
    other.name = "smuggled"
    r = _post(client, "/v1/traces", token, mixed.SerializeToString())
    assert r.status_code == 403
    r = _post(client, "/v1/logs", token, _logs_request(OTHER_HEX).SerializeToString())
    assert r.status_code == 403
    assert recorder.traces == [] and recorder.logs == []


@pytest.mark.asyncio
async def test_a_token_without_a_trace_authorizes_no_telemetry(client, recorder):
    token, _ = await _register(trace_id=None)
    r = _post(client, "/v1/traces", token, _trace_request().SerializeToString())
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "run_token_without_trace"


def test_no_token_or_an_unknown_token_is_refused(client, recorder):
    r = _post(client, "/v1/traces", None, _trace_request().SerializeToString())
    assert r.status_code == 401
    r = _post(client, "/v1/traces", "nope", _trace_request().SerializeToString())
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "run_token_unknown"
    assert recorder.traces == []


@pytest.mark.asyncio
async def test_an_ended_token_is_honoured_by_the_relay_inside_the_grace_and_by_nothing_else(
    client, recorder
):
    from tests.test_capability_mcp import _rpc

    token, _ = await _register("ended", ttl=30)
    r = _post(client, "/v1/traces", token, _trace_request().SerializeToString())
    assert r.status_code == 202
    assert len(recorder.traces) == 1
    # The MCP server refuses the same token from the moment it ended.
    r = _rpc(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "redact", "arguments": {"text": "x"}}},
        token=token,
    )
    assert r.status_code == 401 and r.json()["error"]["code"] == -32001
    r = _rpc(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, token=token)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_an_ended_token_past_its_grace_is_refused(client, recorder):
    """The runner sets the grace as the record's TTL; once it has passed
    the key is gone and the relay answers as for any unknown token."""
    import redis.asyncio as aioredis

    from app.config import settings

    token, _ = await _register("ended", ttl=30)
    async with aioredis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True) as r_:
        await r_.delete(f"run_token:{token}")  # what expiry does at the end of the grace
    r = _post(client, "/v1/traces", token, _trace_request().SerializeToString())
    assert r.status_code == 401
    assert recorder.traces == []


# ------------------------------------------------------------ the body


@pytest.mark.asyncio
async def test_bad_bodies_and_media_types_are_refused(client, recorder):
    token, _ = await _register()
    r = _post(client, "/v1/traces", token, b"\xff\xff not protobuf")
    assert r.status_code == 400
    r = _post(client, "/v1/traces", token, b"{}", content_type="application/json")
    assert r.status_code == 415
    assert recorder.traces == []


@pytest.mark.asyncio
async def test_forwarding_off_accepts_walks_and_drops(client, monkeypatch):
    from app import config as _config

    monkeypatch.setattr(_config.settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
    otlp_relay._reset_for_tests()
    try:
        token, _ = await _register()
        r = _post(client, "/v1/traces", token, _trace_request().SerializeToString())
        assert r.status_code == 202
        assert r.json() == {"accepted": 1, "forwarded": False, "walked": otlp_walk.WalkReport().as_dict()}
    finally:
        otlp_relay._reset_for_tests()


# ------------------------------------------------------------ the runner's side


@pytest.mark.asyncio
async def test_the_container_runner_ends_the_token_with_the_grace(tmp_path):
    """After ``run_phase`` the token record is ``ended`` with the grace as
    its TTL — the relay's window — rather than deleted."""
    import redis.asyncio as aioredis

    from app.agents.container import RUN_TOKEN_END_GRACE_SECONDS, ContainerAgent
    from app.agents.manifest import load_manifest
    from app.config import settings
    from tests.test_container_runner import (
        _ProgressCollector,
        _agent_input,
        _write_container_dir,
        scripted_url,  # noqa: F401
    )

    pytest.importorskip("yaml")
    from tests import test_container_runner as tcr

    server = tcr.ThreadingHTTPServer(("127.0.0.1", 0), tcr._ScriptedHandler)
    thread = tcr.threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        d = _write_container_dir(tmp_path)
        agent = ContainerAgent(load_manifest(d), url, d)
        seen: dict = {}
        original = agent._register_run_token

        async def _register(token, inp, trace_headers=None, deadline=None):
            seen["token"] = token
            await original(token, inp, trace_headers, deadline)

        agent._register_run_token = _register
        await agent.run_phase(
            "echo", _agent_input(user_inputs={"mode": "lazy"}, run_number="RUN-7"), _ProgressCollector()
        )
    finally:
        server.shutdown()
    async with aioredis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True) as r:
        raw = await r.get(f"run_token:{seen['token']}")
        ttl = await r.ttl(f"run_token:{seen['token']}")
    record = json.loads(raw)
    assert record["state"] == "ended"
    assert record["run_number"] == "RUN-7"
    assert 0 < ttl <= RUN_TOKEN_END_GRACE_SECONDS


@pytest.mark.asyncio
async def test_a_container_cannot_forge_the_chassis_stamp_marker(client, recorder):
    """The marker is the chassis vouching for its own writes.

    ``librerun.stamped`` tells the walk "I wrote these exact pairs, leave
    them alone". A container that sent one would be exempting its own
    attributes from redaction — so the relay deletes any that arrives,
    on every container of attributes, before the walk runs. Without this
    the walk is optional for anyone who reads the source.
    """
    from app.observability import otlp_walk

    token, _ = await _register()
    forged = json.dumps({
        "user.id": FIXTURE_EMAIL,
        "librerun.note": FIXTURE_EMAIL,
        "note": FIXTURE_EMAIL,
    })
    req = _trace_request(attrs={
        "user.id": FIXTURE_EMAIL,
        "librerun.note": FIXTURE_EMAIL,
        "note": FIXTURE_EMAIL,
        otlp_walk.STAMP_MARKER: forged,
    })
    r = _post(client, "/v1/traces", token, req.SerializeToString())
    assert r.status_code == 202, r.text

    (forwarded,) = recorder.traces
    assert FIXTURE_EMAIL.encode() not in forwarded.SerializeToString()
    span = forwarded.resource_spans[0].scope_spans[0].spans[0]
    attrs = _attrs(span.attributes)
    assert otlp_walk.STAMP_MARKER not in attrs, "the forged marker was forwarded"
    assert attrs["librerun.note"] == "[REDACTED_EMAIL_ADDRESS_1]"
    assert attrs["note"] == "[REDACTED_EMAIL_ADDRESS_1]"
    # `user.id` is an identity key, so the relay overwrites it from the
    # token after the walk — with the run's tenant reality, not the
    # container's claim.
    assert attrs["user.id"] != FIXTURE_EMAIL


@pytest.mark.asyncio
async def test_a_container_claiming_the_chassis_service_name_is_renamed(client, recorder):
    """And the masquerade check reads what the container sent.

    It used to run after the walk, where ``librerun-backend`` has become
    a placeholder — the recognizers read it as a person's name — so the
    comparison missed and the masquerade survived under a redaction.
    """
    token, _ = await _register()
    req = _trace_request(resource={"service.name": "librerun-backend"})
    r = _post(client, "/v1/traces", token, req.SerializeToString())
    assert r.status_code == 202, r.text

    (forwarded,) = recorder.traces
    resource = _attrs(forwarded.resource_spans[0].resource.attributes)
    assert resource["service.name"] == "echo-v1"
