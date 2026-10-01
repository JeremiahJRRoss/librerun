"""An agent's tool secrets: the policy over K6's store (K8a; D20, D32, D35, D36).

An agent names the third-party secrets it may ask for in its manifest's
``secrets[]`` — a search key, a vector-store key — and nothing else: not a
model provider's key, which the gateway alone holds (L23), and not a
platform setting (D35, ``reserved_reason``). Each name has two rows in
K6's ``secrets`` table (D32):

* ``tenant`` — this tenant's value, set by this tenant's admin;
* ``agent`` — every tenant's default, set by a platform admin.

A run reads its tenant's value in-process through ``caps.secrets.get``
or, in a container, through the MCP ``secret_get`` tool. ``resolve`` is
the one order both follow: the ``tenant`` row, then the ``agent`` row —
each through K6's ``get_secret``, with no environment, so a row no
configured key opens falls through to the next rather than answering —
and then, for an in-process agent alone, the upper-cased name in the
backend's environment (``X_Y`` for ``x_y``), the
fallback L20 has always given the process an agent runs in, and which
D35 keeps. The fallback is never a reserved name and never a value
shorter than ``MIN_CHARS``: the runner scrubs every value it delivered
from what the run persists (``scrub_values``, D20), and a placeholder
such as ``none`` in an operator's ``.env`` would otherwise rewrite every
match in a report. A container's own environment is its own, and the
platform neither reads nor reports it.

The values, their ciphertext and every write of a value go through K6's
``secrets_service``: this module writes a value only through its
``set_secret`` and ``unset_secret``, whose caller commits and then calls
``notify_change`` (the route does, §11's review after K7). It touches one
column itself, ``last_used_at``, the one K6 left for it: ``stamp``
writes it at most once an hour per row per process, in a session of its
own, bumping no version, a failure dropped — a record that a run read the
row, never what it read. The environment fallback records nothing.

Nothing here logs a value, and nothing returns one but ``resolve``, to the
façade that delivers it to the declaring run in its own tenant — the one
exception L31 makes (D20).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Iterable
from uuid import UUID

import structlog
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.secret import Secret
from app.services import secrets_service

if TYPE_CHECKING:  # the manifest imports this module inside a validator
    from app.agents.manifest import AgentManifest

logger = structlog.get_logger(__name__)

# The scopes a tool secret is set in (D32): this tenant's value, and every
# tenant's default.
SCOPES = ("tenant", "agent")

# A value's length, stripped: the floor keeps a placeholder out of the
# scrub set (a stored row is held to it, and an environment value under it
# is treated as unset); the ceiling is far past any real API key.
MIN_CHARS = 8
MAX_CHARS = 4096

# How often one process stamps one row's ``last_used_at``.
STAMP_SECONDS = 3600.0

# Names no tool secret may take, upper-cased (D35): the platform's own
# namespaces. A setting of the backend's and a provider key are reserved
# by name, in ``reserved_reason``.
_RESERVED_PREFIXES = ("LIBRERUN_", "OTEL_")


class SecretNotDeclared(KeyError):
    """A tool secret the agent's manifest does not declare in ``secrets[]``.
    Carries the name, never a value. A ``KeyError``, as a missing key of a
    mapping is, so an agent's ``except KeyError`` reads it."""

    code = "secret_not_declared"

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return f"secret {self.name!r} is not declared in this agent's secrets[]"


class SecretNotSet(KeyError):
    """A declared tool secret with no value: no ``tenant`` row, no ``agent``
    row and, in-process only, no usable fallback in the environment.
    Carries the name, never a value."""

    code = "secret_not_set"

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return f"secret {self.name!r} is declared but has no value in this tenant"


class SecretValueInvalid(ValueError):
    """A value the admin API refuses: not a string, or outside
    ``MIN_CHARS``..``MAX_CHARS`` once stripped. Never carries the value."""

    code = "secret_value_invalid"


def reserved_reason(name: str) -> str | None:
    """Why ``name`` may not be a tool secret, or ``None`` (L23, D35).

    Judged upper-cased, as the environment fallback reads it: a field of
    the backend's settings model or its ``_FILE`` spelling, a model
    provider's key variable, or a name in the platform's ``LIBRERUN_`` or
    ``OTEL_`` namespace. A tool secret named like one of those would make
    the fallback hand an agent the platform's own value — which is also
    what keeps L13: an agent's key put back into the backend's settings
    model fails the manifest of every agent that declares it.
    """
    from app.config import PROVIDER_KEY_VARIABLES, Settings

    upper = name.upper()
    fields = {field.upper() for field in Settings.model_fields}
    if upper in fields:
        return f"{upper} is a setting of the backend's own"
    if upper.endswith("_FILE") and upper[: -len("_FILE")] in fields:
        return f"{upper} is the _FILE spelling of a setting of the backend's own"
    if upper in PROVIDER_KEY_VARIABLES:
        return f"{upper} is a model provider's key, which the gateway alone holds (L23)"
    for prefix in _RESERVED_PREFIXES:
        if upper.startswith(prefix):
            return f"{upper} is in the platform's {prefix} namespace"
    return None


