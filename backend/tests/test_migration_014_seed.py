"""Migration 014's data migration: the legacy file's pipeline becomes rows.

The three gateway tables are DDL and the parity workflow proves the DDL
(a fresh load plus ``alembic upgrade head`` is a no-op, both ways). What
parity's ``--schema-only`` dumps cannot see is the *data* migration:
every tenant that exists at upgrade time must inherit the one
configuration its admin had been editing until now, and a re-run must
change nothing.

So these drive ``_seed_step_configs`` against a REAL PostgreSQL over a
synthetic agents tree — no bundled agent is involved, and the scan is
by convention, so an agent directory is just a directory with an
``agent.yaml`` and a ``config.json``.

Needs a database at ``DATABASE_URL`` with the schema loaded. Without one
the module skips — unless ``LIBRERUN_REQUIRE_DB=1`` (set by the
``database-parity`` workflow), which turns the skip into a failure so a
job that exists to run these cannot pass by running none of them.
"""
from __future__ import annotations

import importlib.util
import json
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REQUIRE_DB = os.environ.get("LIBRERUN_REQUIRE_DB", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "014_agent_gateway_tables.py"
)


def _load_migration():
    """Import the revision file by path — ``alembic/versions`` is not a
    package, and the seeding must be tested as the migration ships it."""
    spec = importlib.util.spec_from_file_location("_m014", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m014 = _load_migration()


@pytest_asyncio.fixture
async def db():
    """Run a function against a connection in an open transaction, then
    roll it back.

    The connection handed to the function is the **sync-facing** one
    SQLAlchemy produces inside ``run_sync`` — which is exactly what
    ``op.get_bind()`` gives a migration here, because ``alembic/env.py``
    drives the chain over an async engine. So the seeding is exercised
    through the same bridge it crosses in production, not a second one
    that only exists for the test.

    Nothing this module writes survives the rollback — including the
    tenants it creates, which is what lets it assert exact row counts on
    a database other tests share.
    """
    from app.config import settings

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
        try:
            yield connection.run_sync
        finally:
            await transaction.rollback()
    await engine.dispose()


def _tenant(conn, slug: str) -> uuid.UUID:
    return conn.execute(
        text(
            "INSERT INTO tenants (name, slug) VALUES (:n, :s) RETURNING id"
        ),
        {"n": slug, "s": f"{slug}-{uuid.uuid4().hex[:8]}"},
    ).scalar_one()


def _agent_tree(root: Path, agent_id: str, pipeline: dict, *, dirname: str | None = None) -> Path:
    agent_dir = root / (dirname or agent_id.replace("-", "_"))
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        f"manifest_version: 1\nid: {agent_id}\nname: Probe\nruntime: python-package\n"
    )
    (agent_dir / "config.json").write_text(json.dumps({"pipeline": pipeline}))
    return agent_dir


PIPELINE = {
    "think": {
        "step_number": 0,
        "provider": "openai",
        "model": "gpt-4o",
        "temperature": 0.0,
        "max_tokens": 1000,
        "timeout_seconds": 30,
        "description": "an LLM step",
    },
    "plan": {
        "step_number": 1,
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "max_tokens": 2000,
        "timeout_seconds": 60,
    },
    "search": {
        "step_number": 2,
        "provider": None,
        "model": None,
        "timeout_seconds": 20,
        "description": "calls no model",
    },
}


def _overlay(tmp_path: Path, agent_dir: Path, pipeline: dict) -> None:
    """What an admin's edit left in the state volume (S2)."""
    path = tmp_path / "state" / "agents" / agent_dir.name / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pipeline": pipeline}))


def _rows(conn, agent_id: str) -> list[dict]:
    return [
        dict(r._mapping)
        for r in conn.execute(
            text(
                "SELECT tenant_id, step_id, provider, model, temperature, "
                "max_tokens, timeout_seconds, updated_by "
                "FROM agent_step_configs WHERE agent_id = :a "
                "ORDER BY tenant_id, step_id"
            ),
            {"a": agent_id},
        ).all()
    ]


@pytest.mark.asyncio
async def test_an_installation_that_edited_nothing_gets_no_rows(db, tmp_path, monkeypatch):
    """A row records a DIVERGENCE. With no overlay the effective
    pipeline IS the packaged file, so there is nothing this installation
    ever chose — and writing it anyway pinned the agent's defaults for
    every tenant present at upgrade time, so the author's next release
    could never reach them (Codex P2). The same default-pinning the
    admin save path was fixed for, arriving through the upgrade."""
    agent_id = f"probe-{uuid.uuid4().hex[:8]}"
    _agent_tree(tmp_path / "agents", agent_id, PIPELINE)
    monkeypatch.setenv("LIBRERUN_AGENTS_PATH", str(tmp_path / "agents"))
    monkeypatch.setenv("LIBRERUN_STATE_DIR", str(tmp_path / "state"))

    def body(conn):
        _tenant(conn, "alpha")
        _tenant(conn, "beta")

        attempted = m014._seed_step_configs(conn)

        assert attempted == 0
        assert _rows(conn, agent_id) == []

    await db(body)


