"""``python -m gateway.rewrap``: K6's rules for the gateway's own rows (K7;
D33, K7-08), against the real tables.

Rotation is "prepend a new key, run this, drop the old one": every
``gateway`` row still under an older key is re-encrypted under the current
one, its key id and fingerprint recomputed and its ``updated_at`` kept; no
other scope's row is touched; ``--rotate-keypair`` swaps the sealing key
and the published one together; ``--discard-unopenable`` is the way out of
a lost key.
"""
from __future__ import annotations

import base64
import io
import os

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text

from app import secrets_keyring as keyring


def fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


@pytest_asyncio.fixture
async def gateway_rows(session, monkeypatch):
    from gateway import config as gateway_config
    from gateway import db, provider_store

    async def clear() -> None:
        async with db.sessionmaker()() as s:
            await s.execute(text("DELETE FROM secrets WHERE scope = 'gateway'"))
            await s.execute(text("DELETE FROM secrets WHERE name LIKE 'k7.rewrap_probe%'"))
            await s.execute(text("DELETE FROM gateway_status"))
            await s.commit()

    def use(*keys: str) -> None:
        monkeypatch.setattr(
            gateway_config.settings, "LIBRERUN_GATEWAY_SECRETS_KEY", SecretStr(",".join(keys))
        )

    provider_store.reset()
    await clear()

    class Rows:
        use_keys = staticmethod(use)

        @staticmethod
        async def put(name: str, value: str, key: str, *, scope: str = "gateway") -> None:
            sealed = keyring.seal([key.encode()], value)
            async with db.sessionmaker()() as s:
                await s.execute(
                    text(
                        "INSERT INTO secrets (scope, name, ciphertext, key_id, fingerprint, updated_at) "
                        "VALUES (:scope, :name, :c, :k, :f, '2026-01-02T03:04:05Z')"
                    ),
                    {"scope": scope, "name": name, "c": sealed.ciphertext,
                     "k": sealed.key_id, "f": sealed.fingerprint},
                )
                await s.commit()

        @staticmethod
        async def all() -> dict:
            async with db.sessionmaker()() as s:
                rows = (
                    await s.execute(
                        text(
                            "SELECT scope, name, ciphertext, key_id, fingerprint, updated_at "
                            "FROM secrets WHERE scope = 'gateway' OR name LIKE 'k7.rewrap_probe%'"
                        )
                    )
                ).all()
            return {r.name: r for r in rows}

        @staticmethod
        async def run(**options) -> tuple[int, str, str]:
            from gateway import rewrap

            out, err = io.StringIO(), io.StringIO()
            code = await rewrap.rewrap(out=out, err=err, **options)
            return code, out.getvalue(), err.getvalue()

    try:
        yield Rows
    finally:
        await clear()
        provider_store.reset()
        await db.dispose()


@pytest.mark.asyncio
async def test_a_row_under_the_old_key_moves_to_the_new_one(gateway_rows):
    old, new = fernet_key(), fernet_key()
    await gateway_rows.put("provider.openai", "sk-rewrap-me-0001", old)
    gateway_rows.use_keys(new, old)

    code, out, err = await gateway_rows.run()

    assert code == 0, err
    row = (await gateway_rows.all())["provider.openai"]
    assert row.key_id == keyring.key_id(new)
    assert row.fingerprint == keyring.fingerprint(new, "sk-rewrap-me-0001")
    assert keyring.unseal([new.encode()], row.ciphertext) == "sk-rewrap-me-0001"
    assert row.updated_at.isoformat().startswith("2026-01-02T03:04:05"), "updated_at moved"
    assert "provider.openai" in out and "sk-rewrap-me-0001" not in out + err


