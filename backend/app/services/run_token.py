"""The run token: one credential, one invocation (blueprint S4, S4a).

A run token is what lets something outside this process act *as* one
invocation of one run: the container runtime hands it to an agent
container, and from S4a the runner mints one for **every** invocation,
in-process included, because the LLM gateway is a separate service and
an in-process agent's model call has to be attributable too.

The record says everything the platform knows about the invocation the
token authorizes — the run, its tenant and agent, the manifest's grants,
the deadline, and the trace the run was minted with (the trace id and
the phase span's ``traceparent``). The relay, the MCP server and the
gateway each read it and each check something different against it: one
token, one trace, one tenant, one run.

``state`` is ``active`` while the invocation runs and ``ended`` for a
short grace afterwards — long enough for a late telemetry batch to land,
never long enough to spend money.

The container runtime and the runner share this module rather than each
keeping their own copy of the record's shape: three readers agreeing on
a document written in two places is how a field quietly stops being
written.
"""
from __future__ import annotations

import json
import secrets
from typing import Any

# How long a token outlives its invocation's deadline, so a request in
# flight at the deadline can still finish.
GRACE_SECONDS = 60

# How long an ``ended`` record stays resolvable for the telemetry relay.
END_GRACE_SECONDS = 30

KEY_PREFIX = "run_token:"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def key(token: str) -> str:
    return f"{KEY_PREFIX}{token}"


def ttl_seconds(deadline_seconds: int) -> int:
    return int(deadline_seconds) + GRACE_SECONDS


def build_record(
    *,
    run_id: Any,
    tenant_id: Any,
    agent_id: str,
    grants: list[str],
    user_id: Any = None,
    run_number: str | None = None,
    deadline_seconds: int,
    trace_id: str | None,
    traceparent: str | None,
    state: str = "active",
) -> dict:
    return {
        "run_id": str(run_id),
        "tenant_id": str(tenant_id),
        "agent_id": agent_id,
        "grants": list(grants),
        "user_id": str(user_id) if user_id is not None else None,
        "run_number": run_number,
        "deadline_seconds": int(deadline_seconds),
        "trace_id": trace_id,
        "traceparent": traceparent,
        "state": state,
    }


async def register(redis, token: str, record: dict) -> None:
    await redis.set(
        key(token),
        json.dumps(record),
        # Scoped to this invocation's own deadline rather than an
        # arbitrary window: a token that outlives its run is a live
        # tenant-scoped credential, and revocation is best-effort.
        ex=ttl_seconds(record["deadline_seconds"]),
    )


async def end(redis, token: str, record: dict) -> None:
    ended = dict(record)
    ended["state"] = "ended"
    await redis.set(key(token), json.dumps(ended), ex=END_GRACE_SECONDS)


async def revoke(redis, token: str) -> None:
    await redis.delete(key(token))
