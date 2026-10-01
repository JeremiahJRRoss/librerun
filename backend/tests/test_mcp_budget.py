"""A tool call inherits what is LEFT of the invocation (Codex round 11, P1).

The façade's budget clock starts when the façade is built. That is right
for the runner, which builds one per phase immediately before wrapping
the phase in `asyncio.timeout(deadline)` — and wrong for the MCP router,
which builds one per TOOL CALL. Handed the token's original
`deadline_seconds`, a `kb_search` arriving in the last second of a phase
was given the whole phase over again.

Not merely a loose ceiling: the MCP handler is a separate HTTP task, so
cancelling the runner at the real deadline does not cancel an embedding
already admitted here. It would go on spending after the invocation had
ended.

The remaining budget comes from the run token's Redis TTL, which already
encodes the absolute deadline: the runner minted the key with
`deadline_seconds + GRACE_SECONDS`, so what is left minus the grace is
what the invocation has left. No new field, no clock to keep in sync, and
it is right for a token minted before this change too.
"""
from __future__ import annotations

import uuid

import pytest

from app import capabilities as caps_mod
from app.services import run_token


def test_the_ttl_is_the_absolute_deadline():
    """The arithmetic the router relies on, stated once."""
    assert run_token.ttl_seconds(300) == 300 + run_token.GRACE_SECONDS


@pytest.mark.parametrize(
    "ttl,expected",
    [
        # Freshly minted: the whole budget.
        (300 + run_token.GRACE_SECONDS, 300.0),
        # Half way through a 300s phase.
        (150 + run_token.GRACE_SECONDS, 150.0),
        # One second left.
        (1 + run_token.GRACE_SECONDS, 1.0),
        # Past the deadline, inside the grace: nothing left, never negative.
        (10, 0.0),
    ],
)
def test_the_remaining_budget_is_derived_from_the_ttl(ttl, expected):
    assert max(0.0, float(ttl) - run_token.GRACE_SECONDS) == expected


@pytest.mark.asyncio
async def test_a_facade_built_late_in_a_phase_does_not_get_the_phase_again():
    """The defect, stated as the invariant it violates: two façades built
    from the same invocation at different times must not both believe
    they have the full budget."""
    run_id, tenant_id = uuid.uuid4(), uuid.uuid4()

    # What the router now passes: the remaining budget, not the original.
    early = caps_mod.for_run(
        run_id=run_id,
        tenant_id=tenant_id,
        agent_id="probe-agent",
        grants=["kb"],
        deadline_seconds=300,
    )
    late = caps_mod.for_run(
        run_id=run_id,
        tenant_id=tenant_id,
        agent_id="probe-agent",
        grants=["kb"],
        deadline_seconds=2,
    )

    assert early.seconds_left() > 290
    assert late.seconds_left() <= 2, (
        "a tool call arriving at the end of a phase was handed the whole "
        "phase again — the budget did not survive the trip"
    )


def test_the_router_reads_the_remaining_budget_and_passes_that():
    """The plumbing, from the source: the router must pass what it
    derived, not the token's original figure. A fix that stopped at
    `_resolve_run_token` would leave the façade on the old value."""
    import inspect

    from app.routers import mcp

    resolve = inspect.getsource(mcp._resolve_run_token)
    assert "_seconds_left" in resolve, "the router never derives a remaining budget"
    assert ".ttl(" in resolve, "the remaining budget is not derived from the TTL"

    call_tool = inspect.getsource(mcp._call_tool)
    assert 'deadline_seconds=run.get("_seconds_left")' in call_tool, (
        "the façade is still built from the token's ORIGINAL deadline"
    )
    assert 'deadline_seconds=run.get("deadline_seconds")' not in call_tool


# --------------------------------------------------------------------------
# Round 15: the record and its expiry are one fact, read once.
# --------------------------------------------------------------------------
#
# `_resolve_run_token` read the record and then the TTL in two round
# trips. Between them the key can expire: the record comes back, the TTL
# comes back `-2` ("no such key"), and the old code simply left
# `_seconds_left` unset — which every caller downstream reads as "no
# deadline given" rather than "this token is gone". The gateway carried
# the identical defect and Codex found it there (round 15, P1); this is
# the same shape in the process that calls the gateway.


class _PipeAnswering:
    def __init__(self, raw, ttl):
        self._answers = [raw, ttl]

    def get(self, _key):
        return self

    def ttl(self, _key):
        return self

    async def execute(self):
        return list(self._answers)


class _RedisAnswering:
    def __init__(self, raw, ttl):
        self._raw, self._ttl = raw, ttl

    def pipeline(self, transaction=True):
        return _PipeAnswering(self._raw, self._ttl)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _bearing(token: str) -> str:
    return f"Bearer {token}"


def _patch_redis(monkeypatch, raw, ttl):
    from app.routers import mcp as mcp_router

    class _Module:
        @staticmethod
        def from_url(*_a, **_kw):
            return _RedisAnswering(raw, ttl)

    monkeypatch.setattr(mcp_router, "aioredis", _Module)


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", [-2, -1])
async def test_a_token_whose_key_has_no_life_left_authorizes_nothing(
    monkeypatch, ttl
):
    """`-2` is a key that is gone, `-1` a key with no expiry. Every mint
    sets `ex=`, so neither is a live token — and letting either through
    with no budget is how an expired credential got a tool call."""
    import json

    from app.routers import mcp as mcp_router

    record = {
        "run_id": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "agent_id": "demo",
        "state": "active",
    }
    _patch_redis(monkeypatch, json.dumps(record), ttl)
    assert await mcp_router._resolve_run_token(_bearing("tok")) is None


@pytest.mark.asyncio
async def test_a_live_token_still_resolves_with_its_budget(monkeypatch):
    """The refusal must not eat the ordinary case."""
    import json

    from app.routers import mcp as mcp_router

    record = {
        "run_id": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "agent_id": "demo",
        "state": "active",
    }
    _patch_redis(monkeypatch, json.dumps(record), 300 + run_token.GRACE_SECONDS)
    got = await mcp_router._resolve_run_token(_bearing("tok"))
    assert got is not None
    assert got["_seconds_left"] == 300.0
    assert got["_token"] == "tok"


def test_the_backend_reads_the_record_and_its_expiry_together():
    """The structural half, the same as the gateway's. A refusal on a
    negative TTL is a backstop; what closes the window is that both
    values come from one `execute()`."""
    import ast
    import inspect

    from app.routers import mcp as mcp_router

    tree = ast.parse(inspect.getsource(mcp_router._resolve_run_token).strip())
    names = [
        node.value.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
    ]
    assert "get" not in names and "ttl" not in names, (
        "_resolve_run_token awaits redis.get/redis.ttl directly, so the "
        f"record and its expiry are two round trips again: {names}"
    )
    assert "execute" in names, (
        f"_resolve_run_token no longer awaits a pipeline execute(): {names}"
    )
