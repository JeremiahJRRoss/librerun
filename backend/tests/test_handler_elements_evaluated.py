"""Route handlers whose comprehension bodies no test ever entered.

`scripts/element_expression_coverage.py` mutates every element
expression in `backend/app/routers` and asks whether the suite notices.
On a tree of 1274 green tests it found these: the line holding each
comprehension ran, and the expression inside it never did, so the
helpers they call could be deleted and the suite would stay green. That
is not a hypothetical — `_step` WAS deleted from `get_progress`, every
run with progress 500'd, and 1232 tests passed (§12 207(v), gap H12).

Each case here drives its handler with a NON-EMPTY collection and
asserts on what comes back, and is paired with the empty case where the
empty case says something. The pairing is the lesson rather than a
convention: an absence test on its own certifies the path it never
enters.

The fakes are deliberately thin. What is under test is that the body
RUNS and renders each row, so a fake session that returns rows is
enough; where a handler's SQL carries a rule — tenant scoping,
ownership — `test_handlers_with_rows.py` and
`test_list_runs_executed.py` are where that is asserted, the second
against a real PostgreSQL. Recorded as gap H14.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from app.database import get_db
from app.middleware import get_current_user, require_admin, require_platform_admin
from app.routers import admin as admin_router
from app.routers import files as files_router


def _user(role: str = "admin"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="operator@example.com",
        role=role,
        tenant_id=uuid.uuid4(),
        is_platform_admin=True,
    )


def _client(router, *, db=None, user=None) -> TestClient:
    user = user or _user()
    app = FastAPI()
    app.include_router(router.router)

    async def _db_dep():
        yield db

    async def _user_dep(request: Request):
        request.state.tenant_id = user.tenant_id
        request.state.current_user = user
        request.state.session_id = uuid.uuid4()
        return user

    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep
    app.dependency_overrides[require_admin] = _user_dep
    app.dependency_overrides[require_platform_admin] = _user_dep
    return TestClient(app)


class _Result:
    """One `db.execute(...)` result, in whichever shape its caller reads."""

    def __init__(self, rows, scalar=None):
        self._rows = rows
        self._scalar = scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._scalar


class _ScriptedDB:
    """Hands back one prepared result per `execute`, in order.

    A handler that runs two queries reads them differently — a count
    through `scalar_one`, rows through `scalars().all()` — so a single
    canned result cannot serve both, and a fake that returned the same
    thing twice would pass the handler rows where it expected a number
    and never reach the comprehension at all.
    """

    def __init__(self, *results):
        self._results = list(results)
        self.calls = 0

    async def execute(self, *_args, **_kwargs):
        self.calls += 1
        if not self._results:
            raise AssertionError("the handler ran more queries than this fake prepared")
        return self._results.pop(0)


# --------------------------------------------------------------------------
# GET /admin/users
# --------------------------------------------------------------------------


def _user_row(email: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        display_name=None,
        role="customer",
        auth_provider="local",
        is_active=True,
        last_sign_in=None,
    )


def test_list_users_renders_a_row_for_every_user():
    rows = [_user_row("a@example.com"), _user_row("b@example.com")]
    db = _ScriptedDB(_Result(rows))

    body = _client(admin_router, db=db).get("/admin/users").json()

    assert [entry["email"] for entry in body] == ["a@example.com", "b@example.com"]
    assert all(entry["role"] == "customer" for entry in body)


def test_list_users_with_no_users_is_an_empty_list():
    db = _ScriptedDB(_Result([]))

    r = _client(admin_router, db=db).get("/admin/users")

    assert r.status_code == 200, r.text
    assert r.json() == []


# --------------------------------------------------------------------------
# GET /admin/feedback
# --------------------------------------------------------------------------


def _feedback_row(comment: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        section_type="analysis",
        comment=comment,
        trace_id="0af7651916cd43dd8448eb211c80319c",
        created_at=datetime.now(timezone.utc),
    )


def test_feedback_aggregate_renders_every_recent_negative():
    grouped = [("analysis", "positive", 3), ("analysis", "negative", 2)]
    recent = [_feedback_row("wrong vendor"), _feedback_row("missed the log")]
    db = _ScriptedDB(_Result(grouped), _Result(recent))

    body = _client(admin_router, db=db).get("/admin/feedback").json()

    assert body["total"] == 5
    assert [n["comment"] for n in body["recent_negatives"]] == [
        "wrong vendor",
        "missed the log",
    ]
    # Every field the element expression builds, each against the SOURCE
    # ROW. The first version of these lines asserted `run_id ==
    # case_id`, which is equally true of a renderer that fills both from
    # the feedback row's own id — every link then points at a run that
    # does not exist, and the case that exists to catch a wrong
    # rendering passes (Codex round 1). The fixture gives `id` and
    # `run_id` different values so the two cannot be confused.
    first = body["recent_negatives"][0]
    assert first["id"] == str(recent[0].id)
    assert first["run_id"] == str(recent[0].run_id)
    assert first["case_id"] == str(recent[0].run_id)
    assert first["id"] != first["run_id"]
    assert first["section_type"] == "analysis"
    assert first["trace_id"] == "0af7651916cd43dd8448eb211c80319c"
    assert first["created_at"].startswith(str(recent[0].created_at.year))


def test_feedback_aggregate_with_no_feedback_reports_zero():
    db = _ScriptedDB(_Result([]), _Result([]))

    body = _client(admin_router, db=db).get("/admin/feedback").json()

    assert body["total"] == 0
    assert body["positive_rate"] == 0.0
    assert body["recent_negatives"] == []


# --------------------------------------------------------------------------
# GET /admin/audit-log
# --------------------------------------------------------------------------


def _audit_row(action_type: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        action_type=action_type,
        user_email="operator@example.com",
        detail={"run_id": "7"},
        ip_address="203.0.113.7",
        created_at=datetime.now(timezone.utc),
    )


def test_list_audit_log_renders_an_entry_for_every_row():
    rows = [_audit_row("run_submitted"), _audit_row("llm_schema_drift")]
    db = _ScriptedDB(_Result(None, scalar=2), _Result(rows))

    body = _client(admin_router, db=db).get("/admin/audit-log").json()

    assert body["total"] == 2
    assert [e["action_type"] for e in body["entries"]] == [
        "run_submitted",
        "llm_schema_drift",
    ]
    # `from_row` stringifies the address and `model_dump(mode="json")`
    # turns the timestamp into text; both happen INSIDE the element
    # expression, so both are unobserved when it never evaluates.
    assert body["entries"][0]["ip_address"] == "203.0.113.7"
    assert isinstance(body["entries"][0]["created_at"], str)


def test_list_audit_log_with_no_rows_is_empty():
    db = _ScriptedDB(_Result(None, scalar=0), _Result([]))

    body = _client(admin_router, db=db).get("/admin/audit-log").json()

    assert body == {"entries": [], "total": 0}


# --------------------------------------------------------------------------
# GET /admin/agent-keys
# --------------------------------------------------------------------------


def _key_row(agent_id: str, source: str, *, issued_by=None):
    return {
        "agent_id": agent_id,
        "key_prefix": "lrk_abc",
        "source": source,
        "role": "current",
        "issued_at": datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        "issued_by": issued_by,
        "previous_since": None,
        "previous_until": None,
        "last_used_at": None,
    }


def test_list_agent_keys_renders_a_row_for_every_key(monkeypatch):
    issuer = uuid.uuid4()
    rows = [_key_row("vita-v1", "admin", issued_by=issuer), _key_row("echo", "env")]

    async def _list_keys(_db, _agent_id=None):
        return rows

    monkeypatch.setattr(admin_router.agent_key_service, "list_keys", _list_keys)

    body = _client(admin_router).get("/admin/agent-keys").json()

    assert [k["agent_id"] for k in body] == ["vita-v1", "echo"]
    # Each row goes through the typed row (K9-08), so a datetime renders
    # as the schema renders it, and every other value passes through.
    assert body[0]["issued_at"] == "2026-09-01T12:00:00Z"
    assert body[0]["key_prefix"] == "lrk_abc"
    assert body[0]["last_used_at"] is None
    # What the admin page is actually asking: an env key is rotated in
    # `.env`, not here, so it must not be offered a button.
    assert body[0]["rotatable"] is True
    assert body[1]["rotatable"] is False
    assert body[0]["issued_by"] == str(issuer)
    assert body[1]["issued_by"] is None


def test_list_agent_keys_with_none_installed_is_empty(monkeypatch):
    async def _list_keys(_db, _agent_id=None):
        return []

    monkeypatch.setattr(admin_router.agent_key_service, "list_keys", _list_keys)

    r = _client(admin_router).get("/admin/agent-keys")

    assert r.status_code == 200, r.text
    assert r.json() == []


# --------------------------------------------------------------------------
# GET /runs/{run_id}/files
# --------------------------------------------------------------------------


class _RunFilesDB(_ScriptedDB):
    def __init__(self, run, files):
        super().__init__(_Result(files))
        self._run = run

    async def get(self, _model, _pk):
        return self._run


def _file_row(name: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        vendor_side="a",
        file_type="log",
        original_name=name,
        file_size_bytes=1234,
        uploaded_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )


def test_list_run_files_renders_a_row_for_every_file():
    user = _user(role="customer")
    run = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=user.tenant_id, user_id=user.id, deleted_at=None
    )
    files = [_file_row("switch.log"), _file_row("controller.log")]
    db = _RunFilesDB(run, files)

    body = _client(files_router, db=db, user=user).get(f"/runs/{run.id}/files").json()

    assert [f["original_name"] for f in body] == ["switch.log", "controller.log"]
    assert body[0]["file_size_bytes"] == 1234
    assert body[0]["vendor_side"] == "a"
    # `uploaded_at.isoformat()` is inside the element expression, so an
    # empty listing never proves a timestamp can be rendered at all.
    assert body[0]["uploaded_at"] == files[0].uploaded_at.isoformat()


def test_list_run_files_with_no_uploads_is_empty():
    user = _user(role="customer")
    run = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=user.tenant_id, user_id=user.id, deleted_at=None
    )
    db = _RunFilesDB(run, [])

    r = _client(files_router, db=db, user=user).get(f"/runs/{run.id}/files")

    assert r.status_code == 200, r.text
    assert r.json() == []