def owner(scope: str, tenant_id: UUID | None, agent_id: str) -> secrets_service.Owner:
    """The K6 owner of one of an agent's rows: ``tenant`` names the tenant
    and the agent, ``agent`` the agent alone (D32)."""
    if scope == "tenant":
        return secrets_service.Owner("tenant", tenant_id, agent_id)
    if scope == "agent":
        return secrets_service.Owner("agent", None, agent_id)
    raise ValueError(f"a tool secret's scope is 'tenant' or 'agent', not {scope!r}")


# ----- reading a value -------------------------------------------------------


def _nothing() -> str:
    return ""


def environment_value(name: str, *, log: bool = True) -> str | None:
    """The in-process fallback: the upper-cased name in the backend's
    environment, stripped — or ``None`` when it is reserved, unset or
    shorter than ``MIN_CHARS`` (logged by name, never by value)."""
    if reserved_reason(name) is not None:
        return None
    variable = name.upper()
    value = os.environ.get(variable, "").strip()
    if not value:
        return None
    if len(value) < MIN_CHARS:
        if log:
            logger.warning(
                "tool_secret_fallback_too_short",
                variable=variable,
                minimum=MIN_CHARS,
                hint="treated as unset: a value this short would be scrubbed "
                "from every report it matches",
            )
        return None
    return value


@dataclass(frozen=True)
class Resolved:
    """A delivered value and where it came from: ``tenant``, ``agent`` or
    ``environment``. ``row`` is the K6 owner of the row that served it,
    ``None`` for the environment."""

    value: str
    source: str
    row: secrets_service.Owner | None


async def _rows_value(
    db: AsyncSession, tenant_id: UUID, agent_id: str, name: str
) -> Resolved | None:
    for scope in SCOPES:
        row = owner(scope, tenant_id, agent_id)
        value = await secrets_service.get_secret(db, row, name, env=_nothing)
        if value:
            return Resolved(value=value, source=scope, row=row)
    return None


async def resolve(
    tenant_id: UUID, agent_id: str, name: str, *, env_fallback: bool
) -> Resolved | None:
    """This tenant's value for ``name``, or ``None``: the ``tenant`` row,
    the ``agent`` row, then — with ``env_fallback`` alone, which the
    runner sets for an in-process agent — the environment. Reads in a
    session of its own, as the façade's ``config`` does."""
    from app import database

    async with database.async_session() as db:
        found = await _rows_value(db, tenant_id, agent_id, name)
    if found is not None:
        return found
    if env_fallback:
        value = environment_value(name)
        if value is not None:
            return Resolved(value=value, source="environment", row=None)
    return None


_STAMPED: dict[tuple, float] = {}


