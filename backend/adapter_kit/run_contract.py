"""The container battery (blueprint S4): the adapter battery's checks,
driven through a Run Contract v1 client against a URL.

An in-process adapter passes ``run_battery`` (``adapter_kit``); a
container passes this: against its URL the runner probes ``/healthz``,
mints a run token and a W3C ``traceparent`` the way the chassis does,
POSTs a phase invocation with the agent's own shipped scenario, consumes
the events stream and checks

1. **schema-valid output** — a JSON object the chassis could persist:
   serialisable, and clean under the chassis boundary walk (a flagged
   key or number would end a real run ``pii_in_output``);
2. **progress seen** — at least one ``progress`` event in the chassis
   vocabulary, with a step id the walk accepts;
3. **``completed``** — exactly one terminal event, ``completed``, its
   output (or the output endpoint's) the object above;
4. **the container's spans carry the chassis trace id** — when the
   container exports to the battery's own relay recorder, every span and
   log record it sent arrived under the minted token and carries the
   trace id from the ``traceparent`` the battery sent; a container with
   no export configured reports the check as skipped, never as passed.

Runnable as a module::

    python -m adapter_kit.run_contract --url http://localhost:8090 \\
        --agent-dir backend/agents/_examples/echo_container --relay-port 18080

The relay recorder listens on ``--relay-port`` for the duration; start
the container with ``OTEL_EXPORTER_OTLP_ENDPOINT`` pointed at it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

from app import capabilities as _capabilities
from app.agents.manifest import AgentManifest, load_manifest
from app.routers import mcp as _chassis_mcp
from app.services.agent_runner import phase_deadline
from app.services import agent_settings_service as _agent_settings
from app.services import agent_step_config_service as _step_configs
from app.services import pii_service
from app.services.pii_service import PiiDetectorUnavailable

# The settle window may be restarted by each arriving batch, but not
# forever: an agent that never stops exporting would otherwise hold the
# battery open indefinitely. Four consecutive windows' worth is the cap,
# and reaching it while telemetry is still arriving is RECORDED rather
# than passed over in silence — a verdict taken over a moving target is
# not the same as one taken over a finished one.
_SETTLE_CAP = 4

# The trace id expected of an invocation the battery sent NO `traceparent`.
# Such an agent may legally start a trace of its own — the contract says
# absence must never be an error — so there is no id to compare against
# and the span check must not invent one. A literal that cannot collide
# with a 32-hex trace id.
_ANY_TRACE = "*"

# What a binding probe's slot holds until the agent answers it. Anything
# other than 401 counts as unbound, so a probe that never ran reads as a
# failure rather than as an absence — an open check is a failing check
# (blueprint 168(b)), and a probe cancelled mid-batch is as open as one
# that was never started.
_UNANSWERED = "cancelled before the agent answered"

# What `container.py` gives the health call as its transport ceiling. The
# number is production's; the BUDGET it is charged to is the phase's.
_HEALTHZ_TIMEOUT = httpx.Timeout(5.0)

# THE SAME IDEA FOR A BINDING PROBE, and for the same reason: a probe is
# not the agent's phase, so the phase deadline is the wrong ceiling for
# it. `collect` used to bound a WHOLE plan at `timeout` while `run_probes`
# ran it strictly serially, so N probes each answering in t needed
# N*t < timeout — and a CONFORMING agent that answers a refusal in 0.3s
# was reported `token_binding: fail` under `--timeout 2`, purely because
# twelve serial probes do not fit one window. Plans grow with every prior
# invocation, so the larger the manifest the likelier that false failure
# (Codex round 29).
#
# Sizing the batch bound at N*`timeout` instead would undo round 21: at a
# production `LIBRERUN_MAX_PHASE_SECONDS` of 3600 an agent that accepts
# connections and never answers would hold the battery for hours rather
# than producing the failing verdict. So the per-probe ceiling is its own
# number, the plan's bound is N of them, and neither depends on the phase
# deadline. Running the plan concurrently instead would not fix it either:
# an agent that serves one request at a time takes the same wall clock,
# and `Container_Agents.md` promises these probes "ask nothing extra of
# you". A refusal an agent needs more than five seconds to produce is a
# finding in itself.
_PROBE_CEILING_SECONDS = 5.0

VALID_STATUSES = {"pending", "running", "complete", "skipped", "error"}
WIRE_STATUSES = {"pending", "running", "completed", "complete", "skipped", "failed", "error"}


@dataclass
class ContractBatteryResult:
    agent_id: str
    scenario: str
    url: str
    # The trace id of the invocation UNDER TEST. It is not the only one:
    # a trace belongs to a run, and the battery starts more than one run,
    # so `traces` below says which trace each invocation was sent.
    trace_id: str
    # Minted token -> the trace id that invocation was given. A span is on
    # the right trace when it matches the entry for the token it arrived
    # under, which is what the span check compares (Codex round 22).
    traces: dict[str, str] = field(default_factory=dict)
    invocation_id: str | None = None
    events: list[tuple[str, dict]] = field(default_factory=list)
    output: dict | None = None
    spans: list[dict] = field(default_factory=list)
    log_records: list[dict] = field(default_factory=list)
    # Every MCP request the agent made to the advertised `run.mcp.url`:
    # `{method, tool, token, why}`, `why` None for a conformant one.
    mcp_calls: list[dict] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)  # name -> pass | fail | skip
    failures: list[str] = field(default_factory=list)
    # Cases the battery could not reach, as opposed to cases it passed.
    # A probe that did not run is not a probe that succeeded, and the
    # report has to be able to say which it was.
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        checks = ", ".join(f"{k}={v}" for k, v in self.checks.items())
        return (
            f"[{verdict}] {self.agent_id} / {self.scenario} @ {self.url}: {checks}"
            + ("" if self.passed else "\n  - " + "\n  - ".join(self.failures))
            + ("" if not self.notes else "\n  ? " + "\n  ? ".join(self.notes))
        )


class _Store:
    """One recorder's collection. Per instance, never class-level: two
    recorders in one process (the battery's and a test's) must not read
    each other's spans, or a silent container would look instrumented."""

    def __init__(self) -> None:
        self.spans: list[dict] = []
        self.log_records: list[dict] = []
        self.lock = threading.Lock()


class _RelayRecorder(BaseHTTPRequestHandler):
    """What the chassis relay would receive: OTLP/HTTP protobuf, by bearer."""

    def log_message(self, *args):
        pass

    @property
    def _store(self) -> _Store:
        return self.server.store  # type: ignore[attr-defined]

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        store = self._store
        try:
            if self.path.endswith("/v1/traces"):
                from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

                req = trace_service_pb2.ExportTraceServiceRequest()
                req.ParseFromString(raw)
                with store.lock:
                    for rs in req.resource_spans:
                        for ss in rs.scope_spans:
                            for s in ss.spans:
                                store.spans.append(
                                    {"name": s.name, "trace_id": s.trace_id.hex(),
                                     "parent_span_id": s.parent_span_id.hex(), "token": token}
                                )
            elif self.path.endswith("/v1/logs"):
                from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

                req = logs_service_pb2.ExportLogsServiceRequest()
                req.ParseFromString(raw)
                with store.lock:
                    for rl in req.resource_logs:
                        for sl in rl.scope_logs:
                            for r in sl.log_records:
                                store.log_records.append(
                                    {"body": r.body.string_value, "trace_id": r.trace_id.hex(), "token": token}
                                )
        except Exception:  # noqa: BLE001 — a bad body is the container's failure, recorded as nothing
            pass
        self.send_response(202)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


class _RecordingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, store: _Store):
        self.store = store
        super().__init__(address, handler)


class RelayRecorder:
    """Serve the recorder on ``port`` (0 = ephemeral) for the battery's life."""

    def __init__(self, port: int = 0, host: str = "0.0.0.0"):
        self._store = _Store()
        self._server = _RecordingServer((host, port), _RelayRecorder, self._store)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.port = self._server.server_address[1]

    @property
    def spans(self) -> list[dict]:
        with self._store.lock:
            return list(self._store.spans)

    @property
    def log_records(self) -> list[dict]:
        with self._store.lock:
            return list(self._store.log_records)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()



# ---------------------------------------------------------------------------
# run.mcp.url — the capability recorder
# ---------------------------------------------------------------------------
#
# WHAT THE BATTERY WAS SENDING WAS NOT WHAT THE CHASSIS SENDS. The POST
# body's `run` object carried `{id, case_id, tenant_id, rerun}` and
# nothing else, while `container.py` advertises `run.mcp.url` and
# `Run_Contract_v1.md:118` names it as the one door to chassis
# capabilities. The consequence is not a missing check, it is a battery
# that could not drive a conformant agent: the SDK raises
# `CapabilityError(-32601, "this invocation advertised no run.mcp.url")`
# on the first `ctx.capabilities.*`, `ctx.pii.redact` or
# `ctx.config.step` an agent makes, so every agent that uses a granted
# capability failed inside the battery for a reason the battery created.
#
# THIS IS A DRIVER, NOT A SECOND CHASSIS. It answers `initialize`,
# `tools/list` and `tools/call` — the whole surface `routers/mcp.py`
# serves — so an agent's client works against it, and it RECORDS every
# request so the battery can say what the agent did. What it does not do
# is reimplement the capabilities: `kb_search` answers no results,
# `run_store_*` is a dict that lives as long as the battery, `audit_log`
# writes nothing. Two of them are not stubbed at all, because the
# chassis's own code is importable and a second copy is how one copy
# quietly stops matching:
#
#   * `tools/list` is `routers/mcp._TOOLS` filtered by
#     `routers/mcp._TOOL_GRANTS` against the manifest's grants,
#     normalized by the façade's own `normalize_grants` — so a tool
#     added to the chassis is advertised here the same day;
#   * `redact` calls `pii_service.redact`, the intake pipeline itself,
#     and raises `-32004` when the detector is unavailable exactly as
#     `routers/mcp.py` does. The models are already loaded in this
#     process (`walked_off_loop`), and this handler runs on the
#     recorder's own thread, not on the battery's event loop.
#
# `config_get` is derived from the manifest's `llm.steps[]` with
# `overridden: false` — there is no tenant here to override anything, and
# saying so in the payload is better than inventing an admin.


class _ToolNotStubbed(RuntimeError):
    """The chassis grew a tool this driver has not learned."""


