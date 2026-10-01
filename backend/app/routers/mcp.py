"""Run-scoped MCP server (blueprint B13, L11(c)).

Chassis capabilities exposed to CONTAINER agents — and any framework's
native MCP client — as MCP tools over streamable HTTP: a stateless
JSON-RPC 2.0 endpoint at ``POST /api/v1/mcp``. Four capabilities are
tool-shaped and served here: ``kb`` (kb_search), ``run_store``
(run_store_get / run_store_set), ``audit`` (audit_log) and, since
blueprint S4, ``pii`` (redact — the intake pipeline on demand).
``progress`` stays on the Run Contract's SSE stream (agent→platform
events are not tool-shaped) and ``llm`` is in-process-only. Beside them,
``config_get`` serves the run's own configuration — its steps and its
settings in the run's tenant — under no grant at all (K5a), and
``secret_get`` one of the run's declared tool secrets on the same terms
(K8a, D20): this tenant's value from the store's rows alone, never the
backend's environment (K8-12), answering ``-32005 secret_not_declared`` or
``-32006 secret_not_set``. It is the one tool whose result is a secret,
and the one exception L31 makes: that value, to the declaring run.

A tool call is untrusted agent input: every tool refuses arguments it
does not declare (``-32602``), and what a call persists is walked at the
boundary exactly as an in-process call is — a flagged key, name or
number answers ``-32003`` with the reason (``pii_in_audit``,
``pii_in_store``), the argument and the path, never the value.

Scoping: the caller authenticates with the SAME per-run bearer token the
chassis minted for its Run Contract invocation (``app/agents/container``
registers ``run_token:{token}`` in Redis with the run's id, tenant,
and manifest grants; revoked when the phase ends). Every tool call is
therefore scoped to one live run and gated by that agent's manifest
``capabilities:`` list — the grant is enforced at the same place as for
in-process agents, the façade.

Deliberately minimal: no sessions, no SSE stream, no resources/prompts —
``initialize``, ``tools/list``, and ``tools/call`` are the whole surface
an MCP tools client needs, and each POST gets one JSON response (legal
per the streamable-HTTP transport).
"""
from __future__ import annotations

import json

import redis.asyncio as aioredis
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response

from app import capabilities as capabilities_mod
from app import version as platform_version
from app.capabilities import CapabilityNotGranted, SecretNotDeclared, SecretNotSet
from app.config import settings
from app.services import run_boundary
from app.services import run_token as _run_token
from app.services.pii_service import PiiDetectorUnavailable, PiiRefused

import structlog

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["mcp"])

PROTOCOL_VERSION = "2025-03-26"

_TOOLS = [
    {
        "name": "kb_search",
        "description": "Search the tenant's internal knowledge base. "
        "Returns a JSON list of {title, url, snippet, relevance_score}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 20,
                },
                "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["queries"],
        },
    },
    {
        "name": "run_store_get",
        "description": "Read one key from this run's scoped key-value store.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "run_store_set",
        "description": "Write one key in this run's scoped key-value store. "
        "Values may be any JSON value.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "value": {}},
            "required": ["key", "value"],
        },
    },
    {
        "name": "audit_log",
        "description": "Write a tenant-scoped audit row for this run, "
        "attributed to the run's owner by the chassis.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action_type": {"type": "string", "maxLength": 30},
                "detail": {"type": "object"},
            },
            "required": ["action_type", "detail"],
        },
    },
    {
        "name": "config_get",
        "description": "This run's effective configuration, live: the "
        "agent's declared LLM steps with this tenant's admin overrides "
        "applied, and its declared settings with this tenant's values. "
        "Needs no grant. Returns {steps: [{step_id, label, provider, "
        "model, temperature, max_tokens, timeout_seconds, overridden}], "
        "settings: [{key, value}]}.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "secret_get",
        "description": "One of this agent's declared tool secrets (its "
        "manifest's secrets[]), this tenant's value: the tenant's own, "
        "else every tenant's default. Rows only — the chassis never "
        "reads its own environment for a container. Needs no grant. "
        "Returns {value}; -32005 secret_not_declared for a name the "
        "manifest does not declare, -32006 secret_not_set for a declared "
        "name with no value.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "maxLength": 64}},
            "required": ["name"],
        },
    },
    {
        "name": "redact",
        "description": "Redact PII from text with the chassis intake "
        "pipeline — the same recognizers and placeholders intake applies. "
        "Returns {text}.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "maxLength": 100000}},
            "required": ["text"],
        },
    },
]

