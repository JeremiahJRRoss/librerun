"""Gate P — no process receives a secret it does not read (K1, L28).

The partition is the security property the gateway exists for. L23 put
the provider credentials in one process; L28 states the general rule
underneath it: *no process receives a secret it does not read*. A
credential delivered to a process that never reads it is pure attack
surface — it is in that container's environment, in `docker inspect`, in
a core dump, and readable by every piece of code the process runs. For
the backend that set includes every in-process agent, which is why the
gateway is a separate service at all.

The check has two halves and neither is a list of what we believe:

* **received** is derived from `compose.yaml`, `agents.compose.yaml` and
  the committed example beside every `env_file` a service names — so a
  key delivered by a file counts exactly as much as one interpolated
  into `environment:`, which is what makes K1's move from the second to
  the first provable rather than merely invisible.
* **read** is derived from the two settings models for the two Python
  processes, plus the backend modules the gateway imports and reads
  `settings.X` from, plus a short hand-written registry for the readers
  that are not Python at all — Postgres, Vector, the OTLP bridge, the
  frontend build, an agent container.

`received - read` must be empty for every service. `backend/tests/
test_no_provider_key_in_backend.py` stays as the backend's other half:
this file proves the key is not *delivered*, that one proves the
process would not *bind* it if it were.

The registry is the weak point — a hand-maintained set always is — so
two things guard it: every service in the merged compose file must
appear in it (a new service cannot slip through unexamined), and no
entry may claim a name the service is not given (an over-broad entry
that silences a real surplus fails on its own).
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]

# The gateway's settings model is a sibling service, not an installed
# package, so its directory goes on the path the way `conftest.py` puts
# the agent SDK there.
#
# APPENDED, not inserted at the front, and that is not style. That
# directory also holds `tests/` and `stub/`, and ten modules of this
# suite do `from tests.X import ...`. At the front, its `tests/` would be
# scanned before `backend/tests/` — today the backend's wins anyway,
# because a regular package (it has `__init__.py`) beats a namespace
# portion wherever it sits; the day somebody adds an `__init__.py` to
# `services/gateway/tests/` that stops being true and this suite starts
# importing another service's test modules, in an order that depends on
# which file pytest collected first. Appending costs nothing: `gateway`
# is not a name anything else on the path answers to.
_GATEWAY_SRC = REPO / "services" / "gateway"
if str(_GATEWAY_SRC) not in sys.path:
    sys.path.append(str(_GATEWAY_SRC))

COMPOSE_FILES = ("compose.yaml", "agents.compose.yaml")


# --------------------------------------------------------------------------
# What counts as a secret
# --------------------------------------------------------------------------
#
# By NAME rather than by a list of the secrets we happen to have today:
# a list would have to be edited for every new credential, and the one
# nobody edits it for is the one this gate exists to catch. The suffixes
# are the ones the tree actually uses; `_FILE` is accepted on the end so
# K2's `<NAME>_FILE` spelling classifies with the value it points at.
_SECRET_SUFFIX = re.compile(r"(_KEY|_SECRET|_PASSWORD|_TOKEN|_CREDENTIALS?)(_FILE)?$")

# Names that carry a credential without saying so in their suffix: both
# of these embed a password in their userinfo in the standard compose
# deployment.
ALSO_SECRET = {"DATABASE_URL", "REDIS_URL"}

# One agent's gateway key, provisioned per agent id (D10). Every name
# under the prefix is a credential; none of them ends in `_KEY`.
AGENT_KEY_PREFIX = "LIBRERUN_AGENT_KEY_"


def is_secret(name: str) -> bool:
    return bool(
        _SECRET_SUFFIX.search(name)
        or name in ALSO_SECRET
        or name.startswith(AGENT_KEY_PREFIX)
    )


# --------------------------------------------------------------------------
# What each service RECEIVES
# --------------------------------------------------------------------------


def compose_documents() -> list[dict]:
    return [yaml.safe_load((REPO / name).read_text()) for name in COMPOSE_FILES]


def _environment_names(service: dict) -> set[str]:
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        return {item.split("=", 1)[0] for item in environment}
    return set(environment)


# A compose path may be an interpolation: since K3 the gateway's provider
# file is listed as `${LIBRERUN_GATEWAY_ENV_FILE:-gateway.env}`, so a
# deployment with no plaintext gateway.env on disk can name the file
# `sops exec-file` decrypts into. The committed example lives beside the
# DEFAULT, which is what every deployment that sets nothing reads, so the
# default is what this gate resolves; a reference with no default names a
# file the tree cannot read and contributes nothing — the gate never
# guesses a delivery it cannot see.
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}")


def _compose_default(path: str) -> str:
    missing = False

    def substitute(match: re.Match) -> str:
        nonlocal missing
        if match.group(2) is None:
            missing = True
            return ""
        return match.group(2)

    resolved = _INTERPOLATION.sub(substitute, path)
    return "" if missing or "$" in resolved else resolved


def _env_file_paths(service: dict) -> list[str]:
    entry = service.get("env_file")
    if entry is None:
        return []
    if isinstance(entry, str):
        entry = [entry]
    paths = [item if isinstance(item, str) else item.get("path", "") for item in entry]
    return [path for path in (_compose_default(raw) for raw in paths) if path]


_DECLARATION = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=")


def example_declarations(path: str) -> set[str]:
    """The names the committed example beside an `env_file` declares.

    Commented declarations count: the example's job is to say where a
    value goes, and shipping the line commented out is cosmetic. A path
    with no committed example (`agent-keys.env`, derived by compose.sh
    from .env before every command) contributes nothing here — see
    `test_the_derived_agent_key_file_is_accounted_for`.
    """
    example = REPO / f"{path}.example"
    if not example.exists():
        return set()
    return {
        match.group(1)
        for line in example.read_text().splitlines()
        if (match := _DECLARATION.match(line))
    }


def received(documents: list[dict] | None = None) -> dict[str, set[str]]:
    """`{service: {NAME, ...}}` — every variable the deployment hands
    each service, from both delivery mechanisms."""
    documents = compose_documents() if documents is None else documents
    out: dict[str, set[str]] = {}
    for document in documents:
        for name, service in (document.get("services") or {}).items():
            names = _environment_names(service)
            for path in _env_file_paths(service):
                names |= example_declarations(path)
            out.setdefault(name, set()).update(names)
    return out


def received_secrets(documents: list[dict] | None = None) -> dict[str, set[str]]:
    return {
        service: {name for name in names if is_secret(name)}
        for service, names in received(documents).items()
    }


# --------------------------------------------------------------------------
# What each process READS
# --------------------------------------------------------------------------


def backend_reads() -> set[str]:
    """The backend's settings model, and the tool secrets its in-process
    agents declare (K8a): the backend is the process a `python-package`
    agent runs in, and it reads `X_Y` from its own environment as a
    declared `x_y`'s fallback. A name no such agent declares, delivered to
    the backend, stays a surplus — by name."""
    from app import config as app_config

    return set(app_config.Settings.model_fields) | in_process_tool_secrets()


def in_process_tool_secrets() -> set[str]:
    """The upper-cased `secrets[]` of every in-process manifest under
    `backend/agents` and its `_examples`: what the fallback may read."""
    from app.agents.manifest import load_manifest

    root = REPO / "backend" / "agents"
    names: set[str] = set()
    for path in sorted([*root.glob("*/agent.yaml"), *root.glob("_examples/*/agent.yaml")]):
        manifest = load_manifest(path.parent)
        if manifest.runtime != "container":
            names.update(name.upper() for name in manifest.secrets)
    return names


def gateway_reads() -> set[str]:
    from gateway.config import GatewaySettings

    return set(GatewaySettings.model_fields) | _borrowed_backend_settings()


def _borrowed_backend_settings() -> set[str]:
    """Backend settings the gateway reads through the modules it imports
    (`app.services.pii_service` and friends). The gateway's own guard
    derives the same set to prove they are DELIVERED; here it is needed
    so those deliveries do not read as a surplus.

    Deliberately a second copy rather than an import of that module: the
    two suites run in different CI jobs, and reaching into
    `services/gateway/tests/` would pull another service's test module
    into this one's import namespace for the sake of twenty lines. The
    two copies cannot drift apart silently — if they did, one of the two
    guards would go red, which is what a duplicated derivation is
    allowed to cost.
    """
    gateway_src = REPO / "services" / "gateway" / "gateway"
    modules: set[str] = set()
    for source in gateway_src.glob("*.py"):
        for match in re.finditer(r"from (app[\w.]*) import ([^\n]+)", source.read_text()):
            base, names = match.group(1), match.group(2)
            for name in (n.strip() for n in names.replace("(", "").replace(")", "").split(",")):
                candidate = f"{base}.{name.split(' as ')[0].strip()}" if name else base
                modules.add(candidate if name[:1].islower() else base)

    found: set[str] = set()
    for module in modules:
        path = REPO / "backend" / (module.replace(".", "/") + ".py")
        if not path.exists():
            path = REPO / "backend" / ("/".join(module.split(".")[:-1]) + ".py")
        if not path.exists():
            continue
        found |= set(re.findall(r"\bsettings\.([A-Z][A-Z0-9_]+)", path.read_text()))
    return found


# The readers that are not a Python settings model. Hand-written because
# there is nothing to derive them from — each is a third-party image or
# a build step reading a documented variable — and deliberately narrow:
# an entry here is a claim that THIS process reads THAT name, and
# `test_no_registry_entry_claims_a_variable_the_service_is_not_given`
# fails on a claim that is not backed by a delivery.
NON_PYTHON_READERS: dict[str, set[str]] = {
    # The official Postgres image's own initialisation variables.
    "postgres": {"POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"},
    # Redis takes no configuration from the environment here.
    "redis": set(),
    # config/vector.yaml and the overlays beside it interpolate these
    # into their sinks. The second group is S7a's LOG leg, delivered by
    # observability.env — the trace leg's credentials are the bridge's
    # and are deliberately not here.
    "vector": {
        "AWS_REGION",
        "CLICKHOUSE_ENDPOINT",
        "CRIBL_HEC_ENDPOINT",
        "CRIBL_HEC_TOKEN",
        "DATADOG_API_KEY",
        "LOKI_ENDPOINT",
        "VECTOR_CRIBL",
        "VECTOR_JAEGER_ENDPOINT",
        "VECTOR_OTLP_FORWARD_ENDPOINT",
        "VECTOR_S3_BUCKET",
        "VECTOR_VIEWER",
        # S7a — the vendor overlays' log leg (config/vector-datadog.yaml,
        # -elastic, -splunk) and the shaping file's service fallback.
        "DATADOG_LOGS_ENDPOINT",
        "ELASTIC_API_KEY",
        "ELASTIC_AUTH_SCHEME",
        "ELASTIC_LOGS_DATASET",
        "ELASTIC_LOGS_NAMESPACE",
        "ELASTIC_URL",
        "LIBRERUN_OBS_SERVICE_NAME",
        "LIBRERUN_OBS_VENDOR",
        "SPLUNK_HEC_TOKEN",
        "SPLUNK_HEC_URL",
        "SPLUNK_INDEX",
        "SPLUNK_SOURCETYPE",
    },
    # The OTLP bridge forwards to Cribl with its own token, and from S7a
    # to one vendor's OTLP intake with that vendor's — delivered by
    # observability-traces.env, which `vector` does not load. Elastic's
    # APM key is a separate name from the cluster's ELASTIC_API_KEY for
    # exactly that reason: one credential per reader.
    "otel-bridge": {
        "CRIBL_OTLP_ENDPOINT",
        "CRIBL_OTLP_TOKEN",
        "DD_AGENT_OTLP_URL",
        "ELASTIC_APM_API_KEY",
        "ELASTIC_APM_AUTH_SCHEME",
        "ELASTIC_APM_OTLP_URL",
        "SPLUNK_ACCESS_TOKEN",
        "SPLUNK_OTLP_URL",
    },
    "jaeger": {"COLLECTOR_OTLP_ENABLED"},
    # Next.js: one baked in at build time, one read at runtime.
    "frontend": {"NEXT_PUBLIC_API_URL", "BACKEND_INTERNAL_URL"},
    # The HTTPS edge (K blueprint T1): config/Caddyfile substitutes these
    # two before it parses — the certificate's source and the site's name.
    # Neither is a secret; its certificate key is a file, never a variable.
    # LIBRERUN_TLS_CA (T2) is config/edge-start.sh's: the paths of a CA's
    # two files under /certs, which it names in the pki block at start —
    # paths again, never a key.
    "edge": {"LIBRERUN_TLS", "LIBRERUN_TLS_CA", "LIBRERUN_TLS_DOMAIN"},
    # The edge's control (T2, D44 refined): the backend's image, but not the
    # backend's settings model — `python -m app.edge_control` reads the one
    # address it takes a change from, and is given no secret at all: no
    # database URL, no store key, no agent.
    "edge-control": {"EDGE_ADDRESS"},
}

# An agent container reads the same four names whatever the agent is:
# the gateway's URL in both spellings, the relay endpoint, and its own
# gateway key — which is delivered under the name OPENAI_API_KEY because
# a framework that only knows how to talk to OpenAI reads that name. The
# VALUE is an agent key and never a provider key, which
# `test_an_agent_containers_openai_key_is_an_agent_key` pins.
AGENT_CONTAINER_READS = {
    "LIBRERUN_GATEWAY_URL",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_SERVICE_NAME",
}


def agent_services() -> set[str]:
    """Services declared in the agent fragment, whatever they are called
    — derived, so an agent added there is covered without an edit."""
    document = yaml.safe_load((REPO / "agents.compose.yaml").read_text())
    return set(document.get("services") or {})


def reads() -> dict[str, set[str]]:
    out = dict(NON_PYTHON_READERS)
    out["backend"] = backend_reads()
    out["gateway"] = gateway_reads()
    for name in agent_services():
        out[name] = AGENT_CONTAINER_READS
    return out


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def surplus_secrets(documents: list[dict] | None = None) -> dict[str, set[str]]:
    """`{service: {NAME, ...}}` for every secret a service is given and
    does not read. Empty is the only acceptable answer."""
    read = reads()
    out: dict[str, set[str]] = {}
    for service, secrets in received_secrets(documents).items():
        extra = {
            name
            for name in secrets
            if name not in read.get(service, set())
            # An agent container's keys are named per agent id, so the
            # registry matches them by prefix rather than by name.
            and not (service in agent_services() and name.startswith(AGENT_KEY_PREFIX))
            # The gateway reads every agent key there is (config.py's
            # agent_key_variables), not a fixed set of names.
            and not (service == "gateway" and name.startswith(AGENT_KEY_PREFIX))
        }
        if extra:
            out[service] = extra
    return out


def test_no_service_receives_a_secret_it_does_not_read():
    """Gate P itself (K blueprint §3.3)."""
    surplus = surplus_secrets()

    assert surplus == {}, (
        "these services are handed a secret they never read — every one is "
        "a credential sitting in a container's environment for nothing:\n"
        + "\n".join(f"  {service}: {sorted(names)}" for service, names in surplus.items())
    )


@pytest.mark.parametrize("name", ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_AI_API_KEY"))
def test_only_the_gateway_receives_a_provider_key(name):
    """L23 and L28's headline, stated as its own assertion so a
    regression names the provider key rather than the general rule.

    An agent container is handed a variable SPELLED `OPENAI_API_KEY`
    whose value is that agent's gateway key; it is excluded by name here
    and checked for its value below."""
    holders = {
        service
        for service, names in received().items()
        if name in names and service not in agent_services()
    }

    assert holders == {"gateway"}, f"{name} reaches {sorted(holders)}; it may reach the gateway alone"


def test_the_backend_receives_no_provider_key():
    """The half that K1 moved, called out on its own: this is what
    `docker inspect librerun-backend` shows."""
    from app.config import PROVIDER_KEY_VARIABLES

    assert set(PROVIDER_KEY_VARIABLES) & received()["backend"] == set()


def test_only_the_backend_gets_its_store_key():
    """K6 (D33, L28): the secrets store's key reaches the process that
    seals and opens the backend's rows, and no other — the gateway's rows
    are sealed under the gateway's own key (K7), never this one. And the
    backend reads it, so the delivery is no surplus."""
    name = "LIBRERUN_BACKEND_SECRETS_KEY"
    holders = {service for service, names in received().items() if name in names}

    assert holders == {"backend"}, f"{name} reaches {sorted(holders)}; it may reach the backend alone"
    assert name in backend_reads() and f"{name}_FILE" in backend_reads()
    assert name not in gateway_reads()

    # The probe: handed to the gateway, it is a surplus Gate P names.
    documents = compose_documents()
    documents[0]["services"]["gateway"]["environment"][name] = "${" + name + ":-}"
    assert name in surplus_secrets(documents).get("gateway", set())


def test_only_the_gateway_gets_its_store_key():
    """K7 (D33, L28): the gateway's store key reaches the gateway alone,
    through gateway.env — its env_file, never an `environment:` line, which
    would blank the value the file delivers (the gateway's compose guard
    holds that half). The gateway reads it; the backend neither receives
    nor reads it, since a backend holding it could open the sealing key."""
    name = "LIBRERUN_GATEWAY_SECRETS_KEY"
    holders = {service for service, names in received().items() if name in names}

    assert holders == {"gateway"}, f"{name} reaches {sorted(holders)}; it may reach the gateway alone"
    assert name in gateway_reads() and f"{name}_FILE" in gateway_reads()
    assert name not in backend_reads()


def test_gate_p_bites_on_the_store_key():
    """The negative probe for K7's slot: the gateway's store key
    interpolated into the backend's block is a surplus Gate P names."""
    documents = _doctored(LIBRERUN_GATEWAY_SECRETS_KEY="${LIBRERUN_GATEWAY_SECRETS_KEY:-}")

    assert surplus_secrets(documents).get("backend") == {"LIBRERUN_GATEWAY_SECRETS_KEY"}