@pytest.mark.asyncio
async def test_an_overlay_identical_to_the_packaged_file_seeds_nothing(
    db, tmp_path, monkeypatch
):
    """An overlay exists but says the same thing — a Save that changed
    nothing. Its presence is not a choice."""
    agent_id = f"probe-{uuid.uuid4().hex[:8]}"
    agent_dir = _agent_tree(tmp_path / "agents", agent_id, PIPELINE)
    _overlay(tmp_path, agent_dir, json.loads(json.dumps(PIPELINE)))
    monkeypatch.setenv("LIBRERUN_AGENTS_PATH", str(tmp_path / "agents"))
    monkeypatch.setenv("LIBRERUN_STATE_DIR", str(tmp_path / "state"))

    def body(conn):
        _tenant(conn, "alpha")

        assert m014._seed_step_configs(conn) == 0
        assert _rows(conn, agent_id) == []

    await db(body)


@pytest.mark.asyncio
async def test_only_the_edited_fields_become_rows(db, tmp_path, monkeypatch):
    """And the fields left alone stay NULL, so a later release moves
    them while the tenant's real choice stands."""
    agent_id = f"probe-{uuid.uuid4().hex[:8]}"
    agent_dir = _agent_tree(tmp_path / "agents", agent_id, PIPELINE)
    edited = json.loads(json.dumps(PIPELINE))
    edited["think"]["model"] = "gpt-4o-mini"
    _overlay(tmp_path, agent_dir, edited)
    monkeypatch.setenv("LIBRERUN_AGENTS_PATH", str(tmp_path / "agents"))
    monkeypatch.setenv("LIBRERUN_STATE_DIR", str(tmp_path / "state"))

    def body(conn):
        a = _tenant(conn, "alpha")
        existing = conn.execute(text("SELECT count(*) FROM tenants")).scalar_one()

        attempted = m014._seed_step_configs(conn)

        # One step diverges, for every tenant. ``plan`` is untouched and
        # ``search`` names no model at all.
        assert attempted == existing
        rows = _rows(conn, agent_id)
        assert {r["step_id"] for r in rows} == {"think"}
        think = next(r for r in rows if r["tenant_id"] == a)
        assert think["model"] == "gpt-4o-mini"
        assert think["provider"] is None
        assert think["temperature"] is None
        assert think["max_tokens"] is None
        assert think["timeout_seconds"] is None
        # The migration is not a user.
        assert think["updated_by"] is None

    await db(body)


@pytest.mark.asyncio
async def test_the_state_overlay_is_the_edit_that_carries(db, tmp_path, monkeypatch):
    """S2 put the overlay in a persisted volume for exactly this moment:
    the packaged file is the defaults, the overlay is what the admin
    actually edited, and the upgrade must inherit the edit."""
    agent_id = f"probe-{uuid.uuid4().hex[:8]}"
    agent_dir = _agent_tree(tmp_path / "agents", agent_id, PIPELINE)
    edited = json.loads(json.dumps(PIPELINE))
    edited["think"]["model"] = "gpt-4o-mini"
    edited["think"]["temperature"] = 0.25
    _overlay(tmp_path, agent_dir, edited)
    monkeypatch.setenv("LIBRERUN_AGENTS_PATH", str(tmp_path / "agents"))
    monkeypatch.setenv("LIBRERUN_STATE_DIR", str(tmp_path / "state"))

    def body(conn):
        _tenant(conn, "alpha")
        m014._seed_step_configs(conn)

        think = [r for r in _rows(conn, agent_id) if r["step_id"] == "think"]
        assert think, "the overlay's steps were not seeded"
        assert {r["model"] for r in think} == {"gpt-4o-mini"}
        assert {r["temperature"] for r in think} == {0.25}

    await db(body)


@pytest.mark.asyncio
async def test_a_rerun_overwrites_nothing(db, tmp_path, monkeypatch):
    """``ON CONFLICT DO NOTHING`` is what makes the upgrade safe to
    repeat: the second pass must not walk back over an admin's edit."""
    agent_id = f"probe-{uuid.uuid4().hex[:8]}"
    agent_dir = _agent_tree(tmp_path / "agents", agent_id, PIPELINE)
    edited = json.loads(json.dumps(PIPELINE))
    edited["think"]["model"] = "gpt-4o-mini"
    _overlay(tmp_path, agent_dir, edited)
    monkeypatch.setenv("LIBRERUN_AGENTS_PATH", str(tmp_path / "agents"))
    monkeypatch.setenv("LIBRERUN_STATE_DIR", str(tmp_path / "state"))

    def body(conn):
        tenant = _tenant(conn, "alpha")

        m014._seed_step_configs(conn)
        before = len(_rows(conn, agent_id))
        conn.execute(
            text(
                "UPDATE agent_step_configs SET model = 'edited-after-upgrade' "
                "WHERE agent_id = :a AND step_id = 'think' AND tenant_id = :t"
            ),
            {"a": agent_id, "t": tenant},
        )

        m014._seed_step_configs(conn)

        rows = _rows(conn, agent_id)
        assert len(rows) == before
        kept = next(
            r for r in rows if r["tenant_id"] == tenant and r["step_id"] == "think"
        )
        assert kept["model"] == "edited-after-upgrade"

    await db(body)


