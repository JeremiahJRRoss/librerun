"""The gateway's database access — reads, and one narrow write.

It shares the platform's PostgreSQL rather than owning its own: the
three tables migration 014 creates are written by the backend (manifest
snapshots, tenants' step overrides) and by this process (agent keys),
and there is exactly one truth about which agent a key belongs to.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from gateway.config import settings

_engine = None
_sessionmaker: async_sessionmaker | None = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.DATABASE_URL.get_secret_value(),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


def sessionmaker() -> async_sessionmaker:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _sessionmaker


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Commits on a clean return so the one write a
    request makes — ``last_used_at`` on the presented key — lands.

    The commit is unconditional, including after a route has already
    called ``release``. I first guarded it with ``if
    session.in_transaction()``, on the reasoning that committing a
    released session would check a connection back OUT of the pool after
    the provider call. Measured, it does not: a commit with nothing
    pending takes no connection and the pool stays at zero. The guard
    prevented nothing, and its comment described a behaviour that does
    not exist — so it is gone rather than kept as reassurance.
    """
    async with sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def release(session: AsyncSession) -> None:
    """Finish this request's database work NOW, not when it responds.

    FastAPI holds a yielded session for the whole lifetime of the
    request, streaming responses included. Every model-call route does
    all of its database work up front — authenticate, resolve the step
    — and then never touches the session again, so without this the
    transaction stayed open across the provider call (Codex P1). Two
    costs, and the first is the serious one:

    - ``keys.touch`` UPDATEs the presented key's row, so its ROW LOCK
      was held until the response finished. Every call using one agent
      key touches one row, so concurrent calls from an agent serialized
      behind the slowest model call in flight;
    - the connection stayed checked out of a pool of ``5 + 10``, so
      fifteen slow calls were enough to make the sixteenth wait on the
      database for a request that has no database work left to do.

    The commit is the whole of it. It ends the transaction, which drops
    the lock AND returns the connection to the pool — I had ``close()``
    here too until a negative test showed it changed nothing either way,
    and a line that does no work while its docstring implies it does is
    worse than no line. ``get_db`` closes the session on its way out.

    The session object stays valid afterwards — ``expire_on_commit`` is
    false, and everything a route carries past this point (the
    principal's snapshot, the resolved step) is a plain dataclass built
    from a row rather than an ORM object with a lazy relationship, so
    nothing it reads needs a connection.
    """
    await session.commit()


async def dispose() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
