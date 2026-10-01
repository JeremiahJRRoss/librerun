"""Provider keys from the admin UI: the backend's half, which never holds one (K7).

L33: a provider key entered in the UI never exists in plaintext in the
backend. The browser seals it to the gateway's public key (RSA-OAEP,
``frontend/src/lib/sealing.ts``); this module stores the blob it cannot open
as the ``gateway``-scope row ``provider.<name>`` — ``key_id`` the literal
``'sealed'`` and ``fingerprint`` the blob's own id, 12 hex characters of its
SHA-256 — and answers ``202``, the route bumping ``secrets:version`` once
the row is committed (``notify_gateway``): only the gateway,
adopting the blob under its own store key, can open it or fingerprint the
key in it (K7-03). There is no RSA and no Fernet in this module, or anywhere
in the backend (``test_no_rsa_code_in_the_backend``).

What the gateway holds, the backend learns from the one row the gateway
writes, ``gateway_status`` (D16 as K7 refines it): each name with its
aliases, its source, the keyed fingerprint of a stored key, who set it and
when, and whether the row was rejected — never a value. A row this process
has written and the gateway has not processed yet reads ``pending``: the
gateway records the ``updated_at`` of the row it acted on as ``set_at`` and
keeps ``updated_at`` when it adopts a blob, so the two agree exactly when it
has seen the row as it stands.

The refusals, each ``{"detail", "code"}``:

* ``404 unknown_provider`` — the names are ``openai``, ``anthropic`` and
  ``google``, one per provider-key variable (K7-02);
* ``503 secrets_store_unconfigured`` — the gateway has published no public
  key (its store key is blank, or it has never reported): there is nothing
  to seal to, and the gateway's environment serves meanwhile (K7-07);
* ``400 plaintext_refused`` — unless the body strict-decodes from base64 to
  exactly 384 bytes, one 3072-bit OAEP block. One rule and one code: a raw
  key is never that, and a Google-shaped key strict-decodes too, so only the
  length tells a blob from a key (K7-16).

A key never passes through here, so there is nothing to log: the audit row
is ``{surface, provider, action}``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import datetime
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import secrets_keyring as keyring
from app.redis import get_redis
from app.services.secrets_service import SecretsStoreUnconfigured

logger = structlog.get_logger(__name__)

NAMES = ("openai", "anthropic", "google")
ROW_PREFIX = "provider."
SEALED = "sealed"
# One OAEP block under a 3072-bit key: what the gateway's keypair is (D15).
BLOB_BYTES = 384
SURFACE = "provider_keys"


class UnknownProvider(LookupError):
    code = "unknown_provider"

    def __init__(self, name: str) -> None:
        super().__init__(
            f"no provider named {name!r}; a key can be set for {', '.join(NAMES)}"
        )


class PlaintextRefused(ValueError):
    """What arrived is not a sealed blob. Carries none of it."""

    code = "plaintext_refused"

    def __init__(self) -> None:
        super().__init__(
            "the body's `sealed` is not a provider key sealed to the gateway (base64 of "
            f"{BLOB_BYTES} bytes). A key is sealed in the browser, never sent as it is; "
            "the admin page does this, and the gateway's environment file takes a key directly."
        )


class GatewayStoreUnconfigured(SecretsStoreUnconfigured):
    """No public key to seal to. Answered ``503 secrets_store_unconfigured``
    by the handler every store refusal shares (``app.main``)."""

    detail = (
        "The gateway has no store key, so it published no public key and a provider key "
        "cannot be sealed to it. Set LIBRERUN_GATEWAY_SECRETS_KEY in the gateway's environment file "
        "(docs/platform/Install.md, \"The gateway's store key\") and recreate the gateway; "
        "until then the provider keys in the gateway's environment serve."
    )


def _row_names() -> list[str]:
    return [f"{ROW_PREFIX}{name}" for name in NAMES]


async def _status(db: AsyncSession):
    return (
        await db.execute(
            text(
                "SELECT version, stub, providers, public_key_pem, updated_at "
                "FROM gateway_status WHERE id = 1"
            )
        )
    ).first()


async def _rows(db: AsyncSession) -> dict[str, object]:
    rows = (
        await db.execute(
            text(
                "SELECT name, key_id, updated_at FROM secrets WHERE scope = 'gateway' "
                "AND tenant_id IS NULL AND agent_id IS NULL AND name = ANY(:names)"
            ),
            {"names": _row_names()},
        )
    ).all()
    return {row.name[len(ROW_PREFIX):]: row for row in rows}


def _seen(entry: dict, row) -> bool:
    """Whether the gateway's entry describes the row as it stands now."""
    if row is None:
        return entry.get("row") is None
    set_at = entry.get("set_at")
    if not set_at:
        return False
    try:
        return datetime.fromisoformat(set_at) == row.updated_at
    except (TypeError, ValueError):
        return False


