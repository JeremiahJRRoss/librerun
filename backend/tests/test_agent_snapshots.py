"""Manifest snapshots: discovery writes them, and a pass stamps the rest absent.

The gateway is a separate process, so ``agent_manifests`` is the only
thing it can ask about an agent. These drive the reconciliation against
a REAL PostgreSQL — the upsert is `ON CONFLICT ... RETURNING xmax = 0`
and the absent stamp is an array predicate, neither of which a double
would prove anything about.

Needs a database at ``DATABASE_URL`` with the schema loaded. Without one
the module skips — unless ``LIBRERUN_REQUIRE_DB=1`` (set by the
``database-parity`` workflow), which turns the skip into a failure so a
job that exists to run these cannot pass by running none of them.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.agents.manifest import AgentManifest
from app.services.agent_snapshot_service import (
    manifest_payload,
    manifest_sha256,
    reconcile_snapshots,
)

REQUIRE_DB = os.environ.get("LIBRERUN_REQUIRE_DB", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


@pytest_asyncio.fixture
async def db():
    """A session in an open transaction, rolled back after — so the rows
    these tests write never outlive them on a shared database."""
    from app.config import settings
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = create_async_engine(settings.DATABASE_URL.get_secret_value())
    try:
        async with engine.connect() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        reason = f"no database at DATABASE_URL ({type(exc).__name__}: {exc})"
        if REQUIRE_DB:
            pytest.fail("LIBRERUN_REQUIRE_DB is set, so this may not skip: " + reason)
        pytest.skip(reason)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(bind=connection)
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


def _manifest(agent_id: str, **overrides) -> AgentManifest:
    base = {
        "id": agent_id,
        "name": "Probe",
        "runtime": "python-package",
        "phases": [{"name": "analyze"}],
        "output": {"mode": "structured"},
    }
    base.update(overrides)
    return AgentManifest.model_validate(base)


async def _row(db, agent_id: str) -> dict | None:
    result = await db.execute(
        text(
            "SELECT agent_id, manifest, sha256, source, discovered_at, absent_at "
            "FROM agent_manifests WHERE agent_id = :a"
        ),
        {"a": agent_id},
    )
    row = result.first()
    return dict(row._mapping) if row else None


@pytest.mark.asyncio
async def test_a_pass_writes_the_validated_manifest_with_its_digest(db):
    agent_id = f"probe-{uuid.uuid4().hex[:8]}"
    manifest = _manifest(
        agent_id,
        capabilities=["llm"],
        llm={"steps": [{"id": "think", "model": "gpt-4o"}]},
    )

    report = await reconcile_snapshots(db, [(agent_id, manifest, "directory")])

    assert agent_id in report.present
    assert agent_id in report.changed
    row = await _row(db, agent_id)
    assert row["source"] == "directory"
    assert row["absent_at"] is None
    assert row["sha256"] == manifest_sha256(manifest_payload(manifest))
    # What the gateway actually reads: the VALIDATED manifest, defaults
    # filled in — the grant and the step defaults, not the raw YAML.
    assert row["manifest"]["capabilities"] == ["llm"]
    assert row["manifest"]["llm"]["redact_outbound"] is True
    assert [s["id"] for s in row["manifest"]["llm"]["steps"]] == ["think"]


@pytest.mark.asyncio
async def test_an_unchanged_manifest_is_not_reported_changed(db):
    agent_id = f"probe-{uuid.uuid4().hex[:8]}"
    manifest = _manifest(agent_id)

    await reconcile_snapshots(db, [(agent_id, manifest, "directory")])
    second = await reconcile_snapshots(db, [(agent_id, manifest, "directory")])

    assert second.present == [agent_id]
    assert second.changed == []
    assert second.marked_absent == []

    edited = _manifest(agent_id, description="now it says something")
    third = await reconcile_snapshots(db, [(agent_id, edited, "directory")])
    assert third.changed == [agent_id]
    row = await _row(db, agent_id)
    assert row["manifest"]["description"] == "now it says something"


@pytest.mark.asyncio
async def test_an_agent_that_disappears_is_stamped_absent_and_kept(db):
    """An uninstall must not take the agent's keys and its tenants' step
    overrides with it, so the row survives — stamped, not deleted."""
    gone = f"gone-{uuid.uuid4().hex[:8]}"
    stays = f"stays-{uuid.uuid4().hex[:8]}"
    await reconcile_snapshots(
        db, [(gone, _manifest(gone), "directory"), (stays, _manifest(stays), "directory")]
    )

    report = await reconcile_snapshots(db, [(stays, _manifest(stays), "directory")])

    assert gone in report.marked_absent
    assert stays not in report.marked_absent
    row = await _row(db, gone)
    assert row is not None, "the snapshot was deleted instead of stamped"
    assert row["absent_at"] is not None
    assert (await _row(db, stays))["absent_at"] is None


@pytest.mark.asyncio
async def test_a_returning_agent_is_present_again(db):
    agent_id = f"probe-{uuid.uuid4().hex[:8]}"
    await reconcile_snapshots(db, [(agent_id, _manifest(agent_id), "directory")])
    await reconcile_snapshots(db, [])
    assert (await _row(db, agent_id))["absent_at"] is not None

    report = await reconcile_snapshots(db, [(agent_id, _manifest(agent_id), "directory")])

    assert report.returned == [agent_id]
    assert (await _row(db, agent_id))["absent_at"] is None


@pytest.mark.asyncio
async def test_zero_agents_stamps_every_row_absent(db):
    """A chassis with an empty agents directory is a healthy
    configuration, and the honest snapshot state for it is "no agent is
    installed" — not "the last boot's agents are still authorized"."""
    ids = [f"probe-{uuid.uuid4().hex[:8]}" for _ in range(3)]
    await reconcile_snapshots(db, [(i, _manifest(i), "directory") for i in ids])

    report = await reconcile_snapshots(db, [])

    assert set(ids) <= set(report.marked_absent)
    assert report.present == []
    for agent_id in ids:
        assert (await _row(db, agent_id))["absent_at"] is not None