# The arguments each tool declares; anything else is refused (-32602), so
# what an agent can write into a row is walked arguments or nothing.
_TOOL_ARGUMENTS = {
    "kb_search": frozenset({"queries", "top_k"}),
    "run_store_get": frozenset({"key"}),
    "run_store_set": frozenset({"key", "value"}),
    "audit_log": frozenset({"action_type", "detail"}),
    "redact": frozenset({"text"}),
    "config_get": frozenset(),
    "secret_get": frozenset({"name"}),
}

# Pre-S1 tool names, accepted on ``tools/call`` for one release (blueprint
# S1, L18) and removed at v1.1. ``tools/list`` advertises only the current
# names.
_TOOL_ALIASES = {
    "case_store_get": "run_store_get",
    "case_store_set": "run_store_set",
}

# Tool → the capability grant it requires, or None for none.
_TOOL_GRANTS = {
    "kb_search": "kb",
    "run_store_get": "run_store",
    "run_store_set": "run_store",
    "audit_log": "audit",
    "redact": "pii",
    # The run's own configuration — its steps and, since K5a, its settings
    # — is the agent's data, not a platform capability: an agent that
    # declares settings and grants nothing reads them all the same (K5-04).
    "config_get": None,
    # The run's own tool secrets (K8a): its data, like its settings.
    "secret_get": None,
}


def tool_granted(name: str, granted) -> bool:
    """Whether a run whose manifest grants ``granted`` is offered ``name``.

    The one predicate: ``tools/list`` here and the container battery's
    recorder (``adapter_kit/run_contract.py``) both ask it, so the two
    sides agree by construction rather than by two copies of a rule.
    """
    grant = _TOOL_GRANTS[name]
    return grant is None or grant in granted


async def _resolve_run_token(authorization: str | None) -> dict | None:
    """The run bound to the caller's bearer, or None."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None
    key = f"run_token:{token}"
    async with aioredis.from_url(
        settings.REDIS_URL.get_secret_value(), decode_responses=True
    ) as redis:
        # The TTL is the invocation's ABSOLUTE deadline, already: the
        # runner minted this key with ``deadline_seconds + GRACE``, so
        # what Redis has left minus the grace is what the invocation has
        # left. Reading it here is what makes the budget survive the trip
        # through a separate HTTP task (Codex P1) — see the façade below.
        #
        # One round trip, because the record and its expiry are one fact
        # about one key. Two awaits let the key expire in between, and
        # the record then came back with no budget attached at all —
        # which downstream reads as "no deadline" rather than "expired"
        # (Codex P1, the same defect the gateway carried).
        pipe = redis.pipeline(transaction=True)
        pipe.get(key)
        pipe.ttl(key)
        raw, ttl = await pipe.execute()
    if raw is None:
        return None
    if not isinstance(ttl, int) or ttl < 0:
        # ``-2`` is a key that is gone, ``-1`` a key with no expiry.
        # Every mint sets one, so neither is a live token, and a tool
        # call on a token whose key has vanished is a call by an expired
        # credential.
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    # An ``ended`` record (the invocation completed or failed; the OTLP
    # relay alone honours it for its grace) authorizes no tool call.
    if isinstance(record, dict) and record.get("state", "active") != "active":
        return None
    if isinstance(record, dict):
        # The bearer itself, under a key the record's own writer never
        # uses, so a capability that reaches the gateway can present the
        # caller's own token rather than a second credential.
        record["_token"] = token
        record["_seconds_left"] = max(0.0, float(ttl) - _run_token.GRACE_SECONDS)
    return record


def _rpc_result(id_, result) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})


def _rpc_error(id_, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _tool_text(payload) -> dict:
    """MCP tools/call result: one text content block carrying JSON."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


