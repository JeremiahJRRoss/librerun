"""``<NAME>_FILE``: the gateway's half (K blueprint K2, L30).

The twin of ``backend/tests/test_secret_files.py``. Two settings models
share one helper, and a helper that only ONE model is actually wired to
is a helper that half the deployment does not have — so the rules are
asserted again here, against the process that holds the provider
credentials.

The gateway has the extra obligation: it is the one process with a
provider key (L23, L28), so the value must reach the outbound call and
nothing else. Both ends are checked — the key attaches, and no dump,
no error message and no ``os.environ`` carries it.
"""
from __future__ import annotations

import os

import pytest
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import secret_files
from app.secret_files import SecretFileUnreadable, SecretSourceConflict
from gateway.config import FILE_BACKED_SECRETS, MASKED_ONLY_SECRETS, GatewaySettings

CANARY = "sk-canary-no-sink-may-carry-c0ffee"


@pytest.fixture
def build(monkeypatch):
    """A ``GatewaySettings`` built against an environment this test owns,
    with the repository's own dotenv out of the way."""

    def _build(**environment: str) -> GatewaySettings:
        for name in (
            *FILE_BACKED_SECRETS,
            *(secret_files.file_variable(n) for n in FILE_BACKED_SECRETS),
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        return GatewaySettings(_env_file=None)

    return _build


@pytest.fixture
def secret_file(tmp_path):
    def _write(contents: str, name: str = "openai_api_key") -> str:
        path = tmp_path / name
        path.write_text(contents, encoding="utf-8")
        return str(path)

    return _write


# --------------------------------------------------------------------------
# The rule, against this model
# --------------------------------------------------------------------------


def test_the_file_is_read_when_the_variable_is_blank(build, secret_file):
    settings = build(OPENAI_API_KEY="", OPENAI_API_KEY_FILE=secret_file(CANARY))

    assert settings.OPENAI_API_KEY.get_secret_value() == CANARY


def test_reading_the_file_does_not_export_the_value(build, secret_file):
    """``docker inspect`` shows a container's environment. The whole
    reason a secret store mounts a file is to keep the value out of it,
    and an entrypoint that exported the file's contents would hand it
    straight back."""
    settings = build(OPENAI_API_KEY="", OPENAI_API_KEY_FILE=secret_file(CANARY))

    assert settings.OPENAI_API_KEY.get_secret_value() == CANARY
    assert os.environ.get("OPENAI_API_KEY") == ""
    assert CANARY not in "".join(os.environ.values())


def test_the_variable_still_wins_when_it_is_set(build):
    settings = build(ANTHROPIC_API_KEY=CANARY)

    assert settings.ANTHROPIC_API_KEY.get_secret_value() == CANARY


def test_both_set_and_different_refuses_by_name(build, secret_file):
    with pytest.raises(SecretSourceConflict) as raised:
        build(OPENAI_API_KEY="from-the-variable", OPENAI_API_KEY_FILE=secret_file(CANARY))

    message = str(raised.value)
    assert "OPENAI_API_KEY" in message and "OPENAI_API_KEY_FILE" in message
    assert CANARY not in message and "from-the-variable" not in message


def test_an_unreadable_file_is_refused_by_path(build, tmp_path):
    missing = str(tmp_path / "never-mounted")

    with pytest.raises(SecretFileUnreadable) as raised:
        build(OPENAI_API_KEY="", OPENAI_API_KEY_FILE=missing)

    assert missing in str(raised.value)


def test_exactly_one_trailing_newline_is_stripped(build, secret_file):
    """The failure this rule exists for: an Authorization header with a
    newline in it is rejected by the provider with a message about the
    key being invalid, and the operator goes looking at the key."""
    settings = build(OPENAI_API_KEY="", OPENAI_API_KEY_FILE=secret_file(f"{CANARY}\n"))

    assert settings.OPENAI_API_KEY.get_secret_value() == CANARY


def test_every_file_backed_field_reads_its_own_file(build, tmp_path):
    environment = {}
    for name in FILE_BACKED_SECRETS:
        path = tmp_path / f"{name.lower()}.secret"
        path.write_text(f"{CANARY}-{name}\n", encoding="utf-8")
        environment[name] = ""
        environment[secret_files.file_variable(name)] = str(path)

    settings = build(**environment)

    for name in FILE_BACKED_SECRETS:
        assert getattr(settings, name).get_secret_value() == f"{CANARY}-{name}"


def test_resolution_survives_a_later_assignment(build, secret_file):
    """The gateway's suite monkeypatches settings constantly, so this is
    not hypothetical here: a re-validation that wrote the bare string
    back would un-mask the provider key the first time any test touched
    any field."""
    settings = build(OPENAI_API_KEY="", OPENAI_API_KEY_FILE=secret_file(CANARY))

    settings.LOG_LEVEL = "DEBUG"

    assert isinstance(settings.OPENAI_API_KEY, SecretStr)
    assert settings.OPENAI_API_KEY.get_secret_value() == CANARY
    assert CANARY not in repr(settings)


# --------------------------------------------------------------------------
# Masking (L31)
# --------------------------------------------------------------------------


def _dumps(settings: GatewaySettings) -> list[str]:
    return [
        repr(settings),
        str(settings),
        f"{settings}",
        settings.model_dump_json(),
        repr(settings.model_dump()),
        repr(settings.model_dump(mode="json")),
    ]


def test_a_settings_dump_shows_stars_and_not_the_secret(build, secret_file):
    settings = build(
        OPENAI_API_KEY="",
        OPENAI_API_KEY_FILE=secret_file(CANARY),
        ANTHROPIC_API_KEY=f"{CANARY}-anthropic",
        DATABASE_URL=f"postgresql+asyncpg://librerun:{CANARY}@db:5432/librerun",
    )

    for rendered in _dumps(settings):
        assert "**********" in rendered
        assert CANARY not in rendered, rendered[:400]


def test_a_blank_secret_renders_empty_and_a_set_one_renders_stars(build, secret_file):
    """The same pair on this model: a provider key that is set never
    shows, and one that is unset is still SecretStr-typed rather than a
    plain string waiting to be filled in."""
    settings = build(OPENAI_API_KEY="", OPENAI_API_KEY_FILE=secret_file(CANARY))

    rendered = repr(settings)

    assert "OPENAI_API_KEY=SecretStr('**********')" in rendered
    assert "ANTHROPIC_API_KEY=SecretStr('')" in rendered
    assert CANARY not in rendered


def test_every_declared_secret_is_a_secretstr():
    for name in (*FILE_BACKED_SECRETS, *MASKED_ONLY_SECRETS):
        annotation = GatewaySettings.model_fields[name].annotation
        assert annotation is SecretStr, f"{name} is {annotation}, so a dump prints it"


def test_a_plain_str_secret_would_be_caught():
    """The negative probe for the two assertions above."""

    class Leaky(BaseSettings):
        model_config = SettingsConfigDict(extra="ignore")
        OPENAI_API_KEY: str = ""

    leaky = Leaky(OPENAI_API_KEY=CANARY)

    assert Leaky.model_fields["OPENAI_API_KEY"].annotation is not SecretStr
    assert CANARY in repr(leaky)


# --------------------------------------------------------------------------
# The declaration
# --------------------------------------------------------------------------


def test_the_model_declares_every_companion():
    problems = secret_files.pairing_problems(
        GatewaySettings.model_fields, FILE_BACKED_SECRETS, MASKED_ONLY_SECRETS
    )

    assert problems == []


def test_a_missing_companion_is_reported():
    """The negative probe: the same checker, on a model that forgot one."""
    problems = secret_files.pairing_problems(
        {"OPENAI_API_KEY", "LOG_LEVEL"}, ["OPENAI_API_KEY"], []
    )

    assert any("OPENAI_API_KEY_FILE" in p for p in problems)


def test_every_secret_shaped_field_is_declared_a_secret():
    declared = {*FILE_BACKED_SECRETS, *MASKED_ONLY_SECRETS}

    undeclared = [
        name
        for name in secret_files.secret_shaped(GatewaySettings.model_fields)
        if name not in declared
    ]

    assert undeclared == [], (
        f"{undeclared} look like credentials but are declared on neither "
        f"FILE_BACKED_SECRETS nor MASKED_ONLY_SECRETS in gateway/config.py"
    )


def test_the_drift_guard_catches_a_new_credential():
    class WithANewKey(GatewaySettings):
        MISTRAL_API_KEY: str = ""

    declared = {*FILE_BACKED_SECRETS, *MASKED_ONLY_SECRETS}
    undeclared = [
        name
        for name in secret_files.secret_shaped(WithANewKey.model_fields)
        if name not in declared
    ]

    assert undeclared == ["MISTRAL_API_KEY"]


def test_the_three_provider_keys_are_all_file_backed():
    """The set this batch exists to serve: a Docker, Podman or
    Kubernetes secret reaching the one process that holds a provider
    credential, with no wrapper (L28, L30)."""
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_AI_API_KEY"):
        assert name in FILE_BACKED_SECRETS
        assert GatewaySettings.model_fields[name].annotation is SecretStr
        assert secret_files.file_variable(name) in GatewaySettings.model_fields


def test_the_agent_key_prefix_is_not_shadowed_by_a_companion():
    """Every ``LIBRERUN_AGENT_KEY_*`` variable is read as ONE AGENT's
    key (``agent_key_variables``), so a settings field spelled that way
    would invert to an agent id. No companion this batch adds may land
    under that prefix."""
    from gateway.config import AGENT_KEY_PREFIX

    for name in GatewaySettings.model_fields:
        assert not name.startswith(AGENT_KEY_PREFIX), name


# --------------------------------------------------------------------------
# …and the value still reaches the call, and nothing else
# --------------------------------------------------------------------------


def test_the_key_still_attaches_to_an_outbound_call(build, secret_file, monkeypatch):
    """Masking is worthless if it also breaks the one use. The key is
    revealed at ``egress.credential_for`` and nowhere earlier."""
    from gateway import egress
    from gateway.steps import ResolvedStep

    settings = build(OPENAI_API_KEY="", OPENAI_API_KEY_FILE=secret_file(CANARY))
    monkeypatch.setattr(egress, "settings", settings)

    step = ResolvedStep(step_id="think", provider="openai", model="gpt-4o")

    assert egress.credential_for("openai") == CANARY
    assert egress.attach_route({}, step)["api_key"] == CANARY


def test_a_provider_error_is_still_scrubbed_of_the_key(build, secret_file, monkeypatch):
    """``_scrubbed`` removes the key BY VALUE from a provider's message
    before it is forwarded. It reads the settings field, so a masked
    field it could not reveal would leave the key in the error text —
    the exact opposite of what masking is for."""
    from gateway import egress

    settings = build(OPENAI_API_KEY="", OPENAI_API_KEY_FILE=secret_file(CANARY))
    monkeypatch.setattr(egress, "settings", settings)

    scrubbed = egress._scrubbed(f"AuthenticationError: api_key={CANARY} is invalid")

    assert CANARY not in scrubbed
    assert "[REDACTED_PROVIDER_KEY]" in scrubbed
