"""The transaction boundary (gap H6), without a database.

``TransactionBoundaryMiddleware`` settles the request's DB sessions on
``http.response.start``: commit for 2xx/3xx, rollback for 4xx/5xx or an
escaping exception, and a failed commit must surface as the response
rather than trail it. These tests drive the real ``get_db`` and the real
middleware through a minimal FastAPI app with a recording fake in place
of the SQLAlchemy session, and pin the ORDER of events against the ASGI
messages — the property the fix exists for. The database-backed proof
(a second connection reading the row the instant the response leaves)
is ``test_h6_transaction_boundary.py``.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, HTTPException, Response
from starlette.testclient import TestClient

import app.database as database
from app.database import DB_SESSIONS_STATE_KEY, bound_sessions, get_db
from app.middleware_transaction import TransactionBoundaryMiddleware


class FakeSession:
    """Records commit/rollback/close in order; can be told to fail commit."""

    def __init__(self, fail_commit: bool = False):
        self.calls: list[str] = []
        self.fail_commit = fail_commit

    async def commit(self):
        self.calls.append("commit")
        if self.fail_commit:
            raise RuntimeError("commit exploded")

    async def rollback(self):
        self.calls.append("rollback")

    async def close(self):
        self.calls.append("close")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


class Recorder:
    """Outermost ASGI wrapper: snapshots the fake's call log at the moment
    each response message is forwarded, so a test can say what had
    happened *before* the first byte left."""

    def __init__(self, app, fakes: list[FakeSession]):
        self.app = app
        self.fakes = fakes
        self.events: list[tuple] = []

    async def __call__(self, scope, receive, send):
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                self.events.append(
                    ("start", message["status"], [list(f.calls) for f in self.fakes])
                )
            await send(message)

        await self.app(scope, receive, send_wrapper)


@pytest.fixture
def fakes(monkeypatch):
    """Replace the session factory with one that hands out recording fakes."""
    made: list[FakeSession] = []
    plan = {"fail_commit": False}

    def factory():
        s = FakeSession(fail_commit=plan["fail_commit"])
        made.append(s)
        return s

    monkeypatch.setattr(database, "async_session", factory)
    made_plan = (made, plan)
    return made_plan


def _build(with_middleware: bool = True) -> FastAPI:
    app = FastAPI()
    if with_middleware:
        app.add_middleware(TransactionBoundaryMiddleware)

    @app.get("/ok")
    async def ok(db=Depends(get_db)):
        return {"bound": bound_sessions_len(db)}

    @app.get("/raises")
    async def raises(db=Depends(get_db)):
        raise HTTPException(409, "state says no")

    @app.get("/returns-4xx")
    async def returns_4xx(db=Depends(get_db)):
        return Response(status_code=404)

    @app.get("/explodes")
    async def explodes(db=Depends(get_db)):
        raise RuntimeError("handler exploded")

    return app


def bound_sessions_len(db) -> int:
    # Only meaningful through the recorder; kept trivial on purpose.
    return 1 if db is not None else 0


def test_success_commits_before_the_start_message_is_forwarded(fakes):
    made, _ = fakes
    rec = Recorder(_build(), made)
    with TestClient(rec) as client:
        r = client.get("/ok")
    assert r.status_code == 200
    assert len(made) == 1, "one session per request"
    # At the moment http.response.start was forwarded the session had
    # ALREADY committed — that is the whole fix.
    assert rec.events == [("start", 200, [["commit"]])]
    # The teardown's commit still runs afterwards (a streaming body or a
    # background task may have written more), then the session closes.
    assert made[0].calls == ["commit", "commit", "close"]


def test_http_exception_rolls_back_and_the_error_response_commits_nothing(fakes):
    made, _ = fakes
    rec = Recorder(_build(), made)
    with TestClient(rec) as client:
        r = client.get("/raises")
    assert r.status_code == 409
    # get_db's own exception path rolled back first (the request stack
    # unwinds before FastAPI builds the 409), and the boundary rolls back
    # again on the 4xx start rather than committing.
    assert "commit" not in made[0].calls
    assert rec.events[0][1] == 409
    assert "rollback" in rec.events[0][2][0]


def test_a_returned_error_status_is_rolled_back_not_committed(fakes):
    made, _ = fakes
    rec = Recorder(_build(), made)
    with TestClient(rec) as client:
        r = client.get("/returns-4xx")
    assert r.status_code == 404
    assert rec.events == [("start", 404, [["rollback"]])]


def test_unhandled_exception_rolls_back_and_still_surfaces_as_500(fakes):
    made, _ = fakes
    rec = Recorder(_build(), made)
    with TestClient(rec, raise_server_exceptions=False) as client:
        r = client.get("/explodes")
    assert r.status_code == 500
    assert "commit" not in made[0].calls
    assert "rollback" in made[0].calls
    # The only start message the recorder saw is ServerErrorMiddleware's
    # 500, sent OUTSIDE the boundary, and by then the session had been
    # rolled back (get_db's exception path, then the boundary's own).
    assert [status for (_, status, _) in rec.events] == [500]
    assert "commit" not in rec.events[0][2][0]


def test_a_failing_commit_becomes_the_response_instead_of_trailing_it(fakes):
    made, plan = fakes
    plan["fail_commit"] = True
    rec = Recorder(_build(), made)
    with TestClient(rec, raise_server_exceptions=False) as client:
        r = client.get("/ok")
    # Never a 2xx for a write that did not happen: the handler's 200 start
    # was never forwarded; the only start that left is the 500 built
    # outside the boundary, after the failed commit and the rollback.
    assert r.status_code == 500
    assert [status for (_, status, _) in rec.events] == [500]
    assert made[0].calls[0] == "commit"
    assert "rollback" in made[0].calls


def test_without_the_middleware_get_db_still_commits_in_teardown(fakes):
    """A bare app (no boundary) must not LOSE writes: the old late commit
    stays as the fallback. This is the degraded mode, not the contract."""
    made, _ = fakes
    rec = Recorder(_build(with_middleware=False), made)
    with TestClient(rec) as client:
        r = client.get("/ok")
    assert r.status_code == 200
    assert rec.events == [("start", 200, [[]])]  # nothing settled before the start...
    assert made[0].calls == ["commit", "close"]  # ...only the teardown commit


def test_non_http_scopes_pass_straight_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    mw = TransactionBoundaryMiddleware(inner)

    import asyncio

    asyncio.run(mw({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_the_binding_lives_on_scope_state_under_the_documented_key():
    captured = {}

    async def inner(scope, receive, send):
        captured["state"] = scope["state"]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        pass

    import asyncio

    asyncio.run(TransactionBoundaryMiddleware(inner)({"type": "http"}, None, send))
    assert captured["state"][DB_SESSIONS_STATE_KEY] == []
    assert bound_sessions({"type": "http", "state": captured["state"]}) == []
    assert bound_sessions({"type": "http"}) is None


def test_the_production_app_mounts_the_boundary_innermost():
    """The chassis app, not a fixture: the boundary must be present, and it
    must be the innermost user middleware so it sees the router's own
    ``http.response.start`` rather than a re-streamed copy."""
    from app.main import app as librerun

    classes = [m.cls for m in librerun.user_middleware]
    assert TransactionBoundaryMiddleware in classes
    # user_middleware is stored outermost-first; the innermost is last.
    assert classes[-1] is TransactionBoundaryMiddleware