class ToolArgumentError(ValueError):
    """Caller-supplied arguments don't match the tool's contract."""


# Server-side bounds. The advertised ``inputSchema`` is advisory to the
# client; anything an agent can send has to be checked here too.
_MAX_QUERIES = 20
_MAX_QUERY_CHARS = 1000
_MAX_KEY_CHARS = 200
# Matches the activity_audit_log.action_type storage column.
_MAX_ACTION_TYPE_CHARS = 30
_MAX_REDACT_CHARS = 100_000
# A tool secret's name, as the manifest bounds it.
_MAX_SECRET_NAME_CHARS = 64


def _require_str(args: dict, key: str, *, max_chars: int) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ToolArgumentError(f"{key!r} must be a non-empty string")
    if len(value) > max_chars:
        raise ToolArgumentError(f"{key!r} exceeds {max_chars} characters")
    return value


async def _run_secret_values(run: dict, tenant_id) -> list[str]:
    """The run's tool secrets from the rows alone — what a container can
    have been handed — for the scrub around the writes it makes here."""
    from app.agents.registry import get_manifest
    from app.services import tool_secrets_service as tool_secrets

    manifest = get_manifest(run.get("agent_id") or "")
    if manifest is None or not manifest.secrets:
        return []
    return await tool_secrets.scrub_values(
        tenant_id, manifest.id, manifest.secrets, env_fallback=False
    )


