"""A setting the gateway READS has to reach the gateway (S4a).

`GatewaySettings.OPENAI_BASE_URL` is used on every OpenAI call, and a
test pins that contract — but compose never passed the variable, so in
the standard deployment the whole feature was inert and an operator's
compatible endpoint was ignored (Codex P1). A setting nothing delivers
is a setting that does not exist, and the unit test could not see that.

The guard is the class: every field of `GatewaySettings` that names a
provider credential or an endpoint must be DELIVERED to the gateway
service. Those are the ones whose absence is silent — the rest have
defaults that are correct on their own.

"Delivered" is two things since K1, not one: an entry in the service's
`environment:` block, or a name declared in the example beside an
`env_file` the service lists. The three provider keys moved to
`gateway.env` (decision L28 — the root `.env` is read by compose, the
backend and the frontend build, and a provider credential is read by
one process), and an `environment:` line for them would have DEFEATED
that file rather than backed it up: an `environment:` entry overrides
an env_file entry even when it interpolates to blank. A guard that
only knew about `environment:` would have forced the weaker layout to
stay, so it learned the other delivery instead of being relaxed.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[3]

# Settings whose whole purpose is to be SET by a deployment: absent, the
# default is not "fine", it is "the operator's choice was dropped".
DEPLOYMENT_SETTINGS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_AI_API_KEY",
    "OPENAI_BASE_URL",
    "LIBRERUN_STUB_LLM",
    "LIBRERUN_KB_EMBED_MODEL",
)


# The gateway's configuration surface is NOT just ``GatewaySettings``.
# It imports backend modules — ``app.services.pii_service`` above all —
# and those read ``app.config.settings.X``. Those reads are invisible to
# a scan of this service's own settings class, which is exactly how
# ``PII_PHONE_REGION`` reached production unset in this container: the
# gateway ran the shared walker with the region stuck at its ``US``
# default, so a phone number the backend refuses passed the outbound
# walk and reached a provider (Codex P1).
#
# So this half is DERIVED rather than listed. The list above still has
# to be maintained by hand — those are fields of this service's own
# settings class and nothing else names them — but the reads below are
# found by reading the code, which means the next backend setting the
# gateway starts consuming is covered by existing.
def borrowed_backend_settings() -> dict[str, set[str]]:
    """``{SETTING: {module, ...}}`` for every ``settings.X`` read by a
    backend module the gateway imports."""
    import re

    gateway_src = REPO / "services" / "gateway" / "gateway"
    modules: set[str] = set()
    for source in gateway_src.glob("*.py"):
        for match in re.finditer(r"from (app[\w.]*) import ([^\n]+)", source.read_text()):
            base, names = match.group(1), match.group(2)
            for name in (n.strip() for n in names.replace("(", "").replace(")", "").split(",")):
                candidate = f"{base}.{name.split(' as ')[0].strip()}" if name else base
                modules.add(candidate if name[:1].islower() else base)

    found: dict[str, set[str]] = {}
    for module in modules:
        path = REPO / "backend" / (module.replace(".", "/") + ".py")
        if not path.exists():
            path = REPO / "backend" / ("/".join(module.split(".")[:-1]) + ".py")
        if not path.exists():
            continue
        for name in set(re.findall(r"\bsettings\.([A-Z][A-Z0-9_]+)", path.read_text())):
            found.setdefault(name, set()).add(path.name)
    return found


def service_environment(document: dict, service: str = "gateway") -> set[str]:
    environment = document["services"][service].get("environment") or {}
    if isinstance(environment, list):
        return {item.split("=", 1)[0] for item in environment}
    return set(environment)


def env_file_entries(document: dict, service: str = "gateway") -> list[dict]:
    """The env_file entries the service lists, as written, in either
    spelling compose accepts — a bare string, a list of strings, or a
    list of mappings with `path:` (the long form, which is what
    `required: false` needs) — each normalised to a mapping."""
    entry = document["services"][service].get("env_file")
    if entry is None:
        return []
    if isinstance(entry, str):
        return [{"path": entry}]
    return [{"path": item} if isinstance(item, str) else dict(item) for item in entry]


# A compose path may be an interpolation. Since K3 the provider file's
# entry reads `${LIBRERUN_GATEWAY_ENV_FILE:-gateway.env}`, so a
# deployment that keeps no plaintext gateway.env on disk can name the
# temporary file `sops exec-file` decrypts into (docs/platform/Install.md,
# "Encrypting .env at rest"). The committed example lives beside the
# DEFAULT, which is the path every deployment that never sets the
# variable reads — so the default is what this guard resolves. A
# reference with no default names a file only the environment knows,
# which the tree cannot read: it resolves to nothing, and a setting
# delivered by a file nobody can see counts as not delivered.
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}")


def compose_default(path: str) -> str:
    """The path compose reads when the environment sets nothing: every
    `${VAR:-default}` / `${VAR-default}` replaced by its default. A
    reference without one (`${VAR}`, `$VAR`) resolves the whole path to
    the empty string."""
    missing = False

    def substitute(match: re.Match) -> str:
        nonlocal missing
        if match.group(2) is None:
            missing = True
            return ""
        return match.group(2)

    resolved = _INTERPOLATION.sub(substitute, path)
    return "" if missing or "$" in resolved else resolved


def env_file_paths(document: dict, service: str = "gateway") -> list[str]:
    """The env_file paths the service reads when the environment sets
    nothing — interpolations resolved to their defaults (K3), and an
    entry with no default dropped rather than guessed."""
    resolved = (compose_default(entry.get("path", "")) for entry in env_file_entries(document, service))
    return [path for path in resolved if path]


# A name declared in `<env_file>.example` — assignment or commented
# assignment alike, because the example's job is to SAY where the value
# goes, and whether a line ships commented out is cosmetic.
_DECLARATION = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=")


def example_declarations(path: str) -> set[str]:
    """The variable names an env_file's committed example declares.

    `agent-keys.env` has no example on purpose — compose.sh derives it
    from .env before every command, one line per installed agent, so
    there is no fixed set of names to commit. It contributes nothing
    here, which is right: it delivers agent keys, not settings.
    """
    example = REPO / f"{path}.example"
    if not example.exists():
        return set()
    return {
        match.group(1)
        for line in example.read_text().splitlines()
        if (match := _DECLARATION.match(line))
    }


def delivered_settings(document: dict, service: str = "gateway") -> set[str]:
    """Everything the deployment actually hands the service: its
    `environment:` block plus every name its env_file examples declare."""
    delivered = service_environment(document, service)
    for path in env_file_paths(document, service):
        delivered |= example_declarations(path)
    return delivered


def compose_document() -> dict:
    return yaml.safe_load((REPO / "compose.yaml").read_text())


def gateway_environment() -> set[str]:
    return delivered_settings(compose_document())


@pytest.mark.parametrize("name", DEPLOYMENT_SETTINGS)
def test_the_gateway_is_given_the_setting(name):
    assert name in gateway_environment(), (
        f"{name} is read by the gateway but compose does not pass it, so "
        f"setting it in .env does nothing"
    )


@pytest.mark.parametrize("name", DEPLOYMENT_SETTINGS)
def test_the_list_names_only_real_settings(name):
    """A list that drifted off the model would stop guarding without
    ever failing."""
    from gateway.config import GatewaySettings

    assert name in GatewaySettings.model_fields


def test_each_is_overridable_from_the_environment():
    """Passed, but hardcoded, is the same failure with an extra step.

    Only for the settings delivered through `environment:`; the ones
    delivered through an env_file are an operator's file by
    construction, and there is nothing in compose to hardcode.
    """
    text = (REPO / "compose.yaml").read_text()
    gateway_block = text.split("\n  gateway:\n", 1)[1].split("\n  vector:", 1)[0]
    in_environment = service_environment(compose_document())

    checked = []
    for name in DEPLOYMENT_SETTINGS:
        if name not in in_environment:
            continue
        for line in gateway_block.splitlines():
            if line.strip().startswith(f"{name}:"):
                assert "${" in line, f"{name} is hardcoded in compose: {line.strip()}"
                checked.append(name)
                break

    # …and the loop actually looked at something. Before K1 every name
    # was in the block, so a `continue` that swallowed the whole list
    # would have turned this test green by skipping it.
    assert checked, "no DEPLOYMENT_SETTINGS reached the hardcoding check"


# --------------------------------------------------------------------------
# The other delivery: an env_file and its committed example (K1)
# --------------------------------------------------------------------------

# The three provider keys, and since K7 the gateway's store key, which
# travels the same way for the same reason (D33): gateway.env or its
# _FILE, never an `environment:` line that would blank it.
PROVIDER_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_AI_API_KEY",
    "LIBRERUN_GATEWAY_SECRETS_KEY",
)


def _provider_env_file_entry(document: dict) -> dict:
    return next(
        entry
        for entry in env_file_entries(document)
        if compose_default(entry.get("path", "")) == "gateway.env"
    )


def test_the_gateway_lists_the_provider_env_file():
    """`required: false`, because the keyless demo has no such file and
    must still boot — a `required: true` here would make D11's zero-key
    demo fail to start."""
    document = compose_document()
    assert "gateway.env" in env_file_paths(document)

    entry = _provider_env_file_entry(document)
    assert entry.get("required") is False, (
        "gateway.env must be optional: the keyless demo ships without one"
    )


def test_the_provider_env_file_can_be_named_from_the_environment():
    """K3: the entry's path is `${LIBRERUN_GATEWAY_ENV_FILE:-gateway.env}`,
    so a deployment that keeps no plaintext gateway.env on disk can point
    the gateway at the file `sops exec-file` decrypts into, and one that
    sets nothing reads the file beside compose.yaml as before. Pinned by
    the variable's name, because docs/platform/Install.md tells the operator that
    name and a renamed variable would leave the recipe pointing at
    nothing."""
    entry = _provider_env_file_entry(compose_document())

    assert entry["path"] == "${LIBRERUN_GATEWAY_ENV_FILE:-gateway.env}"
    assert entry.get("required") is False, (
        "the named file is optional too: `required` cannot depend on whether "
        "the variable is set, and a keyless deployment names none"
    )


def test_the_interpolation_reader_takes_the_default_and_only_the_default():
    assert compose_default("gateway.env") == "gateway.env"
    assert compose_default("${LIBRERUN_GATEWAY_ENV_FILE:-gateway.env}") == "gateway.env"
    assert compose_default("${SOME_DIR-conf}/keys.env") == "conf/keys.env"
    assert compose_default("./${A:-x}/${B:-y}.env") == "./x/y.env"
    # No default means no path the tree can read — not "gateway.env".
    assert compose_default("${LIBRERUN_GATEWAY_ENV_FILE}") == ""
    assert compose_default("$LIBRERUN_GATEWAY_ENV_FILE") == ""


def test_a_path_only_the_environment_knows_delivers_nothing():
    """The negative half of the resolver. Point the provider entry at a
    variable with no default and the three keys must stop counting as
    delivered: the tree cannot read a file whose name it does not know,
    and a guard that fell back to `gateway.env` anyway would pass on a
    compose file that, unset, delivers nothing."""
    document = compose_document()
    assert set(PROVIDER_KEYS) <= delivered_settings(document)

    for entry in document["services"]["gateway"]["env_file"]:
        if compose_default(entry["path"]) == "gateway.env":
            entry["path"] = "${LIBRERUN_GATEWAY_ENV_FILE}"

    assert delivered_settings(document) & set(PROVIDER_KEYS) == set()


@pytest.mark.parametrize("name", PROVIDER_KEYS)
def test_the_example_declares_the_provider_key(name):
    """The example is what makes the delivery legible — to an operator
    and to this guard. Without it the keys would be 'delivered' by a
    file nobody documents and nothing checks."""
    assert name in example_declarations("gateway.env")


@pytest.mark.parametrize("name", PROVIDER_KEYS)
def test_the_provider_key_is_not_also_in_the_environment_block(name):
    """The reason the three lines were REMOVED rather than left beside
    the env_file (K1): an `environment:` entry overrides an env_file
    entry even when it interpolates to blank, so `${OPENAI_API_KEY:-}`
    with nothing in .env would blank the key gateway.env just
    delivered. Present in both places is worse than present in one."""
    assert name not in service_environment(compose_document()), (
        f"{name} is back in the gateway's environment block; it would "
        f"override gateway.env with a blank on every deployment that "
        f"does not also set it in .env"
    )


def test_the_example_declares_nothing_the_gateway_does_not_read():
    """gateway.env is the gateway's file, not a second .env. Every name
    in it is a field of this service's settings model (or the `_FILE`
    spelling of one, which K2 adds)."""
    from gateway.config import GatewaySettings

    fields = set(GatewaySettings.model_fields)
    strays = [
        name
        for name in example_declarations("gateway.env")
        if name not in fields and name.removesuffix("_FILE") not in fields
    ]
    assert strays == [], f"gateway.env.example declares {strays}, which the gateway never reads"


def test_the_example_reader_reads_only_what_is_there():
    """A reader that returned everything would make every setting look
    delivered — the exact shape of a gate that passes by not looking."""
    declared = example_declarations("gateway.env")

    assert declared, "gateway.env.example declares nothing — the reader is broken"
    assert "APP_SECRET_KEY" not in declared
    # A path with no committed example contributes nothing rather than
    # silently matching: agent-keys.env is derived, not committed.
    assert example_declarations("agent-keys.env") == set()


def test_the_env_file_half_bites_when_the_delivery_is_removed():
    """The negative test for the delivery the guard just learned. Take
    the env_file away and the three keys must stop counting as
    delivered — otherwise `env_file` became a word that makes any
    setting pass."""
    document = compose_document()
    assert set(PROVIDER_KEYS) <= delivered_settings(document)

    document["services"]["gateway"].pop("env_file")
    still_delivered = delivered_settings(document) & set(PROVIDER_KEYS)

    assert still_delivered == set(), (
        f"{sorted(still_delivered)} still counted as delivered with no "
        f"env_file at all — the guard is not reading the delivery"
    )


def test_an_example_that_stops_naming_the_key_bites_too():
    """The other half: the env_file entry stays, its example stops
    declaring the key. Delivery must follow the declaration, not the
    filename."""
    document = compose_document()
    document["services"]["gateway"]["env_file"] = [
        {"path": "no-such-secrets-file.env", "required": False}
    ]

    assert delivered_settings(document) & set(PROVIDER_KEYS) == set()


# --------------------------------------------------------------------------
# The settings the gateway borrows from the backend
# --------------------------------------------------------------------------


def test_the_scan_finds_the_borrowed_settings():
    """A derived list that derives nothing proves nothing."""
    borrowed = borrowed_backend_settings()
    assert borrowed, "no backend settings found — the scan is looking in the wrong place"
    assert "PII_PHONE_REGION" in borrowed, (
        "the scan no longer sees the setting that caused this guard to exist"
    )


@pytest.mark.parametrize("name", sorted(borrowed_backend_settings()))
def test_every_borrowed_backend_setting_reaches_the_gateway(name):
    """The gateway runs the backend's walker, so it must run it with the
    backend's configuration. A setting delivered to one process and not
    the other means two redaction strengths in one deployment — and the
    weaker one is the process that talks to providers."""
    sources = sorted(borrowed_backend_settings()[name])
    assert name in gateway_environment(), (
        f"{name} is read by {sources} inside the gateway container, but "
        f"compose does not pass it — so the gateway uses the default while "
        f"the backend uses the operator's value"
    )


@pytest.mark.parametrize("name", sorted(borrowed_backend_settings()))
def test_the_two_services_are_given_the_same_value(name):
    """Not merely present: the SAME expression. Two services reading one
    redaction knob from different variables would be a subtler version of
    the same bug."""
    document = yaml.safe_load((REPO / "compose.yaml").read_text())

    def value(service):
        environment = document["services"][service].get("environment") or {}
        if isinstance(environment, list):
            return dict(item.split("=", 1) for item in environment if "=" in item).get(name)
        return environment.get(name)

    backend, gateway = value("backend"), value("gateway")
    if backend is None:
        pytest.skip(f"{name} is not set for the backend either")
    assert gateway == backend, (
        f"{name} is {gateway!r} for the gateway and {backend!r} for the "
        f"backend — one deployment, two answers"
    )


# --------------------------------------------------------------------------
# The third delivery: the dotenv the gateway reads directly (K1)
# --------------------------------------------------------------------------
#
# `uvicorn gateway.main:app` is a documented way to run this service, and
# there compose delivers nothing — the settings model's own dotenv
# sources are the whole delivery. They are checked here for the same
# reason the compose block is: a source that points at the wrong place is
# a setting that silently does not exist.


def test_the_dotenv_sources_are_anchored_to_the_repository_root():
    from gateway.config import ENV_FILES

    anchored = {str(source) for source in ENV_FILES}

    assert str(REPO / ".env") in anchored
    assert str(REPO / "gateway.env") in anchored, (
        "the gateway does not read gateway.env from the repository root, so "
        "a local uvicorn run has no provider credential"
    )


def test_the_image_layout_does_not_raise():
    """The image flattens the tree to /app/gateway/config.py, where three
    levels up does not exist. Arithmetic that assumed the checkout layout
    would raise IndexError at import — inside the container only, which
    no test of the checkout would ever see."""
    from gateway.config import repository_root

    assert repository_root(pathlib.Path("/app/gateway/config.py")) == pathlib.Path("/")


def _two_files(tmp_path):
    """A pre-K1 upgrade, exactly: the blank provider line left behind in
    .env, the real key in gateway.env."""
    (tmp_path / ".env").write_text("OPENAI_API_KEY=\nLIBRERUN_KB_EMBED_MODEL=openai/from-dotenv\n")
    (tmp_path / "gateway.env").write_text("OPENAI_API_KEY=sk-from-the-gateways-own-file\n")
    return tmp_path / ".env", tmp_path / "gateway.env"


def test_gateway_env_outranks_the_root_dotenv(tmp_path, monkeypatch):
    """The direction is load-bearing: a blank that outranks a real key is
    the same failure that took the three interpolated lines out of
    compose, one layer down."""
    from gateway.config import GatewaySettings

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    dotenv, gateway_env = _two_files(tmp_path)

    settings = GatewaySettings(_env_file=(dotenv, gateway_env))

    # ``SecretStr`` since K2 — the precedence this asserts is unchanged,
    # the field is just masked now and revealed at its use site.
    assert (
        settings.OPENAI_API_KEY.get_secret_value()
        == "sk-from-the-gateways-own-file"
    )
    # …and .env is still read for everything else, which is why it is a
    # source at all.
    assert settings.LIBRERUN_KB_EMBED_MODEL == "openai/from-dotenv"


def test_the_reverse_order_really_would_lose_the_key(tmp_path, monkeypatch):
    """The negative half. Without this the test above could pass because
    pydantic ignores blanks — in which case the order would not matter and
    the comment explaining it would be fiction."""
    from gateway.config import GatewaySettings

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    dotenv, gateway_env = _two_files(tmp_path)

    settings = GatewaySettings(_env_file=(gateway_env, dotenv))

    assert settings.OPENAI_API_KEY.get_secret_value() == "", (
        "a blank line in .env no longer overrides gateway.env — if that is "
        "now true of pydantic-settings, the ordering comment in config.py "
        "needs rewriting rather than trusting"
    )


def test_the_declared_order_is_the_one_that_wins(tmp_path, monkeypatch):
    """Not the two orders in the abstract: the order `ENV_FILES` actually
    declares, filtered to the two filenames, must be the winning one."""
    from gateway.config import ENV_FILES, GATEWAY_ENV_FILENAME, GatewaySettings

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    dotenv, gateway_env = _two_files(tmp_path)
    substitute = {".env": dotenv, GATEWAY_ENV_FILENAME: gateway_env}
    sources = tuple(substitute[pathlib.Path(source).name] for source in ENV_FILES)

    assert (
        GatewaySettings(_env_file=sources).OPENAI_API_KEY.get_secret_value()
        == "sk-from-the-gateways-own-file"
    )
