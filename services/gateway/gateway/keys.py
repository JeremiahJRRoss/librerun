"""Per-agent gateway keys: minting, env reconciliation, and lookup (D10).

A framework that reads ``OPENAI_API_KEY`` from its environment cannot
present a run token — it does not know what a run is. So an agent
container is started with a **LibreRun agent key** in that variable
instead, and this module is what makes one mean something.

What a key is and is not:

* It names an **agent**, never a tenant and never a run. One container
  serves every tenant, so the key alone cannot say whose data a call is
  about. That is why it authenticates ``GET /v1/models`` alone and every
  model call needs ``X-LibreRun-Run-Token`` beside it.
* The **value is never stored**. The row keeps its sha256 and the eight
  characters after ``lr_agent_``, which is what the admin page shows so
  an operator can tell which key they are holding.
* ``key_hash`` is unique across the whole table, so a presented key maps
  to exactly one agent and two agents can never share a value.

Provisioning has a chicken-and-egg that shapes the design: compose
expands ``${LIBRERUN_AGENT_KEY_<ID>}`` **before** any LibreRun service is
running, so a key must exist in ``.env`` before ``up``. The demo script
and the CLI write them; this process reads them at boot and registers
what it finds, rather than issuing them itself.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.config import (
    AGENT_KEY_PREFIX,
    AGENT_KEY_PREVIOUS_SUFFIX,
    KEY_VALUE_PREFIX,
    agent_key_variables,
    settings,
)

logger = structlog.get_logger(__name__)

# The manifest id charset (``app.agents.manifest``). Repeated rather than
# imported so the gateway can read a key variable without loading the
# chassis's manifest module.
_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# An id that inverts to something ending here would be indistinguishable
# from the grace variable of another agent's key.
_RESERVED_ID_SUFFIX = AGENT_KEY_PREVIOUS_SUFFIX.lower().replace("_", "-")


class KeyVariableError(ValueError):
    """A ``LIBRERUN_AGENT_KEY_*`` variable that cannot be read as one
    agent's key. Named at boot with the variable, never the value."""


@dataclass(frozen=True)
class ParsedKeyVariable:
    variable: str
    agent_id: str
    value: str
    role: str  # current | previous


def env_name_for(agent_id: str) -> str:
    """``triage-v1`` -> ``LIBRERUN_AGENT_KEY_TRIAGE_V1`` (D10).

    Compose variable names admit no hyphens, so a raw
    ``${LIBRERUN_AGENT_KEY_triage-v1:?…}`` would parse ``-v1:?…`` as a
    default-value operator and expand a different variable — garbage
    instead of the named failure. The normalisation is upper-case with
    every character outside ``[A-Z0-9]`` replaced by ``_``.

    An id ending in ``-previous`` is refused: its variable would be
    spelled exactly like another agent's rotation variable, and the
    reader has to pick one reading. It picks the rotation, so this id
    could never be provisioned — better a named error at the moment
    something tries than a key that silently belongs to someone else.
    """
    if agent_id.endswith(_RESERVED_ID_SUFFIX):
        raise KeyVariableError(
            f"agent id {agent_id!r} cannot be provisioned from the "
            f"environment: {AGENT_KEY_PREFIX}<ID>{AGENT_KEY_PREVIOUS_SUFFIX} "
            f"already means another agent's outgoing key. Rename the agent."
        )
    return AGENT_KEY_PREFIX + re.sub(r"[^A-Z0-9]", "_", agent_id.upper())


def agent_id_for(variable: str) -> str:
    """The inverse of :func:`env_name_for`.

    On the manifest id charset — lowercase, digits and ``-`` only — the
    normalisation changes exactly one character class, so it is a
    bijection and this recovers the id exactly. A variable whose
    recovered id is not a valid manifest id is refused by name.
    """
    if not variable.startswith(AGENT_KEY_PREFIX):
        raise KeyVariableError(f"{variable} is not an agent key variable")
    body = variable[len(AGENT_KEY_PREFIX) :]
    agent_id = body.lower().replace("_", "-")
    if not _AGENT_ID.match(agent_id):
        raise KeyVariableError(
            f"{variable} does not name a valid agent id (recovered {agent_id!r}; "
            f"agent ids are lowercase letters, digits and hyphens)"
        )
    return agent_id


