"""Who is calling, and may they (D10).

Two credentials, and they answer different questions.

A **run token** is minted per invocation by the chassis and kept in
Redis under ``run_token:{token}``. It names the run, its tenant, its
agent and the trace the run was minted with, so a call carrying one is
attributable exactly: this tenant's data, this run's cost, this run's
trace. Only an ``active`` record is accepted — an ``ended`` one, which
S4 keeps resolvable for the telemetry relay's grace, is refused ``401
run_token_ended``.

A **per-agent key** is what a framework that only reads
``OPENAI_API_KEY`` from its environment can present. It names an agent
and nothing else: one container serves every tenant, so the key alone
cannot say whose data a call is about, and cannot say which run should
carry the spend. That is why it authenticates ``GET /v1/models`` alone
and every model call needs the run token beside it — ``401
run_token_required`` without one, **in every mode**, single-tenant demo
included, because a rule that relaxes on one deployment shape is a rule
nobody can reason about.

Whatever the credential, the **manifest snapshot is the authority** on
what the agent may do. A run token carries its grants in Redis, but
those were written by the runner from the manifest at invocation time;
the snapshot is what an operator's uninstall or an author's edit
actually changes. No snapshot — never written, or stamped absent —
means no agent at all: ``403 agent_unknown``.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import redis.asyncio as aioredis
import structlog
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from gateway import errors, keys, snapshots
from gateway.config import settings

logger = structlog.get_logger(__name__)

RUN_TOKEN_HEADER = "X-LibreRun-Run-Token"
STEP_HEADER = "X-LibreRun-Step"
SCENARIO_HEADER = "X-LibreRun-Scenario"


@dataclass
class Principal:
    """The resolved caller. ``run`` is None for an agent-key-only request,
    which is why such a request may not reach a model."""

    agent_id: str
    snapshot: snapshots.Snapshot
    key_hash: str | None = None
    run_id: str | None = None
    run_number: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    trace_id: str | None = None
    traceparent: str | None = None
    token_grants: list[str] = field(default_factory=list)
    # WHEN the invocation runs out, on this process's monotonic clock —
    # not how much was left when it authenticated.
    #
    # A snapshot taken here is already stale by the time the provider is
    # called: body parsing, step resolution and above all the synchronous
    # Presidio walk run in between, and that walk costs ~2s on a
    # 300-message request. A call arriving with four seconds left would
    # hand the provider four seconds' allowance after spending two of
    # them (Codex P1). An absolute deadline does not decay.
    deadline_at: float | None = None

    def seconds_left(self) -> float | None:
        """What is left NOW. Recomputed at every call site, because the
        interesting call sites are the ones that happen later."""
        if self.deadline_at is None:
            return None
        return max(0.0, self.deadline_at - time.monotonic())

    @property
    def has_run(self) -> bool:
        return self.run_id is not None and self.tenant_id is not None

    def grants(self, capability: str) -> bool:
        """BOTH the current manifest and, on a run token, the grants the
        invocation was minted with.

        The two directions are deliberately asymmetric (Codex P1).

        A **removal** must take effect at once: an operator who takes
        `llm` out of a manifest has withdrawn it, and a token minted an
        hour ago must not go on spending. That is the snapshot's half,
        and it is what this method used to be.

        An **addition** must not reach backwards. `token_grants` was
        loaded from the token record and then never read, so a token
        minted for an agent that granted nothing could begin calling
        models the moment someone added `llm` to the manifest — across a
        restart, say, where token cleanup is best-effort. A credential's
        authority is what it was issued with, not what its subject has
        acquired since.

        An agent key alone carries no invocation, so there is no
        invocation-time grant to consult and the snapshot decides —
        which is all `GET /v1/models` needs.
        """
        if not self.snapshot.grants_capability(capability):
            return False
        if self.has_run:
            return capability in self.token_grants
        return True

    def require(self, capability: str) -> None:
        if self.snapshot.grants_capability(capability):
            if not self.has_run or capability in self.token_grants:
                return
            # Granted now, not granted when this invocation began. Saying
            # "the manifest does not grant it" here would send an
            # operator to look at a manifest that does.
            raise errors.forbidden(
                f"{capability}_not_granted",
                f"agent {self.agent_id!r} grants the {capability!r} "
                f"capability now, but this invocation's run token was "
                f"minted before it did. A credential carries the authority "
                f"it was issued with; start a new run to use it.",
            )
        raise errors.forbidden(
            f"{capability}_not_granted",
            f"agent {self.agent_id!r} does not grant the {capability!r} "
            f"capability in its manifest, so the gateway will not make "
            f"this call on its behalf",
        )


def bearer_of(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        value = header[7:].strip()
        return value or None
    return None


async def _redis():
    return aioredis.from_url(
        settings.REDIS_URL.get_secret_value(), decode_responses=True
    )


# The invocation's own budget lives in this key's TTL: the runner minted
# it with ``deadline_seconds + GRACE_SECONDS``, so what Redis has left
# minus that grace is what the invocation has left. The gateway needs it
# because the gateway is where the money is spent — see ``seconds_left``
# on the Principal.
TOKEN_GRACE_SECONDS = 60


async def read_run_token(token: str) -> dict:
    """The token's record and its remaining budget, read together.

    ``_deadline_at`` goes under a private key the record's own writer
    never uses — the same convention the backend's MCP router follows
    for ``_token``.

    The record and its expiry are one fact about one key, so they are
    read in ONE round trip. Two separate awaits let the key expire in
    between: ``raw`` came back nonempty, the TTL came back ``-2``, and
    this function reported "no expiry" — which every caller downstream
    reads as "no deadline", so an expired credential bought a full step
    timeout of billed provider work (Codex P1).
    """
    key = f"run_token:{token}"
    async with await _redis() as redis:
        pipe = redis.pipeline(transaction=True)
        pipe.get(key)
        pipe.ttl(key)
        raw, ttl = await pipe.execute()
    if not raw:
        raise errors.unauthorized(
            "run_token_invalid", "the run token is unknown or has expired"
        )
    try:
        record = json.loads(raw)
    except ValueError:
        raise errors.unauthorized(
            "run_token_invalid", "the run token record is unreadable"
        )
    if str(record.get("state") or "active") != "active":
        # S4 keeps an ended token resolvable so a late telemetry batch can
        # still land. Spending money on it is a different matter.
        raise errors.unauthorized(
            "run_token_ended",
            "this invocation has ended; its token can still carry telemetry "
            "but can no longer call a model",
        )
    if not isinstance(ttl, int) or ttl < 0:
        # Redis says ``-2`` for a key that is gone and ``-1`` for a key
        # with no expiry. Neither can be a live minted run token: every
        # mint sets ``ex=deadline_seconds + GRACE``. Both used to become
        # ``None`` here, and ``None`` means "no deadline" to everything
        # downstream — decision 68's move one level up, a signal that
        # says STOP turned into a value you can carry on with.
        #
        # The atomic read above makes ``-2`` unreachable while ``raw``
        # is nonempty. This stays anyway: it is what keeps the refusal
        # correct if someone ever splits that pipeline back into two
        # awaits, which is exactly how the defect arrived.
        raise errors.unauthorized(
            "run_token_invalid",
            "the run token is unknown or has expired",
        )
    # Converted to an absolute point immediately: everything after this
    # line costs time, and a relative figure would silently mean "as of
    # whenever this was read".
    record["_deadline_at"] = time.monotonic() + max(
        0.0, float(ttl) - TOKEN_GRACE_SECONDS
    )
    return record


async def authenticate(request: Request, db: AsyncSession) -> Principal:
    """Resolve the caller from whichever credentials are present.

    Does not decide whether the endpoint is reachable — that is
    ``require_model_call`` below, because ``GET /v1/models`` and a chat
    completion have different answers.
    """
    token = (request.headers.get(RUN_TOKEN_HEADER) or "").strip() or None
    presented = bearer_of(request)
    if token is None and presented is None:
        raise errors.unauthorized(
            "credential_required",
            "present a LibreRun agent key as a bearer token, a run token in "
            f"{RUN_TOKEN_HEADER}, or both",
        )

    resolved = None
    if presented is not None:
        resolved = await keys.resolve(db, presented)
        if resolved is None:
            raise errors.unauthorized(
                "agent_key_invalid",
                "the bearer token is not a valid LibreRun agent key — note "
                "that a provider key is never accepted here",
            )

    record: dict = {}
    agent_id: str | None = resolved.agent_id if resolved else None
    if token is not None:
        record = await read_run_token(token)
        token_agent = str(record.get("agent_id") or "")
        if not token_agent:
            raise errors.unauthorized(
                "run_token_invalid", "the run token names no agent"
            )
        if agent_id is not None and token_agent != agent_id:
            # A key for one agent and a token for another is either a
            # misconfigured container or an attempt to spend another
            # agent's budget under its own name.
            raise errors.unauthorized(
                "credential_mismatch",
                "the agent key and the run token name different agents",
            )
        agent_id = token_agent

    assert agent_id is not None  # one of the two branches set it
    snapshot = await snapshots.load(db, agent_id)
    if snapshot is None:
        raise errors.forbidden(
            "agent_unknown",
            f"no installed agent {agent_id!r} — the platform has no current "
            f"manifest for it, so nothing it once declared authorizes a call",
        )

    if resolved is not None:
        await keys.touch(db, resolved.key_hash)

    return Principal(
        agent_id=agent_id,
        snapshot=snapshot,
        key_hash=resolved.key_hash if resolved else None,
        run_id=record.get("run_id"),
        run_number=record.get("run_number"),
        tenant_id=record.get("tenant_id"),
        user_id=record.get("user_id"),
        trace_id=record.get("trace_id"),
        traceparent=record.get("traceparent"),
        token_grants=list(record.get("grants") or []),
        deadline_at=record.get("_deadline_at"),
    )


def require_model_call(principal: Principal) -> None:
    """A model call must be attributable to a run, in every mode."""
    if not principal.has_run:
        raise errors.unauthorized(
            "run_token_required",
            f"a model call needs {RUN_TOKEN_HEADER} beside the agent key: an "
            f"agent key names an agent, not a tenant or a run, so it cannot "
            f"say whose data this call is about or which run pays for it",
        )
