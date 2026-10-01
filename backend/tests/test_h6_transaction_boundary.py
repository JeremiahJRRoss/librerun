"""Gap H6 (blueprint S1): a write is visible before its response is sent.

Every handler flushes and returns; the commit used to happen in
``get_db``'s teardown, which on the FastAPI the image resolves runs
*after* the response has left. A client acting on a response the
instant it arrives could then miss the write — the smoke saw a
sign-in's session row missing (``401 Session not found``) four times in
twenty-one runs, and a logout's ``revoked_at`` had the same window.

These tests drive the REAL app (``app.main``) against a REAL PostgreSQL
and observe the database from a second session at the exact moment the
response starts leaving the ASGI app — before its first byte, which is
the boundary's contract. Each direction is proven both ways:

* **the fix works** — the session row is committed, ``revoked_at`` is
  set, and the revoked token is refused on reuse;
* **the probe bites** — with the middleware's settle step bypassed the
  same probe sees the pre-fix world (nothing committed yet), which is
  what makes the passing tests evidence rather than a tautology.

Needs a database at ``DATABASE_URL`` with the schema loaded. Without one
the module skips — unless ``LIBRERUN_REQUIRE_DB=1`` (set by the
``database-parity`` workflow), which turns the skip into a failure so a
job that exists to run these cannot pass by running none of them.
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text

REQUIRE_DB = os.environ.get("LIBRERUN_REQUIRE_DB", "").strip().lower() in {"1", "true", "yes"}


@pytest_asyncio.fixture
async def db_engine():
    from app.database import engine

    try:
        # Any pooled connection belongs to an earlier test's event loop;
        # start clean, then prove the database is really there.
        try:
            await engine.dispose()
        except Exception:
            pass
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        reason = f"no database at DATABASE_URL ({type(e).__name__}: {e})"
        if REQUIRE_DB:
            pytest.fail("LIBRERUN_REQUIRE_DB is set, so this may not skip: " + reason)
        pytest.skip(reason)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def throwaway_user(db_engine):
    """A credentials user in the platform tenant, removed afterwards."""
    from app.config import settings
    from app.database import async_session
    from app.models import ActivityAuditLog, Tenant, User
    from app.services.auth_service import hash_password

    email = f"h6-{uuid.uuid4().hex}@example.com"
    password = "H6-probe-" + uuid.uuid4().hex
    async with async_session() as db:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.slug == settings.PLATFORM_TENANT_SLUG))
        ).scalar_one()
        user = User(
            tenant_id=tenant.id,
            email=email,
            password_hash=hash_password(password),
            auth_provider="credentials",
            display_name="H6 probe",
            role="customer",
        )
        db.add(user)
        await db.commit()
        user_id = user.id
    try:
        yield SimpleNamespace(id=user_id, email=email, password=password)
    finally:
        async with async_session() as db:
            await db.execute(delete(ActivityAuditLog).where(ActivityAuditLog.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))  # sessions cascade
            await db.commit()


@pytest.fixture
def librerun_app(db_engine):
    from app.main import app

    return app


class ResponseProbe:
    """Wraps the whole ASGI app and runs ``observe`` the instant the
    response's status line is about to leave (``http.response.start``,
    before it is forwarded) — the fix's contract is "committed before
    the first byte leaves", and this is that byte.

    Why this instant, and not the end of the body: the app's outermost
    user middleware is a ``BaseHTTPMiddleware``, which runs the request
    in its own task and re-streams the response through a zero-buffer
    channel. When the outer start message reaches this probe that inner
    task is parked at its first body send, waiting for the channel, so
    it cannot have reached ``get_db``'s teardown yet. That makes the
    observation deterministic in BOTH worlds: with the boundary the
    commit precedes the inner start; without it nothing has committed.
    Observing at the end of the body would race the inner task's
    teardown — the same race the smoke lost four times in twenty-one
    runs — and a flaky negative control proves nothing."""

    def __init__(self, app, observe):
        self.app = app
        self.observe = observe
        self.observations: list = []

    async def __call__(self, scope, receive, send):
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                self.observations.append(await self.observe())
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def _session_rows_for(user_id) -> int:
    """From a SECOND session: how many session rows this user has."""
    from app.database import async_session
    from app.models import Session as SessionRow

    async with async_session() as db:
        return (
            await db.execute(
                select(func.count()).select_from(SessionRow).where(SessionRow.user_id == user_id)
            )
        ).scalar_one()


async def _revoked(session_id) -> bool:
    from app.database import async_session
    from app.models import Session as SessionRow

    async with async_session() as db:
        row = await db.get(SessionRow, session_id)
        return row is not None and row.revoked_at is not None


async def _sign_in(app, user) -> str:
    async with _client(app) as client:
        r = await client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": user.password}
        )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _session_id(token) -> uuid.UUID:
    from app.services.auth_service import decode_jwt

    return uuid.UUID(decode_jwt(token)["session_id"])


@pytest.fixture
def settle_bypassed(monkeypatch):
    """The pre-fix world: nothing settles before the response. Used only
    to prove the probes can see the bug they guard against."""
    from app.middleware_transaction import TransactionBoundaryMiddleware

    async def _late(sessions, status):
        return None

    monkeypatch.setattr(TransactionBoundaryMiddleware, "settle", staticmethod(_late))


@pytest.mark.asyncio
async def test_sign_in_session_row_is_committed_before_the_response_leaves(
    throwaway_user, librerun_app
):
    probe = ResponseProbe(librerun_app, lambda: _session_rows_for(throwaway_user.id))
    async with _client(probe) as client:
        r = await client.post(
            "/api/v1/auth/login",
            json={"email": throwaway_user.email, "password": throwaway_user.password},
        )
    assert r.status_code == 200, r.text
    # The row was visible to another connection before the response's
    # first byte left — a fast client's next request cannot miss it.
    assert probe.observations == [1]

    token = r.json()["access_token"]
    async with _client(librerun_app) as client:
        me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    assert me.json()["email"] == throwaway_user.email


@pytest.mark.asyncio
async def test_logout_revocation_is_committed_before_the_response_leaves(
    throwaway_user, librerun_app
):
    token = await _sign_in(librerun_app, throwaway_user)
    sid = _session_id(token)
    headers = {"Authorization": f"Bearer {token}"}

    probe = ResponseProbe(librerun_app, lambda: _revoked(sid))
    async with _client(probe) as client:
        r = await client.post("/api/v1/auth/logout", headers=headers)
    assert r.status_code == 204, r.text
    assert probe.observations == [True]

    # Reusing the token the instant logout returned is refused.
    async with _client(librerun_app) as client:
        me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 401, me.text
    assert me.json()["detail"] == "Session revoked"


@pytest.mark.asyncio
async def test_the_probe_sees_the_late_commit_when_settle_is_bypassed(
    settle_bypassed, throwaway_user, librerun_app
):
    """Negative control. With the boundary's settle step bypassed the
    commit falls back to get_db's teardown — after the response — and
    the very same probes see exactly that: no row yet at sign-in, not
    revoked yet at logout. If this test ever passes with the bypass
    removed, the probes have stopped observing the right instant."""
    probe = ResponseProbe(librerun_app, lambda: _session_rows_for(throwaway_user.id))
    async with _client(probe) as client:
        r = await client.post(
            "/api/v1/auth/login",
            json={"email": throwaway_user.email, "password": throwaway_user.password},
        )
    assert r.status_code == 200, r.text
    assert probe.observations == [0], "the pre-fix window is gone — is the probe still looking?"
    # ...and the teardown commit still lands it afterwards (no lost writes).
    assert await _session_rows_for(throwaway_user.id) == 1

    token = r.json()["access_token"]
    sid = _session_id(token)
    probe = ResponseProbe(librerun_app, lambda: _revoked(sid))
    async with _client(probe) as client:
        r = await client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 204, r.text
    assert probe.observations == [False]
    assert await _revoked(sid) is True
