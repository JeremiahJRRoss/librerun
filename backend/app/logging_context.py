"""Thin wrappers over ``structlog.contextvars`` for binding per-request fields.

The request middleware calls ``bind_request_context`` at request entry;
``get_current_user`` calls it again once the tenant/user is resolved. Any
subsequent ``structlog`` log call in the same task sees the merged fields
via the ``merge_contextvars`` processor.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from uuid import UUID

import structlog


# ---------------------------------------------------------------------------
# Telemetry planes (Agents_Design.md "Observability contract")
#
# ``run``      — this task is EXECUTING an agent run: the runner has bound
#                ``agent_id`` (and friends) around the phase, so everything
#                from here down — orchestrator steps, agent code, LLM SDK
#                calls — is agent work. This plane may carry LLM prompt /
#                completion content.
# ``platform`` — everything else: HTTP serving (including requests *about*
#                runs — progress polls, run reads), auth, startup, health.
#
# One predicate feeds both the ``librerun_scope`` log field and the
# ``librerun.scope`` span attribute so the two signals can never disagree
# about which plane an event belongs to.
# ---------------------------------------------------------------------------

SCOPE_RUN = "run"
SCOPE_PLATFORM = "platform"


def current_scope() -> str:
    """Return the telemetry plane of the current task's bound context."""
    ctx = structlog.contextvars.get_contextvars()
    return SCOPE_RUN if ctx.get("agent_id") else SCOPE_PLATFORM


# The platform plane binds opaque ids only (blueprint S4, gap H10): the
# user's email is never a context field — it would ride every log line
# of an authenticated request into whatever vendor S7a ships them to.
FORBIDDEN_CONTEXT_FIELDS = frozenset({"user_email", "email"})


def _refuse_forbidden(kwargs: dict[str, Any]) -> None:
    forbidden = FORBIDDEN_CONTEXT_FIELDS.intersection(kwargs)
    if forbidden:
        raise ValueError(
            f"{sorted(forbidden)} may not be bound as log context: the "
            "platform plane carries opaque ids only (tenant_id, user_id, "
            "session_id, request_id)"
        )


def bind_request_context(**kwargs: Any) -> None:
    """Bind one or more fields onto the current contextvars scope.

    Typical keys: ``tenant_id``, ``user_id``, ``session_id``,
    ``request_id``, ``method``, ``path`` — opaque ids and route facts.
    ``user_email`` is refused (S4): identity yes, PII no.
    """
    _refuse_forbidden(kwargs)
    structlog.contextvars.bind_contextvars(**kwargs)


def bind_run_context(run_id: UUID | str) -> None:
    """Bind the active run id for the remainder of this task."""
    structlog.contextvars.bind_contextvars(run_id=str(run_id))


def clear_context() -> None:
    """Drop all bound contextvars. Call in ``finally`` at request end."""
    structlog.contextvars.clear_contextvars()


@contextmanager
def log_context(**kwargs: Any):
    """Scoped bind/unbind — useful for background tasks.

    Usage::

        with log_context(run_id=str(run_id), tenant_id=str(tenant_id)):
            ...
    """
    _refuse_forbidden(kwargs)
    with structlog.contextvars.bound_contextvars(**kwargs):
        yield
