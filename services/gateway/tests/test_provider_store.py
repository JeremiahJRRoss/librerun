"""Provider keys pasted in the admin UI, held by the gateway (K7; L33, D15,
D16, D33).

Against the real tables: the rows are ``secrets`` rows and the status is the
``gateway_status`` row, so a double would prove something about the double.
Each test clears the gateway's rows before and after — nothing else writes
them in this suite — and stops the refresher it started.

The paths covered, in the order the module docstring gives them: two
replicas share one keypair; a keypair no key opens and a key the backend
also holds each refuse the boot; missing tables are waited for, never
made; a blob sealed for another provider is rejected (the label); the blob
``openssl pkeyutl`` makes — the sweep's, and the browser's parameters —
opens; the refresher reloads with no Redis at all; and a key pasted while
the gateway runs reaches the provider on the next call, nothing restarted.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import inspect
import json
import os
import shutil
import subprocess
import threading

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text

from app import secrets_keyring as keyring
from gateway.auth import RUN_TOKEN_HEADER, STEP_HEADER


def fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


@pytest_asyncio.fixture
async def store(session, monkeypatch):
    """``provider_store`` on a clean slate. Depends on ``session`` for its
    probe: no database fails the suite under ``LIBRERUN_REQUIRE_DB``."""
    from gateway import config as gateway_config
    from gateway import db, provider_store

    async def clear() -> None:
        async with db.sessionmaker()() as s:
            await s.execute(text("DELETE FROM secrets WHERE scope = 'gateway'"))
            await s.execute(text("DELETE FROM gateway_status"))
            await s.commit()

    settings = gateway_config.settings
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_AI_API_KEY"):
        monkeypatch.setattr(settings, name, SecretStr(""))
    monkeypatch.setattr(settings, "LIBRERUN_GATEWAY_SECRETS_KEY", SecretStr(fernet_key()))
    await provider_store.stop()
    provider_store.reset()
    await clear()

    class Store:
        module = provider_store

        @staticmethod
        def use_key(value: str) -> None:
            monkeypatch.setattr(settings, "LIBRERUN_GATEWAY_SECRETS_KEY", SecretStr(value))

        @staticmethod
        async def paste(name: str, value: str, *, sealed_for: str | None = None, bump=True) -> bytes:
            """What the backend does with a POST: store the blob it cannot
            open as ``'sealed'``, its id the fingerprint, then bump."""
            from gateway import sealing

            pem = provider_store.public_key_pem()
            assert pem, "no public key to seal to"
            blob = sealing.seal(pem, sealed_for or name, value)
            await Store.store_blob(name, blob, bump=bump)
            return blob

        @staticmethod
        async def store_blob(name: str, blob: bytes, *, bump=True) -> None:
            async with db.sessionmaker()() as s:
                await s.execute(
                    text(
                        "INSERT INTO secrets (scope, name, ciphertext, key_id, fingerprint) "
                        "VALUES ('gateway', :name, :blob, 'sealed', :id) "
                        "ON CONFLICT ON CONSTRAINT uq_secrets_owner_name DO UPDATE SET "
                        "ciphertext = EXCLUDED.ciphertext, key_id = 'sealed', "
                        "fingerprint = EXCLUDED.fingerprint, updated_at = NOW()"
                    ),
                    {"name": f"provider.{name}", "blob": blob,
                     "id": hashlib.sha256(blob).hexdigest()[:12]},
                )
                await s.commit()
            if bump:
                await provider_store._bump_version()

        @staticmethod
        async def rows() -> dict:
            async with db.sessionmaker()() as s:
                found = (
                    await s.execute(
                        text("SELECT name, key_id, ciphertext FROM secrets WHERE scope = 'gateway'")
                    )
                ).all()
            return {r.name: r for r in found}

        @staticmethod
        async def status():
            async with db.sessionmaker()() as s:
                return (
                    await s.execute(
                        text("SELECT version, stub, providers, public_key_pem FROM gateway_status")
                    )
                ).first()

        @staticmethod
        async def until(predicate, seconds: float = 8.0) -> bool:
            deadline = asyncio.get_running_loop().time() + seconds
            while asyncio.get_running_loop().time() < deadline:
                if predicate():
                    return True
                await asyncio.sleep(0.05)
            return predicate()

        @staticmethod
        async def status_until(predicate, seconds: float = 8.0):
            """The status row once ``predicate(row)`` holds, else the last
            row read. A row the refresher writes is waited for, never read
            while its reload is still writing it."""
            deadline = asyncio.get_running_loop().time() + seconds
            row = await Store.status()
            while not predicate(row) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
                row = await Store.status()
            return row

    try:
        yield Store
    finally:
        await provider_store.stop()
        await clear()
        provider_store.reset()
        await db.dispose()


def _entry(store, name: str) -> dict:
    return next(e for e in store.module.entries() if e["name"] == name)


# --------------------------------------------------------------------------
# (b): one keypair, however many replicas; never replaced
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_replicas_one_keypair(store):
    """Two gateways starting together each find no keypair, each make one,
    and one insert wins: both then hold the WINNER's, since a blob sealed to
    the loser's would open on one replica and not the other (K7-08)."""
    from gateway import db, sealing

    keys = store.module.configured_keys()

    async def replica() -> str:
        async with db.sessionmaker()() as s:
            return sealing.public_pem(await store.module._keypair(s, keys))

    first, second = await asyncio.gather(replica(), replica())
    assert first == second
    rows = await store.rows()
    assert list(rows) == ["sealing.private_key"]
    assert rows["sealing.private_key"].key_id == keyring.key_id(keys[0])


