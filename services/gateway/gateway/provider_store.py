"""Provider keys pasted in the admin UI, held by this process alone (K7).

L33: a provider key entered in the UI never exists in plaintext in the
backend. The browser seals it to this process's public key (D15,
``gateway/sealing.py``); the backend stores the blob it cannot open as the
``gateway``-scope row ``provider.<name>``, ``key_id = 'sealed'``, and bumps
``secrets:version``; this module adopts it — opens the blob, re-encrypts
the key under ``LIBRERUN_GATEWAY_SECRETS_KEY`` with K6's keyring, as every
other row in the store is kept — and serves it to the next model call,
nothing restarted. ``gateway.env`` still works: a stored key wins over the
environment's for its provider, and the environment serves every provider
with no stored key (L29).

**The names** (K7-02): one per provider-key variable of
``egress._PROVIDER_KEYS`` — ``openai``, ``anthropic``, ``google`` — each
serving the aliases that variable serves; Bedrock is unmapped.

**The rows**, scope ``gateway``, under this process's store key and no
other (D33): ``provider.<name>`` and ``sealing.private_key``, the sealing
keypair, generated here at first boot. The backend neither opens nor
writes anything but the blobs (``app.services.provider_keys_service``).

**Boot** (``start``, after the agent keys are reconciled): a malformed
store key refuses to boot; a blank one serves the environment alone and
publishes no public key (K7-07). Tables the backend's migration has not
made yet are waited for by the refresher, never created here — the gateway
starts first (K7-09). Then, in order:

(a) a key id of this process's key on another scope's row refuses boot,
    naming both variables: the two keys must differ (D33, K7-15);
(b) a keypair row no configured key opens refuses boot and is never
    replaced — replacing it would orphan every blob sealed to it, silently.
    With no row, one is inserted ``ON CONFLICT DO NOTHING``, and the loser
    of two replicas starting together reads the winner's (K7-08);
(c) the rows load, ``gateway_status`` is upserted, and the sealing key's
    fingerprint is logged, for the operator to compare with the one the
    admin page seals to (D34, K7-05).

**The refresher**, an asyncio task on its own sessions — a request's
session ends at ``db.release`` — reads ``secrets:version`` every 2 s and
reloads on a change, and every 30 s regardless, Redis or no Redis (K7-01).
It adopts each ``'sealed'`` row by an ``UPDATE … WHERE key_id = 'sealed'
AND fingerprint = <the blob's id>``, so of two replicas one wins and the
other reads the result; ``updated_at`` is kept, which is how the backend
tells an adopted row from one this process has not seen. A blob that does
not open stays in the table ``rejected`` (``unsealable``), as does a Fernet
row no configured key opens (``unopenable``), and the environment serves
that provider meanwhile. Late tables get the same checks as boot, and (a)
or (b) failing then stops the process, which compose restarts into the
same refusal at boot.

**Never a value out** (L31): no log line, exception, status entry or
return but ``runtime_key`` carries a key, and that one is read by
``egress.credential_for`` at the moment the key is attached to a call.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime

import structlog
from sqlalchemy import text

from app import secret_files
from app import secrets_keyring as keyring
from gateway import config as _config
from gateway import db, sealing
from gateway.version import __version__

logger = structlog.get_logger(__name__)

KEY_VARIABLE = "LIBRERUN_GATEWAY_SECRETS_KEY"
BACKEND_KEY_VARIABLE = "LIBRERUN_BACKEND_SECRETS_KEY"
SCOPE = "gateway"
KEYPAIR_ROW = "sealing.private_key"
ROW_PREFIX = "provider."
# The key id of a blob the backend stored and this process has not
# adopted: not a digest of any key, so no configured key's id matches it.
SEALED = "sealed"

POLL_SECONDS = 2.0
FULL_RELOAD_SECONDS = 30.0

# Each name, and the variable whose key a stored one takes the place of.
VARIABLES = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_AI_API_KEY",
}
NAMES = tuple(VARIABLES)


class BootRefused(RuntimeError):
    """The gateway must not serve: (a) or (b) of the module docstring.
    The message says what to do and carries no key and no value."""


class _TablesMissing(Exception):
    """``secrets`` or ``gateway_status`` does not exist yet."""


def aliases(name: str) -> list[str]:
    """The provider spellings ``name`` answers for — every alias of its
    variable in ``egress._PROVIDER_KEYS``, in that table's order."""
    from gateway.egress import _PROVIDER_KEYS

    variable = VARIABLES[name]
    return [alias for alias, var in _PROVIDER_KEYS.items() if var == variable]


