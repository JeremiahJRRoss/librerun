"""The ``secrets`` table as D32 keys it (K6): the constraints are the design.

A secret belongs to an owner — the platform, the gateway, an agent (every
tenant's default) or one tenant's agent — and ``scope`` beside a nullable
``tenant_id`` and ``agent_id`` says which. Two constraints make that true
in the database rather than in the service's good intentions:

* ``ck_secrets_scope_owner`` ties each scope to the columns it uses, so a
  ``platform`` row cannot carry a tenant and a ``tenant`` row cannot lose
  one;
* ``uq_secrets_owner_name UNIQUE NULLS NOT DISTINCT`` allows one name per
  owner. ``NULLS NOT DISTINCT`` is load-bearing: a platform row's owner
  columns are both NULL, and a plain UNIQUE treats every NULL as distinct,
  so it would admit the same platform name twice — which
  ``test_plain_unique_admits_it`` shows on a copy of the table.

Real PostgreSQL (15+; the tree runs 16), rows written with SQL so the
table is tested, not the service.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.test_secrets_service import db  # noqa: F401  (the fixture)

INSERT = (
    "INSERT INTO {table} (scope, tenant_id, agent_id, name, ciphertext, key_id, fingerprint, created_by)"
    " VALUES (:scope, :tenant, :agent, :name, :c, 'k' || repeat('0', 15), repeat('f', 12), :by)"
)


async def _insert(db, scope, *, tenant=None, agent=None, name="auth.azure_client_secret",
                  by=None, table="secrets") -> None:
    await db.execute(
        text(INSERT.format(table=table)),
        {"scope": scope, "tenant": tenant, "agent": agent, "name": name, "c": b"gAAAAA", "by": by},
    )


async def _refused(db, *args, **kwargs) -> str | None:
    """The constraint that refused the row, or None when it was admitted.
    In a SAVEPOINT, so a refusal does not end the test's transaction."""
    savepoint = await db.begin_nested()
    try:
        await _insert(db, *args, **kwargs)
    except IntegrityError as exc:
        await savepoint.rollback()
        return str(exc.orig)
    await savepoint.commit()
    return None


async def _tenant(db) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, :n, :s)"),
        {"id": tenant_id, "n": "Probe", "s": f"probe-{tenant_id.hex[:12]}"},
    )
    return tenant_id


@pytest.mark.asyncio
async def test_duplicate_platform_name_refused(db):  # noqa: F811
    assert await _refused(db, "platform") is None
    refusal = await _refused(db, "platform")
    assert refusal is not None and "uq_secrets_owner_name" in refusal

    # One name per OWNER: the same name under another owner is admitted.
    tenant = await _tenant(db)
    assert await _refused(db, "gateway") is None
    assert await _refused(db, "agent", agent="probe-v1") is None
    assert await _refused(db, "agent", agent="other-v1") is None
    assert await _refused(db, "tenant", tenant=tenant, agent="probe-v1") is None
    assert "uq_secrets_owner_name" in (await _refused(db, "agent", agent="probe-v1") or "")
    assert "uq_secrets_owner_name" in (
        await _refused(db, "tenant", tenant=tenant, agent="probe-v1") or ""
    )


@pytest.mark.asyncio
async def test_plain_unique_admits_it(db):  # noqa: F811
    """The control for the test above: the same table with a plain UNIQUE
    takes the duplicate, because its owner columns are NULL and a plain
    UNIQUE never compares NULLs. What refuses it is NULLS NOT DISTINCT."""
    await db.execute(
        text(
            "CREATE TEMP TABLE probe_plain_unique (LIKE secrets INCLUDING DEFAULTS,"
            " UNIQUE (scope, tenant_id, agent_id, name))"
        )
    )
    assert await _refused(db, "platform", table="probe_plain_unique") is None
    assert await _refused(db, "platform", table="probe_plain_unique") is None
    count = (await db.execute(text("SELECT count(*) FROM probe_plain_unique"))).scalar()
    assert count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,tenant,agent,admitted",
    [
        ("platform", False, False, True),
        ("platform", True, False, False),
        ("platform", False, True, False),
        ("gateway", False, False, True),
        ("gateway", False, True, False),
        ("agent", False, True, True),
        ("agent", False, False, False),
        ("agent", True, True, False),
        ("tenant", True, True, True),
        ("tenant", True, False, False),
        ("tenant", False, True, False),
        ("elsewhere", False, False, False),
    ],
)
async def test_scope_check(db, scope, tenant, agent, admitted):  # noqa: F811
    tenant_id = await _tenant(db) if tenant else None
    refusal = await _refused(db, scope, tenant=tenant_id, agent="probe-v1" if agent else None)
    if admitted:
        assert refusal is None, refusal
    else:
        assert refusal is not None and "ck_secrets_scope_owner" in refusal, refusal


@pytest.mark.asyncio
async def test_tenant_delete_cascades(db):  # noqa: F811
    tenant = await _tenant(db)
    await _insert(db, "tenant", tenant=tenant, agent="probe-v1", name="tool_key")
    await _insert(db, "platform", name="kept.across.tenants")

    await db.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant})

    names = (await db.execute(text("SELECT name FROM secrets"))).scalars().all()
    assert "tool_key" not in names
    assert "kept.across.tenants" in names


@pytest.mark.asyncio
async def test_a_deleted_user_leaves_the_row_and_clears_the_name(db):  # noqa: F811
    tenant = await _tenant(db)
    user_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, auth_provider, role)"
            " VALUES (:id, :t, :e, 'credentials', 'admin')"
        ),
        {"id": user_id, "t": tenant, "e": f"{user_id.hex[:10]}@example.com"},
    )
    await _insert(db, "platform", by=user_id)

    await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})

    rows = (await db.execute(text("SELECT created_by FROM secrets"))).all()
    assert rows == [(None,)]
