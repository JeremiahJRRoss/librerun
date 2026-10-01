"""The run hashes expire, and one function is how they get written.

Three Redis hashes are keyed by a run — ``run:{id}:progress``,
``run:{id}:step_models`` and ``run:{id}:kv`` — and nothing deletes any of
them when a run finishes: the only delete is a RE-RUN's
``orchestrator.reset_progress``. Measured before this batch, all three
were ``ttl=-1``: immortal.

The fix is one chokepoint, ``run_boundary.run_hash_write``, which writes
the field and sets the key's expiry in one transaction. The guards below
are deliberately of two kinds, because each catches what the other
cannot:

* **behaviour** — a write really does leave a bounded TTL on a real
  Redis, and really does spend one round trip rather than two;
* **structure** — an AST rule that no production module writes a
  ``run:``-shaped hash any other way, because a census of today's three
  writers is a list of candidates rather than a rule (§12 162(b)), and
  the fourth writer someone adds next year is the one that matters.
"""
from __future__ import annotations

import ast
import pathlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app import redis as app_redis
from app.database import get_db
from app.middleware import get_current_user
from app.routers import runs as runs_router
from app.services import run_boundary

REPO = pathlib.Path(__file__).resolve().parents[2]
PRODUCTION = [REPO / "backend" / "app", REPO / "services" / "gateway" / "gateway"]


def _modules():
    for root in PRODUCTION:
        for path in sorted(root.rglob("*.py")):
            yield path, ast.parse(path.read_text())


def _template(node: ast.JoinedStr) -> str:
    """An f-string with its placeholders blanked, so
    ``f"run:{run_id}:kv"`` reads as ``run:{}:kv`` whatever the variable
    is called."""
    out = []
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            out.append(part.value)
        else:
            out.append("{}")
    return "".join(out)


# Every way redis-py writes a hash field. Spelled out rather than
# matched on a prefix, because a rule that catches only the spelling the
# defect happened to use is a rule about that defect and not about the
# hazard.
HASH_WRITES = {"hset", "hmset", "hsetnx"}

FIX_IT = (
    "Route the write through app.services.run_boundary.run_hash_write, "
    "which sets the key's expiry in the same transaction. If this is a "
    "hash that is NOT keyed by a run and so should not expire with one, "
    "that is a deliberate exception: add it here BY NAME with the reason, "
    "rather than relaxing the rule."
)


def _hash_writes(tree):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in HASH_WRITES
        ):
            yield node


def test_one_function_writes_every_run_hash():
    """A hash-field write appears once in production, in ``run_hash_write``.

    Not "the three writers call the helper" — that is a census, and a
    census cannot speak about the writer that does not exist yet. The
    rule is that the CALL does not appear anywhere else, so a new module
    reaching for ``redis.hset`` on a run hash fails this test rather than
    quietly reintroducing ``ttl=-1``.
    """
    found = [
        f"{path.relative_to(REPO)}:{node.lineno}"
        for path, tree in _modules()
        for node in _hash_writes(tree)
    ]
    assert len(found) == 1, f"{found}\n{FIX_IT}"
    where = found[0]
    assert where.startswith("backend/app/services/run_boundary.py"), (
        f"{where}\n{FIX_IT}"
    )

    # ...and it really is inside the helper, not merely in that file.
    tree = ast.parse((REPO / "backend/app/services/run_boundary.py").read_text())
    helper = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_hash_write"
    )
    assert len(list(_hash_writes(helper))) == 1, FIX_IT


def test_every_run_hash_key_is_spelled_in_one_module():
    """``run:{}:...`` appears only in ``run_boundary``'s key helpers.

    A re-spelled key is the same defect as a second writer wearing
    different clothes: ``orchestrator`` carried its own
    ``f"run:{run_id}:progress"`` and ``capabilities`` its own
    ``f"run:{run_id}:kv"`` while the shared helpers existed. Two copies
    of a key name is how one of them quietly stops matching.
    """
    spellings: dict[str, list[str]] = {}
    for path, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            template = _template(node)
            # A run HASH is `run:{}:something`; `run:{}` alone is a tag
            # (a log field, a trace attribute) and is not this rule's
            # business.
            if template.startswith("run:{}:"):
                spellings.setdefault(template, []).append(
                    f"{path.relative_to(REPO)}:{node.lineno}"
                )
    where = "backend/app/services/run_boundary.py"
    known = {"run:{}:progress", "run:{}:kv", "run:{}:step_models"}
    assert set(spellings) == known, (
        f"{sorted(spellings)}\nA FOURTH run hash is not forbidden — give it a "
        f"key helper in {where}, make sure it is written through "
        f"run_hash_write so it expires with the run, and name it here."
    )
    for template, sites in sorted(spellings.items()):
        assert len(sites) == 1 and sites[0].startswith(where), (
            f"{template} is spelled at {sites}; it belongs in {where} and "
            f"nowhere else. Two copies of a key name is how one of them "
            f"quietly stops matching."
        )