def name_for(provider: str | None) -> str | None:
    """The name whose stored key serves ``provider``, or ``None``."""
    from gateway.egress import _PROVIDER_KEYS

    variable = _PROVIDER_KEYS.get((provider or "").lower())
    for name, var in VARIABLES.items():
        if var == variable:
            return name
    return None


def row_name(name: str) -> str:
    return f"{ROW_PREFIX}{name}"


def configured_keys() -> list[bytes]:
    """This process's store keys, current first; ``[]`` when blank. Raises
    ``keyring.SecretsStoreKeyInvalid`` on a malformed entry."""
    return keyring.parse(_config.settings.LIBRERUN_GATEWAY_SECRETS_KEY, variable=KEY_VARIABLE)


# --------------------------------------------------------------------------
# What this process holds
# --------------------------------------------------------------------------


@dataclass
class _State:
    runtime: dict[str, str] = field(default_factory=dict)
    public_pem: str | None = None
    entries: list[dict] = field(default_factory=list)
    written: str | None = None
    loaded: bool = False
    version: str | None = None


_state = _State()
_task: asyncio.Task | None = None


def runtime_key(provider: str | None) -> str | None:
    """The stored key serving ``provider``, if one is in effect. Read by
    ``egress.credential_for`` alone, at the moment it attaches a key."""
    name = name_for(provider)
    return _state.runtime.get(name) if name else None


def runtime_values() -> list[str]:
    """Every stored key in effect, for ``egress._scrubbed`` to remove by
    value from a provider's error text."""
    return list(_state.runtime.values())


def public_key_pem() -> str | None:
    return _state.public_pem


def entries() -> list[dict]:
    """The status entries as last computed: names, sources, fingerprints."""
    return [dict(entry) for entry in _state.entries]


def reset() -> None:
    """Forget everything this process loaded (tests)."""
    global _state
    _state = _State()


def _env_set(name: str) -> bool:
    value = secret_files.reveal(getattr(_config.settings, VARIABLES[name], ""))
    return bool(value.strip())


# --------------------------------------------------------------------------
# The load: (a), (b), (c)
# --------------------------------------------------------------------------


async def _tables_present(session) -> bool:
    present = (
        await session.execute(
            text(
                "SELECT to_regclass('secrets') IS NOT NULL "
                "AND to_regclass('gateway_status') IS NOT NULL"
            )
        )
    ).scalar()
    return bool(present)


async def _refuse_a_shared_key(session, keys: list[bytes]) -> None:
    """(a): no row of another scope may carry the id of a key this process
    holds — that row was sealed with the same key, so the two processes'
    keys are one (D33)."""
    ids = sorted(keyring.key_ids(keys))
    row = (
        await session.execute(
            text(
                "SELECT scope FROM secrets "
                "WHERE scope <> 'gateway' AND key_id = ANY(:ids) LIMIT 1"
            ),
            {"ids": ids},
        )
    ).first()
    if row is not None:
        raise BootRefused(
            f"{KEY_VARIABLE} holds a key that also sealed a {row.scope}-scope row, so it is "
            f"{BACKEND_KEY_VARIABLE} too. The two store keys must differ (D33): generate a new "
            f"key for the gateway (head -c 32 /dev/urandom | base64 | tr '+/' '-_') and put it "
            f"in gateway.env; the backend's key stays as it is. The keys are not shown here."
        )


async def _row(session, name: str):
    return (
        await session.execute(
            text(
                "SELECT id, ciphertext, key_id, fingerprint, updated_at, updated_by "
                "FROM secrets WHERE scope = 'gateway' AND tenant_id IS NULL "
                "AND agent_id IS NULL AND name = :name"
            ),
            {"name": name},
        )
    ).first()


