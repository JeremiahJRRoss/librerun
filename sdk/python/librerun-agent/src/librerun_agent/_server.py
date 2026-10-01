"""The ASGI application: Run Contract v1's four endpoints from one handler."""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import time
import uuid
from typing import Any, Awaitable, Callable
from urllib.parse import unquote

from . import _capture, _otel
from ._context import CURRENT, Invocation, RunContext

Handler = Callable[[RunContext], Awaitable[dict]]

MAX_BODY_BYTES = 16 * 1024 * 1024
FINISHED_TTL_SECONDS = 600.0
KEEPALIVE_SECONDS = 15.0
_LOGGER = logging.getLogger("librerun_agent")


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


class RunContractApp:
    """The ASGI callable ``serve`` returns."""

    def __init__(
        self,
        handler: Handler,
        *,
        name: str,
        capture_stdio: bool,
        otel_endpoint: str | None,
    ):
        self._handler = handler
        self.name = name
        self._capture_stdio = capture_stdio
        self._otel_endpoint = otel_endpoint
        self._invocations: dict[str, Invocation] = {}
        self._tasks: set[asyncio.Task] = set()
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def startup(self) -> None:
        """Install the process-wide pieces once: export to the relay when
        an endpoint is configured, stdout/stderr capture and thread
        context when asked. Idempotent; also run lazily on the first
        request for servers that skip the lifespan protocol."""
        if self._started:
            return
        self._started = True
        exporting = _otel.configure(service_name=self.name, endpoint=self._otel_endpoint)
        if self._capture_stdio:
            _capture.install_thread_context()
            _capture.install_stdio_capture()
        root = logging.getLogger()
        fallback = [h for h in root.handlers if isinstance(h, _capture.InvocationLogHandler)]
        if exporting:
            # Exported records carry the trace; the Run Contract fallback
            # must not also re-deliver them (an app configured earlier in
            # the same process may have installed it).
            for handler in fallback:
                root.removeHandler(handler)
        else:
            # ``logging`` lines emitted inside an invocation still reach
            # the chassis: as Run Contract ``log`` events.
            if not fallback:
                root.addHandler(_capture.InvocationLogHandler())
            if root.level == logging.NOTSET or root.level > logging.INFO:
                root.setLevel(logging.INFO)

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return
        self.startup()
        await self._http(scope, receive, send)

    async def _lifespan(self, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    self.startup()
                except Exception as exc:  # noqa: BLE001
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await asyncio.to_thread(_otel.flush)
                await send({"type": "lifespan.shutdown.complete"})
                return

    # -- routing -------------------------------------------------------------

    async def _http(self, scope: dict, receive, send) -> None:
        method = scope["method"].upper()
        path = scope["path"]
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if path == "/healthz" and method == "GET":
            await _json(send, 200, {"status": "ok", "agent": self.name})
            return
        if path == "/v1/runs" and method == "POST":
            await self._start(headers, receive, send)
            return
        parts = [unquote(p) for p in path.split("/") if p]
        if len(parts) == 4 and parts[0] == "v1" and parts[1] == "runs" and method == "GET":
            invocation_id, leaf = parts[2], parts[3]
            invocation = self._invocations.get(invocation_id)
            if invocation is None:
                await _json(send, 404, {"error": "unknown invocation"})
                return
            if _bearer(headers) != invocation.token:
                await _json(send, 401, {"error": "bad or missing bearer token"})
                return
            if leaf == "events":
                await self._events(invocation, send)
                return
            if leaf == "output":
                await self._output(invocation, send)
                return
        await _json(send, 404, {"error": "unknown path"})

    async def _start(self, headers: dict, receive, send) -> None:
        token = _bearer(headers)
        if not token:
            await _json(send, 401, {"error": "missing bearer token"})
            return
        try:
            raw = await _read_body(receive)
        except _TooLarge:
            await _json(send, 413, {"error": "body too large"})
            return
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            await _json(send, 400, {"error": "body is not JSON"})
            return
        if not isinstance(payload, dict) or payload.get("contract") != "v1":
            await _json(send, 400, {"error": "unsupported contract version"})
            return
        run = payload.get("run") or {}
        if not isinstance(run, dict):
            await _json(send, 400, {"error": "'run' must be an object"})
            return
        phase = str(payload.get("phase") or "")
        if not phase:
            await _json(send, 400, {"error": "'phase' is required"})
            return
        deadline = payload.get("deadline_seconds")
        if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, int) or deadline < 1):
            await _json(send, 400, {"error": "'deadline_seconds' must be a positive integer"})
            return
        traceparent = headers.get("traceparent")
        self._prune()
        invocation = Invocation(
            id=uuid.uuid4().hex,
            token=token,
            run_id=str(run.get("id") or run.get("case_id") or ""),
            tenant_id=run.get("tenant_id"),
            phase=phase,
            input=payload.get("input") if isinstance(payload.get("input"), dict) else {},
            prior_output=payload.get("prior_output") if isinstance(payload.get("prior_output"), dict) else None,
            user_edits=payload.get("user_edits"),
            rerun=bool(run.get("rerun", False)),
            deadline_seconds=deadline,
            mcp_url=(run.get("mcp") or {}).get("url") if isinstance(run.get("mcp"), dict) else None,
            traceparent=traceparent,
            tracestate=headers.get("tracestate"),
            trace_id=_trace_id_of(traceparent),
        )
        invocation._loop = asyncio.get_running_loop()
        self._invocations[invocation.id] = invocation
        # The handler runs inside its own context, with the invocation
        # current: the writers and exporters read it at each write, and
        # the threads it starts inherit it.
        context = contextvars.copy_context()
        context.run(CURRENT.set, invocation)
        task = asyncio.get_running_loop().create_task(self._run(invocation), context=context)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        # ``invocation_id`` is the contract's name (blueprint S1); ``run_id``
        # is the pre-S1 spelling, sent for one release.
        await _json(send, 201, {"invocation_id": invocation.id, "run_id": invocation.id})

    async def _run(self, invocation: Invocation) -> None:
        if invocation.trace_id:
            _otel.registry.register(invocation.id, invocation.token)
        try:
            with _otel.invocation_span(invocation):
                try:
                    coro = self._handler(RunContext(invocation))
                    if invocation.deadline_seconds:
                        result = await asyncio.wait_for(
                            coro, timeout=invocation.deadline_seconds
                        )
                    else:
                        result = await coro
                    if not isinstance(result, dict):
                        raise TypeError("the handler must return a dict: the phase output")
                finally:
                    # INSIDE the span, on every path. A handler that ends
                    # on `print(..., end="")` leaves a partial line, and
                    # flushing it after the span has exited emits a
                    # record with no trace context: the SDK would send it
                    # under the run token carrying an empty trace id, the
                    # relay would refuse the whole request
                    # `trace_mismatch`, and every other record batched
                    # with it would be lost too.
                    _capture.flush_invocation_output(invocation)
            invocation.output = result
            invocation.status = "completed"
            # Flush before answering completed: a batch that straddles the
            # answer still lands inside the relay's grace, but nothing
            # should have to.
            await asyncio.to_thread(_otel.flush)
            invocation.emit("completed", {"output": result})
        except asyncio.TimeoutError:
            await self._fail(invocation, f"deadline of {invocation.deadline_seconds}s exceeded")
        except asyncio.CancelledError:
            await self._fail(invocation, "cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            await self._fail(invocation, f"{type(exc).__name__}: {exc}")
        finally:
            invocation.finished_at = time.monotonic()
            if invocation.trace_id:
                _otel.registry.end(invocation.id)

    async def _fail(self, invocation: Invocation, error: str) -> None:
        # The run body flushes inside its span; this is the belt for a
        # failure raised before the span was ever entered.
        _capture.flush_invocation_output(invocation)
        invocation.error = error
        invocation.status = "failed"
        await asyncio.to_thread(_otel.flush)
        invocation.emit("failed", {"error": error})

    async def _events(self, invocation: Invocation, send) -> None:
        wakeup = asyncio.Event()
        invocation.register_wakeup(wakeup)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream"),
                    (b"cache-control", b"no-cache"),
                    (b"x-accel-buffering", b"no"),
                ],
            }
        )
        cursor = 0
        try:
            while True:
                wakeup.clear()
                events = invocation.snapshot(cursor)
                cursor += len(events)
                for name, data in events:
                    frame = f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
                    terminal = name in ("completed", "failed")
                    await send({"type": "http.response.body", "body": frame, "more_body": not terminal})
                    if terminal:
                        return
                try:
                    await asyncio.wait_for(wakeup.wait(), KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    await send({"type": "http.response.body", "body": b": keep-alive\n\n", "more_body": True})
        finally:
            invocation.unregister_wakeup(wakeup)

    async def _output(self, invocation: Invocation, send) -> None:
        if invocation.status == "completed":
            await _json(send, 200, {"output": invocation.output})
        elif invocation.status == "failed":
            await _json(send, 409, {"error": invocation.error})
        else:
            await _json(send, 404, {"error": "invocation not complete"})

    def _prune(self) -> None:
        now = time.monotonic()
        stale = [
            k for k, inv in self._invocations.items()
            if inv.finished_at is not None and now - inv.finished_at > FINISHED_TTL_SECONDS
        ]
        for k in stale:
            self._invocations.pop(k, None)

    # -- introspection (tests, diagnostics) ----------------------------------

    def invocation(self, invocation_id: str) -> Invocation | None:
        return self._invocations.get(invocation_id)


class _TooLarge(Exception):
    pass


async def _read_body(receive) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise _TooLarge()
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


async def _json(send, status: int, obj: Any) -> None:
    body = json.dumps(obj).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _bearer(headers: dict) -> str:
    auth = headers.get("authorization", "")
    return auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""


def _trace_id_of(traceparent: str | None) -> str | None:
    if not traceparent:
        return None
    parts = traceparent.strip().split("-")
    if len(parts) != 4 or len(parts[1]) != 32:
        return None
    try:
        int(parts[1], 16)
    except ValueError:
        return None
    return parts[1].lower()


def serve(
    handler: Handler,
    *,
    name: str | None = None,
    capture_stdio: bool | None = None,
    otel_endpoint: str | None = None,
) -> RunContractApp:
    """Turn one async handler into a Run Contract v1 ASGI application.

    ``name`` is the ``service.name`` of exported telemetry (default
    ``OTEL_SERVICE_NAME`` or ``librerun-agent``). ``capture_stdio``
    (default ``LIBRERUN_AGENT_CAPTURE_STDIO``, on) replaces stdout and
    stderr with per-invocation writers and carries the invocation's
    context into threads and executor jobs; ``otel_endpoint`` (default
    ``OTEL_EXPORTER_OTLP_ENDPOINT``) is the chassis relay.
    """
    if not callable(handler):
        raise TypeError("serve() takes an async callable handler(ctx) -> dict")
    return RunContractApp(
        handler,
        name=name or os.environ.get("OTEL_SERVICE_NAME") or "librerun-agent",
        capture_stdio=(
            _truthy(os.environ.get("LIBRERUN_AGENT_CAPTURE_STDIO"), True)
            if capture_stdio is None
            else capture_stdio
        ),
        otel_endpoint=otel_endpoint,
    )


def run(app: RunContractApp, *, host: str = "0.0.0.0", port: int = 8090) -> None:
    """Serve the app with uvicorn (the ``uvicorn`` extra)."""
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("pip install 'librerun-agent[uvicorn]' to use run()") from exc
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)


__all__ = ["RunContractApp", "run", "serve"]