@pytest.mark.asyncio
async def test_unopenable_keypair_refuses_boot(store):
    """A keypair row the configured key cannot open stops the boot, and is
    left exactly as it was: replacing it would orphan every blob sealed to
    it, without a word to anyone."""
    await store.module.reload()
    before = (await store.rows())["sealing.private_key"].ciphertext

    store.use_key(fernet_key())
    store.module.reset()
    with pytest.raises(store.module.BootRefused, match="does not open under LIBRERUN_GATEWAY_SECRETS_KEY"):
        await store.module.start()

    rows = await store.rows()
    assert list(rows) == ["sealing.private_key"]
    assert bytes(rows["sealing.private_key"].ciphertext) == bytes(before), "the keypair was replaced"


@pytest.mark.asyncio
async def test_backend_key_id_refuses_boot(store):
    """One key in both variables: a row of another scope carries the id of
    the gateway's key, so the gateway refuses to start, naming both (D33,
    K7-15)."""
    from gateway import db

    shared = fernet_key()
    sealed = keyring.seal([shared.encode()], "a platform secret")
    async with db.sessionmaker()() as s:
        await s.execute(
            text(
                "INSERT INTO secrets (scope, name, ciphertext, key_id, fingerprint) "
                "VALUES ('platform', 'k7.probe_shared_key', :c, :k, :f)"
            ),
            {"c": sealed.ciphertext, "k": sealed.key_id, "f": sealed.fingerprint},
        )
        await s.commit()
    try:
        store.use_key(shared)
        with pytest.raises(store.module.BootRefused) as refused:
            await store.module.start()
        message = str(refused.value)
        assert "LIBRERUN_GATEWAY_SECRETS_KEY" in message
        assert "LIBRERUN_BACKEND_SECRETS_KEY" in message
        assert shared not in message
        assert "sealing.private_key" not in await store.rows(), "a keypair was made under a shared key"
    finally:
        async with db.sessionmaker()() as s:
            await s.execute(text("DELETE FROM secrets WHERE name = 'k7.probe_shared_key'"))
            await s.commit()


# --------------------------------------------------------------------------
# The environment until the tables exist; never created here (K7-09)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_tables_waited_for(store, monkeypatch):
    """The gateway starts before the backend's migrations: with the tables
    missing it serves gateway.env, writes nothing, and loads once they
    appear. It never creates them — that is the backend's migration."""
    from gateway import config as gateway_config
    from gateway import egress

    assert "CREATE TABLE" not in inspect.getsource(store.module).upper()
    monkeypatch.setattr(gateway_config.settings, "OPENAI_API_KEY", SecretStr("sk-from-gateway-env-0001"))
    real = store.module._tables_present
    calls = {"n": 0}

    async def not_yet(session):
        calls["n"] += 1
        return False if calls["n"] <= 3 else await real(session)

    monkeypatch.setattr(store.module, "_tables_present", not_yet)
    await store.module.start()  # does not raise
    assert egress.credential_for("openai") == "sk-from-gateway-env-0001"
    assert store.module.public_key_pem() is None
    assert await store.status() is None, "a status row was written before the tables existed"

    await store.module.stop()
    # A reload swaps its state in first and writes the status row after it
    # ("Swap first", ``reload()``), so the key is in effect a moment before
    # the row says so. The row is waited for, not read at the swap: the
    # write is slowed here, as a loaded runner slows it, so that order is
    # met on every run rather than now and then.
    write_status = store.module._write_status

    async def slow_write(session, loaded):
        await asyncio.sleep(0.3)
        return await write_status(session, loaded)

    monkeypatch.setattr(store.module, "_write_status", slow_write)
    store.module.start_refresher(poll=0.05, full=0.2)
    assert await store.until(lambda: store.module.public_key_pem() is not None)
    status = await store.status_until(lambda row: row is not None)
    assert status is not None and status.public_key_pem == store.module.public_key_pem()
    assert egress.credential_for("openai") == "sk-from-gateway-env-0001"