class _CountingPipeline:
    def __init__(self, parent: "_CountingRedis") -> None:
        self._parent = parent
        self._queued: list = []

    def hset(self, key, field, value):
        self._queued.append(("hset", key, field, value))
        return self

    def expire(self, key, ttl):
        self._queued.append(("expire", key, ttl))
        return self

    async def execute(self) -> list:
        self._parent.round_trips += 1
        self._parent.commands.extend(self._queued)
        replies = [1 if c[0] == "hset" else True for c in self._queued]
        self._queued = []
        return replies


class _CountingRedis:
    """Counts ROUND TRIPS, which is the thing the change is about.

    A double that only records the final state cannot tell one
    transaction from two awaits — both leave the same hash and the same
    ttl. This one counts the awaits instead.
    """

    def __init__(self) -> None:
        self.round_trips = 0
        self.commands: list = []

    def pipeline(self, transaction: bool = False):
        assert transaction
        return _CountingPipeline(self)

    async def hset(self, key, field, value):
        self.round_trips += 1
        self.commands.append(("hset", key, field, value))

    async def expire(self, key, ttl):
        self.round_trips += 1
        self.commands.append(("expire", key, ttl))


@pytest.mark.asyncio
async def test_a_write_and_its_expiry_are_one_round_trip():
    redis = _CountingRedis()
    await run_boundary.run_hash_write(redis, "run:x:progress", "step", "{}")
    assert redis.round_trips == 1, redis.commands
    assert [c[0] for c in redis.commands] == ["hset", "expire"], redis.commands
    assert redis.commands[1][2] == run_boundary.RUN_KEY_TTL_SECONDS, redis.commands


@pytest.mark.asyncio
async def test_the_key_really_carries_a_bounded_ttl_on_real_redis():
    """Read back from Redis itself, not from a double.

    The double this suite already had for the run store answered
    ``expire`` with ``pass``: against it a dropped expiry can never fail
    a test, however production changes (§12 185(a)). So this one asks the
    server, the way ``test_otlp_relay`` asks it about run tokens.
    """
    import redis.asyncio as aioredis

    from app.config import settings

    key = "run:ttl-probe:progress"
    async with aioredis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True) as client:
        await client.delete(key)
        try:
            await run_boundary.run_hash_write(client, key, "step", "{}")
            ttl = await client.ttl(key)
            # NOT `-1`, which is the leak; and no larger than the
            # constant, which is what bounds it.
            assert 0 < ttl <= run_boundary.RUN_KEY_TTL_SECONDS, ttl
            # A second write slides the window rather than letting the
            # first one run out under a live run.
            await run_boundary.run_hash_write(client, key, "step2", "{}")
            assert await client.ttl(key) >= ttl - 1
        finally:
            await client.delete(key)


# ---------------------------------------------------------------------------
# The CONSEQUENCE of the fix, not its constant.
#
# Adding a TTL where there was none CREATES a failure mode rather than
# inheriting one: seven days after its last progress write, a run parked
# on the human gate loses `run:{id}:progress` and `run:{id}:step_models`
# while the run itself is untouched in Postgres. So the claim that makes
# the TTL safe has to be TESTED, not asserted in a comment: the run still
# reads and is still approvable, and the progress endpoint degrades to an
# empty step list instead of a 500.
#
# The key is really expired here — `pexpire(1)` and a wait — rather than
# deleted or served from an emptied double, because "expired" and "never
# written" have to be the same thing to every reader for the claim to
# hold.
# ---------------------------------------------------------------------------


def _parked_run_app(monkeypatch, run_id, tenant_id, user_id):
    """The runs router over a fake DB holding one parked run.

    Redis is NOT faked: the endpoint's ``get_redis`` reaches the same
    server the write above used, which is the whole point — the question
    is what the real read path does when the key is gone.

    Driven over ``ASGITransport`` rather than ``TestClient`` so the
    request runs on THIS test's event loop. ``app.redis`` caches one
    client in a module global; a ``TestClient`` portal has a loop of its
    own, so the endpoint would reuse a connection bound to whichever loop
    first created it and fail with "attached to a different loop" —
    passing alone and failing in the suite, which is the worst way for a
    test to be wrong.
    """
    run = SimpleNamespace(
        id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        run_number="C-000001",
        status="awaiting_approval",
        current_phase="analyze",
        deleted_at=None,
        agent_id="parked-v1",
    )
    user = SimpleNamespace(
        id=user_id, tenant_id=tenant_id, email="u@example.com", role="customer"
    )

    resumed: list = []

    async def _resume(*args):
        resumed.append(args)

    monkeypatch.setattr(runs_router.agent_runner, "resume_run", _resume)

    async def _log_audit(*args, **kwargs):  # noqa: ARG001
        return None

    monkeypatch.setattr(runs_router, "log_audit", _log_audit)

    db = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    async def _get(_model, pk):
        return run if pk == run_id else None

    db.get = _get

    async def _db_dep():
        yield db

    async def _user_dep(request: Request):
        request.state.tenant_id = tenant_id
        request.state.current_user = user
        request.state.session_id = uuid.uuid4()
        return user

    app = FastAPI()
    app.include_router(runs_router.router)
    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep
    client = AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    )
    return client, run, resumed


