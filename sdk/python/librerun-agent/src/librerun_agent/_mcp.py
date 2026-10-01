"""A minimal MCP client for the chassis's run-scoped tools (stdlib only).

The chassis serves ``initialize`` / ``tools/list`` / ``tools/call`` as a
stateless JSON-RPC 2.0 endpoint at the ``run.mcp.url`` each invocation
receives, authenticated by the same bearer the invocation carries.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any


class CapabilityError(RuntimeError):
    """The chassis answered a JSON-RPC error."""

    def __init__(self, code: int, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class CapabilityUnreachable(CapabilityError):
    """The chassis's MCP endpoint could not be reached at all.

    A transport failure — DNS, a refused connection, a timeout, a reply
    that is not JSON — rather than an answer the chassis gave. It carries
    the JSON-RPC implementation-defined code -32000, which no chassis
    response uses, so an agent can tell "I could not ask" from "I asked
    and was refused".

    THIS EXISTS BECAUSE THE UNTYPED VERSION KILLED A CONFORMANT AGENT.
    `_call_sync` caught only `HTTPError`, so a `URLError` escaped as
    itself through a typed client's own API — and the container-python
    template's documented `except CapabilityError` around
    `ctx.pii.redact` did not catch it. Measured against the template with
    an advertised `run.mcp.url` that answered nothing: the phase ended
    `failed` with `URLError: <urlopen error [Errno 111] Connection
    refused>`, where the template is written to log a warning and carry
    on without the fetched text.
    """


class CapabilityNotGranted(CapabilityError):
    """The manifest does not grant the capability this tool needs (-32002)."""


class PiiRefused(CapabilityError):
    """The chassis refused a write the walk flagged (-32003): a key, a
    name or a number carried PII. The message names the argument and the
    path, never the value."""


class PiiUnavailable(CapabilityError):
    """The chassis's PII detector is not ready (-32004), so it refused
    rather than hand back half-redacted text (blueprint S4c).

    Distinct from :class:`PiiRefused`: nothing was flagged, the walk did
    not happen. An agent that catches this should stop, not retry with
    different text — the message names the detector's state and the
    attach point, never any value.
    """


class SecretNotDeclared(CapabilityError):
    """The manifest declares no tool secret by this name (-32005): it is
    not in ``secrets[]``. A typo or a missing declaration — asking again
    will not help, and the message names the name, never a value."""


class SecretNotSet(CapabilityError):
    """The tool secret is declared, and neither this tenant nor every
    tenant's default has a value (-32006). Read it as "no key": skip what
    needs it, as the demo agent's web search does. A container is answered
    from the platform's rows alone, so its own environment is the fallback
    to reach for here, never the platform's."""