async def providers(db: AsyncSession) -> dict:
    """``GET /admin/providers``: what the gateway reports, each name marked
    ``pending`` while a row it has not processed waits."""
    status = await _status(db)
    rows = await _rows(db)
    reported = {
        entry.get("name"): entry
        for entry in ((status.providers if status is not None else None) or [])
        if isinstance(entry, dict)
    }
    out = []
    for name in NAMES:
        entry = dict(reported.get(name) or {})
        entry.setdefault("name", name)
        entry.setdefault("aliases", [])
        for field in ("source", "fingerprint", "set_by", "set_at", "row", "reason"):
            entry.setdefault(field, None)
        row = rows.get(name)
        if not _seen(entry, row):
            entry["row"] = "pending" if row is not None or entry["row"] is not None else None
            if row is not None:
                entry["reason"] = None
        out.append(entry)
    return {
        "reported": status is not None,
        "stub": bool(status.stub) if status is not None else None,
        "gateway_version": status.version if status is not None else None,
        "updated_at": status.updated_at if status is not None else None,
        "public_key_pem": status.public_key_pem if status is not None else None,
        "providers": out,
    }


def _blob(sealed: object) -> bytes:
    if not isinstance(sealed, str):
        raise PlaintextRefused()
    try:
        blob = base64.b64decode(sealed, validate=True)
    except (binascii.Error, ValueError):
        raise PlaintextRefused() from None
    if len(blob) != BLOB_BYTES:
        raise PlaintextRefused()
    return blob


async def notify_gateway() -> None:
    """Tell every gateway to reload now: ``INCR secrets:version``. Called by
    the route once its transaction has COMMITTED, never before: a gateway
    polling between the two would read the new version and the old row,
    hold that version as seen, and keep the old key until its 30-second full
    reload (Codex on #170)."""
    try:
        redis = await get_redis()
        await redis.incr(keyring.VERSION_KEY)
    except Exception as exc:  # noqa: BLE001 — the gateway reloads every 30 s anyway
        logger.warning("provider_key_version_bump_failed", error_type=type(exc).__name__)


async def set_key(db: AsyncSession, name: str, sealed: object, *, user_id: UUID | None) -> str:
    """Store the blob for ``name``; ``set`` or ``replace``. Refuses before
    anything is written, in the order the module docstring gives. The caller
    commits, then calls ``notify_gateway``."""
    if name not in NAMES:
        raise UnknownProvider(name)
    status = await _status(db)
    if status is None or not status.public_key_pem:
        raise GatewayStoreUnconfigured()
    blob = _blob(sealed)
    blob_id = hashlib.sha256(blob).hexdigest()[:12]
    row = (
        await db.execute(
            text(
                "INSERT INTO secrets (scope, name, ciphertext, key_id, fingerprint, "
                "created_by, updated_by) VALUES ('gateway', :name, :blob, 'sealed', :blob_id, "
                ":user_id, :user_id) ON CONFLICT ON CONSTRAINT uq_secrets_owner_name DO UPDATE "
                "SET ciphertext = EXCLUDED.ciphertext, key_id = 'sealed', "
                "fingerprint = EXCLUDED.fingerprint, updated_at = NOW(), "
                "updated_by = EXCLUDED.updated_by RETURNING (xmax = 0) AS inserted"
            ),
            {"name": f"{ROW_PREFIX}{name}", "blob": blob, "blob_id": blob_id, "user_id": user_id},
        )
    ).one()
    return "set" if row.inserted else "replace"


async def clear_key(db: AsyncSession, name: str) -> bool:
    """Remove ``name``'s row, whatever its state; whether there was one.
    The gateway drops the stored key at its next reload and its environment
    serves that provider again. The caller commits, then calls
    ``notify_gateway``."""
    if name not in NAMES:
        raise UnknownProvider(name)
    removed = (
        await db.execute(
            text(
                "DELETE FROM secrets WHERE scope = 'gateway' AND tenant_id IS NULL "
                "AND agent_id IS NULL AND name = :name RETURNING id"
            ),
            {"name": f"{ROW_PREFIX}{name}"},
        )
    ).first() is not None
    return removed
