"""The chassis OTLP relay for container agents (blueprint S4; gap H9).

``POST /api/v1/_o/otlp/v1/traces`` and ``/v1/logs``, beside the browser
relay: the one way a container's telemetry reaches Vector. The agents
network is internal (blueprint S4, ``compose.yaml``), so no path from a
container to Vector exists but this one, and this one is authenticated
by the **run token** the container already holds — the per-invocation
bearer of Run Contract v1, the one credential that exists at this batch.

**One token, one trace.** The relay looks the token up
(``run_token:{token}``: the agent, the run, the trace id the run was
minted with, the phase span's ``traceparent``, the record's state) and
accepts the request only if every span and every log record in it
carries that token's trace id — ``403 trace_mismatch`` otherwise, the
whole request refused — so a token authorizes exactly its own run's
trace and nothing else, whatever the agent's concurrency: an agent
writes into no other run's trace and never into the platform plane.
The runner marks the record ``ended`` the moment the invocation
completes or fails; the MCP server (and the S4a gateway) accept
``active`` records only, and the relay alone honours an ``ended``
record inside the short grace the record then lives for
(``RUN_TOKEN_END_GRACE_SECONDS``), so a late batch still lands and
nothing else does.

Then the relay stamps ``librerun.scope=run``, ``agent.id`` and the run
identity from the token onto the resource and every span and record,
walks the decoded request with the one walker over the OTLP model
(``otlp_walk`` — links, resource and scope included) and forwards it
to Vector with the same senders the backend's own exporter uses. A
blank ``OTEL_EXPORTER_OTLP_ENDPOINT`` accepts, walks and drops, exactly
like the browser relay.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2

from app import config as _config
from app.agents.container import RUN_TOKEN_END_GRACE_SECONDS  # noqa: F401  (re-exported for callers)
from app.observability import otlp_walk
from app.observability.walkers import build_log_sender, build_sender

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/_o/otlp", tags=["observability"])

# OTLP/HTTP protobuf only: the SDK's exporter speaks it, Vector accepts
# it, and a JSON encoding would be a second parser for no caller.
PROTOBUF_CONTENT_TYPE = "application/x-protobuf"
MAX_BODY_BYTES = 4 * 1024 * 1024
SOURCE_ATTRIBUTE = "librerun.telemetry.source"
SOURCE_VALUE = "container"


class _Senders:
    """Lazily built OTLP senders (traces, logs), endpoint-gated."""

    def __init__(self) -> None:
        self._traces: Any = None
        self._logs: Any = None
        self._resolved = False
        self._enabled = False

    def _resolve(self) -> None:
        if self._resolved:
            return
        self._resolved = True
        settings = _config.settings
        endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
        if not endpoint:
            logger.info(
                "otlp_relay_forwarding_off",
                reason="OTEL_EXPORTER_OTLP_ENDPOINT is blank — accepted requests are walked and dropped",
            )
            return
        try:
            self._traces = build_sender(endpoint, settings.OTEL_EXPORTER_OTLP_PROTOCOL)
            self._logs = build_log_sender(endpoint, settings.OTEL_EXPORTER_OTLP_PROTOCOL)
            self._enabled = True
        except Exception as exc:  # noqa: BLE001 — observability must not break serving
            logger.warning("otlp_relay_sender_unavailable", error=str(exc)[:200])

    @property
    def enabled(self) -> bool:
        self._resolve()
        return self._enabled

    def send_traces(self, request) -> bool:
        self._resolve()
        if self._traces is None:
            return False
        return _ok(self._traces.send(request))

    def send_logs(self, request) -> bool:
        self._resolve()
        if self._logs is None:
            return False
        return _ok(self._logs.send(request))


def _ok(result) -> bool:
    name = getattr(result, "name", None)
    return name == "SUCCESS" or result is True


_senders = _Senders()


def _reset_for_tests() -> None:
    global _senders
    _senders = _Senders()


# --------------------------------------------------------------------------
# Token
# --------------------------------------------------------------------------


async def _resolve_run_token(authorization: str | None) -> dict:
    """The token's record, active or inside its grace; 401 otherwise."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"error": "run_token_required"})
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"error": "run_token_required"})
    async with aioredis.from_url(
        _config.settings.REDIS_URL.get_secret_value(), decode_responses=True
    ) as redis:
        raw = await redis.get(f"run_token:{token}")
    if raw is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"error": "run_token_unknown"})
    try:
        record = json.loads(raw)
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"error": "run_token_unknown"})
    if not isinstance(record, dict) or not record.get("trace_id"):
        # A token minted before the run's trace travelled with it (or
        # with tracing off) authorizes no telemetry.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"error": "run_token_without_trace"})
    # ``ended`` inside the grace is the relay's — and only the relay's —
    # to honour: the key expires at the end of the grace on its own.
    return record