@pytest.mark.asyncio
async def test_an_already_absent_row_is_not_restamped(db):
    """The stamp is when the agent went away, so a second boot without it
    must leave the timestamp alone."""
    agent_id = f"probe-{uuid.uuid4().hex[:8]}"
    await reconcile_snapshots(db, [(agent_id, _manifest(agent_id), "directory")])
    await reconcile_snapshots(db, [])
    first_stamp = (await _row(db, agent_id))["absent_at"]

    report = await reconcile_snapshots(db, [])

    assert agent_id not in report.marked_absent
    assert (await _row(db, agent_id))["absent_at"] == first_stamp


@pytest.mark.asyncio
async def test_the_install_mode_is_recorded_and_updated(db):
    agent_id = f"probe-{uuid.uuid4().hex[:8]}"
    await reconcile_snapshots(db, [(agent_id, _manifest(agent_id), "entry-point")])
    assert (await _row(db, agent_id))["source"] == "entry-point"

    await reconcile_snapshots(db, [(agent_id, _manifest(agent_id), "directory")])
    assert (await _row(db, agent_id))["source"] == "directory"


def test_the_digest_is_canonical_not_dict_order():
    """Two spellings of the same manifest must digest identically, or
    every boot would report every agent changed."""
    payload = {"b": 1, "a": {"y": 2, "x": [3, 4]}}
    reordered = {"a": {"x": [3, 4], "y": 2}, "b": 1}
    assert manifest_sha256(payload) == manifest_sha256(reordered)
    assert manifest_sha256(payload) != manifest_sha256({"b": 2, "a": {}})


def test_the_registry_records_how_each_agent_arrived():
    """``registered_manifests`` is what boot hands the reconciliation, so
    the install mode has to survive registration."""
    from app.agents import registry
    from app.agents.protocol import AgentProtocol

    class _Stub(AgentProtocol):
        agent_id = "stub-source-v1"
        display_name = "Stub"
        description = ""

        def input_schema(self) -> dict:
            return {"type": "object"}

    registry._clear_registry_for_tests()
    try:
        manifest = _manifest("stub-source-v1")
        registry.register(_Stub(), manifest, source="entry-point")
        assert registry.get_source("stub-source-v1") == "entry-point"
        assert registry.registered_manifests() == [
            ("stub-source-v1", manifest, "entry-point")
        ]
        # A caller that bypassed discovery says so rather than claiming a
        # mode it does not have.
        registry.register(_Stub(), manifest)
        assert registry.get_source("stub-source-v1") == "direct"
    finally:
        registry._clear_registry_for_tests()
