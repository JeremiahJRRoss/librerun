"""A row in ``agent_settings`` records a DIVERGENCE (K5a, L32, D17).

The Settings tab posts every value it shows, defaults included, and has
no way to know which of them a tenant ever chose. Stored literally, one
Save would turn every default into this tenant's value and the agent's
next release could never move a default for anyone who had saved. So the
server decides, the way ``agent_step_configs`` already does for steps: a
value equal to the manifest's default is not stored, and ``null`` is the
clear.

It decides AFTER coercing (K5-07): JSON's ``true`` is Python's ``1``, so
``1`` is refused for a boolean rather than stored as ``true``, and a
number input's ``0`` meets a declared ``0.0``.

Real PostgreSQL: the write is an ``ON CONFLICT DO UPDATE`` upsert keyed
by the table's three-column primary key, and the value is JSONB.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.agents.manifest import AgentManifest
from app.services import agent_settings_service as svc

REQUIRE_DB = os.environ.get("LIBRERUN_REQUIRE_DB", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

AGENT = "probe-v1"


@pytest_asyncio.fixture
async def db():
    from sqlalchemy.ext.asyncio import AsyncSession

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
        session = AsyncSession(bind=connection)
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


async def _tenant(db) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, :n, :s)"),
        {"id": tenant_id, "n": "Probe", "s": f"probe-{tenant_id.hex[:12]}"},
    )
    return tenant_id


@pytest_asyncio.fixture
async def tenant(db) -> uuid.UUID:
    """A real tenant row: ``agent_settings.tenant_id`` is a foreign key,
    and the point of these tests is what the table does."""
    return await _tenant(db)


SETTINGS = [
    {"key": "strict", "type": "bool", "default": False},
    {"key": "ratio", "type": "float", "default": 0.0},
    {"key": "limit", "type": "int", "default": 3},
    {"key": "depth", "type": "enum", "options": ["basic", "advanced"], "default": "advanced"},
    {"key": "tags", "type": "string_list", "default": []},
    {"key": "note", "label": "Note", "type": "string", "default": "hello"},
]


def manifest(settings=None) -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "id": AGENT,
            "name": "Probe",
            "runtime": "python-package",
            "phases": [{"name": "work"}],
            "output": {"mode": "structured"},
            "settings": SETTINGS if settings is None else settings,
        }
    )


async def save(db, tenant, updates: list[dict], spec=None, user_id=None) -> list[str]:
    return await svc.apply_updates(db, tenant, AGENT, spec or manifest(), updates, user_id)


async def stored(db, tenant) -> dict:
    return await svc.values_for(db, tenant, AGENT)


async def rows(db, tenant) -> list[tuple]:
    result = await db.execute(
        text(
            "SELECT key, value, updated_by FROM agent_settings "
            "WHERE tenant_id = :t AND agent_id = :a ORDER BY key"
        ),
        {"t": tenant, "a": AGENT},
    )
    return [tuple(row) for row in result.all()]


@pytest.mark.asyncio
async def test_a_bool_setting_refuses_one(db, tenant):
    """``1 == True`` in Python; a boolean setting stores ``true``, never
    ``1``, and one refusal writes nothing at all — not even the valid
    value beside it."""
    with pytest.raises(svc.InvalidSettingValue) as refused:
        await save(db, tenant, [{"key": "limit", "value": 9}, {"key": "strict", "value": 1}])
    assert refused.value.key == "strict"
    assert str(refused.value) == "setting 'strict': must be true or false, not an integer"
    assert await rows(db, tenant) == []

    assert await save(db, tenant, [{"key": "strict", "value": True}]) == ["strict"]
    assert await stored(db, tenant) == {"strict": True}


@pytest.mark.asyncio
async def test_a_value_equal_to_the_default_stores_no_row(db, tenant):
    """What the Settings tab posts when nobody changed anything: every
    key, each at its default — and nothing is stored."""
    everything = [{"key": s["key"], "value": s["default"]} for s in SETTINGS]
    assert await save(db, tenant, everything) == []
    assert await rows(db, tenant) == []

    # K5-07: a number input's 0 is the declared 0.0, not a divergence.
    assert await save(db, tenant, [{"key": "ratio", "value": 0}]) == []
    assert await rows(db, tenant) == []

    # ...and the effective values are the defaults, none overridden.
    effective = svc.effective(manifest(), await stored(db, tenant))
    assert [(e["key"], e["value"], e["overridden"]) for e in effective] == [
        (s["key"], s["default"], False) for s in SETTINGS
    ]


@pytest.mark.asyncio
async def test_the_same_key_written_twice_succeeds(db, tenant):
    """The upsert's conflict path, with two DIFFERENT values: an unchanged
    value writes nothing, and would pass on a broken update."""
    user_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, auth_provider, role)"
            " VALUES (:id, :t, :e, 'credentials', 'admin')"
        ),
        {"id": user_id, "t": tenant, "e": f"{user_id.hex[:10]}@example.com"},
    )

    assert await save(db, tenant, [{"key": "limit", "value": 5}], user_id=user_id) == ["limit"]
    assert await save(db, tenant, [{"key": "limit", "value": 7}], user_id=user_id) == ["limit"]

    assert await rows(db, tenant) == [("limit", 7, user_id)]
    updated_at = (
        await db.execute(
            text("SELECT updated_at FROM agent_settings WHERE tenant_id = :t"), {"t": tenant}
        )
    ).scalar_one()
    assert updated_at is not None


@pytest.mark.asyncio
async def test_each_type_round_trips_through_jsonb(db, tenant):
    values = {
        "strict": True,
        "ratio": 0.25,
        "limit": 12,
        "depth": "basic",
        "tags": ["a", "b"],
        "note": "",
    }
    changed = await save(db, tenant, [{"key": k, "value": v} for k, v in values.items()])
    assert sorted(changed) == sorted(values)
    assert await stored(db, tenant) == values
    assert svc.effective_values(manifest(), await stored(db, tenant)) == values
    assert all(e["overridden"] for e in svc.effective(manifest(), await stored(db, tenant)))


@pytest.mark.asyncio
async def test_null_or_the_default_clears_a_value(db, tenant):
    """An emptied field posts ``null``; typing the default back in is the
    same statement. Both delete the row."""
    await save(db, tenant, [{"key": "note", "value": "changed"}, {"key": "limit", "value": 9}])
    assert await stored(db, tenant) == {"note": "changed", "limit": 9}

    assert await save(db, tenant, [{"key": "note", "value": None}]) == ["note"]
    assert await stored(db, tenant) == {"limit": 9}

    assert await save(db, tenant, [{"key": "limit", "value": 3}]) == ["limit"]
    assert await rows(db, tenant) == []


@pytest.mark.asyncio
async def test_changed_keys_name_what_changed_and_nothing_else(db, tenant):
    """The page posts every key; the audit names the ones that moved."""
    await save(db, tenant, [{"key": "limit", "value": 9}])
    everything = [{"key": s["key"], "value": s["default"]} for s in SETTINGS]
    everything[2] = {"key": "limit", "value": 9}  # unchanged
    everything[5] = {"key": "note", "value": "new"}  # changed
    assert await save(db, tenant, everything) == ["note"]


@pytest.mark.asyncio
async def test_an_undeclared_key_or_a_key_named_twice_is_refused(db, tenant):
    with pytest.raises(svc.InvalidSettingValue, match="declares no such setting"):
        await save(db, tenant, [{"key": "note", "value": "x"}, {"key": "nope", "value": 1}])
    with pytest.raises(svc.InvalidSettingValue, match="more than once"):
        await save(db, tenant, [{"key": "note", "value": "x"}, {"key": "note", "value": "y"}])
    with pytest.raises(svc.InvalidSettingValue, match="one of"):
        await save(db, tenant, [{"key": "depth", "value": "deep"}])
    assert await rows(db, tenant) == []


@pytest.mark.asyncio
async def test_the_agents_next_default_reaches_a_tenant_who_saved(db, tenant):
    """The consequence D17 is for: a tenant who saved without changing
    anything reads the author's next default."""
    await save(db, tenant, [{"key": "depth", "value": "advanced"}])
    later = manifest(
        [{"key": "depth", "type": "enum", "options": ["basic", "advanced"], "default": "basic"}]
    )
    (entry,) = svc.effective(later, await stored(db, tenant))
    assert (entry["value"], entry["overridden"]) == ("basic", False)