async def _keypair(session, keys: list[bytes]):
    """(b): the sealing keypair, made once and shared by every replica."""
    row = await _row(session, KEYPAIR_ROW)
    if row is None:
        private = sealing.generate()
        stored = keyring.seal(keys, sealing.private_pem(private))
        await session.execute(
            text(
                "INSERT INTO secrets (scope, name, ciphertext, key_id, fingerprint) "
                "VALUES ('gateway', :name, :ciphertext, :key_id, :fingerprint) "
                "ON CONFLICT ON CONSTRAINT uq_secrets_owner_name DO NOTHING"
            ),
            {
                "name": KEYPAIR_ROW,
                "ciphertext": stored.ciphertext,
                "key_id": stored.key_id,
                "fingerprint": stored.fingerprint,
            },
        )
        await session.commit()
        # Read back rather than trust the one generated: a replica that
        # lost the race holds the winner's keypair, as every blob needs.
        row = await _row(session, KEYPAIR_ROW)
        if row is None:  # pragma: no cover — deleted between the two
            raise BootRefused("the sealing keypair row vanished while it was being made")
    unopenable = BootRefused(
        f"the sealing keypair row (secrets, scope gateway, {KEYPAIR_ROW}) does not open "
        f"under {KEY_VARIABLE}: the key that sealed it is gone. It is not replaced, since "
        f"every provider key sealed to it would be lost without a word. Put the old key "
        f"back (first in the list, or anywhere in it), or discard what no key opens with "
        f"`docker compose run --rm gateway python -m gateway.rewrap --discard-unopenable` "
        f"and re-enter the provider keys (docs/platform/Install.md, \"The gateway's store key\")."
    )
    if row.key_id not in keyring.key_ids(keys):
        raise unopenable
    try:
        return sealing.load_private(keyring.unseal(keys, row.ciphertext))
    except (keyring.InvalidToken, sealing.Unsealable, UnicodeDecodeError):
        raise unopenable from None


@dataclass
class _Loaded:
    runtime: dict[str, str]
    entries: list[dict]
    public_pem: str | None
    wrote: bool = False  # adopted a blob: other replicas must reload
    again: bool = False  # lost an adoption race: read the result soon


def _entry(name: str, row, *, runtime: bool, fingerprint: str | None, reason: str | None) -> dict:
    env = _env_set(name)
    if runtime:
        source = "runtime"
    else:
        source = "env" if env else "unset"
    return {
        "name": name,
        "aliases": aliases(name),
        "source": source,
        "fingerprint": fingerprint if runtime else None,
        "set_by": str(row.updated_by) if row is not None and row.updated_by else None,
        "set_at": row.updated_at.isoformat() if row is not None and row.updated_at else None,
        "row": None if row is None else ("runtime" if runtime else "rejected"),
        "reason": reason,
    }


