"""Boot reconciliation (blueprint S7, gap H16): a run the backend was
restarted under says so.

A phase runs as a ``BackgroundTasks`` job and dies with the process.
Nothing revisited a run left ``refining`` or ``investigating``, so a
restart mid-phase left a run that said it was running forever, with no
deadline in force. At ``lifespan`` the chassis now marks every such run
``error`` with a stated reason (``backend restarted during phase ...``),
one audit row and one platform-plane log line each, and the run page
offers "Run again". Durable resume is v1.1.

Three layers, each against something real:

1. the predicate, on the real database (a transaction rolled back after):
   which rows are orphans and which are not — parked runs, finished runs,
   deleted runs, and a run written AFTER this boot are left alone;
2. the boot itself — ``app.main`` entered through its real lifespan in a
   subprocess, against rows committed for it, polled until the
   transition lands; the audit rows and the log lines read back;
3. the structure — the lifespan starts the retry and cancels it, and the
   retry keeps trying until the database answers.

Skips without a database, unless ``LIBRERUN_REQUIRE_DB=1`` (CI), where a
skip is a failure.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import main as app_main
from app.services import agent_runner, run_errors

BACKEND_DIR = Path(__file__).resolve().parents[1]
REQUIRE_DB = os.environ.get("LIBRERUN_REQUIRE_DB", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


def _dsn() -> str:
    from app.config import settings

    return settings.DATABASE_URL.get_secret_value()


async def _probe_or_skip(engine) -> None:
    try:
        async with engine.connect() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        reason = f"no database at DATABASE_URL ({type(exc).__name__}: {exc})"
        if REQUIRE_DB:
            pytest.fail("LIBRERUN_REQUIRE_DB is set, so this may not skip: " + reason)
        pytest.skip(reason)


@pytest_asyncio.fixture
async def db():
    """A session in an open transaction, rolled back after."""
    engine = create_async_engine(_dsn())
    await _probe_or_skip(engine)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


_INSERT_TENANT = text("INSERT INTO tenants (id, name, slug) VALUES (:id, :n, :s)")
_INSERT_USER = text(
    "INSERT INTO users (id, tenant_id, email, auth_provider, role)"
    " VALUES (:id, :t, :e, 'google', 'customer')"
)
_INSERT_RUN = text(
    "INSERT INTO runs (id, tenant_id, user_id, run_number, status, agent_id,"
    " current_phase, user_inputs, deleted_at, updated_at)"
    " VALUES (:id, :t, :u, :n, :s, 'probe-v1', :phase, '{}'::jsonb, :deleted,"
    " COALESCE(:updated, NOW()))"
)


async def _world(session) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        _INSERT_TENANT, {"id": tenant_id, "n": "S7", "s": f"s7-{tenant_id.hex[:12]}"}
    )
    await session.execute(
        _INSERT_USER,
        {"id": user_id, "t": tenant_id, "e": f"{user_id.hex[:10]}@example.com"},
    )
    return tenant_id, user_id


async def _run(
    session,
    tenant_id,
    user_id,
    *,
    status: str,
    phase: str | None = "investigate",
    deleted: bool = False,
    updated: datetime | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    await session.execute(
        _INSERT_RUN,
        {
            "id": run_id,
            "t": tenant_id,
            "u": user_id,
            "n": f"RUN-{run_id.int % 100000}",
            "s": status,
            "phase": phase,
            "deleted": datetime.now(timezone.utc) if deleted else None,
            "updated": updated,
        },
    )
    return run_id


async def _state(session, run_id) -> dict:
    row = (
        await session.execute(
            text("SELECT status, error_code, error_detail FROM runs WHERE id = :id"),
            {"id": run_id},
        )
    ).one()
    return {"status": row.status, "error_code": row.error_code, "error_detail": row.error_detail}


async def _audit_rows(session, tenant_id) -> list[dict]:
    rows = (
        await session.execute(
            text(
                "SELECT action_type, user_id, user_email, detail FROM activity_audit_log"
                " WHERE tenant_id = :t ORDER BY created_at"
            ),
            {"t": tenant_id},
        )
    ).all()
    return [
        {"action_type": r.action_type, "user_id": r.user_id, "user_email": r.user_email, "detail": r.detail}
        for r in rows
    ]


# ---------------------------------------------------------- the predicate --


@pytest.mark.asyncio
async def test_runs_left_mid_phase_become_error_with_the_reason(db, log_file):
    tenant_id, user_id = await _world(db)
    investigating = await _run(db, tenant_id, user_id, status="investigating", phase="investigate")
    refining = await _run(db, tenant_id, user_id, status="refining", phase="analyze")
    submitted = await _run(db, tenant_id, user_id, status="submitted", phase=None)
    boot = datetime.now(timezone.utc) + timedelta(seconds=1)

    reconciled = await agent_runner.reconcile_orphaned_runs(db, boot_started_at=boot)

    assert {r["run_id"] for r in reconciled} == {
        str(investigating),
        str(refining),
        str(submitted),
    }
    assert await _state(db, investigating) == {
        "status": "error",
        "error_code": run_errors.BACKEND_RESTARTED,
        "error_detail": "backend restarted during phase 'investigate'",
    }
    assert await _state(db, refining) == {
        "status": "error",
        "error_code": run_errors.BACKEND_RESTARTED,
        "error_detail": "backend restarted during phase 'analyze'",
    }
    # A row with no phase cursor never reached its first phase.
    assert (await _state(db, submitted))["error_detail"] == (
        "backend restarted before the first phase started"
    )
    # The customer page has a sentence for the code, not the code.
    assert "Run it again" in run_errors.user_message(run_errors.BACKEND_RESTARTED)

    # One audit row each: a system event (no user, no address), under an
    # action the schema's CHECK admits, naming the transition.
    rows = await _audit_rows(db, tenant_id)
    assert len(rows) == 3
    for row in rows:
        assert row["action_type"] == "run_update"
        assert row["user_id"] is None and row["user_email"] is None
        assert row["detail"]["action"] == "boot_reconcile"
        assert row["detail"]["reason"].startswith("backend restarted")
    assert {r["detail"]["run_id"] for r in rows} == {
        str(investigating),
        str(refining),
        str(submitted),
    }
    assert {r["detail"]["previous_status"] for r in rows} == {
        "investigating",
        "refining",
        "submitted",
    }

    # One PLATFORM-plane log line each. The plane is decided by whether
    # ``agent_id`` is bound; the chassis is speaking about a run it is
    # not executing, so the agent is named under another key and the
    # line stays on the platform plane.
    lines = [
        json.loads(line)
        for line in log_file.read_text().splitlines()
        if line.strip()
    ]
    orphaned = [l for l in lines if l.get("event") == "run_orphaned_by_restart"]
    assert {l["run_id"] for l in orphaned} == {
        str(investigating),
        str(refining),
        str(submitted),
    }
    for line in orphaned:
        assert line["librerun_scope"] == "platform"
        assert line["agent"] == "probe-v1"
        assert "agent_id" not in line


@pytest.mark.asyncio
async def test_parked_finished_deleted_and_post_boot_runs_are_left_alone(db):
    """The four kinds of non-orphan: a run parked at a gate has no
    invocation in flight and waits on a human by design; a finished run
    is finished; a deleted run is nobody's; and a run written AFTER this
    boot is one this process is driving — which matters because the
    reconcile runs in the background and may succeed minutes after boot
    when the database was late."""
    tenant_id, user_id = await _world(db)
    boot = datetime.now(timezone.utc) + timedelta(seconds=1)
    parked = await _run(db, tenant_id, user_id, status="awaiting_approval", phase="analyze")
    complete = await _run(db, tenant_id, user_id, status="complete")
    errored = await _run(db, tenant_id, user_id, status="error")
    deleted = await _run(db, tenant_id, user_id, status="investigating", deleted=True)
    live = await _run(
        db,
        tenant_id,
        user_id,
        status="investigating",
        updated=boot + timedelta(minutes=5),
    )
    orphan = await _run(db, tenant_id, user_id, status="investigating")

    reconciled = await agent_runner.reconcile_orphaned_runs(db, boot_started_at=boot)

    assert [r["run_id"] for r in reconciled] == [str(orphan)]
    assert (await _state(db, parked))["status"] == "awaiting_approval"
    assert (await _state(db, complete))["status"] == "complete"
    assert (await _state(db, errored)) == {"status": "error", "error_code": None, "error_detail": None}
    assert (await _state(db, deleted))["status"] == "investigating"
    assert (await _state(db, live)) == {
        "status": "investigating",
        "error_code": None,
        "error_detail": None,
    }
    assert len(await _audit_rows(db, tenant_id)) == 1


@pytest.mark.asyncio
async def test_a_second_pass_finds_nothing(db):
    tenant_id, user_id = await _world(db)
    await _run(db, tenant_id, user_id, status="refining", phase="analyze")
    boot = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert len(await agent_runner.reconcile_orphaned_runs(db, boot_started_at=boot)) == 1
    assert await agent_runner.reconcile_orphaned_runs(db, boot_started_at=boot) == []
    assert len(await _audit_rows(db, tenant_id)) == 1


# ----------------------------------------------------------- the restart --


_PROBE = r'''
import asyncio, json, os, sys, time, uuid
import asyncpg
from starlette.testclient import TestClient

from app.config import settings
from app.main import app

IDS = json.loads(os.environ["LR_PROBE_RUN_IDS"])
DSN = settings.DATABASE_URL.get_secret_value().replace("postgresql+asyncpg://", "postgresql://")


async def states():
    # A connection of its own on a loop of its own: the app's engine is
    # bound to the lifespan's loop in TestClient's thread.
    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            "SELECT id, status, error_code, error_detail FROM runs WHERE id = ANY($1::uuid[])",
            [uuid.UUID(i) for i in IDS],
        )
    finally:
        await conn.close()
    return {str(r["id"]): dict(status=r["status"], error_code=r["error_code"], error_detail=r["error_detail"]) for r in rows}


with TestClient(app) as client:
    assert client.get("/api/v1/health").status_code == 200
    deadline = time.time() + 90
    snap = {}
    while time.time() < deadline:
        snap = asyncio.run(states())
        # The reconcile is a background retry; wait for it to have visited
        # the two orphans rather than for a fixed time.
        if all(snap[i]["status"] == "error" for i in IDS[:2]):
            break
        time.sleep(0.5)
print("PROBE " + json.dumps(snap))
'''


@pytest.mark.asyncio
async def test_a_restart_marks_the_runs_it_orphaned_through_the_real_lifespan(tmp_path):
    """The Accept item, end to end: a run parked mid-phase, the app
    restarted (a fresh process entering ``app.main``'s real lifespan),
    and the run reads ``error`` with its reason after boot — while the
    run parked at a gate beside it, and the run touched after the boot,
    do not."""
    engine = create_async_engine(_dsn())
    await _probe_or_skip(engine)
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    # Committed for real: the subprocess has to see them. Removed at the
    # end (the tenant cascades to users, runs and audit rows).
    async with engine.begin() as conn:
        await conn.execute(_INSERT_TENANT, {"id": tenant_id, "n": "S7 restart", "s": f"s7r-{tenant_id.hex[:12]}"})
        await conn.execute(_INSERT_USER, {"id": user_id, "t": tenant_id, "e": f"{user_id.hex[:10]}@example.com"})
        orphan_investigating = uuid.uuid4()
        orphan_refining = uuid.uuid4()
        parked = uuid.uuid4()
        touched_after_boot = uuid.uuid4()
        for run_id, status, phase, updated in (
            (orphan_investigating, "investigating", "investigate", None),
            (orphan_refining, "refining", "analyze", None),
            (parked, "awaiting_approval", "analyze", None),
            # Written "after this boot": a future timestamp stands in for
            # the run this process would be driving by the time a late
            # database let the reconcile succeed.
            (touched_after_boot, "investigating", "investigate", datetime.now(timezone.utc) + timedelta(hours=1)),
        ):
            await conn.execute(
                _INSERT_RUN,
                {
                    "id": run_id,
                    "t": tenant_id,
                    "u": user_id,
                    "n": f"RUN-{run_id.int % 100000}",
                    "s": status,
                    "phase": phase,
                    "deleted": None,
                    "updated": updated,
                },
            )
    log_path = tmp_path / "restart.jsonl"
    ids = [str(orphan_investigating), str(orphan_refining), str(parked), str(touched_after_boot)]
    env = {
        **os.environ,
        "APP_SECRET_KEY": uuid.uuid4().hex,
        "LOG_FILE_PATH": str(log_path),
        "LOG_STDERR_ENABLED": "false",
        "LR_PROBE_RUN_IDS": json.dumps(ids),
    }
    try:
        out = subprocess.run(
            [sys.executable, "-c", _PROBE],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert out.returncode == 0, f"the chassis did not boot:\n{out.stderr[-4000:]}"
        marker = [l for l in out.stdout.splitlines() if l.startswith("PROBE ")]
        assert marker, out.stdout[-2000:]
        after = json.loads(marker[-1][len("PROBE "):])

        assert after[str(orphan_investigating)] == {
            "status": "error",
            "error_code": run_errors.BACKEND_RESTARTED,
            "error_detail": "backend restarted during phase 'investigate'",
        }
        assert after[str(orphan_refining)] == {
            "status": "error",
            "error_code": run_errors.BACKEND_RESTARTED,
            "error_detail": "backend restarted during phase 'analyze'",
        }
        assert after[str(parked)]["status"] == "awaiting_approval"
        assert after[str(touched_after_boot)] == {
            "status": "investigating",
            "error_code": None,
            "error_detail": None,
        }

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT detail FROM activity_audit_log WHERE tenant_id = :t"
                        " AND action_type = 'run_update'"
                    ),
                    {"t": tenant_id},
                )
            ).all()
        details = [r.detail for r in rows if r.detail.get("action") == "boot_reconcile"]
        assert {d["run_id"] for d in details} == {str(orphan_investigating), str(orphan_refining)}

        lines = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        orphaned = [l for l in lines if l.get("event") == "run_orphaned_by_restart"]
        assert {l["run_id"] for l in orphaned} == {str(orphan_investigating), str(orphan_refining)}
        assert all(l["librerun_scope"] == "platform" for l in orphaned)
        done = [l for l in lines if l.get("event") == "orphaned_runs_reconciled"]
        assert done and done[-1]["count"] == 2
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        await engine.dispose()


# ---------------------------------------------------------- the structure --


@pytest.mark.asyncio
async def test_the_orphan_reconcile_retries_until_the_database_answers(monkeypatch):
    attempts = {"n": 0}

    async def flaky(_session, *, boot_started_at):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("the database is not up yet")
        return []

    monkeypatch.setattr(agent_runner, "reconcile_orphaned_runs", flaky)
    monkeypatch.setattr(app_main, "RECONCILE_RETRY_SECONDS", 0.01)

    await asyncio.wait_for(
        app_main._reconcile_orphans_until_done(datetime.now(timezone.utc)), timeout=5
    )
    assert attempts["n"] == 3, "gave up before the database recovered"


@pytest.mark.asyncio
async def test_the_orphan_reconcile_can_be_cancelled(monkeypatch):
    async def never(_session, *, boot_started_at):
        raise RuntimeError("still down")

    monkeypatch.setattr(agent_runner, "reconcile_orphaned_runs", never)
    monkeypatch.setattr(app_main, "RECONCILE_RETRY_SECONDS", 0.01)

    task = asyncio.create_task(
        app_main._reconcile_orphans_until_done(datetime.now(timezone.utc))
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)


def test_the_lifespan_starts_the_orphan_reconcile_and_stops_it():
    """The structural half, in the shape of the manifest reconcile's own
    guard: a reconcile nothing starts is the defect unchanged, and one
    nothing cancels blocks shutdown. Removing the ``create_task`` line
    from the lifespan turns this red — and the restart test above red
    with it, which is the same violation seen end to end."""
    tree = ast.parse(inspect.getsource(app_main.lifespan).strip())
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_reconcile_orphans_until_done" in calls, (
        "the lifespan no longer starts the orphaned-run reconciliation"
    )
    source = inspect.getsource(app_main.lifespan)
    assert source.count("cancel()") >= 2, (
        "the lifespan cancels fewer tasks than it starts, so shutdown waits "
        "on a loop that runs forever"
    )
    # The boot timestamp is captured BEFORE discovery and the detector
    # warm-up, so every run this process drives is written after it.
    boot_at = source.index("boot_started_at = datetime.now")
    assert boot_at < source.index("discover_agents()")
    assert boot_at < source.index("pii_service.warm_detector()")
