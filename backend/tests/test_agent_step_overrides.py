"""A row in ``agent_step_configs`` records a DIVERGENCE (blueprint S4a, L25).

The admin page has no way to know which of the values it is showing a
tenant ever chose — it renders the *effective* step, manifest defaults
and tenant choices in the same inputs — so it posts the whole row. Taken
literally, one click of Save turned every default into a tenant
override: the page then said ``overridden here`` about every field, and
the agent's next release could never reach that tenant again, because
the row went on pinning the old default with nothing on the page to say
why. Codex found it.

So the server decides. A value equal to the manifest's own default is
not an override and is stored as NULL, exactly like an explicit clear,
and a row left carrying nothing is deleted.

Real PostgreSQL: the upsert is ``ON CONFLICT DO UPDATE`` over a partial
column set and the cleanup is a five-column ``IS NULL`` predicate.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.agents.manifest import AgentManifest
from app.services import agent_step_config_service as svc

REQUIRE_DB = os.environ.get("LIBRERUN_REQUIRE_DB", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


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


DEFAULTS = {
    "provider": "openai",
    "model": "gpt-4o",
    "temperature": 0.0,
    "max_tokens": 2000,
    "timeout_seconds": 120,
}


def manifest(**step_overrides) -> AgentManifest:
    step = {"id": "analyze", **DEFAULTS}
    step.update(step_overrides)
    return AgentManifest.model_validate(
        {
            "id": "probe-v1",
            "name": "Probe",
            "runtime": "python-package",
            "phases": [{"name": "analyze"}],
            "output": {"mode": "structured"},
            "capabilities": ["llm"],
            "llm": {"steps": [step]},
        }
    )


@pytest_asyncio.fixture
async def tenant(db) -> uuid.UUID:
    """A real tenant row: ``agent_step_configs.tenant_id`` is a foreign
    key, and the point of these tests is what the table does."""
    tenant_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, :n, :s)"),
        {"id": tenant_id, "n": "Probe", "s": f"probe-{tenant_id.hex[:12]}"},
    )
    return tenant_id


async def save(db, tenant, values: dict, spec=None) -> None:
    await svc.apply_updates(
        db,
        tenant,
        "probe-v1",
        spec or manifest(),
        [{"step_id": "analyze", **values}],
        None,
    )


async def stored(db, tenant) -> dict:
    return (await svc.overrides_for(db, tenant, "probe-v1")).get("analyze", {})


async def rows(db, tenant) -> int:
    return (
        await db.execute(
            text(
                "SELECT count(*) FROM agent_step_configs "
                "WHERE tenant_id = :t AND agent_id = 'probe-v1'"
            ),
            {"t": tenant},
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_a_whole_row_of_defaults_stores_no_override(db, tenant):
    """What the admin page posts when nobody has changed anything."""
    await save(db, tenant, dict(DEFAULTS))

    assert await stored(db, tenant) == {}
    # …and no inert row of nulls left behind, either.
    assert await rows(db, tenant) == 0


@pytest.mark.asyncio
async def test_only_the_changed_field_becomes_an_override(db, tenant):
    await save(db, tenant, {**DEFAULTS, "model": "gpt-4o-mini"})

    assert await stored(db, tenant) == {"model": "gpt-4o-mini"}


@pytest.mark.asyncio
async def test_the_agents_next_default_reaches_a_tenant_who_saved(db, tenant):
    """The consequence the finding is about. The tenant saves without
    changing anything; the author then ships a new default model."""
    await save(db, tenant, dict(DEFAULTS))

    later = manifest(model="gpt-5-mini")
    effective = svc.effective_steps(later, await svc.overrides_for(db, tenant, "probe-v1"))

    assert effective[0]["model"] == "gpt-5-mini"
    assert effective[0]["overridden"] == []


@pytest.mark.asyncio
async def test_a_real_choice_survives_the_agents_next_default(db, tenant):
    """The other direction, which must not break: a tenant who DID
    choose keeps their choice when the default moves."""
    await save(db, tenant, {**DEFAULTS, "model": "claude-sonnet-4-6"})

    later = manifest(model="gpt-5-mini")
    effective = svc.effective_steps(later, await svc.overrides_for(db, tenant, "probe-v1"))

    assert effective[0]["model"] == "claude-sonnet-4-6"
    assert effective[0]["overridden"] == ["model"]


@pytest.mark.asyncio
async def test_sending_the_default_back_clears_an_override(db, tenant):
    """Typing the agent's own number back into the box is a reset. It
    reads the same as never having touched it, because it is."""
    await save(db, tenant, {**DEFAULTS, "max_tokens": 500})
    assert await stored(db, tenant) == {"max_tokens": 500}

    await save(db, tenant, dict(DEFAULTS))

    assert await stored(db, tenant) == {}
    assert await rows(db, tenant) == 0


@pytest.mark.asyncio
async def test_an_explicit_null_clears_an_override(db, tenant):
    """What an emptied input posts. It had to be sent at all: a field
    omitted from the payload leaves the stored row alone, which is why
    clearing a number in the page used to do nothing."""
    await save(db, tenant, {**DEFAULTS, "temperature": 0.7})
    assert await stored(db, tenant) == {"temperature": 0.7}

    await save(db, tenant, {**DEFAULTS, "temperature": None})

    assert await stored(db, tenant) == {}


@pytest.mark.asyncio
async def test_clearing_one_field_leaves_the_others(db, tenant):
    await save(db, tenant, {**DEFAULTS, "model": "gpt-4o-mini", "max_tokens": 500})
    assert await stored(db, tenant) == {"model": "gpt-4o-mini", "max_tokens": 500}

    await save(db, tenant, {**DEFAULTS, "model": "gpt-4o-mini"})

    assert await stored(db, tenant) == {"model": "gpt-4o-mini"}
    assert await rows(db, tenant) == 1


@pytest.mark.asyncio
async def test_an_integer_for_a_float_default_is_still_the_default(db, tenant):
    """A number input parses ``0`` where the manifest declares ``0.0``.
    Comparing by type would store that as an override of itself."""
    await save(db, tenant, {**DEFAULTS, "temperature": 0})

    assert await stored(db, tenant) == {}


@pytest.mark.asyncio
async def test_validation_runs_before_normalization(db, tenant):
    """The order matters and is not obvious. ``True == 1`` in Python, so
    a boolean reaching the default comparison would be swallowed as "no
    change" — but it never gets there: the value is held to the
    declaration's own model first, and a bad one is a 400 rather than a
    silent no-op. Nothing is written, including for the steps that
    shared the request."""
    with pytest.raises(svc.InvalidStepOverride):
        await save(db, tenant, {**DEFAULTS, "max_tokens": True}, manifest(max_tokens=1))

    assert await rows(db, tenant) == 0


@pytest.mark.asyncio
async def test_a_partial_update_does_not_touch_what_it_omits(db, tenant):
    """An API caller sending one field is not posting a whole row."""
    await save(db, tenant, {"model": "gpt-4o-mini", "max_tokens": 500})

    await save(db, tenant, {"max_tokens": 800})

    assert await stored(db, tenant) == {"model": "gpt-4o-mini", "max_tokens": 800}