@pytest.mark.asyncio
async def test_a_parked_run_outlives_its_progress_key():
    import asyncio

    import redis.asyncio as aioredis

    from app.config import settings

    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    progress = run_boundary.progress_key(run_id)
    models = run_boundary.step_models_key(run_id)

    async with aioredis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True) as client:
        try:
            await run_boundary.progress_write(client, run_id, "validate", "complete")
            await run_boundary.run_hash_write(client, models, "validate", "gpt-4o")
            assert await client.exists(progress) == 1

            # Seven days later, compressed: the same end state the TTL
            # produces, reached the same way.
            await client.pexpire(progress, 1)
            await client.pexpire(models, 1)
            for _ in range(50):
                if not await client.exists(progress, models):
                    break
                await asyncio.sleep(0.02)
            assert await client.exists(progress, models) == 0, "key did not expire"
        finally:
            await client.delete(progress, models)

    saved = app_redis.redis_client
    # Force the endpoint to build its client on THIS loop, then put the
    # module back exactly as it was found.
    app_redis.redis_client = None
    try:
        with pytest.MonkeyPatch.context() as monkeypatch:
            http, run, resumed = _parked_run_app(
                monkeypatch, run_id, tenant_id, user_id
            )
            async with http:
                # 200 and an empty list — NOT a 500, and not a 404
                # either: the run is alive, it simply has no steps to
                # report.
                r = await http.get(f"/runs/{run_id}/progress")
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["steps"] == [], body
                assert body["phase_name"] == "analyze", body

                # And the gate still opens. Approval reads the run row,
                # never Redis, which is what makes the expiry cosmetic.
                r = await http.post(f"/runs/{run_id}/approve")
                assert r.status_code == 202, r.text
                assert r.json()["status"] == "investigating"
            assert run.status == "investigating"
            assert len(resumed) == 1, resumed
    finally:
        await app_redis.close_redis()
        app_redis.redis_client = saved


@pytest.mark.asyncio
async def test_a_run_with_progress_rows_renders_them():
    """The endpoint's ORDINARY path — a run that has progress.

    The parked-run test above asserts the EMPTY case, and an empty hash
    means the row comprehension never evaluates its element expression.
    So a `_step` that does not exist at all still passes it, and did:
    a revert deleted the builder, `GET /runs/{id}/progress` raised
    `NameError` for every real run, and the whole suite stayed green
    (Codex P1).

    A test for the absence of something must be paired with one for its
    presence, or it certifies the code path it never enters.
    """
    import redis.asyncio as aioredis

    from app.config import settings

    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    progress = run_boundary.progress_key(run_id)
    models = run_boundary.step_models_key(run_id)

    async with aioredis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True) as client:
        try:
            await run_boundary.progress_write(
                client, run_id, "validate", "complete", duration_ms=12
            )
            await run_boundary.progress_write(client, run_id, "gather", "running")
            await run_boundary.run_hash_write(client, models, "validate", "gpt-4o-mini")

            saved = app_redis.redis_client
            app_redis.redis_client = None
            try:
                with pytest.MonkeyPatch.context() as monkeypatch:
                    http, _run, _resumed = _parked_run_app(
                        monkeypatch, run_id, tenant_id, user_id
                    )
                    async with http:
                        r = await http.get(f"/runs/{run_id}/progress")
                        assert r.status_code == 200, r.text
                        body = r.json()
            finally:
                await app_redis.close_redis()
                app_redis.redis_client = saved
        finally:
            await client.delete(progress, models)

    rows = {s["step_id"]: s for s in body["steps"]}
    assert set(rows) == {"validate", "gather"}, rows
    assert rows["validate"]["status"] == "complete"
    assert rows["validate"]["duration_ms"] == 12
    # The exact-id join still works; it is only the namespaced case that
    # cannot be attributed (gap E5).
    assert rows["validate"]["model"] == "gpt-4o-mini"
    assert rows["gather"]["status"] == "running"
    assert rows["gather"]["model"] is None