# --------------------------------------------------------------------------
# Body
# --------------------------------------------------------------------------


async def _read_protobuf(request: Request) -> bytes:
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type != PROTOBUF_CONTENT_TYPE:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            {"error": "unsupported_media_type", "expected": PROTOBUF_CONTENT_TYPE},
        )
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, {"error": "body_too_large"})
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, {"error": "bad_content_length"})
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, {"error": "body_too_large"})
        chunks.append(chunk)
    return b"".join(chunks)


# --------------------------------------------------------------------------
# Trace match and stamping
# --------------------------------------------------------------------------


def _hex(trace_id: bytes) -> str:
    return trace_id.hex()


def _set_attribute(kvs, key: str, value) -> None:
    for kv in kvs:
        if kv.key == key:
            break
    else:
        kv = kvs.add()
        kv.key = key
    if isinstance(value, bool):
        kv.value.bool_value = value
    elif isinstance(value, int):
        kv.value.int_value = value
    else:
        kv.value.string_value = str(value)


def _identity(record: dict) -> dict[str, Any]:
    identity = {
        "librerun.scope": "run",
        "agent.id": str(record.get("agent_id") or ""),
        "run.id": str(record.get("run_id") or ""),
        "tenant.id": str(record.get("tenant_id") or ""),
        SOURCE_ATTRIBUTE: SOURCE_VALUE,
    }
    if record.get("run_number"):
        identity["run.number"] = str(record["run_number"])
        identity["case.number"] = str(record["run_number"])  # S1 duplicate, dropped at v1.1
    if record.get("run_id"):
        identity["case.id"] = str(record["run_id"])
    return identity


def _claim_service_name(resource, identity: dict[str, Any]) -> None:
    """A container never masquerades as the chassis: its service is its
    agent id unless it named itself something else.

    Decided BEFORE the walk, while the value is still the one the
    container sent. Afterwards ``librerun-backend`` has been redacted to
    something else — the recognizers read it as a person's name — and
    the comparison would no longer match, leaving the masquerade in
    place under a placeholder.
    """
    service = next((kv for kv in resource.attributes if kv.key == "service.name"), None)
    if service is None or service.value.string_value in ("", "librerun-backend"):
        _set_attribute(resource.attributes, "service.name", identity["agent.id"] or "agent")


def _stamp_resource(resource, identity: dict[str, Any]) -> None:
    for key in ("librerun.scope", "agent.id", SOURCE_ATTRIBUTE):
        _set_attribute(resource.attributes, key, identity[key])


def _drop_forged_markers(kvs) -> None:
    """Remove the chassis's stamp marker from anything a container sent.

    The marker is how the chassis tells its own walk "I wrote these
    exact pairs, leave them alone" — so a container that sent one would
    be exempting its own attributes from redaction. Nothing arriving
    here is chassis-written, so any marker is forged by definition and
    goes before the walk runs.
    """
    for index in range(len(kvs) - 1, -1, -1):
        if kvs[index].key == otlp_walk.STAMP_MARKER:
            del kvs[index]


def _check_traces(request, record: dict) -> int:
    """Refuse a foreign trace id, and disarm forged markers. The identity
    is stamped AFTER the walk, from the token: it is re-derived, never
    trusted, so walking whatever the container called `run.id` costs
    nothing."""
    expected = record["trace_id"]
    identity = _identity(record)
    count = 0
    for rs in request.resource_spans:
        _drop_forged_markers(rs.resource.attributes)
        _claim_service_name(rs.resource, identity)
        for ss in rs.scope_spans:
            _drop_forged_markers(ss.scope.attributes)
            for span in ss.spans:
                if _hex(span.trace_id) != expected:
                    raise HTTPException(
                        status.HTTP_403_FORBIDDEN, {"error": "trace_mismatch"}
                    )
                _drop_forged_markers(span.attributes)
                for event in span.events:
                    _drop_forged_markers(event.attributes)
                for link in span.links:
                    _drop_forged_markers(link.attributes)
                count += 1
    return count


