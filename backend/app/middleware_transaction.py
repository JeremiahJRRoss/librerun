"""The transaction boundary: commit before the response starts (gap H6).

Pure ASGI on purpose. ``BaseHTTPMiddleware`` runs the inner app in a
separate task and re-streams its response, which would put the commit on
the wrong side of ``http.response.start`` again; intercepting ``send``
directly is the only place that is unambiguously "after the handler
returned, before the first byte leaves".

Order of events for one request:

1. ``__call__`` binds an empty session list to ``scope["state"]``.
2. ``get_db`` (``app.database``) appends every session it opens.
3. The handler returns; the response object starts sending.
4. On ``http.response.start`` the sessions are *settled*: committed for
   a 2xx/3xx status, rolled back for 4xx/5xx (an error response records
   nothing — the handler raised, FastAPI's handler built the response,
   and the rollback ``get_db`` already did on the exception path is
   repeated harmlessly). Only then is the start message forwarded.
5. A commit that fails raises *before* the start message is forwarded,
   so ``ServerErrorMiddleware`` sends a 500: the client never sees a 2xx
   for a write that did not happen.
6. An exception escaping the app rolls the sessions back and re-raises.
7. ``get_db``'s teardown commits again after the body — real work only
   for writes made after the response started (streaming, background).

The incident record, gap H6 in the development record: four smoke failures in twenty-one runs, all ``401 Session not
found`` on the request after sign-in.
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.database import DB_SESSIONS_STATE_KEY

logger = logging.getLogger(__name__)


class TransactionBoundaryMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        sessions: list[AsyncSession] = []
        state = scope.get("state")
        if not isinstance(state, dict):
            state = scope["state"] = {}
        state[DB_SESSIONS_STATE_KEY] = sessions

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                await self.settle(sessions, int(message["status"]))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException:
            await self.abandon(sessions)
            raise

    @staticmethod
    async def settle(sessions: list[AsyncSession], status: int) -> None:
        """Commit (2xx/3xx) or roll back (4xx/5xx) every bound session.

        A commit error propagates: the start message has not been sent
        yet, so the failure becomes the response instead of trailing it.
        """
        for session in sessions:
            if status < 400:
                await session.commit()
            else:
                await session.rollback()

    @staticmethod
    async def abandon(sessions: list[AsyncSession]) -> None:
        """Roll back on the way out of an exception; never mask it."""
        for session in sessions:
            try:
                await session.rollback()
            except Exception as e:  # the session's own teardown closes it
                logger.warning(
                    "db_rollback_failed error_type=%s error=%s", type(e).__name__, e
                )