async def stamp(row: secrets_service.Owner, name: str) -> None:
    """Record that a run read ``row``'s ``name``: ``last_used_at``, at most
    once per ``STAMP_SECONDS`` per row in this process, in a session of its
    own and with no version bump — nothing cached depends on it. A failed
    write is dropped, not retried within the hour: a missed stamp costs a
    timestamp, and a retry per read would cost a write per read."""
    key = (row.scope, row.tenant_id, row.agent_id, name)
    now = time.monotonic()
    last = _STAMPED.get(key)
    if last is not None and now - last < STAMP_SECONDS:
        return
    _STAMPED[key] = now
    from app import database

    try:
        async with database.async_session() as db:
            await db.execute(
                update(Secret)
                .where(secrets_service._where(row, name))
                .values(last_used_at=func.now())
                .execution_options(synchronize_session=False)
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — a timestamp, never a run's fate
        logger.warning(
            "tool_secret_stamp_failed", scope=row.scope, error_type=type(exc).__name__
        )


def forget_stamps() -> None:
    """Forget which rows this process has stamped (tests)."""
    _STAMPED.clear()


async def scrub_values(
    tenant_id: UUID, agent_id: str, names: Iterable[str], *, env_fallback: bool
) -> list[str]:
    """Every declared name's value in this tenant, de-duplicated and
    sorted longest first — the set the runner scrubs from what a run
    persists (D20), so that a value containing another is replaced whole.
    Reading them stamps nothing: this is not a delivery."""
    values: set[str] = set()
    for name in names:
        found = await resolve(tenant_id, agent_id, name, env_fallback=env_fallback)
        if found is not None:
            values.add(found.value)
    return sorted(values, key=lambda value: (-len(value), value))


# ----- the admin API's half --------------------------------------------------


def _declared(manifest: "AgentManifest", name: str) -> None:
    if name not in manifest.secrets:
        raise SecretNotDeclared(name)


def checked_value(value: object) -> str:
    """``value`` stripped, or ``SecretValueInvalid`` saying why — never
    what it was."""
    if not isinstance(value, str):
        raise SecretValueInvalid("a secret's value is a string")
    stripped = value.strip()
    if not MIN_CHARS <= len(stripped) <= MAX_CHARS:
        raise SecretValueInvalid(
            f"a secret's value is {MIN_CHARS} to {MAX_CHARS} characters once "
            "surrounding whitespace is removed"
        )
    return stripped


async def set_value(
    db: AsyncSession,
    manifest: "AgentManifest",
    scope: str,
    tenant_id: UUID,
    name: str,
    value: object,
    *,
    user_id: UUID | None,
) -> secrets_service.SecretWrite:
    """Seal ``value`` into ``scope``'s row for ``name``: the declaration,
    the scope and the value checked first, in that order, and nothing
    written on a refusal. The caller commits, then calls
    ``secrets_service.notify_change``."""
    _declared(manifest, name)
    row = owner(scope, tenant_id, manifest.id)
    return await secrets_service.set_secret(db, row, name, checked_value(value), user_id=user_id)


async def clear_value(
    db: AsyncSession, manifest: "AgentManifest", scope: str, tenant_id: UUID, name: str
) -> bool:
    """Remove ``scope``'s row for ``name``; whether there was one. The
    caller commits, then calls ``secrets_service.notify_change``."""
    _declared(manifest, name)
    return await secrets_service.unset_secret(db, owner(scope, tenant_id, manifest.id), name)


@dataclass(frozen=True)
class RowState:
    """One row as the admin API describes it: never a value. ``fingerprint``
    is ``None`` for a row no configured key opens, which ``resolve``
    passes over."""

    set: bool
    readable: bool
    fingerprint: str | None
    updated_at: datetime | None
    updated_by: UUID | None
    last_used_at: datetime | None

    @classmethod
    def unset(cls) -> "RowState":
        return cls(False, False, None, None, None, None)


async def _row_states(
    db: AsyncSession, row: secrets_service.Owner
) -> dict[str, RowState]:
    metas = {meta.name: meta for meta in await secrets_service.list_secrets(db, row)}
    if not metas:
        return {}
    used = dict(
        (
            await db.execute(
                select(Secret.name, Secret.last_used_at).where(
                    and_(secrets_service._where(row), Secret.name.in_(sorted(metas)))
                )
            )
        ).all()
    )
    return {
        name: RowState(
            set=True,
            readable=meta.readable,
            fingerprint=meta.fingerprint if meta.readable else None,
            updated_at=meta.updated_at,
            updated_by=meta.updated_by,
            last_used_at=used.get(name),
        )
        for name, meta in metas.items()
    }


@dataclass(frozen=True)
class SecretState:
    """What the admin API says of one declared name in one tenant."""

    name: str
    effective: str
    tenant: RowState
    agent: RowState
    # ``None`` for a container: its environment is its own.
    environment: bool | None


def _effective(tenant: RowState, agent: RowState, environment: bool | None) -> str:
    if tenant.readable:
        return "tenant"
    if agent.readable:
        return "agent"
    if environment:
        return "environment"
    return "unset"


async def states(
    db: AsyncSession, manifest: "AgentManifest", tenant_id: UUID
) -> list[SecretState]:
    """Every declared name, in declaration order, as this tenant's runs
    would resolve it now — without opening a row: readability is K6's
    key-id check, and the environment is asked only for an in-process
    agent."""
    tenant_rows = await _row_states(db, owner("tenant", tenant_id, manifest.id))
    agent_rows = await _row_states(db, owner("agent", None, manifest.id))
    in_process = manifest.runtime != "container"
    out = []
    for name in manifest.secrets:
        tenant = tenant_rows.get(name, RowState.unset())
        agent = agent_rows.get(name, RowState.unset())
        environment = (
            environment_value(name, log=False) is not None if in_process else None
        )
        out.append(
            SecretState(
                name=name,
                effective=_effective(tenant, agent, environment),
                tenant=tenant,
                agent=agent,
                environment=environment,
            )
        )
    return out