# --------------------------------------------------------------------------
# The label, and the blob openssl makes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_providers_blob_rejected(store, monkeypatch):
    """A blob sealed for ``anthropic`` stored as ``openai``'s is rejected —
    the OAEP label binds a blob to its provider (K7-04) — and gateway.env
    serves openai meanwhile. The row stays, for the page to show."""
    from gateway import config as gateway_config
    from gateway import egress

    monkeypatch.setattr(gateway_config.settings, "OPENAI_API_KEY", SecretStr("sk-from-gateway-env-0002"))
    await store.module.reload()
    await store.paste("openai", "sk-ant-sealed-for-another-provider", sealed_for="anthropic")
    await store.module.reload()

    entry = _entry(store, "openai")
    assert (entry["row"], entry["reason"], entry["source"]) == ("rejected", "unsealable", "env")
    assert egress.credential_for("openai") == "sk-from-gateway-env-0002"
    assert (await store.rows())["provider.openai"].key_id == "sealed"


@pytest.mark.asyncio
async def test_openssl_blob_opens(store, tmp_path):
    """The blob ``openssl pkeyutl`` makes with the browser's parameters —
    OAEP, SHA-256 for the digest and MGF1, the label in hex — opens, and
    the fingerprint is ``openssl``'s digest of the SPKI DER (D34). This is
    how Gate S seals its canary outside the sweep (K7-11). The key goes in
    by file, never on argv."""
    from gateway import sealing

    openssl = shutil.which("openssl")
    assert openssl, "openssl is on every runner this suite runs on; the check cannot be skipped"
    await store.module.reload()
    pem = store.module.public_key_pem()
    (tmp_path / "public.pem").write_text(pem)
    (tmp_path / "key.txt").write_text("sk-ant-api03-openssl-vector-0001")
    subprocess.run(
        [
            openssl, "pkeyutl", "-encrypt", "-pubin", "-inkey", str(tmp_path / "public.pem"),
            "-pkeyopt", "rsa_padding_mode:oaep",
            "-pkeyopt", "rsa_oaep_md:sha256",
            "-pkeyopt", "rsa_mgf1_md:sha256",
            "-pkeyopt", "rsa_oaep_label:" + sealing.label("anthropic").hex(),
            "-in", str(tmp_path / "key.txt"),
            "-out", str(tmp_path / "blob.bin"),
        ],
        check=True,
    )
    blob = (tmp_path / "blob.bin").read_bytes()
    await store.store_blob("anthropic", blob)
    await store.module.reload()
    assert store.module.runtime_key("anthropic") == "sk-ant-api03-openssl-vector-0001"
    assert _entry(store, "anthropic")["row"] == "runtime"

    der = subprocess.run(
        [openssl, "pkey", "-pubin", "-in", str(tmp_path / "public.pem"), "-outform", "DER"],
        check=True, capture_output=True,
    ).stdout
    assert sealing.fingerprint(pem) == "SHA256:" + hashlib.sha256(der).hexdigest()


# --------------------------------------------------------------------------
# The refresher
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_refreshes_without_redis(store, monkeypatch):
    """No Redis, so no counter to watch: the full reload (every 30 s in
    production, a fraction of a second here) still finds a pasted key."""
    from gateway import config as gateway_config

    monkeypatch.setattr(gateway_config.settings, "REDIS_URL", SecretStr("redis://127.0.0.1:1/0"))
    await store.module.start()
    await store.module.stop()
    assert await store.module._version() is None, "the dead Redis answered"
    store.module.start_refresher(poll=0.05, full=0.3)
    await store.paste("anthropic", "sk-ant-pasted-without-redis-01", bump=False)
    assert await store.until(
        lambda: store.module.runtime_key("anthropic") == "sk-ant-pasted-without-redis-01"
    ), "the key was never loaded: the full reload does not run without Redis"