def test_the_root_env_example_declares_no_provider_key():
    """L28's goal in one line: the root `.env` carries no provider key.
    The example is what an operator copies, so it is what decides."""
    from app.config import PROVIDER_KEY_VARIABLES

    declared = {
        match.group(1)
        for line in (REPO / ".env.example").read_text().splitlines()
        if (match := re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=", line))
    }

    assert set(PROVIDER_KEY_VARIABLES) & declared == set(), (
        "a provider key is back in .env.example; it belongs in gateway.env.example"
    )


def test_the_backend_never_names_the_gateways_env_file():
    """The gateway's dotenv gained `gateway.env` in K1. The backend's
    must not: it is the one process that must never bind a provider
    credential, and a shared source would be exactly how it would."""
    from app import config as app_config

    sources = app_config.Settings.model_config.get("env_file")
    sources = sources if isinstance(sources, (tuple, list)) else (sources,)

    assert all("gateway.env" not in str(source) for source in sources)

    backend_src = REPO / "backend" / "app"
    offenders = [
        str(path.relative_to(REPO))
        for path in backend_src.rglob("*.py")
        if "gateway.env" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"the backend names gateway.env in {offenders}"


def test_an_agent_containers_openai_key_is_an_agent_key():
    """The one place `OPENAI_API_KEY` legitimately appears outside the
    gateway is an agent container, where it holds a LibreRun agent key.
    The value must interpolate from `LIBRERUN_AGENT_KEY_*` — a fragment
    that passed the provider variable of the same name through would
    read identically and hand a provider credential to an agent."""
    document = yaml.safe_load((REPO / "agents.compose.yaml").read_text())

    checked = 0
    for name, service in (document.get("services") or {}).items():
        environment = service.get("environment") or {}
        value = environment.get("OPENAI_API_KEY") if isinstance(environment, dict) else None
        if value is None:
            continue
        checked += 1
        assert AGENT_KEY_PREFIX in str(value), (
            f"{name} takes OPENAI_API_KEY from {value!r} — an agent container "
            f"is given its own gateway key, never a provider credential"
        )
    assert checked, "no agent container declares OPENAI_API_KEY — the check looked at nothing"


# --------------------------------------------------------------------------
# …and the gate is looking at something
# --------------------------------------------------------------------------


def test_every_compose_service_is_in_the_read_registry():
    """A service the registry does not know would have an empty read set
    and pass by accident only while it holds no secret. Unknown means
    unexamined, so it fails here instead."""
    unknown = set(received()) - set(reads())

    assert unknown == set(), (
        f"{sorted(unknown)} is in compose but not in the reader registry — "
        f"add what it reads (or an empty set, deliberately)"
    )


def test_the_derivation_found_the_secrets_it_should_have():
    """A derivation that derived nothing would make every service clean.
    These four are known-present deliveries across both mechanisms and
    both compose files."""
    secrets = received_secrets()

    assert "APP_SECRET_KEY" in secrets["backend"]
    assert "LIBRERUN_BACKEND_SECRETS_KEY" in secrets["backend"]
    assert "OPENAI_API_KEY" in secrets["gateway"], "the env_file example was not read"
    assert "POSTGRES_PASSWORD" in secrets["postgres"]
    assert any("OPENAI_API_KEY" in secrets[name] for name in agent_services())


def test_the_read_sets_are_not_empty():
    """The other direction: an empty read set makes every delivery a
    surplus, which would be loud — but an import that silently fell back
    to `set()` would make the gate's subject vanish."""
    read = reads()

    assert "APP_SECRET_KEY" in read["backend"]
    assert "OPENAI_API_KEY" in read["gateway"]
    assert "PII_PHONE_REGION" in read["gateway"], (
        "the borrowed-settings scan found nothing — the gateway's backend "
        "reads would all read as a surplus"
    )


def test_no_registry_entry_claims_a_variable_the_service_is_not_given():
    """An over-broad registry entry is how this gate would go quiet: add
    a name nobody delivers and the corresponding surplus is silenced
    forever. Only the hand-written half is checked — a settings model
    legitimately declares fields a deployment leaves unset."""
    given = received()

    overclaimed = {
        service: sorted(names - given.get(service, set()))
        for service, names in NON_PYTHON_READERS.items()
        if names - given.get(service, set())
    }

    assert overclaimed == {}, (
        f"the registry claims variables that reach nothing: {overclaimed}"
    )


def test_the_derived_agent_key_file_is_accounted_for():
    """`agent-keys.env` has no committed example by design — compose.sh
    writes one line per installed agent before every command. The gate
    must therefore not depend on reading it, and the gateway must be the
    only service that lists it."""
    document = yaml.safe_load((REPO / "compose.yaml").read_text())
    listers = {
        name
        for name, service in (document.get("services") or {}).items()
        if "agent-keys.env" in _env_file_paths(service)
    }

    assert listers == {"gateway"}
    assert not (REPO / "agent-keys.env.example").exists()


# --------------------------------------------------------------------------
# The negative probe (K blueprint §3.3)
# --------------------------------------------------------------------------


def _doctored(**backend_environment: str) -> list[dict]:
    """The real compose files with extra entries in the backend's
    environment block — the violation this gate exists to catch."""
    documents = compose_documents()
    documents[0]["services"]["backend"]["environment"].update(backend_environment)
    return documents


def test_the_gate_bites_on_a_provider_key_in_the_backend():
    """The negative probe: `ANTHROPIC_API_KEY` interpolated into the
    backend block must fail the check BY NAME. On a clean tree a broken
    checker and a working one are indistinguishable."""
    documents = _doctored(ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}")

    surplus = surplus_secrets(documents)

    assert "backend" in surplus, "the gate did not notice a provider key in the backend"
    assert surplus["backend"] == {"ANTHROPIC_API_KEY"}


def test_the_gate_bites_on_an_undeclared_tool_secret():
    """K8a's probe: a key no in-process agent declares, delivered to the
    backend, is a surplus by name — the fallback reads only what an agent
    declared, so a `.env` line for anything else reaches a process that
    never reads it (L28). A declared one, the demo agent's, is read."""
    assert "TAVILY_API_KEY" in in_process_tool_secrets()
    assert "backend" not in surplus_secrets(_doctored(TAVILY_API_KEY="${TAVILY_API_KEY:-}"))

    surplus = surplus_secrets(_doctored(ACME_API_KEY="${ACME_API_KEY:-}"))

    assert surplus.get("backend") == {"ACME_API_KEY"}, surplus


def test_the_gate_bites_on_a_secret_delivered_by_env_file():
    """The same violation through the OTHER delivery, because K1 is the
    batch that made a file a delivery: point the backend at the
    gateway's env_file and the three keys must show up as a surplus."""
    documents = compose_documents()
    documents[0]["services"]["backend"]["env_file"] = [
        {"path": "gateway.env", "required": False}
    ]

    surplus = surplus_secrets(documents)

    # …and, since K7, the gateway's store key, which travels in the same
    # file for the same reason and is no more the backend's to read.
    assert surplus.get("backend") == {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_AI_API_KEY",
        "LIBRERUN_GATEWAY_SECRETS_KEY",
    }


def test_the_gate_bites_through_an_interpolated_env_file_path():
    """K3 made the gateway's env_file path a compose interpolation. The
    same violation spelled that way — the backend pointed at
    `${LIBRERUN_GATEWAY_ENV_FILE:-gateway.env}` — must still surface the
    three keys: a resolver that read the interpolation as no path at all
    would turn `${…}` into a word that hides any delivery."""
    documents = compose_documents()
    documents[0]["services"]["backend"]["env_file"] = [
        {"path": "${LIBRERUN_GATEWAY_ENV_FILE:-gateway.env}", "required": False}
    ]

    surplus = surplus_secrets(documents)

    # …and, since K7, the gateway's store key, which travels in the same
    # file for the same reason and is no more the backend's to read.
    assert surplus.get("backend") == {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_AI_API_KEY",
        "LIBRERUN_GATEWAY_SECRETS_KEY",
    }


def test_a_path_with_no_default_is_not_guessed():
    assert _compose_default("gateway.env") == "gateway.env"
    assert _compose_default("${LIBRERUN_GATEWAY_ENV_FILE:-gateway.env}") == "gateway.env"
    assert _compose_default("${LIBRERUN_GATEWAY_ENV_FILE}") == ""
    assert _compose_default("$LIBRERUN_GATEWAY_ENV_FILE") == ""


def test_the_gate_ignores_a_non_secret_surplus():
    """And it is a gate about SECRETS: a spare non-credential variable
    is untidy, not dangerous, and flagging it would train everyone to
    ignore the failure."""
    documents = _doctored(SOME_HARMLESS_FLAG="1")

    assert surplus_secrets(documents) == {}


@pytest.mark.parametrize(
    "name,secret",
    [
        ("OPENAI_API_KEY", True),
        ("APP_SECRET_KEY", True),
        ("POSTGRES_PASSWORD", True),
        ("CRIBL_HEC_TOKEN", True),
        ("DATABASE_URL", True),
        ("LIBRERUN_AGENT_KEY_ECHO_V1", True),
        ("OPENAI_API_KEY_FILE", True),
        ("OPENAI_BASE_URL", False),
        ("LIBRERUN_KB_EMBED_MODEL", False),
        ("PII_PHONE_REGION", False),
        ("VECTOR_S3_BUCKET", False),
    ],
)
def test_the_secret_classifier_is_the_one_we_think_it_is(name, secret):
    """The classifier decides what the gate looks at, so it is pinned in
    both directions: a suffix rule that stopped matching would make the
    gate green by looking at nothing."""
    assert is_secret(name) is secret
