"""``<NAME>_FILE``: a secret delivered as a file (K blueprint K2, L30).

The backend's half. ``services/gateway/tests/test_secret_files.py`` is
the gateway's twin and asserts the same rules against the other settings
model, because one helper serving two models is only worth anything if
both models are actually wired to it.

Every guard here is negative-tested by injecting the violation it
exists to catch (CLAUDE.md): a declaration checker that reports "no
problems" on a tree where nothing is wrong proves nothing at all — it
has to be shown saying "problem" when there is one.
"""
from __future__ import annotations

import os

import pytest
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import secret_files
from app.config import FILE_BACKED_SECRETS, MASKED_ONLY_SECRETS, Settings
from app.secret_files import SecretFileUnreadable, SecretSourceConflict

CANARY = "canary-value-no-sink-may-carry-c0ffee"


@pytest.fixture
def build(tmp_path, monkeypatch):
    """A ``Settings`` built against an environment this test owns.

    ``_env_file=None`` so the repository's own ``.env`` — which an
    operator running the suite on their laptop may well have — cannot
    decide what these assertions measure.
    """

    def _build(**environment: str) -> Settings:
        for name in (
            *FILE_BACKED_SECRETS,
            *(secret_files.file_variable(n) for n in FILE_BACKED_SECRETS),
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        return Settings(_env_file=None)

    return _build


@pytest.fixture
def secret_file(tmp_path):
    def _write(contents: str, name: str = "app_secret_key") -> str:
        path = tmp_path / name
        path.write_text(contents, encoding="utf-8")
        return str(path)

    return _write


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


def test_the_file_is_read_when_the_variable_is_blank(build, secret_file):
    settings = build(APP_SECRET_KEY="", APP_SECRET_KEY_FILE=secret_file(CANARY))

    assert settings.APP_SECRET_KEY.get_secret_value() == CANARY


def test_reading_the_file_does_not_export_the_value(build, secret_file):
    """The point of the convention, as opposed to an entrypoint script
    that does ``export APP_SECRET_KEY=$(cat …)``: the value reaches the
    settings model and never the process environment, where every
    library, every child process and ``docker inspect`` can see it."""
    settings = build(APP_SECRET_KEY="", APP_SECRET_KEY_FILE=secret_file(CANARY))

    assert settings.APP_SECRET_KEY.get_secret_value() == CANARY
    assert os.environ.get("APP_SECRET_KEY") == ""
    assert CANARY not in "".join(os.environ.values())


def test_the_variable_still_wins_when_it_is_set(build, secret_file):
    """A deployment that never heard of this batch is unaffected."""
    settings = build(APP_SECRET_KEY=CANARY)

    assert settings.APP_SECRET_KEY.get_secret_value() == CANARY


def test_both_set_and_equal_is_fine(build, secret_file):
    """The normal shape of a compose override that blanks nothing: the
    two sources agree, so there is no ambiguity to report."""
    settings = build(APP_SECRET_KEY=CANARY, APP_SECRET_KEY_FILE=secret_file(CANARY))

    assert settings.APP_SECRET_KEY.get_secret_value() == CANARY


def test_both_set_and_different_refuses_by_name(build, secret_file):
    with pytest.raises(SecretSourceConflict) as raised:
        build(APP_SECRET_KEY="from-the-variable", APP_SECRET_KEY_FILE=secret_file(CANARY))

    message = str(raised.value)
    assert "APP_SECRET_KEY" in message and "APP_SECRET_KEY_FILE" in message


def test_the_conflict_carries_no_value(build, secret_file):
    """L31, in the one place it is easiest to break: the exception an
    operator reads out of a crash-looping container's log."""
    with pytest.raises(SecretSourceConflict) as raised:
        build(APP_SECRET_KEY="from-the-variable", APP_SECRET_KEY_FILE=secret_file(CANARY))

    rendered = f"{raised.value!r} {raised.value}"
    assert CANARY not in rendered
    assert "from-the-variable" not in rendered


def test_the_conflict_is_not_a_validation_error(build, secret_file):
    """A ``ValueError`` here would come back as pydantic's
    ``ValidationError``, which renders the validator's input — the whole
    merged settings dict, every secret in it — as ``input_value=``. The
    exception written to protect a secret would print all of them, so
    these two are deliberately not ``ValueError``s."""
    from pydantic import ValidationError

    with pytest.raises(SecretSourceConflict):
        build(APP_SECRET_KEY="from-the-variable", APP_SECRET_KEY_FILE=secret_file(CANARY))

    assert not issubclass(SecretSourceConflict, ValueError)
    assert not issubclass(SecretFileUnreadable, ValueError)
    assert not issubclass(SecretSourceConflict, ValidationError)


def test_an_unreadable_file_is_refused_by_path(build, tmp_path):
    missing = str(tmp_path / "never-mounted")

    with pytest.raises(SecretFileUnreadable) as raised:
        build(APP_SECRET_KEY="", APP_SECRET_KEY_FILE=missing)

    assert missing in str(raised.value)


def test_a_file_that_is_not_utf8_is_refused_by_path(build, tmp_path):
    path = tmp_path / "binary_secret"
    path.write_bytes(b"\xff\xfe not text")

    with pytest.raises(SecretFileUnreadable) as raised:
        build(APP_SECRET_KEY="", APP_SECRET_KEY_FILE=str(path))

    assert str(path) in str(raised.value)


@pytest.mark.parametrize(
    "written,expected",
    [
        (f"{CANARY}\n", CANARY),
        (f"{CANARY}\r\n", CANARY),
        (f"{CANARY}\n\n", f"{CANARY}\n"),
        (CANARY, CANARY),
        (f"{CANARY} ", f"{CANARY} "),
        (f"one\n{CANARY}\n", f"one\n{CANARY}"),
    ],
)
def test_exactly_one_trailing_newline_is_stripped(build, secret_file, written, expected):
    """One, not all trailing whitespace: ``printf`` and every editor add
    exactly one, and a secret that legitimately ends in a space is not
    this helper's to change."""
    settings = build(APP_SECRET_KEY="", APP_SECRET_KEY_FILE=secret_file(written))

    assert settings.APP_SECRET_KEY.get_secret_value() == expected


def test_an_empty_file_binds_an_empty_secret(build, secret_file):
    settings = build(APP_SECRET_KEY="", APP_SECRET_KEY_FILE=secret_file(""))

    assert settings.APP_SECRET_KEY.get_secret_value() == ""


def test_a_blank_file_variable_is_treated_as_unset(build):
    """``${APP_SECRET_KEY_FILE:-}`` in a compose file interpolates to the
    empty string, which must mean "not using this" and not "read the
    file at ''"."""
    settings = build(APP_SECRET_KEY=CANARY, APP_SECRET_KEY_FILE="")

    assert settings.APP_SECRET_KEY.get_secret_value() == CANARY


def test_the_file_variable_works_from_a_dotenv(tmp_path, monkeypatch, secret_file):
    """Why the companions are declared FIELDS rather than looked up in
    ``os.environ``: ``extra="ignore"`` means pydantic never even reads a
    dotenv line it has no field for, so an undeclared ``_FILE`` would be
    silently discarded — absent rather than broken, which is the failure
    nobody notices. The documented local dev mode is ``cd backend &&
    uvicorn`` against the repository-root ``.env``."""
    for name in ("APP_SECRET_KEY", "APP_SECRET_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    dotenv = tmp_path / "dotenv"
    dotenv.write_text(
        f"APP_SECRET_KEY=\nAPP_SECRET_KEY_FILE={secret_file(CANARY)}\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=str(dotenv))

    assert settings.APP_SECRET_KEY.get_secret_value() == CANARY


def test_resolution_survives_a_later_assignment(build, secret_file):
    """``validate_assignment`` re-runs every validator, this one
    included, so the resolution has to be idempotent: the second pass
    sees the file's value already in the field and must not read that as
    the variable and the file disagreeing with each other."""
    settings = build(APP_SECRET_KEY="", APP_SECRET_KEY_FILE=secret_file(CANARY))

    settings.LOG_LEVEL = "DEBUG"

    assert isinstance(settings.APP_SECRET_KEY, SecretStr), (
        "the re-run replaced the masked field with the bare string it "
        "held — every later dump would print it"
    )
    assert settings.APP_SECRET_KEY.get_secret_value() == CANARY
    assert settings.LOG_LEVEL == "DEBUG"
    for rendered in _dumps(settings):
        assert CANARY not in rendered


def test_every_file_backed_field_reads_its_own_file(build, tmp_path):
    """Not just ``APP_SECRET_KEY``: the whole list, one file each, so a
    field that was added to the list and forgotten in the model is
    caught here rather than in a deployment."""
    environment = {}
    for name in FILE_BACKED_SECRETS:
        path = tmp_path / f"{name.lower()}.secret"
        path.write_text(f"{CANARY}-{name}\n", encoding="utf-8")
        environment[name] = ""
        environment[secret_files.file_variable(name)] = str(path)

    settings = build(**environment)

    for name in FILE_BACKED_SECRETS:
        assert getattr(settings, name).get_secret_value() == f"{CANARY}-{name}"


# --------------------------------------------------------------------------
# Masking (L31): a settings dump can never print one
# --------------------------------------------------------------------------


def _dumps(settings: Settings) -> list[str]:
    """Every way this object turns into text that could reach a log."""
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
        APP_SECRET_KEY="",
        APP_SECRET_KEY_FILE=secret_file(CANARY),
        AZURE_CLIENT_SECRET=f"{CANARY}-azure",
        INITIAL_ADMIN_PASSWORD=f"{CANARY}-admin",
    )

    for rendered in _dumps(settings):
        assert "**********" in rendered
        assert CANARY not in rendered, rendered[:400]


def test_a_blank_secret_renders_empty_and_a_set_one_renders_stars(build, secret_file):
    """``SecretStr`` masks what it holds, and an empty one holds nothing.

    Worth pinning, because the obvious assertion — "every secret field
    shows ``**********``" — is FALSE for a deployment that has not set
    one, and a CI check written that way fails on a tree with nothing
    wrong. What matters is the pair: a field with a value never shows it,
    and a field without one is still SecretStr-typed, so it is safe the
    day somebody fills it in.
    """
    settings = build(APP_SECRET_KEY="", APP_SECRET_KEY_FILE=secret_file(CANARY))

    rendered = repr(settings)

    assert "APP_SECRET_KEY=SecretStr('**********')" in rendered
    assert "AZURE_CLIENT_SECRET=SecretStr('')" in rendered
    assert CANARY not in rendered


def test_every_declared_secret_is_a_secretstr():
    for name in (*FILE_BACKED_SECRETS, *MASKED_ONLY_SECRETS):
        annotation = Settings.model_fields[name].annotation
        assert annotation is SecretStr, f"{name} is {annotation}, so a dump prints it"


def test_a_plain_str_secret_would_be_caught():
    """The negative probe for the two assertions above: the same checks
    against a model that got it wrong must fail, or they are measuring
    nothing."""

    class Leaky(BaseSettings):
        model_config = SettingsConfigDict(extra="ignore")
        APP_SECRET_KEY: str = ""

    leaky = Leaky(APP_SECRET_KEY=CANARY)

    assert Leaky.model_fields["APP_SECRET_KEY"].annotation is not SecretStr
    assert CANARY in repr(leaky)
    assert "**********" not in repr(leaky)


# --------------------------------------------------------------------------
# The declaration itself: two halves written by hand, so checked
# --------------------------------------------------------------------------


def test_the_model_declares_every_companion():
    problems = secret_files.pairing_problems(
        Settings.model_fields, FILE_BACKED_SECRETS, MASKED_ONLY_SECRETS
    )

    assert problems == []


def test_a_missing_companion_is_reported():
    problems = secret_files.pairing_problems(
        {"APP_SECRET_KEY", "OTHER"}, ["APP_SECRET_KEY"], []
    )

    assert any("APP_SECRET_KEY_FILE" in p for p in problems)


def test_a_companion_with_nothing_to_fill_is_reported():
    problems = secret_files.pairing_problems({"WIDGET_FILE", "WIDGET"}, [], [])

    assert any("WIDGET_FILE" in p and "nothing reads it" in p for p in problems)


def test_a_masked_only_field_may_not_carry_a_companion():
    problems = secret_files.pairing_problems(
        {"SEARCH_TOKEN", "SEARCH_TOKEN_FILE"}, [], ["SEARCH_TOKEN"]
    )

    assert any("SEARCH_TOKEN_FILE" in p for p in problems)


def test_a_field_on_both_lists_is_reported():
    problems = secret_files.pairing_problems(
        {"X", "X_FILE"}, ["X"], ["X"]
    )

    assert any("both" in p for p in problems)


def test_every_secret_shaped_field_is_declared_a_secret():
    """The drift guard. A field named like a credential that is on
    neither list is a credential nobody remembered to mask, and the
    ``_FILE`` convention would not reach it either."""
    declared = {*FILE_BACKED_SECRETS, *MASKED_ONLY_SECRETS}

    undeclared = [
        name
        for name in secret_files.secret_shaped(Settings.model_fields)
        if name not in declared
    ]

    assert undeclared == [], (
        f"{undeclared} look like credentials but are declared on neither "
        f"FILE_BACKED_SECRETS nor MASKED_ONLY_SECRETS in app/config.py"
    )


def test_the_drift_guard_catches_a_new_credential():
    """The negative probe for the test above: add the field the guard
    exists to catch and watch it be caught."""

    class WithANewKey(Settings):
        SLACK_API_KEY: str = ""

    declared = {*FILE_BACKED_SECRETS, *MASKED_ONLY_SECRETS}
    undeclared = [
        name
        for name in secret_files.secret_shaped(WithANewKey.model_fields)
        if name not in declared
    ]

    assert undeclared == ["SLACK_API_KEY"]


def test_a_file_companion_is_not_itself_treated_as_a_secret():
    """A path is not a secret, and a guard that called every ``_FILE``
    field one would demand ``APP_SECRET_KEY_FILE_FILE``."""
    assert "APP_SECRET_KEY_FILE" not in secret_files.secret_shaped(Settings.model_fields)


# --------------------------------------------------------------------------
# The exclusion the blueprint names on purpose
# --------------------------------------------------------------------------


def test_the_agents_search_key_left_the_settings_model(monkeypatch):
    """L13, K8a: the demo agent's search key is its own declared tool
    secret, read through the secrets capability with this variable as its
    in-process fallback, so the backend's settings model neither binds nor
    masks it — K2 had masked it with no ``_FILE`` companion (T11) because
    the agent read ``os.environ`` behind the model's back. Put back in the
    model, it would fail the manifest of every agent that declares it: the
    reserved-name rule reads the model (D35)."""
    from pathlib import Path

    import app.config as cfg
    from app.agents.manifest import ManifestError, load_manifest

    assert "TAVILY_API_KEY" not in Settings.model_fields
    assert "TAVILY_API_KEY" not in {*FILE_BACKED_SECRETS, *MASKED_ONLY_SECRETS}
    assert MASKED_ONLY_SECRETS == ()

    demo = Path(__file__).resolve().parents[1] / "agents" / "vita_v1"
    assert "tavily_api_key" in load_manifest(demo).secrets

    class WithTheKeyBack(Settings):
        TAVILY_API_KEY: SecretStr = SecretStr("")

    monkeypatch.setattr(cfg, "Settings", WithTheKeyBack)
    with pytest.raises(ManifestError, match="reserved: TAVILY_API_KEY is a setting"):
        load_manifest(demo)


def test_no_provider_key_became_file_backed_here():
    """L23 is not relaxed by this batch: the backend still declares no
    provider credential, with or without a companion variable."""
    from app.config import PROVIDER_KEY_VARIABLES

    for name in PROVIDER_KEY_VARIABLES:
        assert name not in Settings.model_fields
        assert secret_files.file_variable(name) not in Settings.model_fields
        assert name not in FILE_BACKED_SECRETS
