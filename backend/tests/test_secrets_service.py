"""The encrypted secrets store's service (K6; L30, L31, D14, D32, D33).

What these hold, against real PostgreSQL and a dict ``FakeRedis``:

* the table holds **ciphertext only** — a Fernet token that opens under the
  store key to the value, and the value nowhere in the row;
* a **blank key refuses writes** (``SecretsStoreUnconfigured``) while reads
  fall back to the environment, so a deployment with no key loses nothing;
* the in-process cache is **real and bounded by the version counter**: it
  serves a value while ``secrets:version`` holds, drops it when a writer,
  its row committed, bumps the counter (``notify_change`` — a write alone
  bumps nothing), and is not used at all without Redis;
* **Redis never holds a value** — one counter, and nothing else;
* a row **no configured key opens** falls back to the environment, is
  logged without its value and lists as unreadable;
* every read and write is **keyed by its owner**, and the gateway's scope
  is refused (D32, D33).

The fixtures here — ``db``, ``store`` and ``FakeRedis`` — are imported by
the other K6 test modules, as this suite's modules share theirs.
"""
from __future__ import annotations

import base64
import os
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import secrets_keyring as keyring
from app.services import secrets_service as svc

REQUIRE_DB = os.environ.get("LIBRERUN_REQUIRE_DB", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

VALUE = "the-secret-value-6f1d2a9c"
NAME = "probe.secret"


def fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


@pytest_asyncio.fixture
async def db():
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import settings

    engine = create_async_engine(settings.DATABASE_URL.get_secret_value())
    try:
        async with engine.connect() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        reason = f"no database at DATABASE_URL ({type(exc).__name__}: {exc})"
        if REQUIRE_DB:
            pytest.fail("LIBRERUN_REQUIRE_DB is set, so this may not skip: " + reason)
        pytest.skip(reason)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        # As the app's sessionmaker makes them (``app/database.py``): the
        # routes commit before they notify, and a fixture's user read after
        # the route must not be reloaded outside the event loop.
        session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


class FakeRedis:
    """The calls the settings cache and the store make, over a dict."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])


class DeadRedis:
    """A Redis that does not answer."""

    async def get(self, key):
        raise ConnectionError("redis is down")

    async def set(self, key, value, ex=None):
        raise ConnectionError("redis is down")

    async def delete(self, *keys):
        raise ConnectionError("redis is down")

    async def incr(self, key):
        raise ConnectionError("redis is down")


@pytest.fixture
def store(monkeypatch):
    """A fresh store key, one ``FakeRedis`` for the store and the settings
    cache, and an empty in-process cache.

    The key is set on ``app.config.settings`` — the object the service
    reads when asked — so ``use_key`` is the operator editing ``.env``
    and recreating the backend.
    """
    import app.config as cfg

    redis = FakeRedis()

    async def _redis():
        return redis

    monkeypatch.setattr("app.services.secrets_service.get_redis", _redis)
    monkeypatch.setattr("app.services.app_settings_service.get_redis", _redis)

    def use_key(value: str) -> None:
        monkeypatch.setattr(cfg.settings, "LIBRERUN_BACKEND_SECRETS_KEY", value)
        svc.clear_cache()

    key = fernet_key()
    use_key(key)
    yield SimpleNamespace(redis=redis, key=key, use_key=use_key)
    svc.clear_cache()


async def _row(db, name: str = NAME):
    return (
        await db.execute(
            text("SELECT ciphertext, key_id, fingerprint FROM secrets WHERE name = :n"),
            {"n": name},
        )
    ).one()


def _env(value: str = "") -> callable:
    return lambda: value


# --------------------------------------------------------------------------
# The row holds ciphertext, and only ciphertext
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ciphertext_only(db, store):
    write = await svc.set_secret(db, svc.PLATFORM, NAME, VALUE, user_id=None)

    ciphertext, key_id, fingerprint = await _row(db)
    token = bytes(ciphertext)
    assert token.startswith(b"gAAAAA"), "the column does not hold a Fernet token"
    assert VALUE.encode() not in token
    assert Fernet(store.key.encode()).decrypt(token).decode() == VALUE
    assert key_id == keyring.key_id(store.key) and len(key_id) == 16
    assert fingerprint == keyring.fingerprint(store.key, VALUE) == write.fingerprint
    assert len(fingerprint) == 12
    assert VALUE not in key_id + fingerprint

    # What leaves the service describes the row and never carries it.
    (meta,) = await svc.list_secrets(db, svc.PLATFORM)
    assert (meta.name, meta.readable, meta.fingerprint) == (NAME, True, fingerprint)
    assert VALUE not in repr(meta) + repr(write)
    assert "ciphertext" not in repr(meta)
    assert VALUE not in repr(keyring.seal([store.key.encode()], VALUE))


@pytest.mark.asyncio
async def test_set_then_replace_is_one_row(db, store):
    first = await svc.set_secret(db, svc.PLATFORM, NAME, VALUE, user_id=None)
    second = await svc.set_secret(db, svc.PLATFORM, NAME, VALUE + "-2", user_id=None)

    assert (first.action, second.action) == ("set", "replace")
    assert first.fingerprint != second.fingerprint
    count = (await db.execute(text("SELECT count(*) FROM secrets WHERE name = :n"), {"n": NAME})).scalar()
    assert count == 1
    assert await svc.get_secret(db, svc.PLATFORM, NAME, env=_env()) == VALUE + "-2"


# --------------------------------------------------------------------------
# A blank key is "unconfigured": writes refuse, reads fall back
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blank_key_refuses_writes(db, store):
    store.use_key("")

    with pytest.raises(svc.SecretsStoreUnconfigured) as refused:
        await svc.set_secret(db, svc.PLATFORM, NAME, VALUE, user_id=None)

    assert refused.value.code == "secrets_store_unconfigured"
    assert VALUE not in str(refused.value)
    count = (await db.execute(text("SELECT count(*) FROM secrets"))).scalar()
    assert count == 0
    # Reads lose nothing: the environment serves.
    assert await svc.get_secret(db, svc.PLATFORM, NAME, env=_env("from-env")) == "from-env"
    # Clearing needs no key: it stores nothing.
    assert await svc.unset_secret(db, svc.PLATFORM, NAME) is False


def test_a_malformed_key_names_its_position_and_never_its_value():
    good = fernet_key()
    hex_key = "ab" * 32  # random_hex 32 / token_hex(32): 32 bytes named, not a Fernet key
    for raw, position in (
        (f"{good},{hex_key}", "entry 2 of 2"),
        (hex_key, "entry 1 of 1"),
        (f"{good},", "entry 2 of 2"),
        (f"{good},,{good}", "entry 2 of 3"),
    ):
        with pytest.raises(keyring.SecretsStoreKeyInvalid) as refused:
            keyring.parse(raw)
        message = str(refused.value)
        assert position in message, message
        assert hex_key not in message and good not in message
    assert keyring.parse("") == [] and keyring.parse(None) == []
    assert keyring.parse(f" {good} , {fernet_key()} ")[0] == good.encode()


def test_the_boot_check_refuses_a_malformed_key_and_logs_a_blank_one(store, caplog):
    import logging

    caplog.set_level(logging.INFO)
    store.use_key("ab" * 32)
    with pytest.raises(keyring.SecretsStoreKeyInvalid):
        svc.check_store_key()

    store.use_key("")
    assert svc.check_store_key() == 0
    assert "secrets_store_unconfigured" in caplog.text

    key = fernet_key()
    store.use_key(f"{key},{fernet_key()}")
    caplog.clear()
    assert svc.check_store_key() == 2
    assert keyring.key_id(key) in caplog.text and key not in caplog.text


# --------------------------------------------------------------------------
# The cache: bounded by the version counter, and nothing without Redis
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_bump_invalidates_cache(db, store):
    await svc.set_secret(db, svc.PLATFORM, NAME, VALUE, user_id=None)
    assert await svc.get_secret(db, svc.PLATFORM, NAME, env=_env()) == VALUE

    # Change the row BEHIND the service's back: no bump, so the cached
    # value still serves — proof there is a cache to invalidate.
    other = keyring.seal([store.key.encode()], "changed-behind-its-back")
    await db.execute(
        text("UPDATE secrets SET ciphertext = :c WHERE name = :n"),
        {"c": other.ciphertext, "n": NAME},
    )
    assert await svc.get_secret(db, svc.PLATFORM, NAME, env=_env()) == VALUE

    # Another process's write bumps the counter; this one then reads again.
    await store.redis.incr(keyring.VERSION_KEY)
    assert await svc.get_secret(db, svc.PLATFORM, NAME, env=_env()) == "changed-behind-its-back"

    # A write through the service tells no one until its writer, having
    # committed, calls notify_change: that bumps the counter and drops
    # this process's own entry.
    before = store.redis.store[keyring.VERSION_KEY]
    await svc.set_secret(db, svc.PLATFORM, NAME, "third", user_id=None)
    assert store.redis.store[keyring.VERSION_KEY] == before
    assert await svc.get_secret(db, svc.PLATFORM, NAME, env=_env()) == "changed-behind-its-back"
    await svc.notify_change(svc.PLATFORM, NAME)
    assert store.redis.store[keyring.VERSION_KEY] != before
    assert await svc.get_secret(db, svc.PLATFORM, NAME, env=_env()) == "third"

    before = store.redis.store[keyring.VERSION_KEY]
    assert await svc.unset_secret(db, svc.PLATFORM, NAME) is True
    assert store.redis.store[keyring.VERSION_KEY] == before
    await svc.notify_change(svc.PLATFORM, NAME)
    assert store.redis.store[keyring.VERSION_KEY] != before
    assert await svc.get_secret(db, svc.PLATFORM, NAME, env=_env("env-value")) == "env-value"


@pytest.mark.asyncio
async def test_no_redis_means_no_cache(db, store, monkeypatch):
    dead = DeadRedis()

    async def _dead():
        return dead

    monkeypatch.setattr("app.services.secrets_service.get_redis", _dead)
    # A write still lands: the counter is a cache's, not a dependency.
    await svc.set_secret(db, svc.PLATFORM, NAME, VALUE, user_id=None)
    assert await svc.get_secret(db, svc.PLATFORM, NAME, env=_env()) == VALUE

    other = keyring.seal([store.key.encode()], "read-every-time")
    await db.execute(
        text("UPDATE secrets SET ciphertext = :c WHERE name = :n"),
        {"c": other.ciphertext, "n": NAME},
    )
    assert await svc.get_secret(db, svc.PLATFORM, NAME, env=_env()) == "read-every-time"


@pytest.mark.asyncio
async def test_redis_holds_no_value(db, store):
    from app.services import app_settings_service

    # Each write as a route makes it: the write, then (committed) the notice.
    await svc.set_secret(db, svc.PLATFORM, NAME, VALUE, user_id=None)
    await svc.notify_change(svc.PLATFORM, NAME)
    await svc.get_secret(db, svc.PLATFORM, NAME, env=_env())
    await svc.set_secret(db, svc.PLATFORM, NAME, VALUE + "-2", user_id=None)
    await svc.notify_change(svc.PLATFORM, NAME)
    await svc.get_secret(db, svc.PLATFORM, NAME, env=_env())
    await svc.list_secrets(db, svc.PLATFORM)
    await svc.unset_secret(db, svc.PLATFORM, NAME)
    await svc.notify_change(svc.PLATFORM, NAME)

    assert set(store.redis.store) == {keyring.VERSION_KEY}
    assert store.redis.store[keyring.VERSION_KEY].isdigit()
    assert all(VALUE not in value for value in store.redis.store.values())

    # The registry's cached path refuses a secret outright, so its Redis
    # cache cannot hold one either.
    with pytest.raises(TypeError):
        await app_settings_service.get_setting(db, "auth.azure_client_secret")
    assert not any("auth.azure_client_secret" in key for key in store.redis.store)


# --------------------------------------------------------------------------
# A row no configured key opens: the environment serves, the row stays
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_row_no_key_opens_falls_back_to_the_environment(db, store, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    await svc.set_secret(db, svc.PLATFORM, NAME, VALUE, user_id=None)
    store.use_key(fernet_key())  # the key that sealed it is gone

    assert await svc.get_secret(db, svc.PLATFORM, NAME, env=_env("from-env")) == "from-env"
    (meta,) = await svc.list_secrets(db, svc.PLATFORM)
    assert meta.readable is False
    assert "secret_unreadable" in caplog.text and VALUE not in caplog.text

    # A ciphertext whose key id matches but which does not open (a damaged
    # row) is the same case, and says so.
    caplog.clear()
    store.use_key(store.key)
    await db.execute(
        text("UPDATE secrets SET ciphertext = :c WHERE name = :n"),
        {"c": b"gAAAAA-not-a-token", "n": NAME},
    )
    assert await svc.get_secret(db, svc.PLATFORM, NAME, env=_env("from-env")) == "from-env"
    assert "does not open" in caplog.text


@pytest.mark.asyncio
async def test_a_row_under_an_older_key_in_the_list_still_opens(db, store):
    old = store.key
    await svc.set_secret(db, svc.PLATFORM, NAME, VALUE, user_id=None)
    store.use_key(f"{fernet_key()},{old}")  # a rotation in flight

    assert await svc.get_secret(db, svc.PLATFORM, NAME, env=_env()) == VALUE
    (meta,) = await svc.list_secrets(db, svc.PLATFORM)
    assert meta.readable is True


# --------------------------------------------------------------------------
# Every query names its owner (D32); the gateway's rows are not the backend's
# --------------------------------------------------------------------------


def test_an_owner_names_exactly_the_columns_its_scope_uses():
    tenant = uuid.uuid4()
    assert svc.Owner("platform") == svc.PLATFORM
    svc.Owner("agent", agent_id="probe-v1")
    svc.Owner("tenant", tenant_id=tenant, agent_id="probe-v1")
    for bad in (
        dict(scope="platform", tenant_id=tenant),
        dict(scope="platform", agent_id="probe-v1"),
        dict(scope="agent"),
        dict(scope="agent", tenant_id=tenant, agent_id="probe-v1"),
        dict(scope="tenant", agent_id="probe-v1"),
        dict(scope="tenant", tenant_id=tenant),
        dict(scope="elsewhere"),
    ):
        with pytest.raises(ValueError):
            svc.Owner(**bad)
    with pytest.raises(ValueError, match="gateway"):
        svc.Owner("gateway")


@pytest.mark.asyncio
async def test_one_owners_row_is_invisible_to_another(db, store):
    tenants = []
    for label in ("a", "b"):
        tenant_id = uuid.uuid4()
        await db.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :n, :s)"),
            {"id": tenant_id, "n": f"Probe {label}", "s": f"probe-{tenant_id.hex[:12]}"},
        )
        tenants.append(tenant_id)
    a = svc.Owner("tenant", tenant_id=tenants[0], agent_id="probe-v1")
    b = svc.Owner("tenant", tenant_id=tenants[1], agent_id="probe-v1")
    agent_wide = svc.Owner("agent", agent_id="probe-v1")

    await svc.set_secret(db, a, "tool_key", "tenant-a's", user_id=None)

    assert await svc.get_secret(db, a, "tool_key", env=_env("env")) == "tenant-a's"
    assert await svc.get_secret(db, b, "tool_key", env=_env("env")) == "env"
    assert await svc.get_secret(db, agent_wide, "tool_key", env=_env("env")) == "env"
    assert await svc.get_secret(db, svc.PLATFORM, "tool_key", env=_env("env")) == "env"
    assert [m.name for m in await svc.list_secrets(db, b)] == []
    assert [m.name for m in await svc.list_secrets(db, svc.PLATFORM)] == []
    assert await svc.unset_secret(db, b, "tool_key") is False
    assert await svc.get_secret(db, a, "tool_key", env=_env("env")) == "tenant-a's"


# --------------------------------------------------------------------------
# One key in both processes (K7; D33, K7-15)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_refused_on_a_shared_key(db, store):
    """A gateway row sealed under the backend's key means the two store keys
    are one: a write refuses ``503 secrets_store_key_shared`` — the class
    the app's store handler answers with its own code — and writes nothing.
    Clear still works: removing ciphertext stores none."""
    await db.execute(
        text(
            "INSERT INTO secrets (scope, name, ciphertext, key_id, fingerprint) "
            "VALUES ('gateway', 'sealing.private_key', :c, :k, 'abcdefabcdef')"
        ),
        {"c": b"gAAAAA-the-gateways-row", "k": keyring.key_id(store.key)},
    )

    with pytest.raises(svc.SecretsStoreKeyShared) as refused:
        await svc.set_secret(db, svc.PLATFORM, NAME, VALUE, user_id=None)

    assert isinstance(refused.value, svc.SecretsStoreUnconfigured), "the app's 503 handler would miss it"
    assert refused.value.code == "secrets_store_key_shared"
    assert "LIBRERUN_BACKEND_SECRETS_KEY" in refused.value.detail
    assert store.key not in refused.value.detail
    assert (
        await db.execute(text("SELECT count(*) FROM secrets WHERE name = :n"), {"n": NAME})
    ).scalar_one() == 0

    # A gateway row under ANOTHER key is the normal case, and refuses nothing.
    await db.execute(
        text("UPDATE secrets SET key_id = :k WHERE scope = 'gateway'"),
        {"k": keyring.key_id(fernet_key())},
    )
    await svc.set_secret(db, svc.PLATFORM, NAME, VALUE, user_id=None)
