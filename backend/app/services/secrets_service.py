"""The encrypted secrets store: the backend's rows (K6; L30, L31, D14, D32, D33).

A secret set in the admin UI is a row in ``secrets``: MultiFernet
ciphertext under ``LIBRERUN_BACKEND_SECRETS_KEY``, the id of the key that
sealed it, and a keyed fingerprint of the value (``app.secrets_keyring``).
Every value of the backend's own rows — scopes ``platform``, ``agent``
and ``tenant`` — goes into that table and comes out of it through this
module. Three other backend modules touch the table: the rotation script
(``app.scripts.rewrap_secrets``), which re-seals each row's ciphertext in
place; ``tool_secrets_service`` (K8a), which reads and stamps
``last_used_at`` through ``_where`` and no value; and
``provider_keys_service`` (K7), which stores, lists and deletes the
``gateway`` rows — blobs sealed in the browser that no backend key opens.
Those rows are the gateway process's, which opens and re-seals them
(``gateway.provider_store``) and rotates them (``gateway.rewrap``).

What it promises (L31), and the tests that hold each:

* **Never a value out.** Nothing here returns a value except
  ``get_secret``, whose caller uses it (the Microsoft sign-in, K8a's tool
  secrets). No log line, exception or return of a list carries a value or
  a ciphertext; ``list_secrets`` does not even select the ciphertext.
* **Never in Redis.** Redis holds one counter, ``secrets:version``, which
  every writer bumps once its row is committed (``notify_change``). A
  reader keeps what it decrypted in this process for at most 30 seconds,
  and only while that counter holds; with no Redis it keeps nothing and
  reads the row every time.
* **Every query names its owner** — scope, tenant (``IS NULL`` for
  ``platform`` and ``agent``), agent and name — so no read or write can
  reach another owner's row by leaving a column out (D32). The
  ``gateway`` scope is refused: those rows are sealed to the gateway's own
  key (K7), and the backend can open none of them.
* **A blank key is "unconfigured"** in every mode (D33): a write raises
  ``SecretsStoreUnconfigured``, which the app answers ``503
  secrets_store_unconfigured``; a read falls back to the environment, so a
  deployment that never sets a key loses nothing it has today (L29). So
  does a row no configured key opens — a lost or rotated key, or a restore
  under another one — which ``list_secrets`` reports as unreadable rather
  than pretending it is live. It decides by key id and never reads a
  ciphertext (§4), so a row damaged in place under a configured key lists
  as set: ``get_secret`` logs it as it falls back, and the rewrap script,
  which opens every row, names it.

``set_secret`` and ``unset_secret`` write and do not commit: the route
commits the row and its audit together, then calls ``notify_change``, as
K7's provider-key routes do. Bumped any earlier — while the write was
merely flushed, as K6 first did — a reader in another request could read
the new version and the old row and keep that row for the cache's 30
seconds. Those 30 seconds now bound only a bump that did not reach Redis.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import structlog
from sqlalchemy import and_, delete, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import config as _config
from app import secrets_keyring as keyring
from app.models.secret import Secret
from app.redis import get_redis

logger = structlog.get_logger(__name__)

# How long a value this process decrypted is reused while the version
# counter holds.
CACHE_SECONDS = 30.0

KEY_VARIABLE = "LIBRERUN_BACKEND_SECRETS_KEY"


class SecretsStoreUnconfigured(RuntimeError):
    """A secret write with no store key configured.

    The app answers it ``503 {"detail", "code": "secrets_store_unconfigured"}``
    (``app.main``), so every endpoint that writes through this service gets
    the same refusal without catching it: the request was fine, the
    deployment is missing a key an operator can add.
    """

    code = "secrets_store_unconfigured"
    detail = (
        "The secrets store has no key, so a secret cannot be stored. Set "
        f"{KEY_VARIABLE} (docs/platform/Install.md, \"The secrets store key\") "
        "and restart the backend; until then the environment's value applies."
    )

    def __init__(self) -> None:
        super().__init__(self.detail)


class SecretsStoreKeyShared(SecretsStoreUnconfigured):
    """A secret write while the backend's store key also sealed a
    ``gateway`` row (K7; D33, K7-15): the two processes' keys are one, and
    a second row under it would be one more the gateway could open.

    Answered ``503 {"detail", "code": "secrets_store_key_shared"}`` by the
    handler every store refusal shares; the gateway refuses to start in
    the same state, naming both variables.
    """

    code = "secrets_store_key_shared"
    detail = (
        f"{KEY_VARIABLE} is also the gateway's store key (a gateway row is sealed under "
        "it), so a secret cannot be stored until they differ (D33). Generate a new key "
        "for the gateway, put it in the gateway's environment file, and recreate the gateway "
        "(docs/platform/Install.md, \"The gateway's store key\"); the keys are not shown here."
    )


@dataclass(frozen=True)
class Owner:
    """Whose secret: the scope and the columns it uses (D32)."""

    scope: str
    tenant_id: UUID | None = None
    agent_id: str | None = None

    def __post_init__(self) -> None:
        if self.scope == "gateway":
            raise ValueError(
                "gateway-scope secrets are sealed to the gateway's own key (K7): "
                "the backend neither writes nor reads them"
            )
        wants = {
            "platform": (False, False),
            "agent": (False, True),
            "tenant": (True, True),
        }.get(self.scope)
        if wants is None:
            raise ValueError(f"unknown secrets scope {self.scope!r}")
        if wants != (self.tenant_id is not None, self.agent_id is not None):
            raise ValueError(
                f"a {self.scope}-scope secret names "
                + {
                    (False, False): "neither a tenant nor an agent",
                    (False, True): "an agent and no tenant",
                    (True, True): "a tenant and an agent",
                }[wants]
            )


PLATFORM = Owner("platform")


def _where(owner: Owner, name: str | None = None):
    """Every column of the owner, always — ``IS NULL`` where the scope
    names none — and the name when one is asked for."""
    clauses = [
        Secret.scope == owner.scope,
        Secret.tenant_id.is_(None)
        if owner.tenant_id is None
        else Secret.tenant_id == owner.tenant_id,
        Secret.agent_id.is_(None)
        if owner.agent_id is None
        else Secret.agent_id == owner.agent_id,
    ]
    if name is not None:
        clauses.append(Secret.name == name)
    return and_(*clauses)


def configured_keys() -> list[bytes]:
    """The backend's store keys, current first; ``[]`` when blank.

    Read from the settings object at call time, so the value a test or a
    reload puts there is the value used. Raises
    ``keyring.SecretsStoreKeyInvalid`` on a malformed entry, which the
    lifespan refuses at boot (``check_store_key``).
    """
    return keyring.parse(_config.settings.LIBRERUN_BACKEND_SECRETS_KEY, variable=KEY_VARIABLE)


def check_store_key() -> int:
    """The boot check (``app.main``'s lifespan): the number of keys.

    Malformed: raises ``SecretsStoreKeyInvalid`` and the backend does not
    start, because a key that parses as nothing would turn every secret
    write into a 503 and every read into a silent fallback. Blank: one
    line saying what that means, and the backend serves (D33). Neither line
    carries a key.
    """
    keys = configured_keys()
    if not keys:
        logger.warning(
            "secrets_store_unconfigured",
            variable=KEY_VARIABLE,
            hint=(
                "no store key: a secret set in Admin -> Settings is refused "
                "(503 secrets_store_unconfigured) and each secret setting "
                "falls back to the environment. docs/platform/Install.md, "
                "\"The secrets store key\", says how to generate one."
            ),
        )
        return 0
    # Not "secrets_store_key": the log walker's named-entity stage reads
    # that name as a person (0.85) and the line would arrive redacted.
    logger.info(
        "secrets_store_key_loaded",
        variable=KEY_VARIABLE,
        keys=len(keys),
        current_key_id=keyring.key_id(keys[0]),
    )
    return len(keys)


# ----- the version counter and the in-process cache ------------------------


@dataclass
class _Cached:
    value: str | None  # None: no row, or a row no configured key opens
    version: str
    expires: float


_CACHE: dict[tuple, _Cached] = {}


def _cache_key(owner: Owner, name: str) -> tuple:
    return (owner.scope, owner.tenant_id, owner.agent_id, name)


async def _version() -> str | None:
    """The counter, or ``None`` when Redis does not answer — in which case
    nothing is cached, because nothing could say when to stop trusting it."""
    try:
        redis = await get_redis()
        value = await redis.get(keyring.VERSION_KEY)
    except Exception as exc:  # noqa: BLE001 — a cache, not a dependency
        logger.warning("secrets_version_unavailable", error_type=type(exc).__name__)
        return None
    return "0" if value is None else str(value)


async def notify_change(owner: Owner, name: str) -> None:
    """Tell every process to stop trusting what it cached — the writer's
    call once the row is committed, never before: told first, a reader
    could read the new version and the old row, and serve that row under
    the new version for ``CACHE_SECONDS``. This process drops its own entry
    too, so it needs no Redis to see its own write."""
    _CACHE.pop(_cache_key(owner, name), None)
    try:
        redis = await get_redis()
        await redis.incr(keyring.VERSION_KEY)
    except Exception as exc:  # noqa: BLE001 — entries expire in 30 s anyway
        logger.warning("secrets_version_bump_failed", error_type=type(exc).__name__)


def clear_cache() -> None:
    """Forget every value this process decrypted (tests; a key change)."""
    _CACHE.clear()


# ----- writes ---------------------------------------------------------------


async def _refuse_a_shared_key(db: AsyncSession, keys: list[bytes]) -> None:
    """No write while a key this process holds also sealed a gateway row
    (D33): its id is a keyed digest of the key, so equal ids are one key.
    Reads the key ids alone — the backend opens no gateway row."""
    shared = (
        await db.execute(
            select(Secret.id)
            .where(Secret.scope == "gateway", Secret.key_id.in_(sorted(keyring.key_ids(keys))))
            .limit(1)
        )
    ).first()
    if shared is not None:
        raise SecretsStoreKeyShared()


@dataclass(frozen=True)
class SecretWrite:
    """What a write did: ``set`` a new row or ``replace`` one, and the
    metadata the API may show. Never the value."""

    action: str
    fingerprint: str
    updated_at: datetime
    updated_by: UUID | None


async def set_secret(
    db: AsyncSession,
    owner: Owner,
    name: str,
    value: str,
    *,
    user_id: UUID | None,
) -> SecretWrite:
    """Seal ``value`` under the current key and upsert it: one statement,
    whose ``xmax = 0`` tells a new row from a replaced one. The caller
    commits, then calls ``notify_change``."""
    keys = configured_keys()
    if not keys:
        raise SecretsStoreUnconfigured()
    if not isinstance(value, str) or not value:
        raise ValueError(f"secret {name!r}: a value is a non-empty string")
    await _refuse_a_shared_key(db, keys)
    sealed = keyring.seal(keys, value)
    statement = (
        pg_insert(Secret)
        .values(
            scope=owner.scope,
            tenant_id=owner.tenant_id,
            agent_id=owner.agent_id,
            name=name,
            ciphertext=sealed.ciphertext,
            key_id=sealed.key_id,
            fingerprint=sealed.fingerprint,
            created_by=user_id,
            updated_by=user_id,
        )
        .on_conflict_do_update(
            constraint="uq_secrets_owner_name",
            set_={
                "ciphertext": sealed.ciphertext,
                "key_id": sealed.key_id,
                "fingerprint": sealed.fingerprint,
                "updated_at": func.now(),
                "updated_by": user_id,
            },
        )
        .returning(
            literal_column("xmax = 0").label("inserted"),
            Secret.fingerprint,
            Secret.updated_at,
            Secret.updated_by,
        )
    )
    row = (await db.execute(statement)).one()
    return SecretWrite(
        action="set" if row.inserted else "replace",
        fingerprint=row.fingerprint,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


async def unset_secret(db: AsyncSession, owner: Owner, name: str) -> bool:
    """Remove the row; whether there was one. Needs no key: deleting
    ciphertext stores nothing, and a row nothing opens any more must stay
    removable after the key that sealed it is gone. The caller commits,
    then calls ``notify_change``."""
    result = await db.execute(
        delete(Secret).where(_where(owner, name)).returning(Secret.id)
    )
    return result.first() is not None


# ----- reads ----------------------------------------------------------------


@dataclass(frozen=True)
class SecretMeta:
    """A row as the API may describe it: no value, no ciphertext.
    ``readable`` is whether a configured key's id is the row's."""

    scope: str
    tenant_id: UUID | None
    agent_id: str | None
    name: str
    fingerprint: str
    readable: bool
    created_at: datetime
    updated_at: datetime
    updated_by: UUID | None


async def list_secrets(db: AsyncSession, owner: Owner) -> list[SecretMeta]:
    """Every row the owner has, never selecting the ciphertext.

    Readability is decided by ``key_id`` alone — no decrypt — against the
    ids of the configured keys, so a row sealed under a key that is gone
    lists as unreadable while the environment serves in its place.
    """
    ids = keyring.key_ids(configured_keys())
    rows = (
        await db.execute(
            select(
                Secret.scope,
                Secret.tenant_id,
                Secret.agent_id,
                Secret.name,
                Secret.key_id,
                Secret.fingerprint,
                Secret.created_at,
                Secret.updated_at,
                Secret.updated_by,
            )
            .where(_where(owner))
            .order_by(Secret.name)
        )
    ).all()
    return [
        SecretMeta(
            scope=row.scope,
            tenant_id=row.tenant_id,
            agent_id=row.agent_id,
            name=row.name,
            fingerprint=row.fingerprint,
            readable=row.key_id in ids,
            created_at=row.created_at,
            updated_at=row.updated_at,
            updated_by=row.updated_by,
        )
        for row in rows
    ]


async def _open_row(db: AsyncSession, owner: Owner, name: str, keys: list[bytes]) -> str | None:
    row = (
        await db.execute(
            select(Secret.ciphertext, Secret.key_id).where(_where(owner, name))
        )
    ).first()
    if row is None:
        return None
    if row.key_id not in keyring.key_ids(keys):
        reason = "no configured key has this row's key id"
    else:
        try:
            return keyring.unseal(keys, row.ciphertext)
        except keyring.InvalidToken:
            reason = "the row does not open under the key its id names"
    logger.warning(
        "secret_unreadable",
        scope=owner.scope,
        tenant_id=str(owner.tenant_id) if owner.tenant_id else None,
        agent_id=owner.agent_id,
        name=name,
        reason=reason,
        fallback="environment",
    )
    return None


async def get_secret(
    db: AsyncSession,
    owner: Owner,
    name: str,
    env: Callable[[], str],
) -> str:
    """The value in effect: the row, else ``env()``.

    ``env()`` also answers for a blank key and for a row no configured key
    opens (logged, without the value). A value decrypted here is reused for
    up to ``CACHE_SECONDS`` while ``secrets:version`` holds, in this
    process and nowhere else.
    """
    keys = configured_keys()
    if not keys:
        return env()
    cache_key = _cache_key(owner, name)
    version = await _version()
    if version is not None:
        hit = _CACHE.get(cache_key)
        if hit is not None and hit.version == version and hit.expires > time.monotonic():
            return hit.value if hit.value is not None else env()
    value = await _open_row(db, owner, name, keys)
    if version is not None:
        _CACHE[cache_key] = _Cached(value, version, time.monotonic() + CACHE_SECONDS)
    return value if value is not None else env()
