"""The rewrap script (K6-07; D14, D33): rotation of the backend's store key.

Rotation is "prepend a key, recreate, rewrap, drop the old key, recreate".
These hold the third step, ``python -m app.scripts.rewrap_secrets``:

* every backend row under an older key ends up under the CURRENT one —
  opened by the new key alone, its ``key_id`` and ``fingerprint``
  recomputed, since both change with the key, and its ``updated_at`` kept;
* the gateway's rows are never touched: they are sealed to the gateway's
  key, which this process does not hold;
* no key, or a malformed one, is exit 2; a row no configured key opens is
  named — never by value — and is exit 3, while the rest are rewrapped;
* ``--dry-run`` writes nothing.
"""
from __future__ import annotations

import io
from contextlib import asynccontextmanager

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text

from app import secrets_keyring as keyring
from app.scripts import rewrap_secrets
from app.services import secrets_service as svc
from tests.test_secrets_service import VALUE, db, fernet_key, store  # noqa: F401  (fixtures)


def _factory(db):  # noqa: F811
    """The script's session factory, yielding the test's own session."""

    @asynccontextmanager
    async def factory():
        yield db

    return factory


async def _rewrap(db, store, *, dry_run=False):  # noqa: F811
    import app.redis

    async def _redis():
        return store.redis

    out, err = io.StringIO(), io.StringIO()
    original = app.redis.get_redis
    app.redis.get_redis = _redis
    try:
        code = await rewrap_secrets.rewrap(
            dry_run=dry_run, session_factory=_factory(db), out=out, err=err
        )
    finally:
        app.redis.get_redis = original
    return code, out.getvalue(), err.getvalue()


async def _rows(db) -> dict:  # noqa: F811
    rows = (
        await db.execute(
            text(
                "SELECT name, scope, ciphertext, key_id, fingerprint, updated_at"
                " FROM secrets ORDER BY name"
            )
        )
    ).all()
    return {row.name: row for row in rows}


@pytest.mark.asyncio
async def test_rotation_with_two_keys(db, store):  # noqa: F811
    old = store.key
    await svc.set_secret(db, svc.PLATFORM, "probe.one", VALUE, user_id=None)
    await svc.set_secret(db, svc.PLATFORM, "probe.two", VALUE + "-2", user_id=None)
    before = await _rows(db)
    version = store.redis.store.get(keyring.VERSION_KEY)

    new = fernet_key()
    store.use_key(f"{new},{old}")  # 1. prepend, and "recreate"
    code, out, err = await _rewrap(db, store)  # 2. rewrap

    assert code == 0, err
    assert "2 row(s) rewrapped" in out
    after = await _rows(db)
    for name, value in (("probe.one", VALUE), ("probe.two", VALUE + "-2")):
        row = after[name]
        # Opened by the NEW key alone.
        assert Fernet(new.encode()).decrypt(bytes(row.ciphertext)).decode() == value
        with pytest.raises(InvalidToken):
            Fernet(old.encode()).decrypt(bytes(row.ciphertext))
        assert row.key_id == keyring.key_id(new) != before[name].key_id
        assert row.fingerprint == keyring.fingerprint(new, value) != before[name].fingerprint
        assert row.updated_at == before[name].updated_at
        assert value not in out + err
    assert store.redis.store.get(keyring.VERSION_KEY) != version

    # 3. drop the old key: every row still opens.
    store.use_key(new)
    assert await svc.get_secret(db, svc.PLATFORM, "probe.one", env=lambda: "env") == VALUE
    assert all(meta.readable for meta in await svc.list_secrets(db, svc.PLATFORM))

    # And again is a no-op.
    code, out, _ = await _rewrap(db, store)
    assert code == 0 and "0 row(s) rewrapped" in out and "2 already under it" in out


@pytest.mark.asyncio
async def test_gateway_rows_untouched(db, store):  # noqa: F811
    gateway_key = fernet_key()
    sealed = keyring.seal([gateway_key.encode()], "the-gateway's-own")
    await db.execute(
        text(
            "INSERT INTO secrets (scope, name, ciphertext, key_id, fingerprint)"
            " VALUES ('gateway', 'provider.openai', :c, :k, :f)"
        ),
        {"c": sealed.ciphertext, "k": sealed.key_id, "f": sealed.fingerprint},
    )
    await svc.set_secret(db, svc.PLATFORM, "probe.one", VALUE, user_id=None)
    before = await _rows(db)

    store.use_key(f"{fernet_key()},{store.key}")
    code, out, err = await _rewrap(db, store)

    assert code == 0, err  # not "unreadable": it is not the backend's to open
    after = await _rows(db)
    gateway = after["provider.openai"]
    assert bytes(gateway.ciphertext) == bytes(before["provider.openai"].ciphertext)
    assert (gateway.key_id, gateway.fingerprint) == (sealed.key_id, sealed.fingerprint)
    assert "provider.openai" not in out + err
    assert "1 row(s) rewrapped" in out