class _MockProvider(http.server.BaseHTTPRequestHandler):
    """An OpenAI-compatible chat endpoint that remembers each bearer's
    SHA-256 — never the key — and answers 401 without one."""

    seen: list[str] = []

    def do_POST(self):  # noqa: N802 — the stdlib's name
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        auth = self.headers.get("Authorization") or ""
        bearer = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if not bearer:
            self._answer(401, {"error": {"message": "no key", "type": "invalid_request_error"}})
            return
        _MockProvider.seen.append(hashlib.sha256(bearer.encode()).hexdigest())
        self._answer(200, {
            "id": "chatcmpl-k7", "object": "chat.completion", "created": 0, "model": "gpt-4o",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "the mock provider answered"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
        })

    def _answer(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # quiet
        pass


@pytest.mark.asyncio
async def test_pasted_key_reaches_provider_unrestarted(store, client, run_token, monkeypatch):
    """The whole path, one process: a call with no key fails; a key pasted
    (the backend's blob, then its bump) is adopted by the running refresher;
    the next call carries it to the provider. Nothing restarted."""
    from gateway import config as gateway_config
    from gateway import telemetry

    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _MockProvider.seen = []
    try:
        monkeypatch.setattr(
            gateway_config.settings, "OPENAI_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1"
        )
        telemetry.init()
        await store.module.start()
        await store.module.stop()
        store.module.start_refresher(poll=0.05, full=5.0)

        token = await run_token()

        async def call():
            return await client.post(
                "/v1/chat/completions",
                headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think"},
                json={"model": "librerun/think", "messages": [{"role": "user", "content": "hi"}]},
            )

        before = await call()
        assert before.status_code != 200, before.text
        assert _MockProvider.seen == [], "a bearer reached the provider with no key anywhere"

        pasted = "sk-k7-pasted-in-the-admin-page-000001"
        await store.paste("openai", pasted)
        assert await store.until(lambda: store.module.runtime_key("openai") == pasted)

        after = await call()
        assert after.status_code == 200, after.text
        assert after.json()["choices"][0]["message"]["content"] == "the mock provider answered"
        assert _MockProvider.seen == [hashlib.sha256(pasted.encode()).hexdigest()]
    finally:
        server.shutdown()


# --------------------------------------------------------------------------
# The blank key, the status row and the log line
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_blank_key_serves_the_environment_and_publishes_no_key(store, monkeypatch):
    """K7-07: no store key, no keypair, no public key — the admin page then
    answers 503 and points at gateway.env, which serves alone."""
    from gateway import config as gateway_config
    from gateway import egress

    store.use_key("")
    monkeypatch.setattr(gateway_config.settings, "ANTHROPIC_API_KEY", SecretStr("sk-ant-from-env-03"))
    await store.module.start()
    status = await store.status()
    assert status is not None and status.public_key_pem is None
    assert "sealing.private_key" not in await store.rows()
    assert egress.credential_for("anthropic") == "sk-ant-from-env-03"
    by_name = {e["name"]: e for e in status.providers}
    assert by_name["anthropic"]["source"] == "env"
    assert by_name["openai"]["source"] == "unset"


@pytest.mark.asyncio
async def test_a_malformed_key_refuses_boot_without_naming_it(store):
    store.use_key("not-a-fernet-key")
    with pytest.raises(keyring.SecretsStoreKeyInvalid) as refused:
        await store.module.start()
    assert "not-a-fernet-key" not in str(refused.value)
    assert "LIBRERUN_GATEWAY_SECRETS_KEY" in str(refused.value)


@pytest.mark.asyncio
async def test_the_status_row_names_and_fingerprints_and_never_holds_a_key(store):
    """What the backend reads (D16): every name with its aliases, the
    source, a keyed fingerprint for a stored key, who set it and when —
    and no value anywhere in the row."""
    from gateway import sealing

    await store.module.reload()
    pasted = "sk-ant-status-row-never-holds-me"
    await store.paste("anthropic", pasted)
    await store.module.reload()
    status = await store.status()
    assert status.version and status.stub is False
    assert status.public_key_pem == store.module.public_key_pem()
    by_name = {e["name"]: e for e in status.providers}
    assert set(by_name) == {"openai", "anthropic", "google"}
    assert by_name["openai"]["aliases"] == ["openai", "azure"]
    assert by_name["google"]["aliases"] == ["gemini", "google", "vertex_ai"]
    anthropic = by_name["anthropic"]
    assert (anthropic["source"], anthropic["row"]) == ("runtime", "runtime")
    assert len(anthropic["fingerprint"]) == 12 and anthropic["set_at"]
    row = (await store.rows())["provider.anthropic"]
    assert row.key_id == keyring.key_id(store.module.configured_keys()[0]), "not adopted"
    assert pasted not in json.dumps(status.providers) + (status.public_key_pem or "")
    # The fingerprint the operator compares, from the public half alone.
    assert sealing.fingerprint(status.public_key_pem).startswith("SHA256:")