class MCPClient:
    def __init__(self, url: str, token: str, *, traceparent: str | None = None, timeout: float = 30.0):
        self.url = url
        self._token = token
        self._traceparent = traceparent
        self._timeout = timeout
        self._next_id = 0

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        # The handler span's context when tracing is on, else the chassis
        # phase span's: either way the call joins the run's one trace.
        current = _current_traceparent() or self._traceparent
        if current:
            headers["traceparent"] = current
        return headers

    def _call_sync(self, name: str, arguments: dict) -> Any:
        self._next_id += 1
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        ).encode()
        request = urllib.request.Request(self.url, data=body, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read() or b"{}"
        except urllib.error.HTTPError as exc:
            # An ANSWER, with a status: the chassis's refusals arrive this
            # way and carry a JSON-RPC error body.
            try:
                payload = json.loads(exc.read() or b"{}")
            except ValueError:
                raise CapabilityError(exc.code, exc.reason) from exc
        # EVERY OTHER FAILURE IS A TRANSPORT FAILURE, and it leaves
        # through this client's own exception rather than as itself. DNS,
        # a refused connection, a timeout, a reply that is not JSON —
        # none of them is an answer, and an agent told to write
        # `except CapabilityError` cannot be expected to also write
        # `except urllib.error.URLError`. The URL is named because it is
        # the one the agent was handed and carries no credential; the
        # bearer never appears.
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise CapabilityUnreachable(
                -32000,
                f"could not reach the chassis at {self.url}: {reason}",
            ) from exc
        else:
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                raise CapabilityUnreachable(
                    -32000,
                    f"the chassis at {self.url} answered {len(raw)} byte(s) "
                    f"that are not JSON",
                ) from exc
        if not isinstance(payload, dict):
            raise CapabilityUnreachable(
                -32000,
                f"the chassis at {self.url} answered JSON that is not an "
                f"object",
            )
        error = payload.get("error")
        if error:
            code = int(error.get("code", -32603))
            message = str(error.get("message", ""))
            if code == -32002:
                raise CapabilityNotGranted(code, message)
            if code == -32003:
                raise PiiRefused(code, message)
            if code == -32004:
                raise PiiUnavailable(code, message)
            if code == -32005:
                raise SecretNotDeclared(code, message)
            if code == -32006:
                raise SecretNotSet(code, message)
            raise CapabilityError(code, message)
        content = (payload.get("result") or {}).get("content") or []
        text = next((c.get("text") for c in content if c.get("type") == "text"), "{}")
        return json.loads(text)

    async def call(self, name: str, arguments: dict) -> Any:
        return await asyncio.to_thread(self._call_sync, name, arguments)


def _current_traceparent() -> str | None:
    try:
        from opentelemetry import trace  # type: ignore

        sc = trace.get_current_span().get_span_context()
        if sc.is_valid:
            return f"00-{sc.trace_id:032x}-{sc.span_id:016x}-{int(sc.trace_flags):02x}"
    except Exception:  # noqa: BLE001 — tracing is optional
        return None
    return None


def _require(client: MCPClient | None) -> MCPClient:
    if client is None:
        raise CapabilityError(
            -32601,
            "this invocation advertised no run.mcp.url — the chassis serves "
            "capabilities to containers over MCP (docs/authoring/Run_Contract_v1.md)",
        )
    return client


class Capabilities:
    """The typed client for the run-scoped tools, under the manifest's
    grants: ``kb`` → ``kb_search``, ``run_store`` → ``run_store_get`` /
    ``run_store_set``, ``audit`` → ``audit_log``."""

    def __init__(self, client: MCPClient | None):
        self._client = client

    async def kb_search(self, query: str | list[str], top_k: int = 10) -> list[dict]:
        queries = [query] if isinstance(query, str) else list(query)
        result = await _require(self._client).call("kb_search", {"queries": queries, "top_k": top_k})
        return list(result.get("results") or [])

    async def run_store_get(self, key: str) -> Any:
        result = await _require(self._client).call("run_store_get", {"key": key})
        return result.get("value")

    async def run_store_set(self, key: str, value: Any) -> None:
        await _require(self._client).call("run_store_set", {"key": key, "value": value})

    async def audit_log(self, action_type: str, detail: dict) -> bool:
        """A tenant-scoped audit row, attributed to the run's owner by the
        chassis — never by an argument."""
        result = await _require(self._client).call(
            "audit_log", {"action_type": action_type, "detail": detail}
        )
        return bool(result.get("ok"))


class Secrets:
    """The tool secrets the manifest declares (``secrets[]``), this tenant's
    values, over the MCP ``secret_get`` tool, which needs no grant (K8a).

    ``get(name)`` returns the value — the one place a secret is handed back,
    to the run of the agent that declares it (L31). It raises
    :class:`SecretNotDeclared` for a name the manifest does not declare and
    :class:`SecretNotSet` when neither this tenant nor the default has a
    value. Nothing here logs or keeps the value: ask when you need it, and
    never put it in the output, which the chassis persists.
    """

    def __init__(self, client: MCPClient | None):
        self._client = client

    async def get(self, name: str) -> str:
        result = await _require(self._client).call("secret_get", {"name": str(name)})
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(value, str):
            # Never read a malformed answer as an empty key.
            raise CapabilityError(-32603, "the chassis answered secret_get without a value")
        return value


class Pii:
    """The chassis intake PII pipeline on demand, under the ``pii`` grant."""

    def __init__(self, client: MCPClient | None):
        self._client = client

    async def redact(self, text: str) -> str:
        result = await _require(self._client).call("redact", {"text": str(text)})
        return str(result.get("text", ""))


__all__ = [
    "Capabilities",
    "CapabilityError",
    "CapabilityNotGranted",
    "CapabilityUnreachable",
    "MCPClient",
    "Pii",
    "PiiRefused",
    "PiiUnavailable",
    "SecretNotDeclared",
    "SecretNotSet",
    "Secrets",
]
