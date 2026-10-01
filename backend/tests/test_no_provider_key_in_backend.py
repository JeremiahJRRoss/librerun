"""No model provider's credential is in the backend process (S4a, L23).

The gateway exists as a separate process so the provider keys live in
one place. The compose job proves the backend CONTAINER receives none —
but the documented local deployment is `cd backend && uvicorn`, and the
settings model reads the repository-root `.env`, which is exactly where
an operator's keys are. Declaring the fields was therefore enough to
break the claim on its own: `settings.OPENAI_API_KEY` held the real
value, and any in-process `python-package` agent could read it by
importing `app.config` — the thing removing `credential()` was for.
Nothing in the backend ever used the fields (Codex P1).

The first test is the one that matters: it writes a `.env` with real
keys in it and requires the settings model not to pick them up. A test
that only looked at the class could pass while pydantic bound them by
some other road.
"""
from __future__ import annotations

import importlib

import pytest

from app import config as app_config
from app.config import PROVIDER_KEY_VARIABLES


@pytest.fixture
def dotenv_settings(tmp_path, monkeypatch):
    """A Settings built against a .env we control, the way the dev-mode
    process builds one against the repository's."""

    def build(lines: dict[str, str]):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "APP_SECRET_KEY=a-real-secret-for-this-test\n"
            + "".join(f"{k}={v}\n" for k, v in lines.items())
        )
        for name in lines:
            monkeypatch.delenv(name, raising=False)
        return app_config.Settings(_env_file=str(env_file))

    return build


def test_a_dotenv_carrying_provider_keys_does_not_materialise_them(dotenv_settings):
    """The whole finding, as the deployment reaches it."""
    settings = dotenv_settings(
        {
            "OPENAI_API_KEY": "sk-a-real-openai-key",
            "ANTHROPIC_API_KEY": "sk-ant-a-real-key",
            "GOOGLE_AI_API_KEY": "a-real-google-key",
        }
    )

    for name in PROVIDER_KEY_VARIABLES:
        assert not hasattr(settings, name), name
    # …and the values are nowhere on the model under any spelling.
    dumped = repr(settings.model_dump())
    for secret in ("sk-a-real-openai-key", "sk-ant-a-real-key", "a-real-google-key"):
        assert secret not in dumped


@pytest.mark.parametrize("name", PROVIDER_KEY_VARIABLES)
def test_the_settings_model_declares_no_provider_key(name):
    assert name not in app_config.Settings.model_fields


def test_an_agents_own_third_party_secret_is_still_delivered(dotenv_settings, monkeypatch):
    """The line is *model provider* credentials, not every secret: an
    agent's own third-party key is still given to the process the agent
    runs in (L20), and removing it would break search rather than protect
    anything. Since K8a it reaches the agent as the in-process fallback of
    the tool secret the agent declares, read from the environment by the
    secrets capability — and bound to no field of this model (L13)."""
    from app.services import tool_secrets_service

    settings = dotenv_settings({"TAVILY_API_KEY": "tvly-agents-own"})
    assert not hasattr(settings, "TAVILY_API_KEY")
    assert "tvly-agents-own" not in repr(settings.model_dump())

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-agents-own")
    assert tool_secrets_service.environment_value("tavily_api_key") == "tvly-agents-own"


def test_no_backend_module_reads_a_provider_key():
    """A field can come back. A reader is what would make it matter, so
    the tree is checked for one — the guard that fails if somebody
    re-adds the field *and* starts using it."""
    import pathlib

    root = pathlib.Path(app_config.__file__).resolve().parents[1]
    offenders = []
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for name in PROVIDER_KEY_VARIABLES:
            if f"settings.{name}" in text:
                offenders.append(f"{path.relative_to(root)}: settings.{name}")

    assert offenders == []


# --------------------------------------------------------------------------
# …and when one is in the environment anyway, the process says so
# --------------------------------------------------------------------------


def test_a_provider_key_in_the_environment_is_reported():
    """Removing the settings field cannot reach a key an operator
    exported into the shell that runs uvicorn: that sits in os.environ,
    readable by any in-process agent whatever the model says."""
    from app.demo import warn_if_provider_key_present

    found = warn_if_provider_key_present({"OPENAI_API_KEY": "sk-exported-by-hand"})

    assert found == ["OPENAI_API_KEY"]


def test_a_clean_environment_reports_nothing():
    from app.demo import warn_if_provider_key_present

    assert warn_if_provider_key_present({"OPENAI_API_KEY": "", "PATH": "/usr/bin"}) == []