class _McpStore:
    """One recorder's record of what the agent asked the chassis for.

    `calls` is every REQUEST, conformant or not — a request refused for a
    bad token is the evidence the check exists to find, so it is recorded
    rather than dropped. `why` is None for a clean one.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.store: dict[str, object] = {}
        self.lock = threading.Lock()
        # THE BATTERY'S OWN LEDGER, BY REFERENCE. `minted_tokens` already
        # records every token the battery issues — the invocation under
        # test, the binding probe's second, every fresh-anchor leg — and
        # a token not in it is one this battery never minted, which is
        # exactly the question this endpoint asks. Holding the same dict
        # rather than a snapshot means a mint site added later is
        # accepted the day it is added; a copy taken at the first
        # invocation would have accused a conformant agent's capability
        # call from any later one. Read with `in` and never iterated, so
        # the battery's writes from its own thread need no lock.
        self.tokens: dict[str, str] | set[str] = {}
        self.granted: frozenset[str] = frozenset()
        self.steps: list[dict] = []
        # The manifest's settings at their defaults, `{key: value}` (K5a):
        # there is no tenant here to have chosen anything else.
        self.settings: dict[str, object] = {}
        # The names the manifest declares in `secrets[]` (K8a). Names only:
        # this recorder holds no secret, so a declared one is "not set".
        self.secrets: frozenset[str] = frozenset()
        # Tool calls this recorder could not answer for reasons of its
        # own — today only an unavailable PII detector.
        self.refusals: list[str] = []


class _McpRecorder(BaseHTTPRequestHandler):
    """The chassis's run-scoped MCP server, as far as an agent can tell."""

    def log_message(self, *args):
        pass

    @property
    def _store(self) -> "_McpStore":
        return self.server.store  # type: ignore[attr-defined]

    def _reply(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record(self, method, tool, token, why) -> None:
        with self._store.lock:
            self._store.calls.append(
                {"method": method, "tool": tool, "token": token, "why": why}
            )

    def do_POST(self):  # noqa: C901 — one dispatch, read top to bottom
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        authorization = self.headers.get("Authorization") or ""
        token = authorization.removeprefix("Bearer ").strip()
        try:
            message = json.loads(raw or b"{}")
        except ValueError:
            self._record("<unparseable>", None, token, "its body is not JSON")
            self._reply({"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32700, "message": "parse error"}}, 400)
            return
        if not isinstance(message, dict):
            self._record("<unparseable>", None, token, "its body is not a JSON-RPC object")
            self._reply({"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32600, "message": "expected a JSON-RPC message"}}, 400)
            return

        method = message.get("method")
        msg_id = message.get("id")
        envelope = None if message.get("jsonrpc") == "2.0" else (
            f"its envelope says jsonrpc={message.get('jsonrpc')!r}, not '2.0'")

        # A notification carries no id and gets no body, exactly as the
        # chassis answers it. Recorded all the same: it is still a request
        # the agent made.
        if msg_id is None:
            self._record(method, None, token, envelope)
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if method == "initialize":
            # UNAUTHENTICATED ON PURPOSE, because the chassis's is:
            # `routers/mcp.py` resolves the bearer only after this branch.
            # A battery that demanded a token here would fail an agent
            # that handshakes before it authenticates, which conforms.
            self._record(method, None, token, envelope)
            self._reply({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": _chassis_mcp.PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "librerun-battery", "version": "1.0.0"},
                "instructions": "The container battery standing in for the "
                                "chassis. Authenticate every request after "
                                "this one with your Run Contract bearer.",
            }})
            return

        store = self._store
        with store.lock:
            minted = token in store.tokens
            granted = store.granted
        if not minted:
            # The finding, in the agent's own words: it reached the
            # chassis with something other than the token it was handed.
            self._record(method, (message.get("params") or {}).get("name")
                         if isinstance(message.get("params"), dict) else None,
                         token,
                         "it carried no bearer token" if not token else
                         "it carried a bearer this battery never minted")
            self._reply({"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32001, "message": "unknown or expired run token"}}, 401)
            return

        if method == "tools/list":
            self._record(method, None, token, envelope)
            # The chassis's own predicate, so a tool that needs no grant
            # (`config_get`, K5a) is offered here exactly as it is there.
            tools = [t for t in _chassis_mcp._TOOLS
                     if _chassis_mcp.tool_granted(t["name"], granted)]
            self._reply({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}})
            return

        if method != "tools/call":
            self._record(method, None, token, f"the chassis serves no method {method!r}")
            self._reply({"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32601, "message": f"method {method!r} not supported"}})
            return

        params = message.get("params")
        if not isinstance(params, dict):
            self._record(method, None, token, "its 'params' is not an object")
            self._reply({"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32602, "message": "'params' must be an object"}})
            return
        name = _chassis_mcp._TOOL_ALIASES.get(params.get("name"), params.get("name"))
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            self._record(method, name, token, "its 'arguments' is not an object")
            self._reply({"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32602, "message": "'arguments' must be an object"}})
            return
        if name not in _chassis_mcp._TOOL_GRANTS:
            self._record(method, name, token, f"the chassis serves no tool {name!r}")
            self._reply({"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32602, "message": f"unknown tool {name!r}"}})
            return
        if not _chassis_mcp.tool_granted(name, granted):
            # NOT a finding against the agent. `-32002` is a legal answer
            # the agent is entitled to receive and handle, and an agent
            # that asks for a tool its manifest does not grant learns it
            # here exactly as it would in production. The predicate is the
            # chassis's own: this recorder refused `config_get` without
            # `llm` while the chassis served it (K5-04), and one copy of
            # the rule is what keeps that from happening twice.
            self._record(method, name, token, None)
            self._reply({"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32002, "message":
                f"capability {_chassis_mcp._TOOL_GRANTS[name]!r} is not granted "
                f"to this agent"}})
            return

        if name == "secret_get":
            # A LEGAL REFUSAL, recorded as conformant like `-32002` above.
            # The battery has no tenant and holds no secret, so a declared
            # name is `-32006 secret_not_set` — the answer the chassis
            # gives a tenant that set nothing, and one an agent must
            # handle — and any other name `-32005 secret_not_declared`.
            # An agent that fails on either is the finding, not this call.
            self._record(method, name, token, envelope)
            secret = args.get("name")
            with store.lock:
                declared = isinstance(secret, str) and secret in store.secrets
            if declared:
                code, message = -32006, (
                    f"secret {secret!r} is declared but has no value in this tenant")
            else:
                code, message = -32005, (
                    f"secret {secret!r} is not declared in this agent's secrets[]")
            self._reply({"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": code, "message": message}})
            return

        self._record(method, name, token, envelope)
        try:
            payload = self._answer(name, args, store)
        except PiiDetectorUnavailable as exc:
            # THE RUNNER'S PROBLEM, NOT THE AGENT'S. `redact` answers
            # through the chassis's own pipeline, so a host with no
            # named-entity model makes this refusal — and an agent that
            # then fails is doing exactly what `PiiUnavailable` tells it
            # to. Recorded as a note so the battery can say which of the
            # two happened, rather than reporting the agent for the
            # environment it was driven in.
            with store.lock:
                store.refusals.append(f"{name}: {exc}")
            self._reply({"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32004, "message": str(exc)}})
            return
        except _ToolNotStubbed as exc:
            # -32603, the chassis's own code for "the server failed",
            # because that is what happened: the failure is this
            # driver's, and calling it -32602 would tell the agent its
            # arguments were wrong.
            with store.lock:
                store.refusals.append(f"{name}: {exc}")
            self._reply({"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32603, "message": str(exc)}})
            return
        except Exception as exc:  # noqa: BLE001 — bad arguments are the agent's
            self._reply({"jsonrpc": "2.0", "id": msg_id, "error": {
                "code": -32602, "message": f"bad arguments: {exc}"}})
            return
        self._reply({"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}]}})

    def _answer(self, name: str, args: dict, store: "_McpStore") -> dict:
        """The stub's answer, in the shape the SDK's client reads."""
        if name == "redact":
            # THE CHASSIS'S OWN PIPELINE, not a stand-in for it: same
            # recognizers, same placeholders, same refusal when stage 3
            # could not run. Off the battery's event loop by construction
            # — this is the recorder's thread.
            text, _ = pii_service.redact(str(args.get("text", "")), quiet=True,
                                         stage="battery_mcp_redact")
            return {"text": text}
        if name == "kb_search":
            # No knowledge base here, and an invented hit would teach an
            # agent author that the battery had one.
            return {"results": []}
        if name == "run_store_get":
            key = str(args["key"])
            with store.lock:
                return {"key": key, "value": store.store.get(key)}
        if name == "run_store_set":
            key = str(args["key"])
            with store.lock:
                store.store[key] = args.get("value")
            return {"ok": True, "key": key}
        if name == "audit_log":
            # `ok` and nothing else: the row's attribution is the
            # chassis's to stamp, and there is no run owner here to stamp.
            return {"ok": True}
        if name == "config_get":
            # The chassis's shape: steps, and settings as `[{key, value}]`.
            with store.lock:
                return {
                    "steps": list(store.steps),
                    "settings": [{"key": key, "value": value}
                                 for key, value in store.settings.items()],
                }
        # A TOOL THE CHASSIS SERVES AND THIS RECORDER HAS NOT LEARNED.
        # `tools/list` is derived from the chassis's table, so a tool
        # added there is advertised here the same day — and would reach
        # this line and be reported to the agent as "bad arguments",
        # which is a lie about whose problem it is.
        # `test_battery_mcp_and_traceparent.py` fails the moment the two
        # diverge, so this message is the second line of defence rather
        # than the first.
        raise _ToolNotStubbed(
            f"the container battery advertises {name!r} because the chassis "
            f"serves it, but its recorder has no stand-in answer for it yet")


class _McpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, store: "_McpStore"):
        self.store = store
        super().__init__(address, handler)


class CapabilityRecorder:
    """The chassis's run-scoped MCP endpoint, served for the battery's life.

    ``advertise_host`` is how the AGENT reaches this process, which is
    not how this process reaches the agent: a container talks to its host
    as ``host.docker.internal``, an in-process agent as ``127.0.0.1``.
    There is nothing in ``--url`` that says which, so it is asked for
    rather than guessed — the same explicitness the relay recorder's
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` already needs.
    """

    def __init__(self, port: int = 0, host: str = "0.0.0.0",
                 advertise_host: str = "127.0.0.1"):
        self._store = _McpStore()
        self._server = _McpServer((host, port), _McpRecorder, self._store)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.port = self._server.server_address[1]
        self.url = f"http://{advertise_host}:{self.port}/"

    def expect(self, *, tokens, granted: frozenset[str],
               steps: list[dict], settings: dict | None = None,
               secrets=None) -> None:
        """What this recorder accepts: the battery's LIVE mint ledger, the
        manifest's normalized grants, the steps and settings `config_get`
        reads (`settings` as `{key: value}`, K5a), and the tool-secret
        names `secret_get` knows to be declared (K8a)."""
        with self._store.lock:
            self._store.tokens = tokens
            self._store.granted = frozenset(granted)
            self._store.steps = list(steps)
            self._store.settings = dict(settings or {})
            self._store.secrets = frozenset(secrets or ())

    @property
    def calls(self) -> list[dict]:
        with self._store.lock:
            return list(self._store.calls)

    @property
    def refusals(self) -> list[str]:
        """Answers this recorder could not give, for reasons of its own."""
        with self._store.lock:
            return list(self._store.refusals)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _terminal_of(
    events: list[tuple[str, dict]],
) -> tuple[str | None, dict, str | None, str | None]:
    """(name, payload, why-unconsumable, why-non-conformant) for THE terminal.

    THE FIRST ONE DECIDES, and there must be exactly one.
    `container.py:388-392` returns the moment `_handle_event` reports a
    terminal, so production acts on the first `completed`/`failed` it
    parses and never reads the frames after it; `Run_Contract_v1.md:148`
    says "Exactly one terminal event (`completed` or `failed`) ends the
    stream".

    ONE COPY OF THAT RULE, because there are three streams to apply it to
    — the invocation under test, a transition's drain, and the fresh
    anchor's — and only the first of them had it right. The other two
    asked `next((v for k, v in events if k == "completed"), None)`, which
    ignores both order and multiplicity, so a `completed` FOLLOWED BY a
    stray `failed` was read as a successful phase that also died.
    Measured on a three-phase ungated manifest whose `second` sends both:
    the battery invoked `['echo', 'second']`, stopped the walk with
    "the second phase ended `failed`", and still reported
    `token_binding: pass`, `passed: True` — on a run the chassis carries
    straight through to `third`, because it returned at the `completed`
    and never reached the frame that changed the battery's mind (Codex
    round 26).
    """
    # A FRAME THE CHASSIS DIES ON IS NOT A FRAME. `_parse_data` runs on
    # EVERY event, so a malformed one anywhere in the stream raises before
    # the terminal is ever reached — checking only the terminal would let
    # a bad `progress` frame through on an invocation production could
    # never have consumed.
    for k, v in events:
        why = _malformed(v)
        if why is not None:
            return None, {}, (
                f"its `{k}` event {why} — the chassis raises "
                f"`ContainerAgentError` while PARSING that frame "
                f"(`container.py:418-434`), so it never reaches the "
                f"terminal, the output, or the fallback"
            ), None
    terminals = [(k, v) for k, v in events if k in ("completed", "failed")]
    if not terminals:
        # UNCONSUMABLE: `container.py:410-414` raises here.
        return None, {}, "its events stream ended without a terminal event", None
    name, payload = terminals[0]
    if len(terminals) > 1:
        # CONSUMABLE BUT NON-CONFORMANT, and the difference is the whole
        # point of returning two fields. Production stops at the first
        # terminal, so it consumes this invocation perfectly happily —
        # telling an author it "raises ContainerAgentError here" would
        # send them hunting an exception that is never thrown (§12 191,
        # the `wrong-consequence-named` case). The defect is real and the
        # consequence is the one stated.
        return name, payload, None, (
            f"its events stream carried {len(terminals)} terminal events "
            f"({', '.join(k for k, _ in terminals)}) and the contract allows "
            f"exactly one; the chassis returns at the first "
            f"(`container.py:388-392`), so it consumed the `{name}` and never "
            f"read the rest — nothing downstream will ever report this for you"
        )
    return name, payload, None, None


# THE FRAME THE CHASSIS WOULD HAVE DIED ON, marked rather than faked.
# `container.py:418-434` raises `ContainerAgentError` for an event whose
# data is not JSON AND for one whose data is JSON but not an object, and
# it does so WHILE PARSING — before any fallback, before any handler. The
# battery used to substitute `{"_unparseable": raw}` for the first, which
# is a dict, so `.get("output")` was None and a `completed` frame fell
# through to `/output` and was accepted; and it passed the second through
# as whatever `json.loads` returned, in defiance of this function's own
# `-> list[tuple[str, dict]]`. Measured against an agent whose transition
# sends a malformed terminal while serving a perfectly good `/output`:
# non-JSON data passed the battery clean, a `progress` frame of non-JSON
# data passed clean, and `data: 42` raised
# `AttributeError: 'int' object has no attribute 'get'` OUT of
# `run_contract_battery` — a crash where a verdict was due (Codex round
# 27). The key is namespaced so no conforming payload can collide with it.
_MALFORMED = "_librerun_malformed_frame"


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    name, data = None, []
    for line in text.splitlines() + [""]:
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif line == "":
            # EITHER, exactly as `container.py:383-388` flushes:
            #     if not event_name and not data_lines: continue
            # so a frame carrying `data:` with no `event:` IS parsed in
            # production, and dies there if its data is malformed. Guarding
            # on `name` alone discarded it, and `data: not-json` ahead of an
            # otherwise valid stream passed the battery clean (Codex round
            # 28). An unnamed frame is reported under the empty name the
            # wire actually carried rather than an invented one.
            if name or data:
                raw = "\n".join(data)
                try:
                    payload = json.loads(raw or "{}")
                except ValueError:
                    payload = {_MALFORMED: raw, "_why": "is not JSON"}
                else:
                    if not isinstance(payload, dict):
                        payload = {_MALFORMED: raw,
                                   "_why": "is JSON but not an object"}
                events.append((name, payload))
            name, data = None, []
    return events


def _object_body(response) -> tuple[dict | None, str | None]:
    """(the decoded JSON OBJECT, why it is not one).

    `httpx`'s `.json()` succeeds for `[]`, `42`, `"x"` and `null` just as
    happily as for an object, and every caller here then reaches for
    `.get()`. Round 27 fixed exactly this assumption inside `_parse_sse`
    and swept no further; `grep -n '.json()'` finds SIX of these, and all
    six raised out of `run_contract_battery` rather than producing a
    verdict. Measured, one per site: a first `POST /v1/runs` answering
    `[...]`, a first `/output` answering `42` or HTML, a transition POST
    answering `42`, a transition `/output` and a fresh anchor's POST and
    `/output` each answering a non-empty array —
    `AttributeError: 'list' object has no attribute 'get'` and a bare
    `JSONDecodeError`, eight shapes, no verdict (Codex round 28).

    `x or {}` is not this check: `[]` and `0` are FALSY and rescued by it,
    while `[1]` and `42` are truthy and go straight through. The first
    run of the probe used `[]` and wrongly reported three of these sites
    safe.
    """
    try:
        decoded = response.json()
    except ValueError:
        return None, f"a body that is not JSON: {response.text[:120]!r}"
    if not isinstance(decoded, dict):
        return None, (f"a JSON body that is not an object: "
                      f"{type(decoded).__name__}")
    return decoded, None


def _malformed(payload: dict) -> str | None:
    """Why the chassis would have raised on this frame, or None."""
    if _MALFORMED not in payload:
        return None
    return (f"carried data that {payload.get('_why', 'is malformed')}: "
            f"{str(payload.get(_MALFORMED, ''))[:120]}")


BINDING_UNFINISHED = (
    "token binding: the cross-invocation probe never ran, so the check "
    "never concluded"
)


async def _probe_status(
    client: httpx.AsyncClient, url: str, bearer: str | None = None
) -> int | str:
    """One binding probe's status code, WITHOUT consuming the body.

    ``/events`` is an SSE stream a correct agent holds open until the
    invocation ends — and the agent this probe exists to catch is
    precisely the one that ACCEPTS a foreign bearer and hands back an
    open stream. ``AsyncClient.get()`` reads the body before returning,
    so against such an agent the battery waited out the whole invocation
    instead of recording the 200 it already had, and with a timeout
    shorter than the invocation it raised ``ReadTimeout`` OUT of
    ``run_contract_battery`` rather than reporting a failure (Codex
    round 2). Measured against an agent holding each stream 8s: 32.1s to
    reach a verdict, and an uncaught ReadTimeout at a 2s timeout.

    The status line arrives with the response head, so opening the
    stream and closing it unread is all this probe ever needed.
    """
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    # `Connection: close` because this probe deliberately does NOT read the
    # body. A leaking agent answers with an SSE stream it then holds open,
    # and a connection abandoned mid-response must not go back into the
    # pool: the next probe reusing it queues behind a server thread still
    # inside that hold and times out. Observed exactly that under full-suite
    # load — `events with a foreign token never answered at all:
    # ReadTimeout('')` against the fixture whose 200 the probe had already
    # received.
    headers["Connection"] = "close"

    # ONE CEILING, FLAT, FROM THE SEND — because the send is now the
    # moment the agent is free.
    #
    # Round 30 made this ceiling restart itself when the invocation
    # released the agent, so that a serialized agent's queued probe was
    # not charged for the wait. Round 31 showed the queue was the wrong
    # thing to tolerate: the probe REQUEST sitting in a FIFO agent's
    # accept queue also delays the `/output` fallback behind it, and that
    # fetch IS inside the invocation's budget. `run_probes` therefore
    # waits for `agent_free` before sending anything at all, which means
    # a probe is never in flight while the agent is busy — and the
    # restart could no longer fire for any agent behaviour.
    #
    # Two mechanisms for one hazard is how one of them quietly stops
    # matching, so the restart is gone and the two jobs are separated:
    # `agent_free` decides WHEN a probe may be sent, this ceiling bounds
    # how long the agent may take to ANSWER one. Round 21's property is
    # unchanged — a hanging agent costs `N * ceiling`, never the phase
    # deadline.
    try:
        async with asyncio.timeout(_PROBE_CEILING_SECONDS):
            async with client.stream("GET", url, headers=headers) as response:
                return response.status_code
    except TimeoutError:
        return (f"no answer within the {_PROBE_CEILING_SECONDS:g}s a binding "
                f"probe is given")
    except httpx.HTTPError as exc:
        # Reported, never raised. An agent that resets the connection
        # rather than answering `401` has failed the obligation as surely
        # as one that answers `200` — and a probe that raises out of the
        # battery reports nothing at all, which is the defect the second
        # POST carried one round earlier and these probes still did
        # (Codex round 4). The caller sees a value it can record.
        return repr(exc)


def _outran(deadline: int, leg: str) -> str:
    """The deadline verdict, in the chassis's own terms.

    `leg` names WHICH part of the exchange overran — answering the POST,
    streaming events, serving output — because "it was too slow" sends an
    author looking in the wrong place, and the three legs fail for quite
    different reasons.
    """
    return (
        f"the invocation outran the {deadline}s deadline it was "
        f"advertised while {leg} — the chassis cancels the whole exchange "
        f"at that deadline and fails the run `PhaseDeadlineExceeded`"
    )


class _InvocationBudget:
    """One invocation's deadline, on a CONTINUOUS wall clock.

    Round 13 put the guard inside the events read alone, leaving the POST
    before it and the `/output` fetch after it bounded only by the
    client's global timeout: an agent sleeping 8s in the POST outran a 2s
    budget untouched, `completed: pass` after 24.8s (round 14). Round 14
    carried one allowance across the three legs — but charged only the
    time spent INSIDE them, so the clock PAUSED between legs while the
    agent's did not. An agent that begins work when its POST returns goes
    on working through the battery's binding probes, and measured: a 5s
    invocation passed a 3s deadline (round 15's health probe fixed a
    third case of the same shape; round 16 found this one).

    Production does not pause. `asyncio.timeout(deadline)` runs from the
    start of `run_phase` to its end, covering everything in between. So
    this is an absolute deadline, fixed when the first segment opens and
    never extended: what elapses between segments is spent whether the
    battery was reading or not.

    That is only compatible with not charging the agent for the
    battery's own probes because no probe is SENT until this budget's
    last segment has closed (`run_probes`). The invocation's clock
    therefore runs POST -> stream -> output, exactly production's span,
    and nothing the probes do can touch it — not their answers, and not
    their place in a serialized agent's queue. Probes used to run beside
    the events read, which kept them out of the clock but not out of
    that queue (Codex round 31).
    """

    def __init__(self, seconds: float) -> None:
        self.total = float(seconds)
        self._deadline: float | None = None

    @property
    def remaining(self) -> float:
        if self._deadline is None:
            return self.total
        return self._deadline - time.monotonic()

    @property
    def started(self) -> bool:
        return self._deadline is not None

    @asynccontextmanager
    async def segment(self):
        """Run one leg of the exchange against the invocation's deadline.

        The deadline is set by the FIRST segment and shared by the rest;
        the gap between two segments comes out of it like everything
        else, which is what makes the clock continuous.
        """
        if self._deadline is None:
            self._deadline = time.monotonic() + self.total
        left = self.remaining
        if left <= 0:
            raise TimeoutError
        async with asyncio.timeout(left):
            yield


async def _consume_events(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    budget: _InvocationBudget,
) -> tuple[int, str]:
    """An events stream read to its end under a WALL-CLOCK budget.

    ``httpx``'s timeout bounds the gap BETWEEN reads, not the total, so
    an agent that keeps chunking is never impatient enough to trip it.
    Production has no such hole: the runner wraps the entire invocation
    in ``asyncio.timeout(deadline)`` (``agent_runner.py:513``) and raises
    ``PhaseDeadlineExceeded`` at the number it advertised. So the battery
    advertising `deadline_seconds` and then waiting arbitrarily long was
    not a lenient version of the platform — it was a DIFFERENT platform,
    and an agent that overruns every budget it is given passed with
    `completed: pass` and no failure at all (Codex round 13). Measured:
    an invocation advertised 3s, emitting a chunk every 0.5s for 20s,
    consumed in full; 40.1s to a clean verdict.

    The budget is the one this invocation was advertised, and what this
    read spends comes out of the same allowance the POST already drew on,
    so the battery is exactly as patient as the chassis promised the
    agent it would be. Expiry raises ``TimeoutError``; every caller turns
    it into a recorded failure, never a traceback.
    """
    # `Connection: close` for the same reason `_probe_status` sets it:
    # on the expiry path this stream is abandoned mid-response, and a
    # connection returned to the pool while a server thread is still
    # writing into it makes the NEXT request time out instead.
    async with budget.segment():
        async with client.stream(
            "GET", url,
            headers={**headers, "Accept": "text/event-stream",
                     "Connection": "close"},
        ) as stream:
            if stream.status_code != 200:
                return stream.status_code, ""
            return 200, "".join([chunk async for chunk in stream.aiter_text()])


async def run_contract_battery(
    url: str,
    *,
    manifest: AgentManifest,
    scenario: dict,
    scenario_name: str = "scenario",
    phase: str | None = None,
    relay: RelayRecorder | None = None,
    expect_spans: bool | None = None,
    capabilities: CapabilityRecorder | None = None,
    expect_mcp: bool | None = None,
    settle_seconds: float = 2.0,
    timeout: float = 600.0,
) -> ContractBatteryResult:
    """Drive one phase invocation and run the four checks."""
    agent_id = manifest.id
    phase = phase or manifest.phases[0].name
    token = secrets.token_urlsafe(32)
    # Every token THIS battery mints. The spans check asks whether foreign
    # telemetry leaked into the run, and "foreign" means another agent —
    # not the battery's own second invocation, which exists to prove the
    # agent binds per invocation rather than keeping an allowlist.
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    traceparent = f"00-{trace_id}-{span_id}-01"
    # TOKEN -> THE TRACE ID THAT INVOCATION WAS SENT. A dict rather than a
    # set because the battery no longer sends one trace context to
    # everything: a trace belongs to a RUN and a parent span to an
    # INVOCATION, so the span a second run exports is correct under a
    # DIFFERENT trace id, and only the token says which.
    minted_tokens = {token: trace_id}
    # The capability endpoint reads THAT SAME DICT, so every token the
    # battery mints — the binding probe's second, every fresh-anchor leg
    # — is accepted the moment it is recorded there, and the deliberately
    # foreign one never is. A snapshot taken here would have accused a
    # conformant agent's capability call from any later invocation of
    # carrying a bearer the battery never issued: the battery's own
    # bookkeeping, reported as the agent's defect.
    if capabilities is not None:
        capabilities.expect(
            tokens=minted_tokens,
            granted=frozenset(_capabilities.normalize_grants(manifest.capabilities)),
            steps=_step_configs.effective_steps(manifest, {}),
            # The declared defaults: no tenant here has chosen otherwise.
            settings=_agent_settings.effective_values(manifest, {}),
            # The declared tool-secret names (K8a): each answered "not set".
            secrets=manifest.secrets,
        )
    result = ContractBatteryResult(agent_id=agent_id, scenario=scenario_name,
                                   url=url.rstrip("/"), trace_id=trace_id,
                                   traces=minted_tokens)
    headers = {"Authorization": f"Bearer {token}", "traceparent": traceparent, "tracestate": "librerun=battery"}
    base = url.rstrip("/")

    # THE BATTERY NEVER ADVERTISES A BUDGET IT WILL NOT WAIT FOR.
    # Round 10 made `deadline_seconds` the manifest's phase budget and
    # left the client reading on `timeout`, so for any phase whose
    # resolved deadline was larger the battery promised time it would not
    # give: an agent that emits progress and then works quietly, well
    # inside the budget it was handed, was abandoned with `events stream
    # failed: ReadTimeout` (Codex round 11). Measured at 5.1s against an
    # agent advertised 300s and silent for 8.
    #
    # `timeout` IS this battery's `LIBRERUN_MAX_PHASE_SECONDS`, so it is
    # passed AS the ceiling rather than applied after one. Composing the
    # chassis function with a second clamp made that sentence false in
    # the direction that matters: `phase_deadline(spec)` had already
    # clamped to the ceiling of whichever process imported the battery
    # (3600 by default), so `--timeout` could only ever lower the
    # advertised budget, never raise it, and a manifest declaring 5000s
    # under `--timeout 5000` still advertised 3600 — an agent whose
    # phases legitimately run longer than this process's ceiling was
    # untestable (Codex round 13).
    # A CEILING BELOW ONE SECOND ADVERTISES MORE THAN IT ENFORCES.
    # `deadline_seconds` is a positive INTEGER on the wire, so `--timeout
    # 0.5` truncates to a ceiling of 0, `phase_deadline` floors that to 1,
    # and the battery promises 1s while the client still cuts the exchange
    # off at 0.5s. Measured: an agent answering at 0.75s — comfortably
    # inside the second it was given — failed `events stream failed:
    # ReadTimeout` (Codex round 14). Refused at the boundary rather than
    # rounded, because silently treating 0.5 as 1 would enforce a budget
    # the caller did not ask for. At one second and above the advertised
    # value is `min(declared, int(timeout)) <= int(timeout) <= timeout`,
    # so what is promised is always inside what is enforced.
    if timeout < 1:
        raise ValueError(
            f"timeout must be at least 1 second (got {timeout}): "
            f"deadline_seconds is a positive integer, so a sub-second "
            f"ceiling would advertise a budget larger than the one the "
            f"battery enforces"
        )

    def advertised_deadline(spec) -> int:
        return phase_deadline(spec, ceiling=int(timeout))

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
        # WARM THE WALKER HERE, before any invocation, deadline or probe
        # exists. Its first call loads models (3.42s measured); every
        # later one is 4ms. Paying that anywhere else means paying it
        # while something is waiting on this loop — which is exactly what
        # round 23's own fix did until CI said so.
        await asyncio.to_thread(pii_service.walk, {"warm": "up"}, quiet=True)

        async def health_probe(budget: _InvocationBudget, first: bool = False) -> bool:
            """`/healthz`, charged to THIS invocation's budget.

            Production probes it inside the phase's own timeout:
            `ContainerAgent.run_phase` calls `_probe_healthz` before the
            POST (`container.py:261`) and the runner wraps the whole of
            `run_phase` in `asyncio.timeout(deadline)`
            (`agent_runner.py:513`). So the probe belongs to every
            invocation's budget, ONCE PER INVOCATION -- not once per
            battery run, before any budget exists, which is what the
            battery did. Measured: a health endpoint taking 2s passed a
            1s phase that the chassis would have failed
            `PhaseDeadlineExceeded`, and three invocations were driven
            while the agent's health was asked after exactly once (Codex
            round 15).

            `first` carries the `healthz` check itself: the earliest
            probe is the one that says whether this agent answers at all.
            """
            try:
                async with budget.segment():
                    probe = await client.get(
                        f"{base}/healthz", timeout=_HEALTHZ_TIMEOUT)
            except TimeoutError:
                result.failures.append(
                    _outran(int(budget.total), "answering /healthz"))
                if first:
                    result.checks["healthz"] = "fail"
                return False
            except httpx.HTTPError as exc:
                result.failures.append(f"healthz unreachable: {exc}")
                if first:
                    result.checks["healthz"] = "fail"
                return False
            if first:
                result.checks["healthz"] = (
                    "pass" if probe.status_code == 200 else "fail")
            if probe.status_code != 200:
                result.failures.append(f"healthz returned {probe.status_code}")
                return False
            return True

        # The PHASE's budget, not the battery's HTTP timeout. The runner
        # computes `deadline = phase_deadline(spec)` inside its phase loop
        # (`agent_runner.py:413`), so every invocation carries its own
        # phase's `min(declared, LIBRERUN_MAX_PHASE_SECONDS)` — the
        # battery was sending `int(timeout)` (120) to every phase of every
        # manifest instead. Measured against a manifest declaring 30s: the
        # chassis would send [30, 30], the battery sent 120, and an agent
        # enforcing its own published deadline answered `400` at the first
        # POST — so the battery never reached a single check. The chassis's
        # own function is imported rather than reimplemented: two copies of
        # a rule is how one of them quietly stops matching.
        under_test = next(
            (p for p in manifest.phases if p.name == phase), manifest.phases[0]
        )
        body = {
            "contract": "v1",
            "agent_id": agent_id,
            "phase": phase,
            "deadline_seconds": advertised_deadline(under_test),
            # `mcp` IS PART OF THE BODY THE CHASSIS SENDS, and leaving it
            # out did not merely skip a check — it made the battery
            # unable to drive a conformant agent at all. `container.py`
            # advertises `run.mcp.url` and `Run_Contract_v1.md:118`
            # names it the one door to chassis capabilities, so the
            # SDK raises `CapabilityError(-32601, "this invocation
            # advertised no run.mcp.url")` at the first
            # `ctx.capabilities.*`, `ctx.pii.redact` or
            # `ctx.config.step` — every agent that uses a capability it
            # was granted failed inside the battery, for a reason the
            # battery created. Omitted entirely when no
            # recorder is serving, because an advertised URL that
            # answers nothing is worse than an absent key: the contract
            # gives the agent a defined behaviour for absence and none
            # for a dead endpoint.
            "run": {"id": "battery-run", "case_id": "battery-run",
                    "tenant_id": "battery-tenant", "rerun": False,
                    **({"mcp": {"url": capabilities.url}}
                       if capabilities is not None else {})},
            "input": scenario,
            "prior_output": None,
            "user_edits": None,
        }
        # The budget starts HERE, not at the events read: the POST is part
        # of the exchange production wraps in `asyncio.timeout(deadline)`.
        budget = _InvocationBudget(body["deadline_seconds"])
        if not await health_probe(budget, first=True):
            return result
        try:
            async with budget.segment():
                started = await client.post(
                    f"{base}/v1/runs", json=body, headers=headers)
        except TimeoutError:
            result.failures.append(_outran(body["deadline_seconds"], "answering POST /v1/runs"))
            result.checks["completed"] = "fail"
            return result
        except httpx.HTTPError as exc:
            result.failures.append(f"POST /v1/runs failed: {exc}")
            return result
        if started.status_code not in (200, 201):
            result.failures.append(f"POST /v1/runs returned {started.status_code}: {started.text[:200]}")
            return result
        payload, not_object = _object_body(started)
        invocation_id = (payload.get("invocation_id") or payload.get("run_id")
                         if payload is not None else None)
        if not isinstance(invocation_id, str) or not invocation_id:
            result.failures.append(
                f"POST /v1/runs answered {started.status_code} with {not_object}"
                if not_object is not None else
                "POST /v1/runs did not answer {'invocation_id': …}")
            return result
        result.invocation_id = invocation_id
        ref = quote(invocation_id, safe="")

        # Token binding. The contract names FOUR obligations, not one:
        # the agent must reject (401) any events/output request for an
        # invocation carrying a DIFFERENT or MISSING token. Probing only
        # "without a token" tests that the agent reads the header
        # at all; it says nothing about binding, which is the foreign
        # cases. An agent whose leaves are readable by anyone who invents
        # a token passed this check clean.
        #
        # The examples S5 adds are not served by the Python SDK, so they
        # do not inherit its binding check — they are guarded only by
        # whatever this battery actually tests, and S6's templates derive
        # from them.
        foreign = secrets.token_urlsafe(32)
        assert foreign != token, "the foreign token must differ from the bound one"
        bindings: dict[str, int | str] = {}
        # SET means "no invocation is occupying the agent right now", and
        # `run_probes` SENDS NOTHING until it is. Cleared as each
        # invocation's streaming leg opens, set again in the `finally`
        # that follows its LAST leg.
        #
        # This is what keeps the battery's own requests out of the
        # invocation entirely — not merely out of its clock. A probe in
        # flight during the stream sits in a FIFO agent's accept queue
        # ahead of the `/output` fallback, which the battery cannot even
        # send until the stream has closed and which IS charged to the
        # invocation's budget (Codex round 31).
        agent_free = asyncio.Event()
        agent_free.set()
        # The noun for the invocation under test, shared with `issued`
        # below so the two spellings of "the first invocation" cannot
        # drift apart.
        FIRST = "the first invocation"

        def unauthenticated_probes(
            noun: str, target: str
        ) -> list[tuple[str, str, str | None]]:
            """(case, url, bearer) for the two questions that need no
            token the agent ever issued — asked of each leaf, and so the
            only probes that can be put to EVERY invocation this battery
            starts.

            They used to be asked of the first invocation alone. Every
            later one — a rerun, the next phase, a second run, a fresh
            anchor — was probed only by `cross_use`, whose bearers are
            tokens the agent itself minted for some other invocation. So
            an agent guarding its later invocations with

                if header and header != expected: 401

            refuses every bearer this battery could show it, answers 401
            to all of them, and serves both leaves to a request carrying
            no Authorization header at all. Measured against exactly such
            an agent: `token_binding: pass`, `passed: True`, no failures,
            while a tokenless GET of its second invocation's events and
            output each returned 200 (Codex round 18).

            NAMING a probe is separate from RUNNING it because the slot
            has to be in `bindings` before the task that fills it can be
            cancelled — a cancelled probe leaves its name behind or it
            leaves nothing (§12 183, and `expect` below).
            """
            return [
                (f"{leaf} on {noun} {case}",
                 f"{base}/v1/runs/{target}/{leaf}", bearer)
                for leaf in ("events", "output")
                for case, bearer in (("without a token", None),
                                     ("with a foreign token", foreign))
            ]

        Plan = list[tuple[str, str, str | None]]

        def expect(plan: Plan) -> Plan:
            """EVERY EXPECTED PROBE IS RECORDED BEFORE ANY OF THEM RUNS.

            `unbound` is a comprehension over the keys that EXIST, so a
            probe cancelled before it wrote its key did not fail — it
            vanished, and with it the obligation it was checking. Round
            16 introduced that hole by collecting the probes with
            `wait_for`: measured, four correct-but-slow 401s under a 3s
            window gave `token_binding: pass`, `passed: True`, no
            failures at all, on a check that half ran (Codex round 17,
            P1). Seeding the slots makes the missing ones say so by name.
            """
            for case, _url, _bearer in plan:
                bindings.setdefault(case, _UNANSWERED)
            return plan

        async def run_probes(plan: Plan) -> None:
            # NOTHING IS SENT UNTIL THE INVOCATION'S LAST LEG IS DONE.
            #
            # Round 30 deferred when a probe's CEILING starts. It left the
            # probe REQUEST where it was — in flight from the moment the
            # stream opened — and against a single-request/FIFO agent that
            # request sits in the accept queue AHEAD of anything the
            # battery sends later. The `/output` fallback cannot be sent
            # until the stream has closed, so it queues BEHIND the probe,
            # and that fetch is inside the invocation's budget. Measured
            # against a conforming FIFO agent whose `completed` carried no
            # inline output, refusing in 8s under a 10s deadline: "the
            # invocation outran the 10s deadline it was advertised while
            # serving its output" — `completed: fail` for time the battery
            # itself spent (Codex round 31). That is round 19's defect
            # reached by a different route: the battery's own requests
            # charged to the agent.
            #
            # Waiting here rather than at each call site is deliberate —
            # all three batches go through this function, and two copies
            # of a rule is how one of them quietly stops matching.
            await agent_free.wait()
            for case, url, bearer in plan:
                bindings[case] = await _probe_status(client, url, bearer)

        # One trace per RUN, one parent span per INVOCATION — the shape the
        # chassis actually sends. `agent_runner.py:439-440` opens a new
        # phase span around every `run_phase` and `container.py:121-125`
        # injects whatever span is current, so a rerun and a next phase
        # share their run's trace id under a fresh parent, and a second
        # run is a different run and so a different trace (S4-B). The
        # battery replayed ONE traceparent byte for byte at every
        # invocation: a stateful agent that refuses to be handed the same
        # parent span twice answered `400` and was reported "per-invocation
        # binding is UNPROVEN" for a request shape the chassis never sends
        # (Codex round 22).
        traces: dict[str, str] = {body["run"]["id"]: trace_id}

        def trace_context(run_id: str) -> dict[str, str]:
            """The traceparent/tracestate pair for ONE invocation of `run_id`."""
            of_run = traces.setdefault(run_id, secrets.token_hex(16))
            return {
                "traceparent": f"00-{of_run}-{secrets.token_hex(8)}-01",
                "tracestate": "librerun=battery",
            }

        async def walked_off_loop(output: object) -> tuple[object, str | None]:
            """`unpersistable`, OFF THE EVENT LOOP.

            The walker loads its models on first use — measured 3.42s,
            then 4ms. Round 23 moved the first walk from the end of the
            battery into the transition loop, where the cross-use probe
            batch is in flight, and blocked the loop those probes run on:
            CI answered `events with another invocation's token on the
            first invocation never answered at all: ConnectTimeout('')`
            with every check otherwise passing, and a heartbeat task
            measured the gap at 3.52s. A thread keeps the loop free; the
            warm-up at the top of the run makes every walk the battery
            performs mid-run cost 4ms.
            """
            return await asyncio.to_thread(walk_output, output)

        def walk_output(output: object) -> tuple[object, str | None]:
            """(what the chassis would PERSIST, why it could not), the
            second being None when it could.

            THE WALKED VALUE IS THE POINT, not just the verdict.
            `agent_runner.py:556-562` replaces the phase's output with
            `run_boundary.walk_value(...)` before persisting it, so the
            next phase's `prior_output` carries placeholders wherever the
            walk redacted. Throwing `.value` away and carrying the
            original handed every later invocation a `prior_output` the
            chassis never sends — measured, a rerun and a next phase both
            received `{"echo": {"who": "ada@example.com"}}` where
            production sends `[REDACTED_EMAIL_ADDRESS_1]`, which a
            stateful agent validating its input can reject and be
            reported UNPROVEN for (Codex round 24).

            THE SAME WALK PRODUCTION RUNS OVER EVERY PHASE'S terminal
            output (`agent_runner.py:556-562`), whose refusal ends that
            run `error` with `pii_in_output` — not just the invocation
            under test. A rerun, a next phase, a second run and a fresh
            anchor all produce outputs the chassis would have walked, and
            the battery walked none of them: measured, an agent whose
            second invocation returned a Luhn-valid card number as a JSON
            number passed the whole battery, and so did one that put it in
            the fresh anchor's output — which the battery then carried
            into the next phase as `prior_output` (Codex round 23).
            """
            try:
                json.dumps(output)
                walked = pii_service.walk(output, quiet=True)
            except (TypeError, ValueError, pii_service.UnwalkableValue) as exc:
                return output, f"its output is not persistable: {exc}"
            if walked.refused:
                bad = walked.refusals[0]
                return output, (f"its output would end a real run `pii_in_output`: "
                                f"{bad.kind} at {bad.path} ({bad.pii_type})")
            return walked.value, None

        async def collect(batch: asyncio.Task, whose: str) -> None:
            """Wait for a probe batch, BOUNDED, and say so if it was cut.

            ONE copy, because there are three batches now and the third
            was awaited directly with no bound at all: `cross_use` runs
            its plan sequentially, so an agent that accepts a connection
            and never answers costs the client timeout PER PROBE —
            measured at 84s against a 2s timeout on a manifest whose
            fresh anchor had two prior invocations to cross-use, which at
            a production timeout is hours of hanging instead of the
            failing verdict this is supposed to produce (Codex round 21).
            A probe the agent never answers must not hold the battery
            open, and it is not the agent's deadline being spent here.
            """
            # A STALL, NOT A TOTAL. Counting the plan from `bindings` at
            # this moment does not work: `collect` is handed a task that
            # has not started, `expect` seeds the slots from INSIDE it,
            # and the count read here is therefore zero — measured, the
            # anchor batch was bounded at "the 1s that 1 serial probes
            # are allowed" and cancelled after three of its twelve. The
            # truth was in the right place and not yet in it.
            #
            # So bound what is actually wrong instead: a batch that has
            # STOPPED ANSWERING. Each probe is already cut at
            # `_PROBE_CEILING_SECONDS`, so a live batch must record an
            # answer within roughly that; one that records nothing for
            # longer has stopped, whatever its length. This needs no plan
            # length, so it cannot be wrong about one, and it is the same
            # shape as the settle window above — measure from the last
            # thing that happened, not from the start.
            stall = _PROBE_CEILING_SECONDS * 2 + 1.0
            answered = -1
            moved = time.monotonic()
            cut = False
            while not batch.done():
                now_answered = sum(
                    1 for v in bindings.values() if v != _UNANSWERED)
                if now_answered != answered:
                    answered, moved = now_answered, time.monotonic()
                if time.monotonic() - moved >= stall:
                    cut = True
                    break
                await asyncio.sleep(0.05)
            if cut:
                batch.cancel()
                result.failures.append(
                    f"{whose} stopped answering for {stall:.0f}s and were "
                    f"cancelled, so the binding check ran on only part of "
                    f"what it claims to cover"
                )
            try:
                await batch
            except (TimeoutError, httpx.HTTPError, asyncio.CancelledError):
                pass

        first_probes = expect(unauthenticated_probes(FIRST, ref))

        async def _live_probes() -> None:
            """The four token probes. The TASK starts here; the first
            REQUEST does not.

            They once ran serially between the POST and the stream, which
            put the battery's own requests inside the invocation's
            timeline: with a paused clock that let a 5s invocation pass a
            3s deadline, and with a continuous one it would have charged
            the agent for probes production never makes (Codex rounds 15
            and 16 pulling in opposite directions). Running them beside
            the stream answered both. It did not answer a third thing —
            a probe merely in flight sits in a serialized agent's accept
            queue ahead of the `/output` fallback, which IS inside the
            budget (round 31) — so `run_probes` now waits for
            `agent_free` and the task created here sends nothing until
            the invocation's last leg has closed.

            This asks nothing new of the agent, and less than it used to.
            """
            await run_probes(first_probes)

        # The verdict is NOT reached here. The case that separates a
        # BINDING from an allowlist needs a token the agent ITSELF
        # issued, for a DIFFERENT invocation — and minting that second
        # invocation while the first is still running asks the agent to
        # hold two at once, which the contract never promises: the
        # chassis makes one invocation per phase and PARKS between them
        # (Run_Contract_v1 §"One contract invocation"). So that probe
        # waits until this invocation has finished, below.
        #
        # Until it concludes the check is OPEN, and open counts as
        # failing. It used to be closed here on the four probes alone,
        # so a single-flight agent that answered the second POST 409 got
        # `token_binding: pass` and `passed: True` with a NOTE — a probe
        # that did not run, reported as one that succeeded (Codex round
        # 2). Nothing consumed the note: `passed` reads only `failures`.
        # Measured against exactly such an agent, whose allowlist was
        # global: certified clean, and then a SEQUENTIAL second
        # invocation's token read the first's output with `200`.
        result.checks["token_binding"] = "incomplete"
        result.failures.append(BINDING_UNFINISHED)

        # This invocation is about to occupy the agent. A serialized one
        # will not answer the probes below until its last leg closes, so
        # their ceilings must not start running yet.
        agent_free.clear()
        probes = asyncio.create_task(_live_probes())
        # THE PROBES ARE COLLECTED AFTER THE INVOCATION'S LAST LEG,
        # not between the stream and the output fetch. The budget is
        # absolute wall clock, so waiting here for the battery's own
        # 401s spent the agent's remaining deadline: measured, an
        # agent whose POST, stream and `/output` took 1.0s against a
        # 5s deadline was failed `the invocation outran the 5s
        # deadline ... while serving its output` — for a request the
        # battery never made, because four 1.5s probes had already
        # eaten the budget (Codex round 19). That also contradicted
        # this PR's own claim that probe time is outside the
        # invocation's deadline. The `finally` moves OUT one level
        # rather than away: every early return below still passes
        # through it, which is what keeps a cancelled batch named
        # (§12 183(a)).
        try:
            try:
                status, text = await _consume_events(
                    client, f"{base}/v1/runs/{ref}/events", headers, budget,
                )
                if status != 200:
                    result.failures.append(f"events stream returned {status}")
                    return result
            except TimeoutError:
                # What the runner would do with this agent, said in the
                # battery's own voice: `PhaseDeadlineExceeded`, run `error`.
                result.failures.append(
                    _outran(body["deadline_seconds"], "streaming its events")
                )
                result.checks["completed"] = "fail"
                return result
            except httpx.HTTPError as exc:
                result.failures.append(f"events stream failed: {exc}")
                return result

            result.events = _parse_sse(text)

            first_kind, first_payload, first_why, first_extra = _terminal_of(
                result.events)
            first_why = first_why or first_extra
            if first_kind != "completed" or first_why is not None:
                # THE SAME HELPER as the drain and the anchor. This site
                # was the only one of the three that had the rule right;
                # it now states it in the one place the others read, so a
                # fourth stream cannot inherit a looser copy.
                result.failures.append(
                    first_why if first_why is not None else
                    f"expected exactly one terminal event, completed; got "
                    f"{[k for k, _ in result.events if k in ('completed', 'failed')]}"
                    + (f" ({first_payload.get('error')})"
                       if first_kind == "failed" else "")
                )
                result.checks["completed"] = "fail"
            else:
                result.checks["completed"] = "pass"
                output = first_payload.get("output")
                if output is None:
                    # Still the same budget: the chassis fetches this inside
                    # the same `asyncio.timeout` as everything before it.
                    try:
                        async with budget.segment():
                            fallback = await client.get(
                                f"{base}/v1/runs/{ref}/output", headers=headers)
                        if fallback.status_code != 200:
                            output = None
                        else:
                            fetched, not_object = _object_body(fallback)
                            if not_object is not None:
                                result.failures.append(
                                    f"`completed` carried no output and "
                                    f"`/output` answered 200 with {not_object}")
                                result.checks["completed"] = "fail"
                                output = None
                            else:
                                output = fetched.get("output")
                    except TimeoutError:
                        result.failures.append(
                            _outran(body["deadline_seconds"], "serving its output")
                        )
                        result.checks["completed"] = "fail"
                        output = None
                    except httpx.HTTPError as exc:
                        # REPORTED, NEVER RAISED — the third time this file
                        # has had to learn it (the second POST at round 2,
                        # the binding probes at round 4). This fetch caught
                        # only `TimeoutError`, so an agent that reset the
                        # connection here took the exception straight out of
                        # `run_contract_battery` and the CLI printed a
                        # traceback instead of a verdict. Measured:
                        # `httpx.RemoteProtocolError: Server disconnected
                        # without sending a response` escaping from this
                        # line (Codex round 32). The two sibling fallbacks
                        # already caught both.
                        #
                        # A SEPARATE clause rather than a widened one,
                        # because the verdicts differ: `_outran` says the
                        # agent was too slow, which is not what happened
                        # and would send an author to the wrong place.
                        result.failures.append(
                            f"`completed` carried no output and `/output` "
                            f"could not be read: {exc!r} — the chassis raises "
                            f"`ContainerAgentError` here, so the run ends "
                            f"`error` rather than producing a result"
                        )
                        result.checks["completed"] = "fail"
                        output = None
                result.output = output
        finally:
            # THE LAST LEG IS DONE, whichever way it went — so the
            # agent is free from here, and this is the release that lets
            # `run_probes` SEND its first request. Set before `collect`,
            # because `collect` is what waits for that batch and its
            # stall window assumes the batch can make progress.
            agent_free.set()
            # Collected whichever way the stream went.
            await collect(probes, "the invocation's own binding probes")

        names = [p.name for p in manifest.phases]
        index = names.index(phase) if phase in names else 0
        drained = 0
        # TRANSITIONS THE RUN COULD NO LONGER REACH, subtracted from the
        # plan before the verdict. A `failed` phase ends its run, and
        # asking a run for a phase whose predecessor never ran in it is a
        # request the chassis never makes — counting that against
        # `drained` would fail an agent for refusing one. What "could no
        # longer reach" means is now decided by DRIVING a replacement run
        # to the phase the next transition needs, not by assuming the
        # replacement reaches one phase and subtracting everything past
        # it: the assumption certified an agent that leaks from the third
        # phase on (Codex round 34). Only a replacement run that itself
        # cannot get there subtracts anything.
        unreachable = 0
        # Every transition into a second invocation that THIS manifest can
        # actually produce — and a gated phase produces two, which is the
        # point. `runs.py:466` accepts an edit while a run is parked and
        # schedules `rerun_current_phase`, so for a phase followed by an
        # approval gate the chassis makes BOTH the next-phase invocation
        # and, if the user edits, a rerun of this phase. They are distinct
        # request paths: a rerun holds `phase` constant as well as
        # `run.id`, so an agent that pools tokens by `(run.id, phase)` —
        # or that pools only when `run.rerun` is true — is invisible to
        # the next-phase probe. Measured against exactly such an agent:
        # next-phase probe `token_binding: pass` with two disjoint buckets,
        # and the rerun-shaped request leaking `200` (Codex round 7).
        #
        # Both shipped multi-phase manifests (vita_v1, langgraph_triage)
        # gate their second phase, so the rerun is the transition their
        # first phase really gets — the branch round 6 had removed.
        # (name, why, build). The NAME keys the binding cases and has to
        # read as a noun in "events with <name> answered 200, not 401";
        # the WHY is the report's explanation. Using one string for both
        # produced case names with a rationale embedded in them.
        #
        # BUILD is a function of the output the PREVIOUS invocation
        # actually returned, not a finished dict, because `prior_output`
        # is whatever the phase before it produced — and after a rerun
        # that is the RERUN's analysis (`agent_runner.py:485-489` reads
        # the snapshot the rerun just wrote), not the original's. A dict
        # frozen before the loop cannot express that.
        transitions: list[tuple[str, str, Callable[[dict, dict], dict]]] = []
        # WALKED HERE, because `carried` is taken from it on the next line
        # and becomes the next invocation's `prior_output`. The late
        # output check reports what this walk found rather than walking
        # again, so the value the report shows and the value the battery
        # carries are the same one.
        first_output_why: str | None = None
        if isinstance(result.output, dict):
            walked_first, first_output_why = await walked_off_loop(result.output)
            if first_output_why is None:
                result.output = walked_first
        carried = result.output if isinstance(result.output, dict) else {}
        if index + 1 < len(manifest.phases):
            # ORDER is part of producibility, not just membership. The
            # set was right and the sequence was not: completing the
            # final phase sets `run.status = "complete"`
            # (`agent_runner.py:568-577`) and `runs.py:466` accepts an
            # edit only in `awaiting_approval`/`refining`, so the only
            # order the chassis can produce is
            #   phase -> park -> rerun -> approve -> next phase.
            # Advancing first asked a stateful agent to rerun a completed
            # run. Measured against an agent correct on every axis the
            # battery checks — tokens bound per invocation, a rerun
            # accepted exactly while parked: `token_binding: fail`,
            # "per-invocation binding is UNPROVEN: the agent would not
            # start a rerun of echo ... (400)", for a sequence the
            # chassis never sends (Codex round 8).
            #
            # THE WHOLE REACHABLE SEQUENCE, not just the next step.
            # `agent_runner.py:382` is `while i < len(phases)` and `:640`
            # is `i += 1`; `:626` returns only on `not ok`,
            # `awaiting_approval` or `complete`, and `:589-591` parks
            # ONLY when `phases[i+1].approval`. So an ungated manifest
            # runs phase 0 -> 1 -> 2 -> ... in ONE run with no human in
            # between, and a gate stops the run but not the sequence: the
            # user approves and it carries on. Planning `index + 1` alone
            # certified everything past it unseen. Measured on a
            # three-phase ungated manifest against an agent that binds
            # correctly for two invocations and pools by `run.id` from
            # the third: the battery invoked `['echo', 'second']`,
            # reported `token_binding: pass`, `passed: True`, no
            # failures — while the third invocation's `/events` and
            # `/output` both answered `200` to the FIRST invocation's
            # token (Codex round 25).
            #
            # `step` is bound as a DEFAULT ARGUMENT in every closure
            # below. Python closes over the variable, not its value, so a
            # lambda reading `step` from the enclosing scope would see
            # the loop's last value in every transition — one list of
            # builds that all target the final phase, which is the hole
            # this walk exists to close wearing a different hat.
            step = index
            while step + 1 < len(manifest.phases):
                nxt = step + 1
                if getattr(manifest.phases[nxt], "approval", False):
                    # A rerun carries the user's edit TEXT (`AgentInput.
                    # user_edits` is `str | None`) and the prior output,
                    # which `agent_runner.py:485-489` supplies whenever a
                    # rerun runs. There is no user here, so the label says
                    # what it is rather than impersonating one.
                    #
                    # The rerun repeats the phase the gate PARKED, which
                    # is `step` — not the phase under test once the walk
                    # has moved on — so it carries that phase's name and
                    # that phase's own budget rather than the body's.
                    transitions.append((
                        f"the {names[step]} rerun's token",
                        f"a rerun of {names[step]}, which the gate on "
                        f"{names[nxt]} makes producible",
                        lambda prior, run, step=step: {
                            **body, "run": {**run, "rerun": True},
                            "phase": names[step],
                            "deadline_seconds": advertised_deadline(
                                manifest.phases[step]),
                            "user_edits": f"battery rerun of {scenario_name}",
                            "prior_output": prior},
                        # A rerun REPEATS `step`, so a run that has
                        # completed `step` and is parked at the gate can
                        # produce it. This is the fourth element every
                        # transition now carries: how far a replacement
                        # run must be driven before the transition is
                        # producible on it, or None when the transition
                        # starts a run of its own (Codex round 34).
                        step,
                    ))
                transitions.append((
                    f"the {names[nxt]} phase's token",
                    f"the next phase ({names[nxt]}) of the same run",
                    # The NEXT phase's budget, which is its own: a rerun
                    # and a second run both hold the phase constant and so
                    # inherit theirs.
                    lambda prior, run, nxt=nxt: {
                        **body, "run": run,
                        "phase": names[nxt],
                        "deadline_seconds": advertised_deadline(
                            manifest.phases[nxt]),
                        "prior_output": prior},
                    # `nxt - 1`, which is `step`: the phase before this
                    # one has to have run in that run.
                    nxt - 1,
                ))
                step = nxt

        # WHERE THE SEQUENCE ENDS. Everything up to here is the WALK:
        # transitions the chassis produces by advancing THIS run, each one
        # reachable only if its predecessor ran. Everything after is
        # RUN-SCOPED — it starts a run of its own, so nothing that happens
        # to this one can make it unproducible. The reachability stop
        # below applies to the first group and must not touch the second.
        walk_len = len(transitions)

        # A SECOND RUN OF THE PHASE UNDER TEST, in BOTH branches — this
        # used to be the `else` of the walk above, reached only when the
        # manifest had no later phase.
        #
        # That made every cross-use pair on a multi-phase manifest cross a
        # PHASE boundary, and an agent that pools its tokens per phase
        # refuses all of those correctly. Measured against exactly such an
        # agent on a two-phase ungated manifest: `token_binding: pass`,
        # `passed: True`, no failures — and then a real second run of the
        # first phase read the first run's `/output` with `200` (Codex
        # round 32). The round-2 defect scoped by phase: membership in a
        # pool is not binding to an invocation, whatever keys the pool.
        #
        # It goes LAST rather than first so the walk's `prior_output`
        # chain is untouched: a next-phase invocation must carry the
        # output of ITS run's previous phase, and threading a different
        # run's output through it would ask the agent for a request the
        # chassis never makes. The cost is that when the LAST walk
        # transition ends `failed` this one is counted unreachable and
        # skipped — the sequence is over by then, and a mid-walk failure
        # still reaches it, because that path re-anchors and carries on.
        transitions.append((
            "another invocation's token",
            ("a second run, because this manifest invokes this phase "
             "once per run and the same-run case cannot arise")
            if index + 1 >= len(manifest.phases) else
            ("a second run of the same phase, which no transition in the "
             "walk above crosses — every one of those changes the phase"),
            # `_prior` is ignored on purpose: a fresh run's first
            # phase has no prior output whatever this battery has
            # seen, and `body` already carries `prior_output: None`.
            lambda _prior, run: {**body, "run": {**run, "id": "battery-run-2",
                                                 "case_id": "battery-run-2"}},
            # None: it starts a run of its own, so there is no prefix to
            # drive and no replacement run to drive it on.
            None,
        ))

        # ATTEMPTED, not planned. A transition whose invocation ends
        # `failed` sets the run to error (`agent_runner.py:574-577`), and
        # the chassis invokes nothing after that — so the rest of the
        # sequence stops and the verdict is taken over what was actually
        # reachable. Comparing `drained` against the PLANNED count would
        # turn that legal stop into a binding failure.
        # The ANCHOR is the invocation the cross-uses are taken against
        # and the run the remaining transitions branch from. It starts as
        # the invocation under test and MOVES when a transition ends
        # `failed`: that run is over, but the transitions it was carrying
        # are not necessarily unreachable — a gated phase advances when
        # the user APPROVES the original invocation, without ever editing
        # it, so the later phase has an independent path (Codex round 10).
        # Dropping it certified an agent whose reruns are correct and
        # whose ordinary next-phase invocations pool tokens by `run.id`:
        # measured `token_binding: pass` over a real leak never probed.
        anchor_run = body["run"]
        fresh_runs = 0
        # Why the last `_fresh_anchor()` gave nothing, when the reason is
        # more specific than "it would not start one". The caller's
        # generic sentence is right for a refusal and wrong for an
        # invocation that started, streamed and then could not produce a
        # result — the second instance of the round-20 defect.
        anchor_why: str | None = None
        # Every invocation this battery has started, as (noun, ref,
        # token). Each new one is cross-used against all of these, both
        # ways, before joining them.
        issued: list[tuple[str, str, str]] = [(FIRST, ref, token)]

        async def cross_use(noun: str, new_ref: str, new_token: str) -> None:
            """Everything a NEWLY STARTED invocation owes: both directions
            against every earlier one, and the two probes that carry no
            token the agent issued. Then remember it.

            ONE copy of the rule, because it has two callers now: the
            transition loop and the re-anchor. The fresh anchor used to be
            appended to `issued` without being probed at all, so an agent
            whose cleanup after a failed rerun let the ORIGINAL token read
            the replacement run was certified — while the report, and this
            PR, claimed every pair (Codex round 12). Two copies of a rule
            is how one of them quietly stops matching.
            """
            # Named before the first await, for the reason in `expect`:
            # this runs as a task too, and a cancelled batch must leave
            # its unanswered probes named rather than missing.
            plan = expect(
                [
                    (f"{leaf} with {direction}",
                     f"{base}/v1/runs/{target}/{leaf}", bearer)
                    for other_noun, other_ref, other_token in issued
                    for direction, target, bearer in (
                        (f"{noun}'s token on {other_noun}", other_ref, new_token),
                        (f"{other_noun}'s token on {noun}", new_ref, other_token),
                    )
                    for leaf in ("events", "output")
                ]
                # The two tokenless questions belong to the invocation
                # that was just started, not to the first one, and
                # `cross_use` is where every invocation this battery
                # starts passes (Codex round 18).
                + unauthenticated_probes(noun, new_ref)
            )
            await run_probes(plan)
            issued.append((noun, new_ref, new_token))

        # HOW FAR the last `_fresh_anchor()` actually drove its run — the
        # highest phase index it completed, or None if it never got the
        # phase under test to `completed`. The caller needs both halves:
        # an agent that will not give the battery a working replacement
        # run at all leaves the check unable to conclude, while an agent
        # whose LATER phase fails on that run has genuinely put the rest
        # of the sequence out of reach, which is a note and a shorter
        # plan rather than a charge against it.
        reached_on_fresh: int | None = None
        # WHY the last leg stopped: True ONLY for a valid `failed`
        # terminal. Every other way a leg can end — a 500, a timeout, two
        # terminals, a stream that just stops, an output the chassis
        # could not have obtained — means the chassis could not consume
        # an invocation it CAN produce, which is a check that did not
        # conclude and not a shorter plan. Treating them alike let an
        # agent answer 500 on a replay leg and take `token_binding: pass`
        # (Codex round 35).
        leg_ended_failed = False

        async def _fresh_anchor(needs: int) -> tuple[str, str, dict, dict] | None:
            """A new run, driven from the phase under test up to `needs`.

            Returns (ref, token, run, output) for the LAST leg it drove,
            or None if the agent would not give the battery a run that
            reaches there. `reached_on_fresh` says how far it got.

            REPLAYING THE PREFIX IS THE POINT. This drove ONE phase and
            stopped, and the caller then subtracted every transition that
            started further along than that — treating the helper's reach
            as production's. It is not: a user whose edit fails starts
            another run, APPROVES instead of editing, and reaches the
            later phase that way. Measured on `p1 -> p2 -> gated p3 -> p4`
            with the p2 rerun answering a legal `failed`, against an agent
            bound per invocation everywhere the battery looked and pooling
            by `run.id` from p3 on: `token_binding: pass`, no failures,
            `third` and `fourth` never invoked — and a real replacement
            run that replayed the prefix read p1's token into p3's
            `/output` with `200` (Codex round 34).
            """
            nonlocal fresh_runs, anchor_why, reached_on_fresh
            fresh_runs += 1
            anchor_why = None
            reached_on_fresh = None
            run_id = f"battery-run-fresh-{fresh_runs}"
            fresh_run = {**body["run"], "id": run_id, "case_id": run_id,
                         "rerun": False}
            ordinal = fresh_runs

            async def _leg(step: int, prior: dict | None
                           ) -> tuple[str, str, dict] | None:
                """One invocation of `manifest.phases[step]` on this run.

                ONE copy of the rule for every leg, including the first.
                The prefix legs are not scaffolding the battery is allowed
                to leave unexamined: each is an invocation this battery
                started, so each is cross-used like any other before the
                next one begins (§12 168 — the fresh anchor was appended
                to `issued` without being probed once already).
                """
                nonlocal anchor_why, leg_ended_failed
                leg_ended_failed = False
                leg_phase = names[step]
                leg_deadline = advertised_deadline(manifest.phases[step])
                leg_token = secrets.token_urlsafe(32)
                # The context FIRST: `trace_context` is what mints this
                # run's trace id, and `minted_tokens` records the one it
                # minted.
                leg_headers = {**headers,
                               "Authorization": f"Bearer {leg_token}",
                               **trace_context(run_id)}
                minted_tokens[leg_token] = traces[run_id]
                leg_budget = _InvocationBudget(leg_deadline)
                if not await health_probe(leg_budget):
                    anchor_why = (f"it was not healthy when the chassis would "
                                  f"have started its {leg_phase}")
                    return None
                try:
                    async with leg_budget.segment():
                        started_leg = await client.post(
                            f"{base}/v1/runs",
                            json={**body, "run": fresh_run, "phase": leg_phase,
                                  "deadline_seconds": leg_deadline,
                                  "prior_output": prior, "user_edits": None},
                            headers=leg_headers,
                        )
                except (httpx.HTTPError, TimeoutError) as exc:
                    anchor_why = (f"the POST that would have started its "
                                  f"{leg_phase} did not complete: {exc!r}")
                    return None
                if started_leg.status_code not in (200, 201):
                    # THE PHRASE STAYS, and the code is added beside it.
                    # This path had no `anchor_why` at all, so the caller
                    # rendered its generic "the agent would not start a
                    # fresh run" sentence — which is accurate here, is
                    # what an existing guard anchors on, and is not made
                    # truer by being replaced. Round 35 needed the
                    # REASON, not a different sentence (§12 202(f)).
                    anchor_why = (f"the agent would not start a fresh run to "
                                  f"reach the remaining transition(s) — it "
                                  f"answered {started_leg.status_code} to the "
                                  f"POST that would have started its "
                                  f"{leg_phase}")
                    return None
                try:
                    leg_body, leg_not_object = _object_body(started_leg)
                    if leg_not_object is not None:
                        anchor_why = (f"its {leg_phase} answered "
                                      f"{started_leg.status_code} with "
                                      f"{leg_not_object}, so it named no "
                                      f"invocation")
                        return None
                    # `run_id` is the pre-v1.1 spelling of the same field
                    # and the contract still accepts it — as do the first
                    # POST above, the ordinary transition parser, and
                    # `container.py:302-308`. Reading only `invocation_id`
                    # here told a conformant agent it "would not start a
                    # fresh run" (Codex round 12).
                    leg_id = (leg_body.get("invocation_id")
                              or leg_body.get("run_id"))
                except ValueError:
                    anchor_why = (f"its {leg_phase} answered "
                                  f"{started_leg.status_code} with a body that "
                                  f"is not JSON")
                    return None
                if not isinstance(leg_id, str) or not leg_id:
                    anchor_why = (f"its {leg_phase} answered "
                                  f"{started_leg.status_code} naming no "
                                  f"invocation")
                    return None
                leg_ref = quote(leg_id, safe="")
                # THE INVOCATION NOW EXISTS, so it is probed however it
                # ends. `return`ing before the cross-use left every leg
                # that ended `failed` — or timed out, or answered 500 —
                # started, remembered by the agent, and never tested
                # against any other token: an agent could expose that
                # invocation's events and output to another invocation's
                # bearer and still take `token_binding: pass` (Codex round
                # 35). Round 12's defect, in the one place the replay
                # newly creates. The `finally` is what makes it EVERY
                # exit path rather than the paths I remembered.
                try:
                    # It has to SUCCEED, or it is no better an anchor than the
                    # one it replaces.
                    try:
                        leg_status, leg_text = await _consume_events(
                            client, f"{base}/v1/runs/{leg_ref}/events",
                            leg_headers, leg_budget,
                        )
                        if leg_status != 200:
                            anchor_why = (f"its {leg_phase} answered {leg_status} to "
                                          f"its own events stream, so the chassis "
                                          f"could not have consumed that invocation")
                            return None
                    except (httpx.HTTPError, TimeoutError) as exc:
                        # An anchor that outran its budget is no anchor: the
                        # chassis would have cancelled it. Same verdict as a
                        # refusal — the caller records that the binding check
                        # could not conclude.
                        anchor_why = (f"its {leg_phase} could not be drained with "
                                      f"its own token: {exc!r}")
                        return None
                    leg_events = _parse_sse(leg_text)
                    # THE SAME TWO CONTRACT RULES AS THE DRAIN, on every
                    # invocation the recovery path starts. `not any(k ==
                    # "completed")` said nothing about WHICH of the two
                    # happened, and the shape filter read `output: []` as no
                    # output at all.
                    leg_kind, leg_payload, leg_why, leg_extra = _terminal_of(
                        leg_events)
                    leg_finished = (leg_payload if leg_kind == "completed"
                                    and leg_extra is None else None)
                    if leg_finished is None:
                        # THE ONE CASE THAT IS THE AGENT'S PREROGATIVE rather
                        # than its defect. `leg_why` is a stream that never
                        # terminated and `leg_extra` a second terminal; both
                        # are contract violations the chassis dies on, so
                        # neither may quietly shorten the plan.
                        leg_ended_failed = (leg_kind == "failed"
                                            and leg_why is None
                                            and leg_extra is None)
                        anchor_why = (
                            leg_why if leg_why is not None else
                            leg_extra if leg_extra is not None else
                            f"its {leg_phase} ended `failed`, so it is no better "
                            f"an anchor than the one it replaces"
                        )
                        return None
                    leg_output = None
                    leg_got = leg_finished.get("output")
                    if isinstance(leg_got, dict):
                        leg_output = leg_got
                    elif leg_got is not None:
                        anchor_why = (
                            f"its {leg_phase}'s `completed` carried an `output` "
                            f"that is {type(leg_got).__name__}, not a JSON object"
                        )
                        return None
                    if leg_output is None:
                        # Same fallback as above: `completed` may carry no
                        # inline output and the result live at `/output`.
                        try:
                            async with leg_budget.segment():
                                leg_fallback = await client.get(
                                    f"{base}/v1/runs/{leg_ref}/output",
                                    headers=leg_headers,
                                )
                            if leg_fallback.status_code != 200:
                                anchor_why = (f"its {leg_phase}'s `completed` "
                                              f"carried no output and `/output` "
                                              f"answered "
                                              f"{leg_fallback.status_code}")
                            else:
                                leg_decoded, leg_bad = _object_body(leg_fallback)
                                if leg_bad is not None:
                                    anchor_why = (f"its {leg_phase}'s `completed` "
                                                  f"carried no output and its "
                                                  f"`/output` answered 200 with "
                                                  f"{leg_bad}")
                                    return None
                                fetched = leg_decoded.get("output")
                                if isinstance(fetched, dict):
                                    leg_output = fetched
                                else:
                                    anchor_why = (
                                        f"its {leg_phase}'s `completed` carried no "
                                        f"output and `/output` answered 200 but "
                                        f"`output` is {type(fetched).__name__}, "
                                        f"not a JSON object"
                                    )
                        except TimeoutError:
                            anchor_why = (
                                f"its {leg_phase}'s `completed` carried no output "
                                f"and the fetch outran the {leg_deadline}s "
                                f"deadline that invocation was advertised"
                            )
                        except ValueError:
                            anchor_why = (f"its {leg_phase}'s `completed` "
                                          f"carried no output and `/output` "
                                          f"answered 200 with a body that is not "
                                          f"JSON")
                        except httpx.HTTPError as exc:
                            anchor_why = (f"its {leg_phase}'s `completed` "
                                          f"carried no output and the fetch "
                                          f"failed at the transport: {exc!r}")
                    # THE SAME RULE AS THE DRAIN, on every invocation the
                    # recovery path starts: production raises
                    # `ContainerAgentError` when the fallback fetch cannot
                    # produce a JSON object (`container.py:506-531`), so an
                    # anchor whose result the chassis could not have obtained
                    # is no anchor. It used to carry `{}` forward silently,
                    # which made the NEXT transition's `prior_output` a
                    # fiction as well.
                    if anchor_why is None and leg_output is not None:
                        leg_output, anchor_why = await walked_off_loop(leg_output)
                    if anchor_why is not None:
                        return None
                    return leg_ref, leg_token, leg_output or {}
                finally:
                    # THE NOUN CARRIES BOTH ORDINALS — which replacement
                    # run, and which phase of it. Two legs of one run
                    # sharing a noun would overwrite each other's
                    # `bindings` entries, the round-10 defect in the shape
                    # §12 190(a) names.
                    # NO `agent_free` PAIR HERE, and that is a statement
                    # rather than an omission: every request this leg
                    # makes is AWAITED above, so the event is already set
                    # and `run_probes` proceeds without waiting. The pair
                    # belongs wherever a batch is CREATED while an
                    # invocation still holds the agent; this one is
                    # created after.
                    await collect(
                        asyncio.create_task(
                            cross_use(f"fresh run {ordinal}'s {leg_phase}",
                                      leg_ref, leg_token)),
                        f"fresh run {ordinal}'s {leg_phase} cross-use probes",
                    )

            carried_on_fresh: dict | None = None
            last: tuple[str, str, dict] | None = None
            # FROM THE PHASE UNDER TEST FORWARD, one phase at a time and
            # each one carrying the last one's output — the sequence the
            # chassis produces for a user who approves rather than edits.
            # `needs` is where the caller has to get to, never further:
            # a leg the remaining plan does not need is an invocation
            # production would not have made here.
            for step in range(index, needs + 1):
                last = await _leg(step, carried_on_fresh)
                if last is None:
                    return None
                reached_on_fresh = step
                carried_on_fresh = last[2]
            if last is None:
                return None
            return last[0], last[1], fresh_run, last[2]

        # Round 9 counted transitions ATTEMPTED rather than planned,
        # because a failed transition STOPPED the sequence and the planned
        # count would have read that legal stop as a binding failure.
        # Round 10 replaced the stop with a re-anchor, so the loop reaches
        # every planned transition unless the agent refuses a fresh run —
        # and that is a check which did not conclude, which
        # `drained != len(transitions)` already fails. The distinction had
        # become one that cannot matter, and its injection reported
        # `not-caught` and was RIGHT (§12 172(f)); deleted rather than
        # kept beside a stronger condition.
        stopped_after: str | None = None
        # Set when a `failed` transition ends the run the WALK was
        # advancing. The walk stops there; the run-scoped transitions
        # after it are untouched and still run.
        walk_abandoned = False
        for position, (name, label, build, _needs) in enumerate(transitions):
            if walk_abandoned and position < walk_len:
                continue
            result.notes.append(f"the binding probe used {label}")
            second_body = build(carried, anchor_run)
            # The transition's own noun, for case names and for the note
            # that explains a stop: every label is a clause and reads as
            # nonsense joined to a verb (§12 173(g)).
            short = (name[: -len("'s token")] if name.endswith("'s token")
                     else name)
            second = secrets.token_urlsafe(32)
            assert second not in (token, foreign), "each probe token is its own"
            # ONE header set for this whole invocation: production injects
            # the phase span once per `run_phase` and every request of that
            # invocation carries it, so the POST, the drain and the output
            # fetch must agree.
            second_headers = {**headers, "Authorization": f"Bearer {second}",
                              **trace_context(second_body["run"]["id"])}
            minted_tokens[second] = traces[second_body["run"]["id"]]
            neighbour_id: str | None = None
            why: str | None = None
            second_budget = _InvocationBudget(second_body["deadline_seconds"])
            if not await health_probe(second_budget):
                result.failures.append(
                    f"{label}: the agent was not healthy when the chassis "
                    f"would have started it"
                )
                continue
            try:
                async with second_budget.segment():
                    neighbour = await client.post(
                        f"{base}/v1/runs", json=second_body,
                        headers=second_headers,
                    )
            except TimeoutError:
                neighbour = None
                why = (
                    f"the agent did not answer the second POST /v1/runs "
                    f"within the {second_body['deadline_seconds']}s that "
                    f"invocation was advertised"
                )
            except httpx.HTTPError as exc:
                # The FIRST POST has always turned a transport failure into
                # a result rather than a traceback; these must too, or the
                # rule that an open check is a failing check is broken on
                # the path where the agent is least well behaved.
                neighbour = None
                why = f"the second POST /v1/runs failed at the transport: {exc!r}"
            if neighbour is not None:
                if neighbour.status_code not in (200, 201):
                    why = (
                        f"the agent would not start {label} "
                        f"({neighbour.status_code}) even after the first had "
                        f"finished, so no token it had issued existed to cross-use"
                    )
                else:
                    started_second, second_not_object = _object_body(neighbour)
                    candidate = (
                        started_second.get("invocation_id")
                        or started_second.get("run_id")
                        if started_second is not None else None)
                    if second_not_object is not None:
                        why = (
                            f"the agent answered {label} "
                            f"{neighbour.status_code} with {second_not_object}, "
                            f"so there was nothing to address a cross-use to"
                        )
                    elif not isinstance(candidate, str) or not candidate:
                        why = (
                            f"the agent answered {label} "
                            f"{neighbour.status_code} but named no invocation_id, "
                            f"so there was nothing to address a cross-use to"
                        )
                    elif candidate == invocation_id:
                        why = (
                            f"the agent answered {label} with the SAME "
                            f"invocation_id ({candidate}), replacing the first "
                            f"invocation rather than starting another, so there "
                            f"is no other invocation to cross-use"
                        )
                    else:
                        neighbour_id = candidate
            if neighbour_id is None:
                # NOT a pass, and not a note. An unproven MUST is not a
                # satisfied one; `why` says which of the four ways it got
                # here, because an author cannot act on the wrong one.
                result.failures.append(f"per-invocation binding is UNPROVEN: {why}")
                continue

            # BOTH directions, because the leak is DIRECTIONAL: an agent
            # that hands each invocation a SNAPSHOT of the tokens issued so
            # far refuses the newer token on the older invocation and
            # accepts the older on the newer.
            second_ref = quote(neighbour_id, safe="")
            # `bindings` is keyed by case NAME, and the reverse
            # direction's name used to be the same string for every
            # transition — so for a gated manifest the next-phase
            # iteration OVERWROTE the rerun's result, and an agent that
            # accepts the first token on the rerun but refuses it on the
            # next phase turned its own `200` into a `401` and passed
            # (Codex round 10). Naming the transition keeps every
            # observed violation.
            # EVERY ORDERED PAIR, not only the pairs involving the
            # anchor. "Per-invocation" is a claim about all of them, and
            # anchoring left the (rerun -> next phase) pair unprobed: an
            # agent whose next-phase invocation accepts the token of the
            # rerun immediately before it answered 401 to everything this
            # loop asked and passed (Codex round 11). With three
            # invocations there are six ordered pairs and the anchored
            # loop asked about four.
            # NOT BEFORE THE DRAIN — see `_live_probes`. Under a
            # continuous clock a serial cross-use spends the drained
            # invocation's deadline on the battery's own requests, which
            # is the round-16 defect in its second instance. Enumerating
            # the instances rather than fixing the one in front of me is
            # the rule §12 181 exists for. Since round 31 this task does
            # not send before the drain either, only after it.
            # Same pair as the first invocation's, for the same reason:
            # this one occupies the agent too, so its probes must not be
            # sent — nor queued behind it — until its drain is done.
            agent_free.clear()
            crossing = asyncio.create_task(cross_use(short, second_ref, second))

            # Drain it with its OWN token before moving on: execution may
            # begin LAZILY when the events stream is opened, so an
            # abandoned invocation either never runs or outlives the
            # battery. A refusal here is a FAILURE — an agent that will not
            # let an invocation's own bearer read it has produced something
            # the chassis could never consume.
            try:
                # THIS invocation's advertised budget, not the first
                # one's: a next-phase transition carries its own phase's
                # `deadline_seconds`, and the whole point of sending the
                # per-phase number is that the phases differ.
                drain_status, drained_text = await _consume_events(
                    client, f"{base}/v1/runs/{second_ref}/events",
                    second_headers,
                    second_budget,
                )
                if drain_status == 200:
                    # What this invocation returned is the prior
                    # output of whatever the chassis invokes NEXT, so
                    # after the rerun the next phase receives the
                    # RERUN's analysis rather than the original's —
                    # which only became expressible once the rerun
                    # was ordered first. The drain already reads the
                    # whole stream; it used to discard the text.
                    drained_events = _parse_sse(drained_text)
                    # THE PAYLOAD FIRST, THE SHAPE SECOND. Filtering on
                    # `isinstance(..., dict)` inside the search made a
                    # `completed` carrying `output: []` indistinguishable
                    # from one carrying no output at all, so the battery
                    # fell through to `/output` and accepted whatever it
                    # found. Production falls back ONLY when the event's
                    # output is `None` (`container.py:324-328`) and
                    # `_to_result` raises on any other non-object, so an
                    # agent valid only on its first invocation passed
                    # (Codex round 21).
                    drain_kind, drain_payload, drain_why, drain_extra = (
                        _terminal_of(drained_events))
                    if drain_extra is not None:
                        # A FAILURE, BUT NOT AN UNCONSUMABLE ONE: the rest
                        # of this block goes on treating the FIRST terminal
                        # exactly as production does, so the walk continues
                        # the way the chassis would rather than being
                        # truncated by a frame production never reads.
                        result.failures.append(f"{label}: {drain_extra}")
                    finished = (drain_payload if drain_kind == "completed"
                                else None)
                    inline = None
                    # WHY this invocation produced nothing usable, or
                    # None if it produced something. `drained` counts
                    # invocations the CHASSIS COULD HAVE CONSUMED, and
                    # it used to be incremented the moment the stream
                    # answered 200 — before the fetch below, whose every
                    # failure was then swallowed. Production raises
                    # `ContainerAgentError` for a non-200, a non-JSON
                    # body and a non-object `output`
                    # (`container.py:506-531`), and
                    # `PhaseDeadlineExceeded` for a fetch that outruns
                    # the phase. Measured against an agent correct on its
                    # first invocation and on every token, whose second
                    # said `completed` with no output and answered
                    # `/output` 503: `token_binding: pass`, `passed:
                    # True`, no failures — on an invocation the chassis
                    # could never have consumed (Codex round 20).
                    unusable: str | None = None
                    if drain_why is not None:
                        # A TERMINAL EVENT IS THE CONTRACT, not a
                        # nicety: `_consume_events` raises "events
                        # stream ... ended without a terminal event"
                        # (`container.py:410-414`), so a stream that
                        # just stops is one the chassis could not have
                        # consumed. It used to count as drained, and an
                        # agent whose second invocation only ever
                        # emitted progress passed (Codex round 21).
                        unusable = drain_why
                    elif finished is not None and not isinstance(
                        finished.get("output"), dict
                    ) and finished.get("output") is not None:
                        unusable = (
                            f"`completed` carried an `output` that is "
                            f"{type(finished.get('output')).__name__}, not a "
                            f"JSON object"
                        )
                    elif finished is not None and finished.get("output") is not None:
                        inline = finished["output"]
                    elif finished is not None:
                        # `completed` without inline output is
                        # CONFORMANT: the contract puts the result at
                        # `/output` and the production client fetches
                        # it there (`container.py:324-328`). Reading
                        # only the inline form handed the next phase
                        # the ORIGINAL invocation's output, and an
                        # agent that validates `prior_output` answered
                        # `400` and was reported UNPROVEN for it
                        # (Codex round 11).
                        try:
                            async with second_budget.segment():
                                fallback = await client.get(
                                    f"{base}/v1/runs/{second_ref}/output",
                                    headers=second_headers,
                                )
                            if fallback.status_code != 200:
                                unusable = (
                                    f"`completed` carried no output and "
                                    f"`/output` answered {fallback.status_code}")
                            else:
                                decoded, bad_body = _object_body(fallback)
                                fetched = (decoded.get("output")
                                           if decoded is not None else None)
                                if bad_body is not None:
                                    unusable = (
                                        f"`completed` carried no output and "
                                        f"`/output` answered 200 with {bad_body}")
                                elif isinstance(fetched, dict):
                                    inline = fetched
                                else:
                                    unusable = (
                                        f"`completed` carried no output and "
                                        f"`/output` answered 200 but `output` is "
                                        f"{type(fetched).__name__}, not a JSON object"
                                    )
                        except TimeoutError:
                            unusable = (
                                f"`completed` carried no output and the fetch "
                                f"outran the {second_body['deadline_seconds']}s "
                                f"deadline that invocation was advertised"
                            )
                        except ValueError:
                            unusable = ("`completed` carried no output and `/output` "
                                        "answered 200 with a body that is not JSON")
                        except httpx.HTTPError as exc:
                            unusable = (f"`completed` carried no output and the fetch "
                                        f"failed at the transport: {exc!r}")
                    # WHICH failure production raises matters to the
                    # author reading this: a contract violation is
                    # `ContainerAgentError`, an output the walk refuses
                    # ends the run `error` with `pii_in_output`. Sending
                    # someone to hunt the wrong exception is the §12
                    # 173(g) defect in miniature.
                    refused_by_walk = False
                    if unusable is None and inline is not None:
                        inline, unusable = await walked_off_loop(inline)
                        refused_by_walk = unusable is not None
                    if unusable is not None:
                        # NOT drained: an invocation whose result the
                        # chassis could not have obtained is one the
                        # chassis could not have consumed, and
                        # `drained != len(transitions)` is what says the
                        # binding check did not conclude over all of them.
                        result.failures.append(
                            f"{label}: {unusable}, so the chassis could not have "
                            f"consumed that invocation — production "
                            + ("ends that run `error` with `pii_in_output`"
                               if refused_by_walk else
                               "raises ContainerAgentError here")
                        )
                    else:
                        drained += 1
                    if inline is not None:
                        carried = inline
                    # `failed` is a legal terminal event, so this is
                    # not the agent's defect — it is the end of this
                    # run, and advancing past it would ask for a
                    # transition the chassis could not make. Measured
                    # before this check existed: an agent whose rerun
                    # failed and which then refused the next phase
                    # with `409` was reported "per-invocation binding
                    # is UNPROVEN ... (409)" for a sequence the
                    # chassis never sends (Codex round 9).
                    if drain_kind == "failed":
                        # The NAME, not the label: every label is a
                        # clause ("a rerun of echo, which the gate on
                        # second makes producible") and reads as
                        # nonsense joined to a verb. The names are
                        # written as "<the thing>'s token" for the
                        # binding cases, so the thing itself is the
                        # name without that suffix (§12 173(g) — a
                        # correct verdict rendered unusable is still
                        # a defect).
                        suffix = "'s token"
                        stopped_after = (
                            name[: -len(suffix)] if name.endswith(suffix)
                            else name
                        )
                else:
                    result.failures.append(
                        f"{label}: its own token was refused by its own "
                        f"events stream ({drain_status}), so the "
                        f"chassis could not have consumed that invocation"
                    )
            except TimeoutError:
                result.failures.append(
                    f"{label}: it outran the "
                    f"{second_body['deadline_seconds']}s deadline it was "
                    f"advertised — the chassis cancels a phase at its "
                    f"deadline, so this invocation could never have finished"
                )
            except httpx.HTTPError as exc:
                result.failures.append(
                    f"{label}: its events could not be read with its own "
                    f"token: {exc!r}"
                )
            finally:
                agent_free.set()
                await collect(crossing, "the cross-use probes")
            if stopped_after is not None:
                stopped_after = None
                # POSITION, not name. Identity by name held only because
                # `manifest.py:392` rejects duplicate phase names, so a
                # guarantee in another file was deciding whether this loop
                # stops — and the walk above multiplies the names it has
                # to keep apart. The index is the thing actually being
                # asked about.
                if position == len(transitions) - 1:
                    break
                # A FRESH RUN REACHES WHEREVER THE PLAN NEEDS IT TO. It
                # used to drive the phase under test and stop, so only the
                # transition following `index` was producible on it and
                # everything past that was subtracted — the helper's reach
                # standing in for production's. Asking a run for a phase
                # whose predecessor never ran in it is still a request the
                # chassis never makes (the round-8/9 defect, which the
                # subtraction was put here to stop reintroducing); the
                # answer is to DRIVE the replacement run to that
                # predecessor, which is what a user does by starting
                # another run and approving rather than editing.
                if position + 1 >= walk_len:
                    # Only RUN-SCOPED transitions are left. They begin
                    # their own run, so the end of this one reaches them
                    # not at all: no fresh anchor is needed and nothing is
                    # unreachable. Carrying on is the whole point —
                    # breaking here let an agent that pools tokens per
                    # phase refuse every cross-phase probe, answer
                    # `failed` for the later phase, and take
                    # `token_binding: pass` with the one probe that
                    # catches it subtracted as unreachable. Measured
                    # exactly that, with the leak then live at `200`
                    # (Codex round 33). I had written this off as a
                    # narrow cost; a legal `failed` is not a narrow case.
                    #
                    # Still NOTED. Silently carrying on would leave the
                    # report with no mention that the run ended, which is
                    # the thing an author most needs to see — and the
                    # first version of this `continue` did exactly that,
                    # caught by its own regression test asserting the
                    # premise rather than only the verdict.
                    result.notes.append(
                        f"{short} ended `failed`, which ends that run; the "
                        f"sequence had nothing after it, and the second run "
                        f"of the phase under test is not part of that "
                        f"sequence, so it still ran"
                    )
                    continue
                # That RUN is over; the remaining transitions are not.
                # A gated phase advances when the user approves the
                # original invocation, so the later phase is reachable on
                # a path that never touches the edit — re-anchor on a
                # fresh run, REPLAY the prefix the next transition needs
                # under it, and carry on. Dropping them instead left a
                # run-scoped token pool on the ordinary next-phase path
                # untested and passed, and stopping the replay at one
                # phase left the same pool untested from the third phase
                # on (Codex rounds 10 and 34).
                needed = transitions[position + 1][3]
                moved = await _fresh_anchor(needed)
                if moved is None:
                    if reached_on_fresh is not None and leg_ended_failed:
                        # The replacement run STARTED, its phase under
                        # test succeeded, and a phase of the prefix ended
                        # on a VALID `failed`. That is the agent putting
                        # the rest of the sequence out of its own reach,
                        # which is a shorter plan rather than a charge
                        # against it — the same judgement the walk makes
                        # when the original run ends, applied to the run
                        # built to replace it. `leg_ended_failed` is what
                        # keeps it to that one case: a 500, a timeout, a
                        # second terminal or an unusable output all mean
                        # the chassis could not consume a producible
                        # invocation, and they fall through to the
                        # failure below.
                        #
                        # The WALK's remainder only — `walk_len`, not
                        # `len(transitions)`, or a run-scoped transition
                        # the battery is about to run would be subtracted
                        # from the verdict as if it had been skipped.
                        unreachable = walk_len - (position + 1)
                        result.notes.append(
                            f"{short} ended `failed`, which ends that run, "
                            f"and the replacement run could not be driven "
                            f"far enough to produce the rest of the "
                            f"sequence — it reached {names[reached_on_fresh]} "
                            f"and then {anchor_why}. The remaining "
                            f"transition(s) were not probed rather than "
                            f"being asked for out of order. Any second run "
                            f"of the phase under test is unaffected and "
                            f"still ran."
                        )
                        walk_abandoned = True
                        continue
                    result.failures.append(
                        f"{short} ended `failed` and the fresh run meant to "
                        f"reach the remaining transition(s) could not be used: "
                        f"{anchor_why}, so the binding check could not "
                        f"conclude for them"
                        if anchor_why is not None else
                        f"{short} ended `failed` and the agent would not "
                        f"start a fresh run to reach the remaining "
                        f"transition(s), so the binding check could not "
                        f"conclude for them"
                    )
                    # ABANDON THE WALK, NOT THE LIST. This used to
                    # `break`, which also skipped the run-scoped second
                    # run of the phase under test — and that transition
                    # starts a run of its own, so an unusable replacement
                    # run cannot make it unproducible. Round 33 fixed
                    # exactly this reasoning at the reachability stop and
                    # left it standing here, where the verdict already
                    # fails and so nothing was being wrongly certified;
                    # what was lost is a probe an author could have acted
                    # on. Nothing is subtracted: the check did not
                    # conclude, `drained` falls short of the plan, and
                    # the verdict says so.
                    walk_abandoned = True
                    continue
                fresh_ref, fresh_token, anchor_run, carried = moved
                # NO `cross_use` CALL HERE ANY MORE, and its absence is
                # the point. The replacement run is several invocations
                # now, not one, and cross-using only the last of them
                # would leave every replayed phase in `issued` unprobed —
                # the round-12 defect (an anchor appended without being
                # probed) multiplied by the length of the prefix. Every
                # leg is cross-used by `_leg`, under a noun carrying both
                # ordinals: which replacement run, and which phase of it.
                # One copy of the rule, at the only place that knows an
                # invocation has just started.
                replayed = ", ".join(
                    names[step] for step in range(index + 1, needed + 1))
                result.notes.append(
                    f"{short} ended `failed`, so the rest of the sequence "
                    f"was probed against a fresh run whose {phase} "
                    f"succeeded — the path a user takes by approving "
                    f"rather than editing"
                    + (f", with {replayed} replayed under it to reach them"
                       if replayed else "")
                )

        # The check has now concluded — every producible transition was
        # either reached or recorded UNPROVEN — so the open marker comes
        # off. Any path that returns before here keeps it, which is the
        # point of it (§12 168(b)).
        result.failures.remove(BINDING_UNFINISHED)
        # 401 is the contract's literal code, so demanding exactly 401 —
        # not 403, not "any 4xx" — is the contract rather than strictness.
        unbound = {case: code for case, code in bindings.items() if code != 401}
        for case, code in sorted(unbound.items()):
            result.failures.append(
                f"{case} answered {code}, not 401" if isinstance(code, int)
                else f"{case} never answered at all: {code}"
            )
        # EVERY producible transition must have been reached and drained.
        # A gated phase has two, and passing on one of them would certify
        # exactly the request path the other exists to cover.
        result.checks["token_binding"] = (
            "pass" if drained == len(transitions) - unreachable and not unbound
            else "fail"
        )

        # ------------------------------------------------------------------
        # traceparent_optional — the header the contract says may be absent
        # ------------------------------------------------------------------
        #
        # `Run_Contract_v1.md:409-411` states it as contract, not as
        # courtesy: "ignore both and you still conform — ABSENCE OF EITHER
        # HEADER MUST NEVER BE AN ERROR, and a v1 agent written before S4
        # keeps working unchanged." The battery sent `traceparent` and
        # `tracestate` on every request it ever made
        # (`headers` above, `trace_context` for every later run), so the
        # one obligation an agent is most likely to have got wrong — a
        # framework that parses the header and raises on `None` — was the
        # one the battery could not see.
        #
        # A WHOLE INVOCATION, not a bare POST. An agent can accept the
        # POST and die later, when its exporter asks for the parent it
        # assumed was there, so the probe is held to the same three
        # obligations as the invocation under test: it answers, it
        # streams, it reaches `completed`. Anything less would certify
        # the first request and none of the work.
        #
        # THERE IS NO LEGITIMATE SKIP. Every agent must tolerate absence,
        # so unlike `spans` and `mcp` this check has no caller-declared
        # expectation: it passes or it fails.
        headerless_token = secrets.token_urlsafe(32)
        # `_ANY_TRACE`, not a trace id: with no `traceparent` sent there
        # is nothing for this invocation's spans to be a child OF, and an
        # agent that starts its own trace here is behaving correctly.
        minted_tokens[headerless_token] = _ANY_TRACE
        headerless_run = "battery-run-headerless"
        headerless_body = {
            **body,
            "run": {**body["run"], "id": headerless_run,
                    "case_id": headerless_run, "rerun": False},
            "prior_output": None,
            "user_edits": None,
        }
        # AUTHORIZATION AND NOTHING ELSE. Built fresh rather than by
        # subtracting from `headers`, so a header added to `headers`
        # later cannot quietly reappear here and make this probe test the
        # opposite of what it is named for.
        headerless_headers = {"Authorization": f"Bearer {headerless_token}"}
        headerless_budget = _InvocationBudget(headerless_body["deadline_seconds"])
        headerless_why: str | None = None
        if not await health_probe(headerless_budget):
            headerless_why = ("it was not healthy when the headerless "
                              "invocation would have started")
        else:
            try:
                async with headerless_budget.segment():
                    without = await client.post(
                        f"{base}/v1/runs", json=headerless_body,
                        headers=headerless_headers)
            except TimeoutError:
                without = None
                headerless_why = _outran(headerless_body["deadline_seconds"],
                                         "answering a POST /v1/runs sent with "
                                         "no traceparent")
            except httpx.HTTPError as exc:
                without = None
                headerless_why = (f"the POST sent with no traceparent failed "
                                  f"at the transport: {exc!r}")
            if without is not None:
                if without.status_code not in (200, 201):
                    # THE SENTENCE NAMES THE CAUSE, because this is the
                    # failure the check exists for and a bare status code
                    # would send the reader to the wrong place.
                    headerless_why = (
                        f"it answered {without.status_code} to a POST /v1/runs "
                        f"carrying no traceparent and no tracestate; the "
                        f"contract says absence of either header must never "
                        f"be an error"
                        + (f": {without.text[:200]}" if without.text else ""))
                else:
                    payload_h, not_object_h = _object_body(without)
                    ref_h = (payload_h.get("invocation_id")
                             or payload_h.get("run_id")
                             if payload_h is not None else None)
                    if not_object_h is not None:
                        headerless_why = (
                            f"its headerless invocation answered "
                            f"{without.status_code} with {not_object_h}")
                    elif not isinstance(ref_h, str) or not ref_h:
                        headerless_why = (
                            "its headerless invocation named no invocation_id")
                    else:
                        code_h, raw_h = await _consume_events(
                            client,
                            f"{base}/v1/runs/{quote(ref_h, safe='')}/events",
                            headerless_headers, headerless_budget)
                        if code_h != 200:
                            headerless_why = (
                                f"the events stream of its headerless "
                                f"invocation answered {code_h}"
                                if code_h else
                                f"the events stream of its headerless "
                                f"invocation could not be read: {raw_h}")
                        else:
                            events_h = _parse_sse(raw_h)
                            name_h, _, unconsumable_h, nonconformant_h = (
                                _terminal_of(events_h))
                            if unconsumable_h is not None:
                                headerless_why = (f"its headerless invocation "
                                                  f"{unconsumable_h}")
                            elif nonconformant_h is not None:
                                headerless_why = (f"its headerless invocation "
                                                  f"{nonconformant_h}")
                            elif name_h != "completed":
                                headerless_why = (
                                    f"its headerless invocation ended "
                                    f"`{name_h}`, not `completed`")
        if headerless_why is None:
            result.checks["traceparent_optional"] = "pass"
        else:
            result.checks["traceparent_optional"] = "fail"
            result.failures.append(
                f"an invocation sent with NO traceparent did not conform: "
                f"{headerless_why}")

    # 2. progress
    # THE SAME RULE ON THE FIRST INVOCATION'S STREAM — but REPORTED ONCE.
    # A malformed frame is a run the chassis would have ended
    # `ContainerAgentError` while parsing, so grading a progress
    # vocabulary out of it would be grading a stream production never got
    # through. `_terminal_of` already scans EVERY event of this stream and
    # its failure is appended by the `completed` check above, so appending
    # here too said the same thing twice about one frame — measured as
    # three failures for one bad `progress` line. A correct verdict
    # rendered unusable is still a defect (§12 173(g)). This marks the
    # check unpassable and leaves the explaining to the one place that
    # already does it.
    malformed_frames = [d for _, d in result.events if _malformed(d) is not None]
    progress = [d for e, d in result.events
                if e == "progress" and _malformed(d) is None]
    if malformed_frames:
        result.checks["progress"] = "fail"
    elif not progress:
        result.failures.append("no progress event was streamed")
        result.checks["progress"] = "fail"
    else:
        problems = []
        for event in progress:
            status = str(event.get("status", ""))
            if status not in WIRE_STATUSES:
                problems.append(f"progress status {status!r} is outside the contract vocabulary")
            step = str(event.get("step_id") or "")
            if not step:
                problems.append("a progress event has no step_id")
            elif pii_service.check_identifier(step, path="progress.step_id") is not None:
                problems.append("a progress step_id is flagged by the chassis walk (it would be dropped)")
        result.failures.extend(sorted(set(problems)))
        result.checks["progress"] = "pass" if not problems else "fail"

    # 1. output
    if result.checks.get("completed") == "pass":
        if not isinstance(result.output, dict):
            result.failures.append("completed.output (or the output endpoint) is not a JSON object")
            result.checks["output"] = "fail"
        else:
            # THE SAME WALKER as every other invocation's output: this
            # check used to be the only one that ran, which is how a
            # later invocation's `pii_in_output` went unseen.
            why = first_output_why
            if why is None:
                result.checks["output"] = "pass"
            else:
                result.failures.append(f"the invocation under test: {why}")
                result.checks["output"] = "fail"

    # 4. spans
    if relay is None:
        result.checks["spans"] = "skip"
        if expect_spans:
            result.failures.append("spans expected but no relay recorder was given")
    else:
        deadline = time.monotonic() + settle_seconds
        # HOW THIS GOT HERE, because two earlier rules are gone and a
        # comment that still described them would be a false claim about
        # the code under it. Waiting for ANY span let the probe's own
        # prompt export end the window early (round 6); waiting for the
        # FIRST INVOCATION's alone let a binding invocation's batch —
        # which may legally straddle `completed` — arrive after the
        # verdict, so a wrong-trace span from it was missed (round 7).
        # Measured then: told to wait 20s it returned in 5.68s and
        # reported "the container exported no spans" about an agent whose
        # span arrived at 12s. A probe that mutates shared state is part
        # of every check that reads it (§12 171(e)) — and this loop reads
        # `relay.spans`.
        #
        # The rule NOW is the window measured from the LATEST ARRIVAL,
        # not from the start, so every batch restarts it. "Every minted
        # token has produced a span" was satisfied by the first CORRECT
        # batch, so an invocation that exported a good batch promptly and
        # a wrong-trace one later escaped with most of the window
        # unspent. Measured: told to settle for 8s it returned in 5.39s
        # and reported `spans: pass` about an agent whose wrong-trace
        # span arrived at 7.2s — inside the window it had asked for
        # (Codex round 8). The contract allows precisely that flush: the
        # relay "still honours it for a short grace (30 s) so a flush
        # that straddles your `completed` still lands".
        #
        # This is strictly stronger than the predicate it replaces rather
        # than a different trade: `last_arrival` starts at the settle's
        # own start, so the window is never shorter than the old full one
        # and is longer whenever anything arrives. The token-by-token
        # condition is gone because this subsumes it — a token that never
        # exports still costs a full window, which is what the window is
        # for.
        cap = deadline + settle_seconds * (_SETTLE_CAP - 1)
        capped = False
        seen = -1
        last_arrival = time.monotonic()
        while True:
            observed = len(relay.spans) + len(relay.log_records)
            if observed != seen:
                seen, last_arrival = observed, time.monotonic()
            now = time.monotonic()
            if now - last_arrival >= settle_seconds:
                break
            if now >= cap:
                capped = True
                break
            await asyncio.sleep(0.05)
        # SNAPSHOT AT THE VERIFIED QUIET POINT, with nothing unobserved
        # after it. There used to be an unconditional 0.2s sleep here:
        # anything arriving in it was snapshotted WITHOUT the window this
        # loop exists to measure, so a batch landing in that gap could be
        # followed — after the snapshot but well inside `settle_seconds`
        # of it — by a wrong-trace batch nobody ever looked for, and
        # `spans` passed on a quiet window that was never observed after
        # the newest arrival (Codex round 24). The loop already requires
        # `settle_seconds` with no change in count, so a batch still
        # being written moves the count and restarts it; the extra sleep
        # bought nothing the window did not already cover.
        result.spans = relay.spans
        result.log_records = relay.log_records
        # The EVIDENCE is the first invocation's telemetry, under the
        # token this battery minted for it. The binding probe's second
        # invocation shares the trace context and is on `minted_tokens`
        # so its spans are not called foreign — but counting them as
        # evidence let an agent that exports nothing for an ordinary
        # invocation and something for a rerun collect `spans: pass`.
        # Measured against exactly such an agent: one span recorded,
        # `rerun-only-span`, under the SECOND token, and the check passed
        # while the invocation under test had exported nothing at all
        # (Codex round 5 — a permissive hole opened by round 1's own fix).
        # An OPEN check is a FAILING check (§12 168(b)) — the rule this
        # battery already applies to `token_binding`, and which I broke in
        # the cap I added one round earlier. A note left `passed` true, so
        # an exporter emitting correct spans through the cap and a
        # wrong-trace batch immediately afterwards was certified by a
        # battery that had already written "taken over a moving target"
        # into its own report. Measured: 77 spans, `spans: pass`,
        # `passed: True`, and the wrong-trace span never recorded at all
        # (Codex round 9).
        if capped:
            result.failures.append(
                f"telemetry was still arriving when the settle window hit "
                f"its {_SETTLE_CAP}x cap ({_SETTLE_CAP * settle_seconds:.1f}s), "
                f"so the spans check never concluded: raise settle_seconds, or "
                f"find out why this agent never stops exporting"
            )

        own = [s for s in result.spans if s["token"] == token]
        own_records = [r for r in result.log_records if r["token"] == token]
        # INTEGRITY is checked whether or not there is evidence. When the
        # first invocation exports nothing and the binding invocation
        # exports on a trace of its own, `own` is empty — and skipping
        # straight to `skip` returned PASS over telemetry the production
        # relay refuses as `trace_mismatch` (Codex round 7). Evidence and
        # integrity are separate questions and are now asked separately.
        ours = [s for s in result.spans if s["token"] in minted_tokens]
        our_records = [r for r in result.log_records if r["token"] in minted_tokens]
        # `_ANY_TRACE` excuses ONE invocation and only from THIS
        # comparison: the headerless probe below, whose spans are
        # correct under whatever trace the agent chose. It is still
        # `ours`, so a span arriving under its token is not foreign, and
        # every other token is still held to the id it was sent.
        wrong = [s for s in ours
                 if minted_tokens[s["token"]] not in (s["trace_id"], _ANY_TRACE)]
        foreign = [s for s in result.spans if s["token"] not in minted_tokens]
        # ...and RECORDS under a token this battery never minted. The
        # relay authenticates logs exactly as it authenticates spans, so
        # a record smuggled under an invented bearer is one it refuses —
        # and it was slipping through because only spans were counted
        # (Codex round 10).
        foreign_records = [
            r for r in result.log_records if r["token"] not in minted_tokens
        ]
        wrong_records = [
            r for r in our_records
            if minted_tokens[r["token"]] not in (r["trace_id"], _ANY_TRACE)
        ]
        if wrong or foreign or wrong_records or foreign_records:
            result.failures.append(
                f"{len(wrong)} span(s) and {len(wrong_records)} record(s) carry another trace id, "
                f"{len(foreign)} span(s) and {len(foreign_records)} record(s) arrived under another token"
            )
            result.checks["spans"] = "fail"
        elif not own:
            result.checks["spans"] = "skip" if not expect_spans else "fail"
            if expect_spans:
                result.failures.append("the container exported no spans to the relay recorder")
        else:
            # EVIDENCE that the invocation under test was instrumented.
            # Integrity was already settled above, over everything this
            # battery minted.
            result.checks["spans"] = "pass"
        # ...and none of those verdicts was reached over a finished
        # window if the cap cut it short. `fail` is the more specific
        # word and keeps it; `pass` and `skip` must not stand.
        if capped and result.checks["spans"] != "fail":
            result.checks["spans"] = "incomplete"

    # 5. mcp — what the agent asked the chassis for, and how it asked
    #
    # SKIP IS A REAL ANSWER HERE, and the contract says so: "Agents that
    # need no platform services may ignore the key entirely"
    # (`Run_Contract_v1.md:132`). So this check cannot demand a call — it
    # reports what arrived, and the CALLER says whether silence is
    # acceptable, exactly as `expect_spans` does for the relay. A matrix
    # entry that declares `expect_mcp` and goes quiet fails; one that
    # declares nothing and goes quiet skips, and skip is never rendered
    # as pass.
    if capabilities is None:
        # `incomplete`, not `skip`, when the caller asked for this check
        # and nothing was serving: `skip` says "there was nothing to
        # look at", and that is false here — the caller said there would
        # be and the battery could not look. The word is the one the
        # span check already uses for a verdict it could not reach over
        # a finished window, and it is deliberately NOT `fail`, which
        # would accuse the agent of the caller's own misconfiguration.
        result.checks["mcp"] = "incomplete" if expect_mcp else "skip"
        if expect_mcp:
            result.failures.append(
                "MCP calls expected but no capability recorder was given")
    else:
        calls = capabilities.calls
        result.mcp_calls = calls
        # A call refused for its bearer is the finding, not an absence:
        # the endpoint answered `-32001` and recorded WHY, so the battery
        # can name it rather than report a quiet "no calls".
        bad = [c for c in calls if c["why"] is not None]
        if bad:
            result.checks["mcp"] = "fail"
            result.failures.append(
                f"{len(bad)} of {len(calls)} MCP request(s) were not "
                f"conformant: "
                + "; ".join(
                    f"{c['method']}"
                    + (f" {c['tool']}" if c["tool"] else "")
                    + f" — {c['why']}"
                    for c in bad[:5]
                )
                + ("" if len(bad) <= 5 else f" (+{len(bad) - 5} more)")
            )
        elif not calls:
            result.checks["mcp"] = "skip" if not expect_mcp else "fail"
            if expect_mcp:
                result.failures.append(
                    "the container made no MCP request to the run.mcp.url it "
                    "was advertised")
        else:
            result.checks["mcp"] = "pass"
        # WHAT WAS EXAMINED, beside the verdict. "No findings" and
        # "nothing was looked at" are the same sentence otherwise, and
        # this endpoint is the only witness to either.
        tools = sorted({c["tool"] for c in calls if c["tool"]})
        result.notes.append(
            f"mcp: {len(calls)} request(s) to run.mcp.url"
            + (f", tools: {', '.join(tools)}" if tools else ", no tools/call")
            # THE URL, when nothing came. Silence has two causes an
            # author cannot tell apart from a verdict: an agent that
            # never asks, and an agent that asked an address it could
            # not reach. Naming what was advertised turns the second
            # into something a reader can check — `--mcp-advertise-host`
            # is how the agent reaches this process, and the default is
            # right only when the two share a network namespace.
            + (f" (advertised {capabilities.url}; if your agent calls a "
               f"capability and this says 0, check --mcp-advertise-host)"
               if not calls else "")
        )
        for refusal in capabilities.refusals:
            result.notes.append(
                f"mcp: this battery could not answer {refusal} — the agent's "
                f"failure, if it failed, is the host's and not its own")
    return result


def _load_scenario(agent_dir: Path, manifest: AgentManifest, name: str | None) -> tuple[str, dict]:
    scenarios = sorted((agent_dir / manifest.scenarios).glob("*.json"))
    if name:
        scenarios = [p for p in scenarios if p.stem == name]
    if not scenarios:
        return "inline", {}
    data = json.loads(scenarios[0].read_text())
    return data.get("name", scenarios[0].stem), data.get("user_inputs", {})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the container battery against a Run Contract v1 URL.")
    parser.add_argument("--url", required=True, help="the container's base URL")
    parser.add_argument("--agent-dir", required=True, help="the agent directory (agent.yaml, scenarios/)")
    parser.add_argument("--scenario", help="a scenario file stem under scenarios/ (default: the first)")
    parser.add_argument("--phase", help="the phase to invoke (default: the manifest's first)")
    parser.add_argument("--relay-port", type=int, default=0, help="serve the relay recorder here (0 = off)")
    parser.add_argument("--expect-spans", action="store_true", help="fail unless spans arrive with the trace id")
    parser.add_argument("--settle-seconds", type=float, default=3.0)
    # ON BY DEFAULT, because `run.mcp.url` is part of the body the
    # chassis sends and a battery that omits it drives something
    # production never produces. `--no-mcp` exists for the one case that
    # is not a configuration error: a driver whose agent genuinely cannot
    # reach back, where an advertised URL that answers nothing would be
    # worse than the absence the contract defines.
    parser.add_argument(
        "--mcp-port", type=int, default=0,
        help="serve the capability recorder (run.mcp.url) here "
             "(0 = an ephemeral port; --no-mcp turns it off)")
    parser.add_argument(
        "--mcp-advertise-host", default="127.0.0.1",
        help="the host the AGENT uses to reach this battery — "
             "host.docker.internal for a container, 127.0.0.1 in-process "
             "(default: 127.0.0.1)")
    parser.add_argument(
        "--no-mcp", action="store_true",
        help="do not advertise run.mcp.url at all")
    parser.add_argument(
        "--expect-mcp", action="store_true",
        help="fail unless the agent calls the advertised run.mcp.url")
    # This battery's LIBRERUN_MAX_PHASE_SECONDS: it bounds how long the
    # client waits AND clamps the `deadline_seconds` it advertises, so the
    # two can never disagree. `Container_Agents.md` tells authors to raise
    # it and, until now, argparse rejected the flag it named (Codex round
    # 12) — a documented instruction that exits with an error.
    parser.add_argument(
        "--timeout", type=float, default=600.0,
        help="this battery's LIBRERUN_MAX_PHASE_SECONDS: the ceiling on "
             "the deadline_seconds advertised to each phase, and the "
             "wall-clock budget the battery then waits for that phase to "
             "finish in (default: 600)",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON too")
    args = parser.parse_args(argv)

    agent_dir = Path(args.agent_dir)
    manifest = load_manifest(agent_dir)
    scenario_name, scenario = _load_scenario(agent_dir, manifest, args.scenario)
    relay = RelayRecorder(args.relay_port) if args.relay_port else None
    capabilities = None if args.no_mcp else CapabilityRecorder(
        args.mcp_port, advertise_host=args.mcp_advertise_host)
    try:
        result = asyncio.run(
            run_contract_battery(
                args.url,
                manifest=manifest,
                scenario=scenario,
                scenario_name=scenario_name,
                phase=args.phase,
                relay=relay,
                expect_spans=args.expect_spans,
                capabilities=capabilities,
                expect_mcp=args.expect_mcp,
                settle_seconds=args.settle_seconds,
                timeout=args.timeout,
            )
        )
    finally:
        if relay is not None:
            relay.stop()
        if capabilities is not None:
            capabilities.stop()
    print(result.summary())
    if args.json:
        print(json.dumps({
            "passed": result.passed, "checks": result.checks, "failures": result.failures,
            "trace_id": result.trace_id, "spans": len(result.spans), "log_records": len(result.log_records),
            "events": [e for e, _ in result.events],
            # The REQUESTS, not just their number: a reader deciding
            # whether a `skip` was honest needs to see that nothing came
            # rather than take the count on trust. The bearer is a
            # credential, so it is reported as whether it was one the
            # battery minted, never as itself.
            "mcp": [{"method": c["method"], "tool": c["tool"], "why": c["why"]}
                    for c in result.mcp_calls],
            "notes": result.notes,
        }, indent=2))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
