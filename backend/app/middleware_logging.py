"""Per-request context middleware.

Responsibilities:
    - Assign each request a ``request_id`` and expose it on
      ``request.state`` + the response header ``X-Request-ID``.
    - Bind ``request_id``, ``method``, and ``path`` into
      ``structlog.contextvars`` for the request's lifetime. Subsequent
      ``get_current_user`` calls add ``tenant_id`` / ``user_id`` on top.
    - Does *not* emit an access log line. Uvicorn's own ``uvicorn.access``
      logger remains the source of truth and is reshaped into JSON via
      ``logging_formatters.reshape_uvicorn_access_record``.

Why we don't ``clear_contextvars()`` in ``finally``:
    Uvicorn's access log fires *after* this middleware returns, while the
    ASGI response body is flushed — still inside the same asyncio task.
    Clearing before that would strip ``request_id`` / ``tenant_id`` from
    the access line. Each incoming request runs in its own task, so
    task-local contextvars are GC'd when the task completes. FastAPI
    ``BackgroundTasks`` run inside the same task and intentionally inherit
    the context (so pipeline background jobs see ``tenant_id`` etc.).
"""
from __future__ import annotations

from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.logging_context import bind_request_context


class LoggingContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = uuid4().hex
        request.state.request_id = request_id
        bind_request_context(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