def _stamp_traces(request, record: dict) -> None:
    identity = _identity(record)
    for rs in request.resource_spans:
        _stamp_resource(rs.resource, identity)
        for ss in rs.scope_spans:
            for span in ss.spans:
                for key, value in identity.items():
                    _set_attribute(span.attributes, key, value)


def _check_logs(request, record: dict) -> int:
    expected = record["trace_id"]
    identity = _identity(record)
    count = 0
    for rl in request.resource_logs:
        _drop_forged_markers(rl.resource.attributes)
        _claim_service_name(rl.resource, identity)
        for sl in rl.scope_logs:
            _drop_forged_markers(sl.scope.attributes)
            for log_record in sl.log_records:
                if _hex(log_record.trace_id) != expected:
                    raise HTTPException(
                        status.HTTP_403_FORBIDDEN, {"error": "trace_mismatch"}
                    )
                _drop_forged_markers(log_record.attributes)
                count += 1
    return count


def _stamp_logs(request, record: dict) -> None:
    identity = _identity(record)
    for rl in request.resource_logs:
        _stamp_resource(rl.resource, identity)
        for sl in rl.scope_logs:
            for log_record in sl.log_records:
                for key, value in identity.items():
                    _set_attribute(log_record.attributes, key, value)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


def _accepted(count: int, report: otlp_walk.WalkReport, forwarded: bool) -> dict:
    return {"accepted": count, "forwarded": forwarded, "walked": report.as_dict()}


@router.post("/v1/traces", status_code=status.HTTP_202_ACCEPTED)
async def relay_traces(
    request: Request, authorization: str | None = Header(default=None)
) -> dict:
    record = await _resolve_run_token(authorization)
    raw = await _read_protobuf(request)
    export = trace_service_pb2.ExportTraceServiceRequest()
    try:
        export.ParseFromString(raw)
    except DecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"error": "bad_protobuf"})
    count = _check_traces(export, record)
    report = otlp_walk.walk_trace_request(export)
    # After the walk, not before: the identity is re-derived from the run
    # token, so whatever the container called `run.id` is walked as
    # content and then overwritten with the truth.
    _stamp_traces(export, record)
    otlp_walk.log_report(report, signal="relay.traces")
    forwarded = False
    if count and _senders.enabled:
        forwarded = await asyncio.to_thread(_senders.send_traces, export)
    logger.info(
        "otlp_relay_accepted",
        signal="traces",
        agent_id=record.get("agent_id"),
        run_id=record.get("run_id"),
        spans=count,
        forwarded=forwarded,
        token_state=record.get("state", "active"),
    )
    return _accepted(count, report, forwarded)


@router.post("/v1/logs", status_code=status.HTTP_202_ACCEPTED)
async def relay_logs(
    request: Request, authorization: str | None = Header(default=None)
) -> dict:
    record = await _resolve_run_token(authorization)
    raw = await _read_protobuf(request)
    export = logs_service_pb2.ExportLogsServiceRequest()
    try:
        export.ParseFromString(raw)
    except DecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"error": "bad_protobuf"})
    count = _check_logs(export, record)
    report = otlp_walk.walk_logs_request(export)
    _stamp_logs(export, record)
    otlp_walk.log_report(report, signal="relay.logs")
    forwarded = False
    if count and _senders.enabled:
        forwarded = await asyncio.to_thread(_senders.send_logs, export)
    logger.info(
        "otlp_relay_accepted",
        signal="logs",
        agent_id=record.get("agent_id"),
        run_id=record.get("run_id"),
        records=count,
        forwarded=forwarded,
        token_state=record.get("state", "active"),
    )
    return _accepted(count, report, forwarded)


@router.get("/v1/traces")
@router.get("/v1/logs")
async def relay_method_not_allowed() -> Response:
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED)
