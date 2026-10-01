"""The record and its expiry are one fact, read once.

`read_run_token` used to `GET` the record and then `TTL` the same key in
a second round trip. Between the two the key can expire: `raw` comes back
nonempty and Redis answers `-2` — "no such key" — which the old branch
turned into `None`, meaning "no expiry". Nothing downstream reads `None`
as "unknown": `Principal.seconds_left()` returns `None`, `egress._within`
yields with no scope at all, and an expired credential buys a full step
timeout of billed provider work (Codex P1).

Two things are checked here, because either alone passes by not looking:

- the READ is one round trip, so the race has no window;
- a negative TTL is REFUSED if one is ever seen anyway, which is what
  keeps the fix correct should someone split the pipeline back into two
  awaits.
"""
from __future__ import annotations

import ast
import inspect
import json

import pytest


@pytest.mark.asyncio
async def test_a_gone_key_is_refused_rather_than_read_as_unlimited(monkeypatch):
    """`-2` with a record present: the two-await race, replayed."""
    from gateway import auth, errors

    record = {"run_id": "r", "tenant_id": "t", "agent_id": "a", "state": "active"}
    monkeypatch.setattr(auth, "_redis", _redis_answering(json.dumps(record), -2))

    with pytest.raises(errors.GatewayError) as caught:
        await auth.read_run_token("token")
    assert caught.value.status_code == 401
    assert caught.value.code == "run_token_invalid"


@pytest.mark.asyncio
async def test_a_key_with_no_expiry_is_refused_too(monkeypatch):
    """`-1` is "exists, never expires". Every mint sets `ex=`, so this is
    not a live token either — and "no expiry" is the reading that made
    the budget disappear in the first place."""
    from gateway import auth, errors

    record = {"run_id": "r", "tenant_id": "t", "agent_id": "a", "state": "active"}
    monkeypatch.setattr(auth, "_redis", _redis_answering(json.dumps(record), -1))

    with pytest.raises(errors.GatewayError) as caught:
        await auth.read_run_token("token")
    assert caught.value.code == "run_token_invalid"


@pytest.mark.asyncio
async def test_a_live_token_still_carries_its_budget(monkeypatch):
    """The refusal must not eat the ordinary case."""
    from gateway import auth

    record = {"run_id": "r", "tenant_id": "t", "agent_id": "a", "state": "active"}
    ttl = 300
    monkeypatch.setattr(auth, "_redis", _redis_answering(json.dumps(record), ttl))

    got = await auth.read_run_token("token")
    principal_budget = got["_deadline_at"]
    assert principal_budget is not None
    import time

    left = principal_budget - time.monotonic()
    assert 0 < left <= ttl - auth.TOKEN_GRACE_SECONDS + 1


def test_the_record_and_its_expiry_are_read_in_one_round_trip():
    """The structural half. A refusal on `-2` is a backstop; the reason
    `-2` cannot be seen beside a live record is that both come from one
    `execute()`. Two separate awaits reopen the window with every test
    above still green, which is the shape of a guard that passes by not
    looking."""
    source = inspect.getsource(
        __import__("gateway.auth", fromlist=["read_run_token"]).read_run_token
    )
    tree = ast.parse(source.strip())
    awaited = [
        node.value.func
        for node in ast.walk(tree)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
    ]
    names = [f.attr for f in awaited if isinstance(f, ast.Attribute)]
    assert "ttl" not in names and "get" not in names, (
        "read_run_token awaits redis.get/redis.ttl directly, so the record "
        f"and its expiry are two round trips again: {names}"
    )
    assert "execute" in names, (
        "read_run_token no longer awaits a pipeline execute(); the record "
        f"and its expiry must come back together: {names}"
    )


def _redis_answering(raw, ttl):
    """A stand-in for `auth._redis` that answers one pipeline."""

    class _Pipe:
        def get(self, _key):
            return self

        def ttl(self, _key):
            return self

        async def execute(self):
            return [raw, ttl]

    class _Redis:
        def pipeline(self, transaction=True):
            return _Pipe()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    async def _factory():
        return _Redis()

    return _factory
