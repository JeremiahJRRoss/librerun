"""The request's database work ends before the model call does.

FastAPI holds a yielded session for the whole lifetime of a request,
streaming responses included. Every model-call route does all of its
database work up front — authenticate, resolve the step — and then never
touches the session again, so the transaction stayed open across the
provider call (Codex round 16, P1). Two costs:

- `keys.touch` UPDATEs the presented key's row, so its ROW LOCK was held
  until the response finished. One agent key is one row, so concurrent
  calls from an agent serialized behind the slowest model call in
  flight — a correctness-shaped performance bug, since the second call
  waits on the first for reasons that have nothing to do with it;
- the connection stayed checked out of a pool of 5 + 10, so fifteen slow
  calls made the sixteenth wait on a database it has no work for.

Both halves are checked: that `release` really returns the connection
and really commits, and that the routes still call it before egress.
"""
from __future__ import annotations

import ast
import inspect

import pytest
import pytest_asyncio

from conftest import _skip_or_fail


@pytest_asyncio.fixture
async def engine_and_session():
    """The gateway's OWN engine — these tests assert on its pool — on a
    fresh event loop each time.

    `db.engine()` is a module singleton bound to whichever loop first
    used it, and pytest-asyncio hands every test a new one, so without
    the disposals below the second test in this file fails with a
    loop-affinity RuntimeError.

    The first version of this fixture caught that as `except Exception`
    and called `pytest.skip("no database")`. The suite stayed green with
    one of its guards not running at all — the exact failure this batch
    keeps finding, committed in the fixture of a test written to catch
    it. So the rescue is narrow: a DBAPI or socket error means the
    database is absent (and `LIBRERUN_REQUIRE_DB` still turns that into
    a failure, as everywhere else in this suite); anything else is a
    real fault and is raised.
    """
    import sqlalchemy.exc
    from sqlalchemy import text

    from gateway import db

    await db.dispose()
    session = db.sessionmaker()()
    try:
        await session.execute(text("SELECT 1"))
    except (sqlalchemy.exc.DBAPIError, OSError) as exc:
        await session.close()
        await db.dispose()
        _skip_or_fail(f"no database at DATABASE_URL ({type(exc).__name__}: {exc})")
    # The probe above checked a connection out. Give it back, or every
    # test in this file starts one short and reads the pool wrong.
    await session.rollback()
    assert db.engine().pool.checkedout() == 0, "the fixture leaked its probe"
    yield db.engine(), session
    await session.close()
    await db.dispose()


@pytest.mark.asyncio
async def test_release_returns_the_connection_to_the_pool(engine_and_session):
    from sqlalchemy import text

    from gateway import db

    engine, session = engine_and_session
    await session.execute(text("SELECT 1"))
    assert engine.pool.checkedout() >= 1, "a query should hold a connection"

    await db.release(session)
    assert engine.pool.checkedout() == 0, (
        "release() left the connection checked out, so the provider call "
        "still runs with one of the pool's fifteen"
    )


@pytest.mark.asyncio
async def test_the_dependency_does_not_take_one_back_after_release(
    engine_and_session,
):
    """The property the fix depends on: once a route has released, the
    dependency's own teardown must not take a connection back.

    Two assertions, and they are not the same. The first catches a
    `release` that commits without closing — the connection would stay
    checked out for the whole provider call and the fix would do
    nothing. The second is the teardown itself; it holds today because
    a commit with nothing pending is a no-op, which I measured rather
    than assumed after a guard I had written for it turned out to
    prevent nothing.

    The real dependency is driven here rather than its logic restated:
    a test that reimplements `get_db` inline proves its own copy sound
    and says nothing about the code that runs."""
    from sqlalchemy import text

    from gateway import db

    from sqlalchemy import event

    engine, _unused = engine_and_session

    dependency = db.get_db()
    session = await dependency.__anext__()
    await session.execute(text("SELECT 1"))
    await db.release(session)
    assert engine.pool.checkedout() == 0, "release() did not free the connection"

    # Counted, not sampled. Reading `checkedout()` after the teardown
    # misses a teardown that takes a connection and gives it back — it
    # would show zero either way, and a query after the provider call is
    # exactly the round trip this fix removes.
    taken = []
    pool = engine.sync_engine.pool

    def _took(*_args):
        taken.append(1)

    event.listen(pool, "checkout", _took)
    try:
        with pytest.raises(StopAsyncIteration):
            await dependency.__anext__()  # the route returned; the dependency ends
    finally:
        event.remove(pool, "checkout", _took)
    assert not taken, (
        "get_db took a connection on its way out, so every request pays a "
        "database round trip after its model call has already finished"
    )
    assert engine.pool.checkedout() == 0, "the teardown left a connection out"


@pytest.mark.asyncio
async def test_release_commits_rather_than_discarding_the_write(
    engine_and_session,
):
    """The one write a request makes — `last_used_at` on the presented
    key — must still land. A release that rolled back would lose it
    silently, which is worse than holding the lock."""
    import uuid

    from sqlalchemy import text

    from gateway import db, keys

    engine, session = engine_and_session
    agent_id = f"release-test-{uuid.uuid4().hex[:8]}"
    key_hash = keys.hash_key(keys.mint_key())
    await session.execute(
        text(
            "INSERT INTO agent_keys (agent_id, key_hash, key_prefix, role, "
            "source) VALUES (:a, :h, 'aaaaaaaa', 'current', 'env')"
        ),
        {"a": agent_id, "h": key_hash},
    )
    await keys.touch(session, key_hash)
    await db.release(session)

    probe = db.sessionmaker()()
    try:
        landed = (
            await probe.execute(
                text("SELECT last_used_at FROM agent_keys WHERE key_hash = :h"),
                {"h": key_hash},
            )
        ).scalar_one_or_none()
        assert landed is not None, "release() did not commit the touch"
        await probe.execute(
            text("DELETE FROM agent_keys WHERE key_hash = :h"), {"h": key_hash}
        )
        await probe.commit()
    finally:
        await probe.close()


def test_every_model_call_route_releases_before_it_calls_a_provider():
    """The structural half. The behavioural tests above prove `release`
    works; this proves the routes still use it — and that nothing has
    been added below it that needs the session again, which would make
    the release a rollback of work the route still expects."""
    from gateway import main

    tree = ast.parse(inspect.getsource(main))
    routes = {"chat_completions", "embeddings"}
    seen = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name not in routes:
            continue
        seen.add(fn.name)

        released_at = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "release"
        ]
        assert released_at, f"{fn.name} never releases its database session"
        released = min(released_at)

        egress_at = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "egress"
        ]
        for call_at in egress_at:
            assert released < call_at, (
                f"{fn.name} calls egress at line {call_at} before releasing "
                f"its session at {released}"
            )

        used_after = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Name)
            and n.id == "session"
            and n.lineno > released
        ]
        assert not used_after, (
            f"{fn.name} uses `session` after releasing it (lines "
            f"{used_after}); the release has to be the last database line"
        )
    assert seen == routes, f"routes missing from this check: {routes - seen}"
