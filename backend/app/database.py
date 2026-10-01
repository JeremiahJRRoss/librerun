"""Engine, session factory, and the request-bound ``get_db`` dependency.

Transaction boundary (gap H6, blueprint S1)
-------------------------------------------
Handlers flush and return; the commit was the job of this ``yield``
dependency's teardown. On the FastAPI the image resolves, a
request-scoped dependency's exit code runs *after the response has been
sent*, so a client acting on a response the instant it arrived could
beat the commit: a sign-in's session row was not there yet
(``401 Session not found`` on the very next request) and a logout's
``revoked_at`` was not visible yet, so the token still worked.

The boundary is central now, not per handler. ``get_db`` binds every
session it opens to the request (``scope["state"]``) and
``app.middleware_transaction.TransactionBoundaryMiddleware`` commits
them the moment the response starts — before the first byte leaves the
process — rolling back on an exception or an error response. The
teardown below still commits, so whatever a streaming body or a
background task writes through the same session *after* the response
started is kept, and an app that mounts the routers without the
middleware (a bare test harness) degrades to the old late commit rather
than losing writes.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from starlette.requests import Request

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.DATABASE_URL.get_secret_value(),
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Key under the ASGI ``scope["state"]`` where the middleware keeps the list
# of sessions bound to the request. ``Request.state`` wraps that same dict,
# so ``request.state.librerun_db_sessions`` reads it too.
DB_SESSIONS_STATE_KEY = "librerun_db_sessions"


def bound_sessions(scope) -> list[AsyncSession] | None:
    """The request's bound sessions, or ``None`` outside the middleware."""
    state = scope.get("state")
    if not isinstance(state, dict):
        return None
    sessions = state.get(DB_SESSIONS_STATE_KEY)
    return sessions if isinstance(sessions, list) else None


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, committed before the response leaves.

    Dependency caching means every ``Depends(get_db)`` in one request —
    the auth dependency's and the handler's — shares this session, so
    one commit settles the whole request.
    """
    async with async_session() as session:
        registry = bound_sessions(request.scope)
        if registry is not None:
            registry.append(session)
        try:
            yield session
            # Normally a no-op (the middleware already committed at
            # ``http.response.start``); real work only for writes made
            # after the response started — a streaming body, a
            # background task — or outside the middleware entirely.
            await session.commit()
        except Exception:
            await session.rollback()
            raise