@pytest.mark.asyncio
async def test_a_row_the_declaration_no_longer_accepts_is_inert(db, tenant):
    """The author changed a setting's type or removed it: the tenant's old
    row serves the default, never a value of the wrong type."""
    await save(db, tenant, [{"key": "limit", "value": 9}, {"key": "note", "value": "kept"}])
    later = manifest(
        [
            {"key": "limit", "type": "bool", "default": False},
            {"key": "renamed", "type": "string", "default": "d"},
        ]
    )
    assert svc.effective_values(later, await stored(db, tenant)) == {
        "limit": False,
        "renamed": "d",
    }
    assert not any(e["overridden"] for e in svc.effective(later, await stored(db, tenant)))


@pytest.mark.asyncio
async def test_values_for_reads_one_tenant(db, tenant):
    """The service's own half of Gate T: a second tenant's row is not this
    tenant's (``test_gate_t.py`` holds the three surfaces that read it)."""
    other = await _tenant(db)
    await save(db, other, [{"key": "note", "value": "other tenant's"}])
    await save(db, tenant, [{"key": "note", "value": "this tenant's"}])
    assert await stored(db, tenant) == {"note": "this tenant's"}
    assert await stored(db, other) == {"note": "other tenant's"}
    assert await svc.values_for(db, uuid.uuid4(), AGENT) == {}
