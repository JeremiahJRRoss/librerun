"""The platform plane binds opaque ids only (blueprint S4, gap H10):
``user_email`` is no longer a bound context field — binding it fails —
and the request middleware binds ``tenant_id`` / ``user_id`` alone.
"""
from __future__ import annotations

import pytest
import structlog

from fastapi import Depends

from app.logging_context import bind_request_context, log_context

# The queue-only pipeline against a temp JSONL file (see the module it
# comes from): the platform plane's records are what this asserts on.
from tests.test_logging_queue import queue_log  # noqa: F401


def test_binding_user_email_is_refused():
    with pytest.raises(ValueError, match="user_email"):
        bind_request_context(tenant_id="t", user_email="x@y.z")
    with pytest.raises(ValueError, match="user_email"):
        with log_context(user_email="x@y.z"):
            pass
    assert "user_email" not in structlog.contextvars.get_contextvars()


def test_opaque_ids_still_bind():
    bind_request_context(tenant_id="t", user_id="u", session_id="s", request_id="r")
    ctx = structlog.contextvars.get_contextvars()
    assert ctx["tenant_id"] == "t" and ctx["user_id"] == "u"
    assert "user_email" not in ctx


def test_the_request_middleware_binds_no_email():
    """Read the binding call rather than drive the auth stack: the fields
    the middleware binds are the contract, and the refusal above is what
    makes a regression fail loudly at runtime."""
    import inspect

    from app import middleware

    source = inspect.getsource(middleware.get_current_user)
    assert "user_email" not in source
    assert "bind_request_context(" in source


# ------------------------------------------------- the sequence, for real --


def test_a_signed_in_user_whose_email_is_the_fixture_leaves_it_nowhere(queue_log, monkeypatch):
    """The sequence, not an inspection of it.

    ``test_the_request_middleware_binds_no_email`` reads the source,
    which the test itself admits. Source reading cannot see what the
    auth stack binds on the way past — a session lookup, a user load, an
    admin gate — nor what a span would carry, so the sequence is driven
    here: sign in, make an authenticated request, open an admin page, as
    a user whose address IS the fixture. Every record the platform plane
    wrote, and every attribute a span would carry from the same bound
    context, must be free of it.
    """
    import json
    import uuid
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    import structlog
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from app import middleware
    from app.models import User
    from app.observability.span_enricher import _CONTEXTVAR_TO_ATTRIBUTE

    from tests.test_logging_queue import FIXTURE_EMAIL, _records

    tenant_id, user_id, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    user = User(id=user_id, tenant_id=tenant_id, role="admin", email=FIXTURE_EMAIL)
    user.is_active = True
    session = SimpleNamespace(
        id=session_id,
        user_id=user_id,
        revoked_at=None,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    class _DB:
        async def get(self, model, pk):
            return user if model is User else None

    monkeypatch.setattr(middleware, "decode_jwt", lambda token: {"session_id": str(session_id)})

    async def _get_session(db, sid):  # noqa: ARG001
        return session

    monkeypatch.setattr(middleware, "get_session", _get_session)

    seen: list[dict] = []
    app = FastAPI()

    async def _db():
        return _DB()

    @app.get("/runs")
    async def _runs(current: User = Depends(middleware.get_current_user)):
        seen.append(dict(structlog.contextvars.get_contextvars()))
        structlog.get_logger("app.routers.runs").info("listed runs")
        return {"ok": True}

    @app.get("/admin/page")
    async def _admin(current: User = Depends(middleware.require_admin)):
        seen.append(dict(structlog.contextvars.get_contextvars()))
        structlog.get_logger("app.routers.admin").info("opened the admin page")
        return {"ok": True}

    app.dependency_overrides[middleware.get_db] = _db

    with TestClient(app) as client:
        # Sign-in: the address is the credential, and goes no further.
        bind_request_context(request_id=uuid.uuid4().hex)
        structlog.get_logger("app.routers.auth").info("login_attempt", email_domain="example.com")
        assert client.get("/runs", headers={"Authorization": "Bearer t"}).status_code == 200
        assert client.get("/admin/page", headers={"Authorization": "Bearer t"}).status_code == 200

    assert len(seen) == 2, "the authenticated routes did not run"

    # 1. The log plane.
    text = json.dumps(_records(queue_log))
    assert FIXTURE_EMAIL not in text, "a platform-plane record carries the address"
    assert "listed runs" in text and "opened the admin page" in text

    # 2. The bound context, which is what a span is stamped from.
    for context in seen:
        assert context["user_id"] == str(user_id)
        assert context["tenant_id"] == str(tenant_id)
        assert FIXTURE_EMAIL not in json.dumps(context)
        # Every attribute the enricher would stamp, from this context.
        stamped = {
            attribute: context[key]
            for key, attributes in _CONTEXTVAR_TO_ATTRIBUTE.items()
            if context.get(key) not in (None, "")
            for attribute in ((attributes,) if isinstance(attributes, str) else attributes)
        }
        assert FIXTURE_EMAIL not in json.dumps(stamped), stamped
        assert stamped["user.id"] == str(user_id)
