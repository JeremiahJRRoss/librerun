"""``ctx.llm`` and ``ctx.config`` — model calls from inside a container.

An agent container reaches the LLM gateway over HTTP, presenting the
invocation's **run token**, so the call is attributable to exactly this
run and this tenant. It never presents a provider key, and it never
names a model: it names a **step**, and the gateway resolves the step to
a provider, a model and limits from the tenant's admin configuration at
request time (L25, D13). A model change is then an edit in the UI, not a
redeploy of this container.

There are two ways in. ``ctx.llm.complete()`` / ``.text()`` make the
call for you — the handler's route. ``ctx.llm.client()`` and
``ctx.llm.step()`` are for when a FRAMEWORK makes the call instead
(LlamaIndex, the Vercel AI SDK, ``openai``): ``client()`` hands over the
gateway's address, this agent's key and **this invocation's** headers,
and ``step()`` returns the one model string the gateway parses as a step
id. Build the framework's client from them inside the handler, never at
import time — the headers carry the run token, and a client that
outlives its invocation carries the wrong one.

``ctx.config`` is the other side of that: the steps the agent declared,
with this tenant's overrides applied, read live through the run-scoped
MCP server — which is why it can show an admin's edit that the manifest
baked into the image knows nothing about.

The gateway's address comes from ``LIBRERUN_GATEWAY_URL``, or from the
``OPENAI_BASE_URL`` a framework-shaped deployment already sets (the
compose fragment sets both), so an SDK agent and a framework agent in
the same container agree on where the gateway is.

The transport is ``urllib`` on a worker thread, like the MCP client's,
because **the SDK's core is stdlib-only** (``dependencies = []`` in
``pyproject.toml``). An agent image installs the SDK and its own code
and nothing else; a module-scope ``import httpx`` here would break
``import librerun_agent`` in every such image — which is exactly what
the container battery caught when this file was first written.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

RUN_TOKEN_HEADER = "X-LibreRun-Run-Token"
STEP_HEADER = "X-LibreRun-Step"

# The variable a framework-shaped container reads for its credential.
# It holds the agent's LIBRERUN GATEWAY key, never a provider key
# (blueprint S4a, D10): ``agents.compose.yaml`` sets it from
# ``LIBRERUN_AGENT_KEY_<ID>``. ``LIBRERUN_AGENT_KEY`` is accepted as the
# explicit spelling for a container that would rather not overload the
# OpenAI name.
AGENT_KEY_ENVS = ("LIBRERUN_AGENT_KEY", "OPENAI_API_KEY")

# What ``step()`` returns and the gateway parses: a step id wearing a
# model field's clothes. The gateway reads ``X-LibreRun-Step`` first and
# falls back to this prefix (``gateway/steps.py:requested_step_id``), so
# a framework that gives you a ``model`` string but no per-call headers
# can still name a step — which is the whole reason the prefix exists.
STEP_MODEL_PREFIX = "librerun/"

# The DEFAULT of ``LIBRERUN_MAX_PHASE_SECONDS``, used only when an
# invocation carried no deadline of its own. It is a fallback, never a
# clamp: an operator may raise that ceiling, and this container has no
# way to know they did — see ``_timeout``.
DEFAULT_MAX_PHASE_SECONDS = 3600.0


class LlmError(RuntimeError):
    """The gateway refused or failed the call.

    ``code`` is the gateway's own (``unknown_step``, ``pii_in_identifier``,
    ``step_required``, …) so a handler can branch on it and a log line can
    name it. The message never contains a value, only a path.
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def gateway_url() -> str:
    explicit = (os.environ.get("LIBRERUN_GATEWAY_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    # A framework-shaped container points OPENAI_BASE_URL at the gateway's
    # /v1; strip that suffix so both spellings resolve to one base.
    base = (os.environ.get("OPENAI_BASE_URL") or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


@dataclass(frozen=True)
class GatewayClient:
    """The three things an OpenAI-compatible client needs to talk to the
    LibreRun gateway **for one invocation**.

    Built per invocation and never cached across them, because
    ``default_headers`` carries this invocation's run token: a client
    built once at import time would carry one run's token into every
    later run, so on a multi-tenant deployment the gateway would resolve
    the wrong tenant's step configuration — or, once that token expired,
    refuse every call (D10). The frameworks this exists for
    (LlamaIndex's ``OpenAILike``, the Vercel AI SDK's ``createOpenAI``,
    ``openai.OpenAI``) all take exactly these three, under names of
    their own — ``api_base`` here, ``baseURL`` there — so the fields are
    named after what they ARE and the example maps them.

    ``api_key`` is the agent's LibreRun gateway key, never a provider
    key: it names an agent and nothing else, and every model call needs
    the run token in ``default_headers`` beside it.
    """

    base_url: str
    api_key: str
    default_headers: dict[str, str] = field(default_factory=dict)


class Llm:
    """Model calls for one invocation."""

    def __init__(self, invocation) -> None:
        self._inv = invocation

    def _timeout(self) -> float:
        """The transport ceiling for one call: what is left of this
        invocation's deadline.

        It was a flat 120s, and a step may be configured for longer —
        the platform's own flagship agent ships one at 180. The client
        then hung up on a call the admin had allowed, the provider
        billed for the work, and the agent was told
        ``gateway_unreachable`` about a gateway that was fine (Codex
        P1). The gateway enforces the step timeout and ``_server`` wraps
        the handler in the deadline; this exists only so a dead socket
        cannot hold the worker, so it takes the budget already being
        enforced rather than inventing a shorter one.
        """
        left = self._inv.seconds_left()
        if left is None:
            # No deadline came with this invocation, so the only bound
            # available is the platform's DEFAULT ceiling. A guess, and
            # labelled as one.
            return DEFAULT_MAX_PHASE_SECONDS
        # When the invocation DID supply a deadline, that is the real
        # policy and the constant must not touch it. `MAX_PHASE_SECONDS`
        # is the default value of `LIBRERUN_MAX_PHASE_SECONDS`, which an
        # operator may raise, and `deadline_seconds` has no maximum of
        # its own — so clamping here disconnected a container at 3600s on
        # a deployment whose ceiling was 7200 and whose step was allowed
        # 5400, abandoning billed work and blaming the gateway for it
        # (Codex P2). The clamp was the same mistake the ceiling itself
        # was: a bound applied by something that does not own the policy.
        return max(1.0, float(left))

    def _headers(self, step: str) -> dict[str, str]:
        headers = {RUN_TOKEN_HEADER: self._inv.token, STEP_HEADER: step}
        if self._inv.traceparent:
            # So the LLM span hangs inside this run's tree rather than
            # starting one of its own.
            headers["traceparent"] = self._inv.traceparent
        if self._inv.tracestate:
            headers["tracestate"] = self._inv.tracestate
        return headers

    def _post(self, path: str, payload: dict, step: str, timeout: float) -> dict:
        base = gateway_url()
        if not base:
            raise LlmError(
                0,
                "gateway_not_configured",
                "neither LIBRERUN_GATEWAY_URL nor OPENAI_BASE_URL is set, so "
                "there is nowhere to send a model call",
            )
        headers = {"Content-Type": "application/json", **self._headers(step)}
        request = urllib.request.Request(
            f"{base}{path}",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            code, message = "gateway_error", f"{exc.code} {exc.reason}"
            try:
                error = (json.loads(exc.read() or b"{}") or {}).get("error") or {}
                code = str(error.get("code") or code)
                # The gateway never puts a value in a refusal message,
                # only a path, so this is safe to raise and to log.
                message = str(error.get("message") or message)
            except ValueError:
                pass
            raise LlmError(exc.code, code, message) from None
        except urllib.error.URLError as exc:
            # ``urlopen(timeout=)`` surfaces as a socket timeout wrapped
            # in URLError; saying "unreachable" for it points a reader at
            # the gateway when the gateway answered the handshake and
            # simply took too long. Status 0 means "no reply at all", so
            # a timeout is 504 instead (Codex P1).
            if isinstance(exc.reason, TimeoutError):
                raise LlmError(
                    504,
                    "gateway_timeout",
                    f"the gateway did not answer within {timeout:g}s, the "
                    f"invocation's remaining budget",
                ) from exc
            raise LlmError(0, "gateway_unreachable", str(exc.reason)) from exc
        except TimeoutError as exc:
            raise LlmError(
                504,
                "gateway_timeout",
                f"the gateway did not answer within {timeout:g}s, the "
                f"invocation's remaining budget",
            ) from exc
        except OSError as exc:
            raise LlmError(0, "gateway_unreachable", str(exc)) from exc

    def step(self, step_id: str) -> str:
        """The ``model`` string that names ``step_id`` to the gateway.

        Hand it to a framework wherever it asks for a model name. The
        agent still names no model: this is a step id, and the tenant's
        admin configuration decides which provider and model answer it
        at request time (L25, D13), so retargeting a step is an edit in
        the admin page and not a redeploy of this container.
        """
        step_id = str(step_id).strip()
        if not step_id:
            raise ValueError("a step id is required: name one of the manifest's llm.steps[]")
        return f"{STEP_MODEL_PREFIX}{step_id}"

    def client(self) -> GatewayClient:
        """The gateway's address, this agent's key and this invocation's
        headers — for a framework that brings its own HTTP client.

        ``ctx.llm.complete()`` is the direct route and needs none of
        this; ``client()`` is for the case where the model call is made
        by a framework (LlamaIndex, the Vercel AI SDK, ``openai``)
        rather than by the handler. Build the framework's client from it
        **inside the handler**, per invocation — see ``GatewayClient``.

        Raises ``LlmError`` rather than returning a half-built client:
        a framework handed an empty base URL or an empty key fails much
        later and blames the model.
        """
        base = gateway_url()
        if not base:
            raise LlmError(
                0,
                "gateway_not_configured",
                "neither LIBRERUN_GATEWAY_URL nor OPENAI_BASE_URL is set, so "
                "there is nowhere to send a model call",
            )
        key = ""
        for name in AGENT_KEY_ENVS:
            key = (os.environ.get(name) or "").strip()
            if key:
                break
        if not key:
            raise LlmError(
                0,
                "agent_key_not_configured",
                "this container holds no LibreRun agent key: set "
                + " or ".join(AGENT_KEY_ENVS)
                + " to the agent's LIBRERUN_AGENT_KEY_<ID> value (never a "
                "provider key)",
            )
        headers = {RUN_TOKEN_HEADER: self._inv.token}
        if self._inv.traceparent:
            headers["traceparent"] = self._inv.traceparent
        if self._inv.tracestate:
            headers["tracestate"] = self._inv.tracestate
        return GatewayClient(
            base_url=f"{base}/v1", api_key=key, default_headers=headers
        )

    async def complete(self, step: str, messages: list[dict], **kwargs: Any) -> dict:
        """One chat completion for ``step``, as an OpenAI response dict.

        The step must be declared in the agent's manifest
        (``llm.steps[]``) — the gateway refuses an id that is not, and it
        is right to: an invented one would have no provider, model or
        limits an admin ever chose.

        There is deliberately **no per-call timeout**, and passing one is
        an error rather than a silent no-op. This used to take a
        ``timeout`` keyword that replaced the invocation's budget
        outright, which handed every caller the exact failure
        ``_timeout()`` above exists to prevent: a client timeout shorter
        than the step's closes THIS socket while the gateway goes on
        running the provider call and billing it, and the agent is told
        ``gateway_timeout`` about work that is still happening and still
        being paid for (Codex P2).

        Refused rather than ignored, because an author who writes
        ``timeout=5`` believes something about the call, and a parameter
        that quietly does nothing is the defect this batch has now
        found five times on the other side of the same door.
        """
        if "timeout" in kwargs:
            raise ValueError(
                "ctx.llm.complete() takes no 'timeout': a step's timeout is "
                "the tenant admin's to set (L25), and shortening it here "
                "would only abandon a provider call that the gateway keeps "
                "running and billing. Change the step's timeout in the admin "
                "configuration instead."
            )
        payload = {"model": f"librerun/{step}", "messages": messages, **kwargs}
        return await asyncio.to_thread(
            self._post, "/v1/chat/completions", payload, step, self._timeout()
        )

    async def text(self, step: str, prompt: str, **kwargs: Any) -> str:
        """The one-liner: a single user message in, the reply's text out."""
        response = await self.complete(
            step, [{"role": "user", "content": prompt}], **kwargs
        )
        choice = (response.get("choices") or [{}])[0]
        return str((choice.get("message") or {}).get("content") or "")


class Config:
    """This run's effective configuration — its steps and its settings —
    read live over MCP (the ``config_get`` tool, which needs no grant).

    Live rather than cached on purpose: an admin who changes a model or a
    setting in the UI expects the next call to use it, and a value this
    object remembered from the start of the invocation would be the one
    thing that did not.
    """

    def __init__(self, client) -> None:
        self._client = client

    async def steps(self) -> list[dict]:
        from ._mcp import _require

        result = await _require(self._client).call("config_get", {})
        return list(result.get("steps") or [])

    async def step(self, step_id: str) -> dict | None:
        for step in await self.steps():
            if step.get("step_id") == step_id:
                return step
        return None

    async def settings(self) -> dict[str, Any]:
        """The settings your manifest declares (``settings[]``), with this
        tenant's values: ``{key: value}``, every declared key present — a
        tenant that never changed one reads its default.

        ``{}`` for an agent that declares none, and from a chassis older
        than the ``settings[]`` manifest field, whose ``config_get`` answer
        carries no ``settings``.
        """
        from ._mcp import _require

        result = await _require(self._client).call("config_get", {})
        return {
            entry["key"]: entry.get("value")
            for entry in result.get("settings") or []
            if isinstance(entry, dict) and "key" in entry
        }


__all__ = ["Config", "GatewayClient", "Llm", "LlmError", "gateway_url"]