async def _load(session) -> _Loaded:
    keys = configured_keys()
    if not await _tables_present(session):
        raise _TablesMissing()
    private = None
    if keys:
        await _refuse_a_shared_key(session, keys)
        private = await _keypair(session, keys)
    ids = keyring.key_ids(keys)
    loaded = _Loaded(
        runtime={},
        entries=[],
        public_pem=sealing.public_pem(private) if private is not None else None,
    )
    for name in NAMES:
        row = await _row(session, row_name(name))
        if row is None:
            loaded.entries.append(_entry(name, None, runtime=False, fingerprint=None, reason=None))
            continue
        if row.key_id == SEALED:
            if private is None:
                loaded.entries.append(
                    _entry(name, row, runtime=False, fingerprint=None, reason="unsealable")
                )
                continue
            try:
                value = sealing.open_blob(private, name, row.ciphertext)
            except sealing.Unsealable:
                loaded.entries.append(
                    _entry(name, row, runtime=False, fingerprint=None, reason="unsealable")
                )
                continue
            stored = keyring.seal(keys, value)
            adopted = await session.execute(
                text(
                    "UPDATE secrets SET ciphertext = :ciphertext, key_id = :key_id, "
                    "fingerprint = :fingerprint WHERE id = :id AND key_id = 'sealed' "
                    "AND fingerprint = :blob"
                ),
                {
                    "ciphertext": stored.ciphertext,
                    "key_id": stored.key_id,
                    "fingerprint": stored.fingerprint,
                    "id": row.id,
                    "blob": row.fingerprint,
                },
            )
            await session.commit()
            if adopted.rowcount != 1:
                # Another replica adopted it, or the admin replaced it:
                # either way the row is no longer this blob. Keep what was
                # in effect for this name and look again at once.
                loaded.again = True
                previous = _state.runtime.get(name)
                if previous is not None:
                    loaded.runtime[name] = previous
                loaded.entries.append(
                    next(
                        (dict(e) for e in _state.entries if e.get("name") == name),
                        _entry(name, None, runtime=False, fingerprint=None, reason=None),
                    )
                )
                continue
            loaded.wrote = True
            loaded.runtime[name] = value
            loaded.entries.append(
                _entry(name, row, runtime=True, fingerprint=stored.fingerprint, reason=None)
            )
            continue
        if row.key_id not in ids:
            loaded.entries.append(
                _entry(name, row, runtime=False, fingerprint=None, reason="unopenable")
            )
            continue
        try:
            value = keyring.unseal(keys, row.ciphertext)
        except (keyring.InvalidToken, UnicodeDecodeError):
            loaded.entries.append(
                _entry(name, row, runtime=False, fingerprint=None, reason="unopenable")
            )
            continue
        loaded.runtime[name] = value
        loaded.entries.append(
            _entry(name, row, runtime=True, fingerprint=row.fingerprint, reason=None)
        )
    return loaded


def _environment_entries() -> list[dict]:
    """What is in effect before the tables exist: the environment alone."""
    return [_entry(name, None, runtime=False, fingerprint=None, reason=None) for name in NAMES]


async def _write_status(session, loaded: _Loaded) -> bool:
    """Upsert the one status row when what it says has changed."""
    stub = bool(_config.settings.LIBRERUN_STUB_LLM)
    payload = json.dumps(loaded.entries, sort_keys=True)
    signature = json.dumps([__version__, stub, payload, loaded.public_pem])
    if signature == _state.written:
        return False
    await session.execute(
        text(
            "INSERT INTO gateway_status (id, version, stub, providers, public_key_pem, updated_at) "
            "VALUES (1, :version, :stub, CAST(:providers AS JSONB), :pem, NOW()) "
            "ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version, stub = EXCLUDED.stub, "
            "providers = EXCLUDED.providers, public_key_pem = EXCLUDED.public_key_pem, "
            "updated_at = NOW()"
        ),
        {"version": __version__, "stub": stub, "providers": payload, "pem": loaded.public_pem},
    )
    await session.commit()
    _state.written = signature
    return True


def _log_changes(before: list[dict], after: list[dict]) -> None:
    """One line per provider whose state moved; never a value."""
    old = {entry["name"]: entry for entry in before}
    for entry in after:
        prior = old.get(entry["name"]) or {}
        state = (entry["source"], entry["row"], entry["reason"], entry["fingerprint"])
        if state == (prior.get("source"), prior.get("row"), prior.get("reason"), prior.get("fingerprint")):
            continue
        if entry["row"] == "rejected":
            logger.warning(
                "gateway_provider_key_rejected",
                provider=entry["name"],
                reason=entry["reason"],
                serving=entry["source"],
            )
        else:
            logger.info(
                "gateway_provider_key_source",
                provider=entry["name"],
                source=entry["source"],
                fingerprint=entry["fingerprint"],
            )


async def _version() -> str | None:
    """``secrets:version``, or ``None`` when Redis does not answer."""
    import redis.asyncio as aioredis

    try:
        async with aioredis.from_url(
            _config.settings.REDIS_URL.get_secret_value(), decode_responses=True
        ) as redis:
            value = await redis.get(keyring.VERSION_KEY)
    except Exception as exc:  # noqa: BLE001 — a hint, not a dependency
        logger.debug("gateway_secrets_version_unavailable", error_type=type(exc).__name__)
        return None
    return "0" if value is None else str(value)


