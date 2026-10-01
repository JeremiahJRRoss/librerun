"""The secrets store's keys and its cipher (K6; D14, D33).

A store key is a comma-separated list of Fernet keys, the first one current:
``LIBRERUN_BACKEND_SECRETS_KEY`` for the backend's rows and, from K7,
``LIBRERUN_GATEWAY_SECRETS_KEY`` for the gateway's. Rotation is "prepend a
key, run the rewrap script, drop the old one", which is what a list whose
first entry seals and whose every entry opens gives for free: MultiFernet.

No database import and no Redis import, and that is the point of this
module rather than a tidiness: the gateway image copies ``backend/app``, and
K7's gateway opens its own rows with this same code under its own key. The
backend's store is ``app.services.secrets_service``.

Two digests, both HMAC-SHA256 under the key and each under its own label,
so neither can stand in for the other:

* ``key_id(key)`` — 16 hex characters naming the key that sealed a row.
  It is what lets a row no configured key opens be reported as such
  without decrypting anything, and what K7's gateway compares to refuse a
  key that is also the backend's. It is never the key and never a plain
  hash of it.
* ``fingerprint(key, value)`` — 12 hex characters, all the API ever shows
  of a value, so an operator can tell whether two deployments hold the
  same secret. Keyed, so a fingerprint in a screenshot is no offline
  oracle for guessing a short value; it changes with the key, which is why
  the rewrap script recomputes it.

Nothing here logs, and no exception carries a key or a value: an entry
that is not a Fernet key is named by its position in the list (L31).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.secret_files import reveal

# The one thing the store keeps in Redis: a counter every write bumps, so a
# reader's in-process cache knows when to stop trusting itself. Never a
# value.
VERSION_KEY = "secrets:version"

KEY_ID_LENGTH = 16
FINGERPRINT_LENGTH = 12

_KEY_ID_LABEL = b"librerun-secrets:key-id:v1"
_FINGERPRINT_LABEL = b"librerun-secrets:fingerprint:v1\x00"

# 32 random bytes as url-safe base64: 43 characters and one '=' of padding.
# `random_hex 32` and `token_hex(32)` are 64 hex characters — a string that
# names 32 bytes and is not a Fernet key (K6-01).
_FERNET_KEY = re.compile(r"^[A-Za-z0-9_-]{43}=$")

__all__ = [
    "FINGERPRINT_LENGTH",
    "InvalidToken",
    "KEY_ID_LENGTH",
    "Sealed",
    "SecretsStoreKeyInvalid",
    "VERSION_KEY",
    "fingerprint",
    "key_id",
    "key_ids",
    "multifernet",
    "parse",
    "rotate",
    "seal",
    "unseal",
]


class SecretsStoreKeyInvalid(RuntimeError):
    """An entry of a store-key list is not a Fernet key.

    Names the variable and the entry's position, never the entry: this is
    printed by whatever watches the process start. ``RuntimeError`` rather
    than ``ValueError`` for ``secret_files.SecretSourceConflict``'s reason —
    pydantic wraps a ``ValueError`` raised in a validator into a message
    that quotes the validator's whole input.
    """


def parse(raw: Any, *, variable: str = "LIBRERUN_BACKEND_SECRETS_KEY") -> list[bytes]:
    """The keys in ``raw``, current first; ``[]`` when it is blank.

    ``raw`` is whatever the settings model holds — a ``SecretStr``, a plain
    string or ``None``. Every entry must be a Fernet key: a blank entry (a
    doubled or trailing comma) is refused like a malformed one, because a
    list that silently lost a key is how a rotation opens nothing.
    """
    text = reveal(raw).strip()
    if not text:
        return []
    entries = text.split(",")
    keys: list[bytes] = []
    for position, entry in enumerate(entries, 1):
        entry = entry.strip()
        if not _FERNET_KEY.match(entry):
            raise SecretsStoreKeyInvalid(
                f"{variable}: entry {position} of {len(entries)} is not a Fernet key "
                f"(32 random bytes as url-safe base64 — 44 characters ending in '='; "
                f"generate one with: head -c 32 /dev/urandom | base64 | tr '+/' '-_'). "
                f"The value is not shown here, and should not be."
            )
        keys.append(entry.encode("ascii"))
    return keys


def _raw(key: bytes | str) -> bytes:
    if isinstance(key, str):
        key = key.encode("ascii")
    return base64.urlsafe_b64decode(key)


def key_id(key: bytes | str) -> str:
    """A keyed id of ``key``: which key sealed a row, never the key."""
    return hmac.new(_raw(key), _KEY_ID_LABEL, hashlib.sha256).hexdigest()[:KEY_ID_LENGTH]


def key_ids(keys: Iterable[bytes]) -> set[str]:
    """The ids of every configured key: a row whose ``key_id`` is not among
    them is one no configured key opens."""
    return {key_id(key) for key in keys}


def fingerprint(key: bytes | str, value: str) -> str:
    """A keyed, truncated digest of ``value`` under ``key``."""
    return hmac.new(
        _raw(key), _FINGERPRINT_LABEL + value.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:FINGERPRINT_LENGTH]


def multifernet(keys: Sequence[bytes]) -> MultiFernet:
    """Every configured key: the first seals, all of them open."""
    if not keys:
        raise ValueError("a MultiFernet needs at least one key")
    return MultiFernet([Fernet(key) for key in keys])


@dataclass(frozen=True)
class Sealed:
    """A value as the table holds it. ``repr`` shows no ciphertext."""

    ciphertext: bytes
    key_id: str
    fingerprint: str

    def __repr__(self) -> str:  # a token in a traceback is still a token
        return f"Sealed(key_id={self.key_id!r}, fingerprint={self.fingerprint!r})"


def seal(keys: Sequence[bytes], value: str) -> Sealed:
    """``value`` under the current key, with that key's id and the value's
    fingerprint under it."""
    current = keys[0]
    return Sealed(
        ciphertext=Fernet(current).encrypt(value.encode("utf-8")),
        key_id=key_id(current),
        fingerprint=fingerprint(current, value),
    )


def unseal(keys: Sequence[bytes], ciphertext: bytes) -> str:
    """The value, opened by whichever configured key sealed it. Raises
    ``InvalidToken`` when none does."""
    return multifernet(keys).decrypt(bytes(ciphertext)).decode("utf-8")


def rotate(keys: Sequence[bytes], ciphertext: bytes) -> Sealed:
    """``ciphertext`` re-encrypted under the current key by
    ``MultiFernet.rotate``, with the id and fingerprint recomputed, since
    both change with the key. Raises ``InvalidToken`` when no configured
    key opens it."""
    multi = multifernet(keys)
    token = bytes(ciphertext)
    value = multi.decrypt(token).decode("utf-8")
    current = keys[0]
    return Sealed(
        ciphertext=multi.rotate(token),
        key_id=key_id(current),
        fingerprint=fingerprint(current, value),
    )