def parse_key_variables(environ: dict | None = None) -> list[ParsedKeyVariable]:
    """Read the environment into ``(agent_id, value, role)`` entries.

    ``LIBRERUN_AGENT_KEY_<ID>`` is the current key;
    ``LIBRERUN_AGENT_KEY_<ID>_PREVIOUS`` is the value being rotated out,
    which keeps working while the variable exists (an env-provisioned
    previous key has no expiry window — removing the line is what ends
    it). Raises on the two ways a variable can be unreadable: an id off
    the manifest charset, and two agents sharing one value.
    """
    parsed: list[ParsedKeyVariable] = []
    for variable, value in sorted(agent_key_variables(environ).items()):
        role = "current"
        name = variable
        if variable.endswith(AGENT_KEY_PREVIOUS_SUFFIX):
            role = "previous"
            name = variable[: -len(AGENT_KEY_PREVIOUS_SUFFIX)]
            if name == AGENT_KEY_PREFIX.rstrip("_"):
                raise KeyVariableError(
                    f"{variable} names no agent — the _PREVIOUS suffix "
                    f"belongs after an agent id"
                )
        agent_id = agent_id_for(name)
        if not value.startswith(KEY_VALUE_PREFIX):
            # The format is not decoration. An operator who pastes a
            # PROVIDER key here — the obvious mistake, since the variable
            # lands in the container's OPENAI_API_KEY — was previously
            # accepted: reconciliation registered its hash, the gateway
            # honoured it as that agent's credential, and compose handed
            # the agent a live provider credential that an egress-enabled
            # container could spend directly, exactly the thing D10 exists
            # to prevent (Codex P1). Failing here stops `up` by variable
            # name, which is what the prefix was for.
            #
            # The message names the variable and the expected shape, and
            # never the value: the value may BE the provider key.
            raise KeyVariableError(
                f"{variable} does not hold a LibreRun agent key — a value "
                f"must begin {KEY_VALUE_PREFIX!r}. This variable becomes the "
                f"agent's OPENAI_API_KEY, and the gateway is what holds "
                f"provider credentials; if you pasted a provider key here, "
                f"it belongs in the gateway's own environment instead. Issue "
                f"an agent key on the agent's admin page, or let "
                f"scripts/demo.sh write one."
            )
        parsed.append(
            ParsedKeyVariable(
                variable=variable, agent_id=agent_id, value=value, role=role
            )
        )

    # A rotation variable with nothing to rotate to is the one shape the
    # reading above cannot disambiguate: LIBRERUN_AGENT_KEY_FOO_PREVIOUS
    # is agent `foo`'s outgoing key, but it is spelled exactly like agent
    # `foo-previous`'s current one. Read alone it would install a
    # previous-role key with no current — a credential with no expiry and
    # no successor. Refuse it and say both readings.
    current_agents = {e.agent_id for e in parsed if e.role == "current"}
    for entry in parsed:
        if entry.role == "previous" and entry.agent_id not in current_agents:
            raise KeyVariableError(
                f"{entry.variable} is ambiguous: read as agent "
                f"{entry.agent_id!r}'s outgoing key it has no current key to "
                f"replace ({env_name_for(entry.agent_id)} is unset), and read "
                f"as agent {entry.agent_id + _RESERVED_ID_SUFFIX!r}'s current "
                f"key it uses an id the platform cannot provision. Set the "
                f"current key, or rename the agent."
            )

    # Two agents sharing a value would make a presented key ambiguous;
    # the unique index on key_hash is the belt, this is the braces, and
    # compose.sh stops the derivation before `up` so neither is reached.
    by_value: dict[str, str] = {}
    for entry in parsed:
        owner = by_value.get(entry.value)
        if owner is not None and owner != entry.agent_id:
            raise KeyVariableError(
                f"{entry.variable} carries the same value as another agent's "
                f"key ({owner}); a key must name exactly one agent"
            )
        by_value.setdefault(entry.value, entry.agent_id)
    return parsed


