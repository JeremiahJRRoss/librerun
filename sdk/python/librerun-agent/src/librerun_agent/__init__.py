"""The LibreRun agent SDK (blueprint S4; decision L27).

One async handler becomes a Run Contract v1 agent::

    from librerun_agent import serve, RunContext

    async def handler(ctx: RunContext) -> dict:
        ctx.progress("running", step="gather", label="Gathering context")
        hits = await ctx.capabilities.kb_search(ctx.input["question"], top_k=3)
        clean = await ctx.pii.redact(ctx.input["question"])
        await ctx.capabilities.audit_log("kb_queried", {"hits": len(hits)})
        return {"answer": clean, "sources": [h["url"] for h in hits]}

    app = serve(handler)

``serve`` returns an ASGI application with the four contract endpoints
(``/healthz``, ``POST /v1/runs``, ``GET /v1/runs/{id}/events``,
``GET /v1/runs/{id}/output``); run it with any ASGI server. The SDK is
the one documented control surface: every function an agent needs from
the platform — start, resume, status, progress, gates, configuration,
capabilities, logs — is a ``RunContext`` member or a Run Contract event
(``docs/authoring/SDK.md``).
"""
from __future__ import annotations

from ._context import RunContext
from ._llm import GatewayClient, LlmError
from ._mcp import (
    CapabilityError,
    CapabilityNotGranted,
    CapabilityUnreachable,
    PiiRefused,
    PiiUnavailable,
    SecretNotDeclared,
    SecretNotSet,
)
from ._server import RunContractApp, run, serve

__all__ = [
    "GatewayClient",
    "LlmError",
    "CapabilityError",
    "CapabilityNotGranted",
    "CapabilityUnreachable",
    "PiiUnavailable",
    "PiiRefused",
    "RunContext",
    "SecretNotDeclared",
    "SecretNotSet",
    "RunContractApp",
    "run",
    "serve",
]
__version__ = "1.1.0b1"