@pytest.mark.asyncio
async def test_a_tenant_created_after_the_upgrade_gets_no_rows(db, tmp_path, monkeypatch):
    """The manifest defaults are the fallback, so a new tenant starts
    from them rather than from a frozen copy of an old file."""
    agent_id = f"probe-{uuid.uuid4().hex[:8]}"
    _agent_tree(tmp_path / "agents", agent_id, PIPELINE)
    monkeypatch.setenv("LIBRERUN_AGENTS_PATH", str(tmp_path / "agents"))
    monkeypatch.setenv("LIBRERUN_STATE_DIR", str(tmp_path / "state"))

    def body(conn):
        m014._seed_step_configs(conn)
        later = _tenant(conn, "later")

        assert [r for r in _rows(conn, agent_id) if r["tenant_id"] == later] == []

    await db(body)


@pytest.mark.asyncio
async def test_a_tree_with_nothing_to_seed_seeds_nothing(db, tmp_path, monkeypatch):
    """The negative case the scan exists to survive: no agent, an agent
    with no manifest, an agent whose id is off the manifest charset, and
    an agent with no legacy file each contribute nothing — and a chassis
    with zero agents on disk upgrades like any other."""
    root = tmp_path / "agents"
    (root / "no_manifest").mkdir(parents=True)
    (root / "no_manifest" / "config.json").write_text(json.dumps({"pipeline": PIPELINE}))
    bad = root / "bad_id"
    bad.mkdir()
    (bad / "agent.yaml").write_text("manifest_version: 1\nid: Not_A_Valid_Id\n")
    (bad / "config.json").write_text(json.dumps({"pipeline": PIPELINE}))
    no_config = root / "no_config"
    no_config.mkdir()
    (no_config / "agent.yaml").write_text("manifest_version: 1\nid: no-config\n")
    scaffold = root / "_template"
    scaffold.mkdir()
    (scaffold / "agent.yaml").write_text("manifest_version: 1\nid: scaffold\n")
    (scaffold / "config.json").write_text(json.dumps({"pipeline": PIPELINE}))
    monkeypatch.setenv("LIBRERUN_AGENTS_PATH", str(root))
    monkeypatch.setenv("LIBRERUN_STATE_DIR", str(tmp_path / "state"))

    def body(conn):
        _tenant(conn, "alpha")

        assert m014._seed_step_configs(conn) == 0
        assert m014._discovered_pipelines() == []

        monkeypatch.setenv("LIBRERUN_AGENTS_PATH", str(tmp_path / "absent"))
        assert m014._seed_step_configs(conn) == 0

    await db(body)


def test_the_scan_bites_when_the_tree_is_right(tmp_path, monkeypatch):
    """The companion to the negative case: with one valid agent beside
    the four that contribute nothing, the scan finds exactly that one.
    Without this, a scan broken into always returning nothing would pass
    every assertion above."""
    root = tmp_path / "agents"
    _agent_tree(root, "good-agent", PIPELINE)
    bad = root / "bad_id"
    bad.mkdir()
    (bad / "agent.yaml").write_text("manifest_version: 1\nid: Not_A_Valid_Id\n")
    (bad / "config.json").write_text(json.dumps({"pipeline": PIPELINE}))
    monkeypatch.setenv("LIBRERUN_AGENTS_PATH", str(root))
    monkeypatch.setenv("LIBRERUN_STATE_DIR", str(tmp_path / "state"))

    found = m014._discovered_pipelines()
    assert [agent_id for agent_id, _ in found] == ["good-agent"]
    effective, packaged = found[0][1]
    assert set(effective) == set(PIPELINE)
    # Both blocks come back: a row records a divergence, so the scan
    # has to carry what the value is being compared AGAINST.
    assert packaged == effective


def test_a_later_root_wins_like_discovery(tmp_path, monkeypatch):
    """``LIBRERUN_AGENTS_PATH`` is a list and a later directory's agent
    replaces an earlier one with the same id — the demo's
    ``agents:agents/_examples`` shape. The seed must agree with
    discovery, or an upgraded tenant would inherit a configuration from
    a copy of the agent the backend does not run."""
    first = tmp_path / "a"
    second = tmp_path / "b"
    _agent_tree(first, "shared-id", PIPELINE, dirname="one")
    other = json.loads(json.dumps(PIPELINE))
    other["think"]["model"] = "from-the-second-root"
    _agent_tree(second, "shared-id", other, dirname="two")
    monkeypatch.setenv("LIBRERUN_AGENTS_PATH", f"{first}{os.pathsep}{second}")
    monkeypatch.setenv("LIBRERUN_STATE_DIR", str(tmp_path / "state"))

    found = dict(m014._discovered_pipelines())
    effective, _ = found["shared-id"]
    assert effective["think"]["model"] == "from-the-second-root"