@pytest.mark.asyncio
async def test_another_scopes_row_is_never_touched(gateway_rows):
    """D33: the backend's rows are sealed to the backend's key, which this
    process neither holds nor may try. (A row under a key the gateway also
    holds is refused outright: see the shared-key case below.)"""
    old, new, backends = fernet_key(), fernet_key(), fernet_key()
    await gateway_rows.put("provider.openai", "sk-rewrap-beside-a-platform-row", old)
    await gateway_rows.put("k7.rewrap_probe_platform", "a backend secret", backends, scope="platform")
    before = (await gateway_rows.all())["k7.rewrap_probe_platform"]
    gateway_rows.use_keys(new, old)

    code, _, err = await gateway_rows.run()

    assert code == 0, err
    after = (await gateway_rows.all())["k7.rewrap_probe_platform"]
    assert (bytes(after.ciphertext), after.key_id) == (bytes(before.ciphertext), before.key_id)


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(gateway_rows):
    old, new = fernet_key(), fernet_key()
    await gateway_rows.put("provider.anthropic", "sk-ant-dry-run-0001", old)
    gateway_rows.use_keys(new, old)

    code, out, _ = await gateway_rows.run(dry_run=True)

    assert code == 0
    assert "would rewrap: provider.anthropic" in out
    assert (await gateway_rows.all())["provider.anthropic"].key_id == keyring.key_id(old)


@pytest.mark.asyncio
async def test_rows_no_key_opens_are_named_then_discarded(gateway_rows):
    """A lost key: exit 3, the rows named; ``--discard-unopenable`` then
    deletes them by name — the keypair's too, and every blob that could
    only have opened under it — and the gateway makes a new keypair at its
    next start."""
    lost, current = fernet_key(), fernet_key()
    await gateway_rows.put("provider.google", "AIza-lost-key-0001", lost)
    await gateway_rows.put("sealing.private_key", "not a real pem, and unopenable anyway", lost)
    gateway_rows.use_keys(current)

    code, _, err = await gateway_rows.run()
    assert code == 3
    assert "unopenable: provider.google" in err and "unopenable: sealing.private_key" in err

    code, out, err = await gateway_rows.run(discard_unopenable=True)
    assert code == 0, err
    assert "discarded: provider.google" in out and "discarded: sealing.private_key" in out
    assert await gateway_rows.all() == {}


@pytest.mark.asyncio
async def test_rotate_keypair_swaps_both_halves_and_rejects_old_blobs(gateway_rows):
    """``--rotate-keypair`` replaces the private half in its row and the
    public half in ``gateway_status`` in one transaction; a stored key is a
    Fernet row and is untouched; a blob sealed to the old key that was not
    adopted yet is rejected at the next load (K7-08)."""
    import hashlib

    from gateway import db, provider_store, sealing

    key = fernet_key()
    gateway_rows.use_keys(key)
    await provider_store.reload()
    old_pem = provider_store.public_key_pem()
    await gateway_rows.put("provider.anthropic", "sk-ant-stored-before-rotation", key)
    blob = sealing.seal(old_pem, "openai", "sk-sealed-to-the-old-keypair")
    async with db.sessionmaker()() as s:
        await s.execute(
            text(
                "INSERT INTO secrets (scope, name, ciphertext, key_id, fingerprint) "
                "VALUES ('gateway', 'provider.openai', :b, 'sealed', :f)"
            ),
            {"b": blob, "f": hashlib.sha256(blob).hexdigest()[:12]},
        )
        await s.commit()
    stored_before = (await gateway_rows.all())["provider.anthropic"]

    code, out, err = await gateway_rows.run(rotate_keypair=True)
    assert code == 0, err
    assert "rotated: sealing.private_key" in out

    async with db.sessionmaker()() as s:
        published = (
            await s.execute(text("SELECT public_key_pem FROM gateway_status WHERE id = 1"))
        ).scalar_one()
    assert published and published != old_pem
    stored_after = (await gateway_rows.all())["provider.anthropic"]
    assert bytes(stored_after.ciphertext) == bytes(stored_before.ciphertext)

    await provider_store.reload()
    assert provider_store.public_key_pem() == published
    entries = {e["name"]: e for e in provider_store.entries()}
    assert (entries["openai"]["row"], entries["openai"]["reason"]) == ("rejected", "unsealable")
    assert provider_store.runtime_key("anthropic") == "sk-ant-stored-before-rotation"


@pytest.mark.asyncio
async def test_no_key_and_a_shared_key_are_refused(gateway_rows):
    gateway_rows.use_keys()
    code, _, err = await gateway_rows.run()
    assert code == 2 and "blank" in err

    shared = fernet_key()
    await gateway_rows.put("k7.rewrap_probe_shared", "a backend secret", shared, scope="platform")
    gateway_rows.use_keys(shared)
    code, _, err = await gateway_rows.run()
    assert code == 2 and "LIBRERUN_BACKEND_SECRETS_KEY" in err
