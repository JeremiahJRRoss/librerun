"""Issuing, rotating and revoking per-agent gateway keys (D10).

The gateway registers the keys the **environment** carries at boot
(``services/gateway/gateway/keys.py``). This is the other half: the keys
an operator issues from the admin UI for an agent whose container is not
started from ``.env`` — an agent installed after the fact, a framework
pointed at the gateway by hand.

It is **platform-scoped**, and the endpoints that use it are behind the
platform-admin gate: a key names an agent, not a tenant, so a tenant
admin issuing one would be minting a credential that answers for every
tenant's runs.

The value is shown **once**, at issuance, and never stored: only its
sha256 and the eight characters after ``lr_agent_``. An operator who
loses it rotates rather than recovers.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The prefix every issued key carries, so a LibreRun agent key is
# distinguishable from a provider key at a glance.
KEY_VALUE_PREFIX = "lr_agent_"

# How long a rotation leaves the previous key working by default.
DEFAULT_GRACE_HOURS = 24


class KeyError_(RuntimeError):
    """Refused: the request cannot be satisfied as asked."""


@dataclass
class IssuedKey:
    agent_id: str
    value: str
    prefix: str


def mint() -> str:
    """256 random bits behind the recognisable prefix."""
    return KEY_VALUE_PREFIX + secrets.token_urlsafe(32)


def hash_key(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def prefix_of(value: str) -> str:
    body = value.strip()
    if body.startswith(KEY_VALUE_PREFIX):
        body = body[len(KEY_VALUE_PREFIX) :]
    return body[:8]


async def list_keys(db: AsyncSession, agent_id: str | None = None) -> list[dict]:
    """What is installed, for the admin page. Never a value."""
    clause = "WHERE agent_id = :a" if agent_id else ""
    rows = (
        await db.execute(
            text(
                f"SELECT agent_id, key_prefix, source, role, issued_at, issued_by, "
                f"previous_since, previous_until, last_used_at "
                f"FROM agent_keys {clause} ORDER BY agent_id, role"
            ),
            {"a": agent_id} if agent_id else {},
        )
    ).all()
    return [dict(row._mapping) for row in rows]


async def _current(db: AsyncSession, agent_id: str) -> dict | None:
    row = (
        await db.execute(
            text(
                "SELECT id, source FROM agent_keys "
                "WHERE agent_id = :a AND role = 'current'"
            ),
            {"a": agent_id},
        )
    ).first()
    return dict(row._mapping) if row else None


async def issue(
    db: AsyncSession, agent_id: str, issued_by: UUID | None
) -> IssuedKey:
    """A first key for an agent that has none.

    Refuses when a current key already exists — replacing one silently
    would break whatever is holding it. Rotation is the operation that
    replaces a key, and it says so.
    """
    if await _current(db, agent_id) is not None:
        raise KeyError_(
            f"agent {agent_id!r} already has a current key; rotate it instead, "
            f"which keeps the old one working for its grace window"
        )
    value = mint()
    await db.execute(
        text(
            "INSERT INTO agent_keys "
            "(agent_id, key_hash, key_prefix, source, role, issued_by) "
            "VALUES (:a, :h, :p, 'admin', 'current', :u)"
        ),
        {
            "a": agent_id,
            "h": hash_key(value),
            "p": prefix_of(value),
            "u": issued_by,
        },
    )
    await db.flush()
    return IssuedKey(agent_id=agent_id, value=value, prefix=prefix_of(value))


async def rotate(
    db: AsyncSession,
    agent_id: str,
    issued_by: UUID | None,
    grace_hours: int = DEFAULT_GRACE_HOURS,
) -> IssuedKey:
    """A new current key, with the old one demoted for ``grace_hours``.

    Refuses to rotate an **environment-provisioned** key: that value
    lives in ``.env`` and is what the container was started with, so a
    rotation here would break the running container and be undone at the
    gateway's next boot. Those are rotated in ``.env``, by moving the old
    value to the ``_PREVIOUS`` line.
    """
    current = await _current(db, agent_id)
    if current is None:
        return await issue(db, agent_id, issued_by)
    if current["source"] == "env":
        raise KeyError_(
            f"agent {agent_id!r} is provisioned from the environment "
            f"(LIBRERUN_AGENT_KEY_*). Rotate it there — move the old value to "
            f"the _PREVIOUS line and recreate the gateway — or this rotation "
            f"would break the running container and be undone at the next boot"
        )
    # One previous slot per agent: make room before demoting.
    await db.execute(
        text("DELETE FROM agent_keys WHERE agent_id = :a AND role = 'previous'"),
        {"a": agent_id},
    )
    await db.execute(
        text(
            "UPDATE agent_keys SET role = 'previous', previous_since = NOW(), "
            "previous_until = NOW() + make_interval(hours => :h) WHERE id = :i"
        ),
        {"i": current["id"], "h": int(grace_hours)},
    )
    value = mint()
    await db.execute(
        text(
            "INSERT INTO agent_keys "
            "(agent_id, key_hash, key_prefix, source, role, issued_by) "
            "VALUES (:a, :h, :p, 'admin', 'current', :u)"
        ),
        {"a": agent_id, "h": hash_key(value), "p": prefix_of(value), "u": issued_by},
    )
    await db.flush()
    return IssuedKey(agent_id=agent_id, value=value, prefix=prefix_of(value))


async def revoke(db: AsyncSession, agent_id: str) -> int:
    """Drop every admin-issued key for one agent, current and previous.

    Environment keys are left alone: the environment is their source of
    truth and the gateway would reinstall them at its next boot, so
    deleting one here would look like a revocation that silently undid
    itself.
    """
    result = await db.execute(
        text(
            "DELETE FROM agent_keys WHERE agent_id = :a AND source = 'admin' "
            "RETURNING id"
        ),
        {"a": agent_id},
    )
    return len(result.all())


def finished_grace(row: dict, now: datetime | None = None) -> bool:
    until = row.get("previous_until")
    return bool(until and now and until <= now)