def test_an_agents_own_secret_is_not_reported():
    from app.demo import warn_if_provider_key_present

    assert warn_if_provider_key_present({"TAVILY_API_KEY": "tvly-agents-own"}) == []


def test_the_warning_names_variables_and_never_values(caplog):
    import logging

    from app.demo import warn_if_provider_key_present

    with caplog.at_level(logging.WARNING):
        warn_if_provider_key_present({"ANTHROPIC_API_KEY": "sk-ant-do-not-print-me"})

    assert "sk-ant-do-not-print-me" not in caplog.text


# --------------------------------------------------------------------------
# K7 (L33, D33): the half of T3 a sealed provider key needs
# --------------------------------------------------------------------------


def _loads_as_a_private_key(value: str) -> bool:
    from cryptography.hazmat.primitives import serialization

    try:
        serialization.load_pem_private_key(value.encode("utf-8"), password=None)
    except (ValueError, TypeError, UnicodeError):
        return False
    return True


@pytest.mark.asyncio
async def test_backend_holds_no_loadable_private_key():
    """No value the backend holds — a secret field of its settings, raw, or
    any row of the store its own key list opens — loads as a private key.
    The gateway's sealing keypair sits in the same table, sealed under the
    GATEWAY's key: if the two variables held one key, the backend's
    MultiFernet would open it and every sealed provider key with it.

    The two keys are taken from the environment when set, so the operator's
    mistake is the test's input: ``LIBRERUN_BACKEND_SECRETS_KEY`` and
    ``LIBRERUN_GATEWAY_SECRETS_KEY`` set to one value turn this red, as they
    stop the gateway's boot. Unset, each is a fresh key, as two real ones
    are."""
    import base64
    import os

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from pydantic import SecretStr
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app import secrets_keyring as keyring
    from app.config import settings

    def fresh() -> str:
        return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")

    backend_keys = keyring.parse(settings.LIBRERUN_BACKEND_SECRETS_KEY) or [fresh().encode()]
    gateway_key = (os.environ.get("LIBRERUN_GATEWAY_SECRETS_KEY") or "").split(",")[0].strip() or fresh()

    # The gateway's keypair, as its store keeps it (gateway/provider_store.py).
    private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    sealed = keyring.seal([gateway_key.encode()], pem)

    engine = create_async_engine(settings.DATABASE_URL.get_secret_value())
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        "INSERT INTO secrets (scope, name, ciphertext, key_id, fingerprint) "
                        "VALUES ('gateway', 'k7.probe_keypair', :c, :k, :f)"
                    ),
                    {"c": sealed.ciphertext, "k": sealed.key_id, "f": sealed.fingerprint},
                )
                rows = (await connection.execute(text("SELECT scope, name, ciphertext FROM secrets"))).all()
            finally:
                await transaction.rollback()
    except OSError as exc:  # pragma: no cover — the suite's database is required in CI
        pytest.fail(f"no database at DATABASE_URL: {exc}")
    finally:
        await engine.dispose()

    held = []
    for name, field in type(settings).model_fields.items():
        value = getattr(settings, name, None)
        if isinstance(value, SecretStr):
            held.append((f"settings.{name}", value.get_secret_value()))
    for row in rows:
        try:
            held.append((f"secrets {row.scope}:{row.name}", keyring.unseal(backend_keys, row.ciphertext)))
        except (keyring.InvalidToken, UnicodeDecodeError):
            continue

    loadable = [where for where, value in held if value and _loads_as_a_private_key(value)]
    assert loadable == [], (
        f"the backend can load a private key from {loadable}: its store key opens the "
        f"gateway's sealing keypair, so the two store keys are one (D33)"
    )


def test_no_rsa_code_in_the_backend():
    """L33: the sealed path's only RSA code is the gateway's
    (``services/gateway/gateway/sealing.py``). The backend relays a blob
    it cannot open; asymmetric primitives in its code would be the first
    step to opening one."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    pattern = re.compile(
        r"cryptography\.hazmat\.primitives\.asymmetric|from cryptography\.hazmat\.primitives "
        r"import[^\n]*\b(rsa|padding)\b|load_pem_private_key|RSAPrivateKey|OAEP\("
    )
    offenders = [
        f"{path.relative_to(root.parent)}:{number}"
        for path in sorted(root.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == [], f"asymmetric crypto in the backend: {offenders}"