async def _call_tool(name: str, args: dict, run: dict):
    """Dispatch one tool call through the same façade in-process agents
    use — grants enforced identically. Arguments are validated here
    because a tool call is untrusted agent input."""
    from uuid import UUID

    undeclared = sorted(set(args) - _TOOL_ARGUMENTS[name])
    if undeclared:
        raise ToolArgumentError(
            f"{name!r} does not declare argument(s) {undeclared}"
        )
    # A token minted just before the S1 deploy carries the old key for the
    # rest of its TTL; read either for one release.
    run_id = UUID(run.get("run_id") or run["case_id"])
    caps = capabilities_mod.for_run(
        run_id=run_id,
        tenant_id=UUID(run["tenant_id"]),
        # The run's agent, which the runner writes into every token it
        # mints: whose configuration ``config_get`` reads (K5a).
        agent_id=run["agent_id"],
        grants=run.get("grants", []),
        # The run's owner, issued into the token record by the runner:
        # the attribution the audit tool stamps, never an argument.
        user_id=UUID(run["user_id"]) if run.get("user_id") else None,
        # The caller's own token: a capability that reaches a model
        # (``kb_search``'s query embedding) presents it, so the spend
        # lands on this run like an in-process agent's would.
        run_token=run.get("_token"),
        # What the invocation has LEFT, not what it started with.
        #
        # This façade is built per TOOL CALL, and its budget clock starts
        # at construction — so handing it the token's original
        # ``deadline_seconds`` gave a ``kb_search`` arriving in the last
        # second of a phase the whole phase over again. That is not
        # merely a loose ceiling: the MCP handler is a separate HTTP
        # task, so cancelling the runner at the real deadline does not
        # cancel an embedding already admitted here, and it would go on
        # spending after the invocation ended (Codex P1).
        #
        # I had seen this shape while writing the original fix and
        # reasoned past it — "still bounded by the phase deadline" —
        # which was true of the in-process path and false of this one.
        deadline_seconds=run.get("_seconds_left"),
    )
    if name == "config_get":
        # Read at call time, never cached: an admin editing a model or a
        # setting in the UI must be visible to the next call, which is the
        # whole point of configuration being data (L25, L32). This is what
        # populates ``ctx.config`` in the SDK — the manifest alone cannot,
        # because it holds defaults, not this tenant's values. Through the
        # façade's ``config`` member, so a container reads exactly what an
        # in-process agent's ``caps.config`` reads, in the run's tenant.
        steps = await caps.config.steps()
        settings = await caps.config.settings()
        # Through the SAME envelope as every other tool. Returned raw,
        # the payload never reached the SDK: its client reads
        # ``result.content`` and substituted ``{}``, so a container
        # asking ``ctx.config.steps()`` was told there were none — and
        # told it silently, since an empty list is a legal answer
        # (Codex P2). One tool answering in a different shape is a shape
        # nobody notices until something reads it.
        return _tool_text(
            {
                "steps": steps,
                "settings": [
                    {"key": key, "value": value} for key, value in settings.items()
                ],
            }
        )
    if name == "kb_search":
        queries = args.get("queries")
        if not isinstance(queries, list) or not queries:
            raise ToolArgumentError("'queries' must be a non-empty array of strings")
        if len(queries) > _MAX_QUERIES:
            raise ToolArgumentError(f"'queries' exceeds {_MAX_QUERIES} entries")
        if not all(isinstance(q, str) and q for q in queries):
            raise ToolArgumentError("'queries' entries must be non-empty strings")
        raw_top_k = args.get("top_k", 10)
        if not isinstance(raw_top_k, int) or isinstance(raw_top_k, bool):
            raise ToolArgumentError("'top_k' must be an integer")
        top_k = max(1, min(50, raw_top_k))
        results = await caps.kb.search(
            [q[:_MAX_QUERY_CHARS] for q in queries], top_k=top_k
        )
        return _tool_text({"results": results})
    if name == "run_store_get":
        key = _require_str(args, "key", max_chars=_MAX_KEY_CHARS)
        value = await caps.run_store.get(key)
        return _tool_text({"key": key, "value": value})
    if name == "run_store_set":
        key = _require_str(args, "key", max_chars=_MAX_KEY_CHARS)
        if "value" not in args:
            raise ToolArgumentError("'value' is required")
        # The run's tool secrets are scrubbed from what it stores (K8a):
        # what the rows hold now, and every value the run was delivered
        # in this process before an admin replaced it (Codex on #173).
        with run_boundary.scrubbing(
            await _run_secret_values(run, caps.tenant_id), run_id=run_id
        ):
            await caps.run_store.set(key, args["value"])
        return _tool_text({"ok": True, "key": key})
    if name == "audit_log":
        action_type = _require_str(
            args, "action_type", max_chars=_MAX_ACTION_TYPE_CHARS
        )
        detail = args.get("detail")
        if not isinstance(detail, dict):
            raise ToolArgumentError("'detail' must be an object")
        # Report what actually happened: the capability swallows write
        # failures so a run's fate never hinges on an audit row, which
        # would otherwise let this tool answer "ok" for a dropped write.
        # The run's tool secrets are scrubbed from the row (K8a), as in
        # run_store_set.
        with run_boundary.scrubbing(
            await _run_secret_values(run, caps.tenant_id), run_id=run_id
        ):
            persisted = await caps.audit.log(action_type, detail)
        return _tool_text({"ok": bool(persisted)})
    if name == "redact":
        text = _require_str(args, "text", max_chars=_MAX_REDACT_CHARS)
        return _tool_text({"text": await caps.pii.redact(text)})
    if name == "secret_get":
        secret = _require_str(args, "name", max_chars=_MAX_SECRET_NAME_CHARS)
        # This façade was built with no environment fallback: a container
        # is answered from the rows alone (K8-12). The value is the tool's
        # result and nothing else — no log line here names it or the name.
        # It joins the run's set in this process, so the writes this run
        # makes later — here, or in its output — are scrubbed of it even
        # once an admin has replaced the row (Codex on #173).
        with run_boundary.scrubbing((), run_id=run_id):
            value = await caps.secrets.get(secret)
        return _tool_text({"value": value})
    raise KeyError(name)