def hash_key(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def prefix_of(value: str) -> str:
    """The eight characters after ``lr_agent_``, for the admin page.

    A value that does not carry the prefix — an operator pasting a
    provider key into the variable — still gets a prefix recorded, from
    its own first eight characters, so the page can name what is
    installed rather than showing a blank.
    """
    body = value.strip()
    if body.startswith(KEY_VALUE_PREFIX):
        body = body[len(KEY_VALUE_PREFIX) :]
    return body[:8]


def mint_key() -> str:
    """A fresh key: the recognisable prefix and 256 random bits."""
    return KEY_VALUE_PREFIX + secrets.token_urlsafe(32)


@dataclass
class ResolvedKey:
    agent_id: str
    role: str
    key_hash: str


_LOOKUP = text(
    """
    SELECT agent_id,
           role,
           key_hash,
           (role = 'current'
            OR (role = 'previous'
                AND (previous_until IS NULL OR previous_until > NOW()))) AS usable
      FROM agent_keys
     WHERE key_hash = :key_hash
    """
)


async def resolve(db: AsyncSession, value: str) -> ResolvedKey | None:
    """The agent a presented key belongs to, or None.

    ``current`` is accepted; ``previous`` is accepted while its window is
    open — ``previous_until`` null (an env-provisioned previous key,
    whose life ends when the line is removed) or still ahead. The
    comparison is the database's ``NOW()``, not this process's clock, so
    a gateway with a skewed clock cannot extend or cut a grace window.
    """
    if not value.startswith(KEY_VALUE_PREFIX):
        # Cheaper than a query and, more to the point, true: a bearer
        # that is not shaped like a LibreRun agent key is not one,
        # whatever the table happens to contain. Reconciliation refuses
        # to register such a value now, and this is what makes a row
        # written before it stop working rather than linger.
        return None
    digest = hash_key(value)
    row = (await db.execute(_LOOKUP, {"key_hash": digest})).first()
    if row is None or not row.usable:
        return None
    return ResolvedKey(agent_id=row.agent_id, role=row.role, key_hash=digest)


async def touch(db: AsyncSession, key_hash: str) -> None:
    await db.execute(
        text("UPDATE agent_keys SET last_used_at = NOW() WHERE key_hash = :h"),
        {"h": key_hash},
    )


# --------------------------------------------------------------------------
# Boot reconciliation: the environment is the source of truth for the keys
# it names, and only for those. An admin-issued key the environment does
# not mention is left alone.
# --------------------------------------------------------------------------


@dataclass
class EnvReconcileReport:
    registered: list[str] = None  # agent ids with a current env key
    rotated: list[str] = None  # agent ids whose current value changed
    taken_over: list[str] = None  # agent ids whose admin key was demoted
    removed: int = 0  # env rows whose variable is gone

    def __post_init__(self) -> None:
        self.registered = self.registered or []
        self.rotated = self.rotated or []
        self.taken_over = self.taken_over or []


async def reconcile_env_keys(
    db: AsyncSession, environ: dict | None = None
) -> EnvReconcileReport:
    """Make ``agent_keys`` agree with the ``LIBRERUN_AGENT_KEY_*`` lines.

    Runs at boot, inside one transaction the caller commits:

    * a value the environment names becomes that agent's ``current``
      env-backed key (its ``_PREVIOUS`` companion, if any, the
      ``previous`` one);
    * **takeover is atomic** — when an env line arrives for an agent
      whose current key is admin-issued, that row is demoted to
      ``previous`` with the standard grace window and the env key
      inserted in the same transaction, so a container still holding the
      admin key keeps working until it is recreated;
    * an env-sourced row the environment no longer names is **deleted**,
      which is what makes "replace the value and recreate the gateway"
      refuse the old one.
    """
    report = EnvReconcileReport()
    entries = parse_key_variables(environ)
    by_agent: dict[str, dict[str, ParsedKeyVariable]] = {}
    for entry in entries:
        by_agent.setdefault(entry.agent_id, {})[entry.role] = entry

    # A key must name exactly one agent across the whole table, not only
    # across the environment — an admin-issued key for another agent
    # collides just as badly, and the unique index would fail the boot
    # with a constraint name instead of the variable's.
    for entry in entries:
        digest = hash_key(entry.value)
        other = (
            await db.execute(
                text(
                    "SELECT agent_id FROM agent_keys "
                    "WHERE key_hash = :h AND agent_id <> :a"
                ),
                {"h": digest, "a": entry.agent_id},
            )
        ).scalar_one_or_none()
        if other is not None:
            raise KeyVariableError(
                f"{entry.variable} carries a value already issued to agent "
                f"{other!r}; a key must name exactly one agent"
            )

    named_hashes: list[str] = [hash_key(e.value) for e in entries]

    # 1. Drop env-sourced rows the environment no longer names. Admin rows
    #    are untouched: the environment speaks for env keys only.
    removed = await db.execute(
        text(
            "DELETE FROM agent_keys WHERE source = 'env' "
            "AND key_hash <> ALL(:keep) RETURNING id"
        ),
        {"keep": named_hashes},
    )
    report.removed = len(removed.all())

    grace_hours = int(settings.LIBRERUN_KEY_ROTATION_GRACE_HOURS)

    for agent_id, roles in sorted(by_agent.items()):
        # CURRENT first. The current-key takeover clears the `previous`
        # slot before demoting an admin key into it, so installing the
        # declared predecessor first meant deleting it one step later:
        # on the first reboot after an environment takeover the operator
        # got the admin key they were replacing in the `previous` slot
        # and their own `_PREVIOUS` value rejected (Codex P2). Taking
        # over first leaves the demoted admin key where the declared
        # predecessor then replaces it, which is what declaring it means.
        for role in ("current", "previous"):
            entry = roles.get(role)
            if entry is None:
                continue
            digest = hash_key(entry.value)
            occupant = (
                await db.execute(
                    text(
                        "SELECT id, key_hash, source FROM agent_keys "
                        "WHERE agent_id = :a AND role = :r"
                    ),
                    {"a": agent_id, "r": role},
                )
            ).first()
            if occupant is not None and occupant.key_hash == digest:
                # Already installed — only the bookkeeping can drift.
                await db.execute(
                    text(
                        "UPDATE agent_keys SET source = 'env', "
                        "previous_until = NULL WHERE id = :i"
                    ),
                    {"i": occupant.id},
                )
                if role == "current":
                    report.registered.append(agent_id)
                continue
            if occupant is not None:
                if role == "current" and occupant.source == "admin":
                    # Takeover: the admin key becomes `previous` with the
                    # standard window so a container still on it keeps
                    # working until it is recreated with the env key.
                    await db.execute(
                        text(
                            "DELETE FROM agent_keys "
                            "WHERE agent_id = :a AND role = 'previous'"
                        ),
                        {"a": agent_id},
                    )
                    await db.execute(
                        text(
                            "UPDATE agent_keys SET role = 'previous', "
                            "previous_since = NOW(), "
                            "previous_until = NOW() + make_interval(hours => :h) "
                            "WHERE id = :i"
                        ),
                        {"i": occupant.id, "h": grace_hours},
                    )
                    report.taken_over.append(agent_id)
                else:
                    await db.execute(
                        text("DELETE FROM agent_keys WHERE id = :i"),
                        {"i": occupant.id},
                    )
                if role == "current":
                    report.rotated.append(agent_id)
            # The value may already exist for this agent in the other
            # slot (an operator swapping a key back); move it rather than
            # inserting a second row with the same hash.
            since = "NOW()" if role == "previous" else "NULL"
            moved = await db.execute(
                text(
                    f"UPDATE agent_keys SET role = :r, source = 'env', "
                    f"previous_since = {since}, previous_until = NULL "
                    f"WHERE agent_id = :a AND key_hash = :h RETURNING id"
                ),
                {"a": agent_id, "h": digest, "r": role},
            )
            if moved.first() is None:
                await db.execute(
                    text(
                        f"INSERT INTO agent_keys "
                        f"(agent_id, key_hash, key_prefix, source, role, "
                        f" issued_at, previous_since) "
                        f"VALUES (:a, :h, :p, 'env', :r, NOW(), {since})"
                    ),
                    {
                        "a": agent_id,
                        "h": digest,
                        "p": prefix_of(entry.value),
                        "r": role,
                    },
                )
            if role == "current":
                report.registered.append(agent_id)

    logger.info(
        "agent_keys_reconciled",
        registered=sorted(set(report.registered)),
        rotated=sorted(set(report.rotated)),
        taken_over=sorted(set(report.taken_over)),
        removed=report.removed,
    )
    return report
