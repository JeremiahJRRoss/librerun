"""Talking to the LLM gateway from inside the backend (blueprint S4a).

The gateway is reached over HTTP as a **service**, never imported as a
library. That is the whole point of it being a separate process: egress
code inside this one would keep provider credentials here, in the one
process every ``python-package`` agent shares, and a credential an agent
can read is a credential an agent has.

Every call presents the invocation's run token, so the gateway can
attribute the spend to a run and a tenant, and carries the current
``traceparent``, so the LLM span lands inside the run's tree rather than
starting one of its own.
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

RUN_TOKEN_HEADER = "X-LibreRun-Run-Token"
STEP_HEADER = "X-LibreRun-Step"

# The platform's own embedding step. The gateway routes it from platform
# configuration, and authorizes it with the ``kb`` grant.
KB_EMBED_STEP = "kb_embed"

def transport_timeout(seconds_left: float | None) -> float:
    """The HTTP ceiling for ONE call to the gateway.

    Three clocks bound a model call and only two of them are policy:

    - the **phase deadline** — ``asyncio.timeout(deadline)`` around the
      whole invocation in ``agent_runner`` — the chassis's backstop;
    - the **step timeout** — ``llm.steps[].timeout_seconds``, resolved by
      the gateway and handed to LiteLLM — the admin's choice (L25, D13);
    - this one, which is neither. It exists so a dead socket cannot hold
      a worker, and it must never be what decides a call is over.

    It was a flat 120s, and the shipped ``generate_resolution_plan`` step
    allows 180 — so the client hung up on a call the admin had said could
    run, the provider billed for the work already done, and what came
    back was ``gateway_unreachable``: a healthy gateway reported as down,
    at status 0, which the retry rule reads as worth trying again. Two
    more full attempts, each killed the same way (Codex P1).

    So it is *derived* rather than chosen: the invocation's own remaining
    budget, under the platform ceiling. Then the gateway's step timeout
    always fires first — it is the smaller number by construction
    whenever the configuration is sane — and when it is not, the thing
    that fires is the deadline that was about to cancel the phase anyway.
    ``None`` (a caller with no invocation, such as a test) gets the
    ceiling: the longest any phase may run, so it cannot pre-empt a step.
    """
    from app.config import settings

    ceiling = max(1.0, float(settings.LIBRERUN_MAX_PHASE_SECONDS))
    if seconds_left is None:
        return ceiling
    return max(1.0, min(float(seconds_left), ceiling))


# Connecting is not the same as answering. A ceiling of an hour is the
# right budget for a model that thinks for an hour and the wrong one for
# a TCP handshake, so the handshake keeps a short bound of its own.
CONNECT_TIMEOUT = 10.0


class GatewayError(RuntimeError):
    """The gateway refused or failed the call.

    Carries the gateway's own code (``unknown_step``, ``pii_in_identifier``,
    …) so a caller can act on it and a log line can name it.
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def base_url() -> str:
    from app.config import settings

    return (settings.LIBRERUN_GATEWAY_URL or "").rstrip("/")


def _headers(run_token: str | None, step: str) -> dict[str, str]:
    from app.observability import run_trace

    headers = {STEP_HEADER: step}
    if run_token:
        headers[RUN_TOKEN_HEADER] = run_token
    headers.update(run_trace.propagation_headers())
    return headers


async def _post(path: str, payload: dict, *, run_token: str | None, step: str, timeout: float):
    url = base_url()
    if not url:
        raise GatewayError(
            0,
            "gateway_not_configured",
            "LIBRERUN_GATEWAY_URL is unset, so there is nowhere to send a "
            "model call — the gateway is a default compose service and a "
            "local uvicorn needs it running from ./compose.sh up -d",
        )
    budget = httpx.Timeout(timeout, connect=min(CONNECT_TIMEOUT, timeout))
    try:
        async with httpx.AsyncClient(timeout=budget) as client:
            response = await client.post(
                f"{url}{path}", json=payload, headers=_headers(run_token, step)
            )
    except httpx.ConnectTimeout as exc:
        # Nothing answered the handshake: the gateway really is not there.
        raise GatewayError(0, "gateway_unreachable", str(exc)) from exc
    except httpx.TimeoutException as exc:
        # It took the request and did not answer in time. Saying
        # "unreachable" here sent an operator to look at a healthy
        # service, and status 0 is the code for "no reply at all" — so
        # this is 504, which is still retryable (a slow provider is worth
        # another try) but names what happened (Codex P1).
        logger.warning("llm_gateway_timeout", step=step, timeout_seconds=timeout)
        raise GatewayError(
            504,
            "gateway_timeout",
            f"the gateway did not answer within {timeout:g}s, the "
            f"invocation's remaining budget: {exc}",
        ) from exc
    except httpx.HTTPError as exc:
        raise GatewayError(0, "gateway_unreachable", str(exc)) from exc
    if response.is_success:
        return response.json()
    code, message = "gateway_error", response.text[:500]
    try:
        error = (response.json() or {}).get("error") or {}
        code = str(error.get("code") or code)
        # The gateway never puts a value in a refusal message, only a
        # path, so this is safe to log and to raise.
        message = str(error.get("message") or message)
    except ValueError:
        pass
    logger.warning(
        "llm_gateway_refused", status=response.status_code, code=code, step=step
    )
    raise GatewayError(response.status_code, code, message)


async def chat(
    *,
    run_token: str | None,
    step: str,
    messages: list[dict],
    seconds_left: float | None = None,
    **kwargs: Any,
) -> dict:
    """One chat completion. The model is NOT ours to choose — the step is
    the whole request, and the gateway resolves the rest.

    ``seconds_left`` is the invocation's remaining budget; the transport
    ceiling comes from it rather than from a constant of this module's
    own, so it can never fire before the step timeout the admin set."""
    payload = {"model": f"librerun/{step}", "messages": messages, **kwargs}
    return await _post(
        "/v1/chat/completions",
        payload,
        run_token=run_token,
        step=step,
        timeout=transport_timeout(seconds_left),
    )


async def embed(
    *,
    run_token: str | None,
    inputs: list[str],
    step: str = KB_EMBED_STEP,
    seconds_left: float | None = None,
) -> list[list[float]]:
    payload = {"model": f"librerun/{step}", "input": list(inputs)}
    response = await _post(
        "/v1/embeddings",
        payload,
        run_token=run_token,
        step=step,
        timeout=transport_timeout(seconds_left),
    )
    return [item.get("embedding") or [] for item in (response.get("data") or [])]


async def health(timeout: float = 2.0) -> dict | None:
    """The gateway's unauthenticated ``/healthz``, or None when it cannot
    be reached. Used by ``/api/v1/meta`` to report keyless mode, which
    this process no longer knows on its own."""
    url = base_url()
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{url}/healthz")
        if response.is_success:
            return response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return None
