"""The gateway's authorization state is this boot's, not the last one's.

The gateway is a separate process and resolves every agent from
`agent_manifests`, so until the backend persists the current discovery
the gateway is working from the PREVIOUS boot's rows: an agent
uninstalled, or stripped of its `llm` grant, keeps authorizing model
calls, and a run token left in Redis by the restart carries that
authority to its TTL (Codex round 21, P1).

This was one attempt and a warning — a warning whose own text said "the
gateway sees the previous boot's rows until this succeeds" while nothing
existed that could ever make it succeed. The availability argument
behind it stands: a database that is not up yet must not stop the API
from booting. Giving up was the part that was wrong.
"""
from __future__ import annotations

import ast
import asyncio
import inspect

import pytest

from app import main as app_main


@pytest.mark.asyncio
async def test_reconciliation_retries_until_it_is_persisted(monkeypatch):
    attempts = {"n": 0}

    async def flaky(_session):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("the database is not up yet")

    import app.services.agent_snapshot_service as service

    monkeypatch.setattr(service, "reconcile_registered_agents", flaky)
    monkeypatch.setattr(app_main, "RECONCILE_RETRY_SECONDS", 0.01)

    await asyncio.wait_for(app_main._reconcile_until_persisted(), timeout=5)
    assert attempts["n"] == 3, "gave up before the database recovered"


@pytest.mark.asyncio
async def test_one_success_does_not_retry(monkeypatch):
    attempts = {"n": 0}

    async def fine(_session):
        attempts["n"] += 1

    import app.services.agent_snapshot_service as service

    monkeypatch.setattr(service, "reconcile_registered_agents", fine)
    monkeypatch.setattr(app_main, "RECONCILE_RETRY_SECONDS", 0.01)

    await asyncio.wait_for(app_main._reconcile_until_persisted(), timeout=5)
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_the_retry_can_be_cancelled(monkeypatch):
    """It runs for the life of the process, so shutdown has to end it —
    a task that swallows CancelledError would hang the shutdown."""

    async def never(_session):
        raise RuntimeError("still down")

    import app.services.agent_snapshot_service as service

    monkeypatch.setattr(service, "reconcile_registered_agents", never)
    monkeypatch.setattr(app_main, "RECONCILE_RETRY_SECONDS", 0.01)

    task = asyncio.create_task(app_main._reconcile_until_persisted())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)


def test_the_lifespan_starts_the_retry_and_stops_it():
    """The structural half: a retry nothing starts is no better than the
    warning it replaced, and one nothing cancels blocks shutdown."""
    tree = ast.parse(inspect.getsource(app_main.lifespan).strip())
    names = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_reconcile_until_persisted" in names, (
        "the lifespan no longer starts the reconciliation retry"
    )
    assert "cancel" in names, (
        "the lifespan never cancels the retry task, so shutdown waits on a "
        "loop that runs forever"
    )


def test_the_lifespan_does_not_reconcile_inline_and_give_up():
    """The shape that shipped: a try/except around the call itself, with
    the failure logged and forgotten. If it comes back, the tests above
    still pass — they exercise the helper, not the boot."""
    source = inspect.getsource(app_main.lifespan)
    assert "reconcile_registered_agents" not in source, (
        "the lifespan reconciles inline again; one attempt there is the "
        "defect, whatever the helper does"
    )