@router.post("/mcp")
async def mcp_endpoint(
    request: Request, authorization: str | None = Header(default=None)
):
    """The stateless streamable-HTTP MCP endpoint."""
    try:
        message = await request.json()
    except Exception:  # noqa: BLE001
        return _rpc_error(None, -32700, "parse error", status=400)
    if not isinstance(message, dict):
        return _rpc_error(None, -32600, "expected a JSON-RPC message", status=400)

    method = message.get("method")
    msg_id = message.get("id")

    # Notifications (no id) need no body — acknowledge and return.
    if msg_id is None:
        return Response(status_code=202)

    if method == "initialize":
        return _rpc_result(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "librerun", "version": platform_version.__version__},
                "instructions": "Run-scoped LibreRun capabilities. "
                "Authenticate every request with your Run Contract "
                "bearer token.",
            },
        )

    # Everything else is run-scoped: resolve the bearer first.
    run = await _resolve_run_token(authorization)
    if run is None:
        return _rpc_error(
            msg_id, -32001, "unknown or expired run token", status=401
        )

    if method == "tools/list":
        # Tokens minted before S1 may still carry the ``case_store``
        # grant spelling; normalize exactly as the façade does.
        granted = set(capabilities_mod.normalize_grants(run.get("grants", [])))
        tools = [t for t in _TOOLS if tool_granted(t["name"], granted)]
        return _rpc_result(msg_id, {"tools": tools})

    if method == "tools/call":
        # JSON-RPC allows positional params; this server only accepts the
        # object form, and says so rather than crashing on .get().
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _rpc_error(msg_id, -32602, "'params' must be an object")
        name = params.get("name")
        # Pre-S1 tool names keep working for one release (_TOOL_ALIASES).
        name = _TOOL_ALIASES.get(name, name)
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _rpc_error(msg_id, -32602, "'arguments' must be an object")
        if name not in _TOOL_GRANTS:
            return _rpc_error(msg_id, -32602, f"unknown tool {name!r}")
        try:
            result = await _call_tool(name, args, run)
        except CapabilityNotGranted as exc:
            return _rpc_error(msg_id, -32002, str(exc))
        # Before the generic clause below, which catches ``KeyError`` —
        # and both of these are one (K8a).
        except SecretNotDeclared as exc:
            return _rpc_error(msg_id, -32005, str(exc))
        except SecretNotSet as exc:
            return _rpc_error(msg_id, -32006, str(exc))
        except PiiRefused as exc:
            # The reason, the argument and the path — never the value.
            return _rpc_error(msg_id, -32003, str(exc))
        except PiiDetectorUnavailable as exc:
            # Blueprint S4c: the `redact` tool answers with the detector's
            # state, not with text it could not walk — and not with the
            # generic -32603 below, which an agent would read as "the
            # chassis is broken, retry" rather than "redaction is not
            # available, do not proceed". -32004 is what the SDK raises
            # `PiiUnavailable` on.
            logger.warning(
                "mcp_pii_detector_unavailable",
                tool=name,
                state=exc.state,
                error=exc.error,
                run_id=run.get("run_id"),
            )
            return _rpc_error(msg_id, -32004, str(exc))
        except (ToolArgumentError, KeyError, TypeError, ValueError, AttributeError) as exc:
            return _rpc_error(msg_id, -32602, f"bad arguments: {exc}")
        except Exception:  # noqa: BLE001
            # The detail stays server-side: this response crosses into
            # third-party agent code, and driver/exception text leaks
            # infrastructure shape (hosts, ports, credentials in URLs).
            logger.exception("mcp_tool_failed", tool=name, run_id=run.get("run_id"))
            return _rpc_error(msg_id, -32603, "tool failed; see chassis logs")
        logger.info(
            "mcp_tool_called",
            tool=name,
            agent_id=run.get("agent_id"),
            run_id=run.get("run_id"),
        )
        return _rpc_result(msg_id, result)

    return _rpc_error(msg_id, -32601, f"method {method!r} not supported")