async def _bump_version() -> str | None:
    import redis.asyncio as aioredis

    try:
        async with aioredis.from_url(
            _config.settings.REDIS_URL.get_secret_value(), decode_responses=True
        ) as redis:
            return str(await redis.incr(keyring.VERSION_KEY))
    except Exception as exc:  # noqa: BLE001 — every replica reloads within 30 s anyway
        logger.warning("gateway_secrets_version_bump_failed", error_type=type(exc).__name__)
        return None


async def reload(version_before: str | None = None) -> bool:
    """One full load against the tables; whether it completed.

    ``version_before`` is ``secrets:version`` as read before the load, which
    this process then holds as seen — so a write during the load is a change
    the next tick reloads for. Its own bump after adopting a blob is held as
    seen too; a lost adoption race holds nothing, so the next tick looks
    again. Raises ``BootRefused`` for (a) and (b). Returns ``False`` while
    the tables do not exist yet, having left the environment in effect.
    """
    async with db.sessionmaker()() as session:
        try:
            loaded = await _load(session)
        except _TablesMissing:
            if not _state.entries:
                _state.entries = _environment_entries()
            return False
        before = _state.entries
        previous_pem = _state.public_pem
        # Swap first: the next model call uses what is now in the table.
        _state.runtime = loaded.runtime
        _state.public_pem = loaded.public_pem
        _state.entries = loaded.entries
        _state.loaded = True
        await _write_status(session, loaded)
    _log_changes(before, loaded.entries)
    if loaded.public_pem is not None and loaded.public_pem != previous_pem:
        # The operator's out-of-band check (D34): the admin page shows the
        # fingerprint it seals to, and it must be this one. A field the
        # log walk leaves alone only in this exact shape (``SHA256:`` and
        # 64 hex, ``app.observability.walkers``): walked, a digest never
        # survives, and the check would compare a placeholder.
        logger.info("gateway_sealing_key", fingerprint=sealing.fingerprint(loaded.public_pem))
    _state.version = version_before
    if loaded.wrote:
        bumped = await _bump_version()
        if bumped is not None:
            _state.version = bumped
    if loaded.again:
        _state.version = None
    return True


async def start() -> None:
    """The boot half. Refuses (raises) on a malformed key, (a) or (b);
    otherwise leaves either the tables' state or the environment's in
    effect, and the refresher running."""
    keys = configured_keys()  # a malformed entry raises here, before any I/O
    if not keys:
        logger.warning(
            "gateway_secrets_store_unconfigured",
            variable=KEY_VARIABLE,
            hint=(
                "no gateway store key: gateway.env serves the provider keys alone and "
                "the admin page cannot take one (it has no public key to seal to). "
                "docs/platform/Install.md, \"The gateway's store key\", says how to add one."
            ),
        )
    _state.entries = _environment_entries()
    try:
        done = await reload(await _version())
    except BootRefused:
        raise
    except Exception as exc:  # noqa: BLE001 — the refresher tries again in 2 s
        logger.warning("gateway_provider_keys_deferred", error_type=type(exc).__name__)
        done = False
    if not done:
        logger.info(
            "gateway_provider_keys_waiting_for_tables",
            hint="the environment serves until the backend's migrations have run",
        )
    start_refresher()


def _refuse(exc: BootRefused) -> None:
    """(a) or (b) found by the refresher: log and stop the process. Compose
    restarts it (``restart: unless-stopped``) into the same refusal at
    boot, which is where an operator looks."""
    logger.error("gateway_refused", reason=str(exc))
    os.kill(os.getpid(), signal.SIGTERM)


async def _refresh_forever(poll: float, full: float) -> None:
    last_full = time.monotonic()
    while True:
        await asyncio.sleep(poll)
        version = await _version()
        due = time.monotonic() - last_full >= full
        changed = version is not None and version != _state.version
        if not (due or changed or not _state.loaded):
            continue
        try:
            done = await reload(version)
        except BootRefused as exc:
            _refuse(exc)
            return
        except Exception as exc:  # noqa: BLE001 — keep what is in effect; try again
            logger.warning("gateway_provider_keys_reload_failed", error_type=type(exc).__name__)
            continue
        if done:
            last_full = time.monotonic()


def start_refresher(*, poll: float = POLL_SECONDS, full: float = FULL_RELOAD_SECONDS) -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.get_running_loop().create_task(_refresh_forever(poll, full))


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _task = None