@pytest.mark.asyncio
async def test_no_usable_key_is_exit_2(db, store):  # noqa: F811
    store.use_key("")
    code, _, err = await _rewrap(db, store)
    assert code == 2 and "blank" in err

    store.use_key("ab" * 32)
    code, _, err = await _rewrap(db, store)
    assert code == 2 and "entry 1 of 1" in err and "ab" * 32 not in err


@pytest.mark.asyncio
async def test_rows_no_key_opens_are_named_and_exit_3(db, store):  # noqa: F811
    lost = store.key
    await svc.set_secret(db, svc.PLATFORM, "probe.lost", VALUE, user_id=None)
    kept = fernet_key()
    store.use_key(kept)
    await svc.set_secret(db, svc.PLATFORM, "probe.stale", VALUE + "-2", user_id=None)
    newest = fernet_key()
    store.use_key(f"{newest},{kept}")  # `lost` is gone for good

    code, out, err = await _rewrap(db, store)

    assert code == 3
    assert "platform: probe.lost" in err and "unreadable" in err
    assert VALUE not in out + err and lost not in out + err
    after = await _rows(db)
    assert after["probe.stale"].key_id == keyring.key_id(newest)
    assert after["probe.lost"].key_id == keyring.key_id(lost)


@pytest.mark.asyncio
async def test_a_damaged_row_under_the_current_key_is_named(db, store):  # noqa: F811
    """A row damaged in place keeps its key id, so counting rows by id alone
    called it current and exited 0 while nothing opened it (Codex, K6
    round 1). Another key's well-formed token stands in for the damage."""
    await svc.set_secret(db, svc.PLATFORM, "probe.damaged", VALUE, user_id=None)
    await svc.set_secret(db, svc.PLATFORM, "probe.sound", VALUE + "-2", user_id=None)
    foreign = keyring.seal([fernet_key().encode()], VALUE)
    await db.execute(
        text("UPDATE secrets SET ciphertext = :c WHERE name = 'probe.damaged'"),
        {"c": foreign.ciphertext},
    )
    before = await _rows(db)

    for dry_run in (True, False):
        code, out, err = await _rewrap(db, store, dry_run=dry_run)
        assert code == 3, (dry_run, out, err)
        assert "1 already under it, 1 no configured key opens" in out
        assert "unreadable: platform: probe.damaged" in err and "probe.sound" not in err
        assert VALUE not in out + err
    after = await _rows(db)
    assert bytes(after["probe.damaged"].ciphertext) == bytes(before["probe.damaged"].ciphertext)


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(db, store):  # noqa: F811
    await svc.set_secret(db, svc.PLATFORM, "probe.one", VALUE, user_id=None)
    before = await _rows(db)
    version = store.redis.store.get(keyring.VERSION_KEY)
    store.use_key(f"{fernet_key()},{store.key}")

    code, out, _ = await _rewrap(db, store, dry_run=True)

    assert code == 0
    assert "would rewrap: platform: probe.one" in out
    after = await _rows(db)
    assert bytes(after["probe.one"].ciphertext) == bytes(before["probe.one"].ciphertext)
    assert after["probe.one"].key_id == before["probe.one"].key_id
    assert store.redis.store.get(keyring.VERSION_KEY) == version


@pytest.mark.asyncio
async def test_a_row_changed_while_it_ran_is_not_overwritten(db, store, monkeypatch):  # noqa: F811
    """The UPDATE carries the ciphertext the script read, so a value set
    between its SELECT and its UPDATE is never replaced by an older one."""
    await svc.set_secret(db, svc.PLATFORM, "probe.one", VALUE, user_id=None)
    old = store.key
    new = fernet_key()
    store.use_key(f"{new},{old}")
    real_rotate = keyring.rotate

    async def replaced_meanwhile():
        await svc.set_secret(db, svc.PLATFORM, "probe.one", "set-meanwhile", user_id=None)

    calls = []

    def rotate_then_race(keys, ciphertext):
        calls.append(1)
        return real_rotate(keys, ciphertext)

    monkeypatch.setattr(rewrap_secrets.keyring, "rotate", rotate_then_race)
    # The race, staged: the replacement lands before the script's UPDATE.
    original_execute = db.execute

    async def execute(statement, *args, **kwargs):
        if calls and getattr(statement, "is_update", False) and not getattr(execute, "raced", False):
            execute.raced = True
            await replaced_meanwhile()
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", execute)
    code, out, _ = await _rewrap(db, store)

    assert code == 0 and "changed while this ran" in out
    assert await svc.get_secret(db, svc.PLATFORM, "probe.one", env=lambda: "env") == "set-meanwhile"
