"""The agents network and the example containers (blueprint S4, S5-R; gap G1).

The manifest's ``network.egress`` (default false, extra keys refused),
the agents listing carrying ``runtime`` and ``network`` for the admin
page, and the compose topology read from the files themselves: the
``agents`` network is internal, ``egress`` is not, the backend sits on
both its default network and ``agents``, Vector / Postgres / Redis do
not, and EVERY service in ``agents.compose.yaml`` — the three example
containers since S5-R — joins ``agents`` only, carries the agent-id
label, persists no logs, holds exactly one credential (its own gateway
key, by ``:?``), and, when it exports telemetry at all, points it at the
chassis relay with no credential; each manifest's ``network.egress``
matches its service's networks, and the backend is given the variable
each manifest's ``container.url`` names.

Parametrised over the fragment rather than written per service: an
example added without its key, its label or its URL variable is caught
by the check that already exists, which is the difference between a
guard and a list of the agents someone remembered.
"""
from __future__ import annotations

import os
import pathlib
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.agents.manifest import AgentManifest, load_manifest

REPO = Path(__file__).resolve().parents[2]


def _manifest(**extra):
    return AgentManifest.model_validate(
        {
            "id": "c-v1", "name": "c", "runtime": "container",
            "container": {"url": "http://c:1"}, "input_schema": "s.json",
            "phases": [{"name": "run"}], "output": {"mode": "structured"},
            **extra,
        }
    )


def test_network_egress_defaults_to_false_and_refuses_unknown_keys():
    assert _manifest().network.egress is False
    assert _manifest(network={"egress": True}).network.egress is True
    with pytest.raises(ValidationError):
        _manifest(network={"egress": True, "ingress": True})


def test_the_agents_listing_carries_runtime_and_network(monkeypatch):
    from app.agents import registry
    from tests.test_container_runner import _write_container_dir

    registry._clear_registry_for_tests()
    try:
        d = _write_container_dir(Path(__import__("tempfile").mkdtemp()), url="http://c:1")
        manifest = load_manifest(d)
        from app.agents.container import ContainerAgent

        registry.register(ContainerAgent(manifest, "http://c:1", d), manifest, agent_dir=d)
        import asyncio

        from app.routers.agents import list_agents_endpoint

        (row,) = asyncio.run(list_agents_endpoint(_=None))
        assert row["runtime"] == "container"
        assert row["network"] == {"egress": False}
    finally:
        registry._clear_registry_for_tests()


def _compose():
    return yaml.safe_load((REPO / "compose.yaml").read_text())


def _fragment():
    return yaml.safe_load((REPO / "agents.compose.yaml").read_text())


def test_compose_declares_the_agents_and_egress_networks():
    compose = _compose()
    assert "agents.compose.yaml" in compose["include"]
    networks = compose["networks"]
    assert networks["agents"] == {"internal": True}
    assert "egress" in networks and not (networks["egress"] or {}).get("internal")
    backend = compose["services"]["backend"]
    # Exactly these three: ``edge`` is where the HTTPS edge reaches it (K
    # blueprint T1); test_tls_guard.py holds that network to its members.
    assert set(backend["networks"]) == {"default", "agents", "edge"}
    for name in ("vector", "postgres", "redis"):
        assert "agents" not in (compose["services"][name].get("networks") or [])


def _agent_services():
    return sorted(_fragment()["services"])


def _key_variable(agent_id: str) -> str:
    """``LIBRERUN_AGENT_KEY_<ID>``, derived exactly as ``scripts/demo.sh``
    derives it: upper-cased, every character outside [A-Z0-9] an
    underscore (compose variable names admit no hyphens)."""
    return "LIBRERUN_AGENT_KEY_" + re.sub(r"[^A-Z0-9]", "_", agent_id.upper())


def test_the_fragment_carries_every_example_container():
    """A census that came back empty would make every check below
    vacuous, and a fragment that quietly lost a service would read as
    success."""
    assert _agent_services() == ["echo-agent", "llamaindex-agent", "vercel-agent"], (
        "the three example containers of blueprint S5-R are what the demo "
        "promises; changing this list is a change to promise 1 (five agents)"
    )


# The services the tree builds (A2, #133): the platform's three and the
# three examples, and `edge-control` (T2), which builds the backend's
# context under the backend's image name — so six images still. Another is
# a decision, and this list is where it is taken.
BUILT = {"gateway", "backend", "frontend", "edge-control", "echo-agent", "llamaindex-agent", "vercel-agent"}
LOCAL_PREFIX = "${LIBRERUN_IMAGE_PREFIX:-localhost/librerun}/"


def _built_services() -> dict[str, dict]:
    """Every service with a ``build``, from both files as written — the
    lines compose reads, not a comment about them."""
    services = {**_compose()["services"], **_fragment()["services"]}
    return {name: spec for name, spec in services.items() if (spec or {}).get("build")}


def _asks_a_registry(services: dict[str, dict]) -> list[str]:
    """The built services compose would ask a registry for first: any
    ``pull_policy`` but ``build``, or an image named anywhere but the
    local default — the only name that keeps podman's ``missing`` default
    off a registry too (the K blueprint's A2)."""
    return sorted(
        name
        for name, spec in services.items()
        if spec.get("pull_policy") != "build" or not str(spec.get("image", "")).startswith(LOCAL_PREFIX)
    )


def test_every_service_that_builds_never_pulls():
    """A release is source only (L37, D26): each service the tree builds
    is built from the checkout on every ``up`` and never pulled — with any
    other policy compose asks a registry first, which is issue #133."""
    built = _built_services()
    assert set(built) == BUILT, sorted(built)
    assert _asks_a_registry(built) == []


def test_a_built_service_without_the_policy_is_named():
    """The double: one line gone, or one name moved off the local default,
    must be named by the check above, or it would pass a tree that pulls.
    (``librerun-smoke`` → ``source-build`` runs the same rule on compose's
    resolved model, and turns its own checker red first.)"""
    import copy

    built = _built_services()
    without = copy.deepcopy(built)
    del without["frontend"]["pull_policy"]
    renamed = copy.deepcopy(built)
    renamed["echo-agent"]["image"] = "ghcr.io/someone/librerun-echo-agent:dev"
    assert _asks_a_registry(without) == ["frontend"]
    assert _asks_a_registry(renamed) == ["echo-agent"]
    # An empty census names nothing: `set(built) == BUILT` above refuses one.
    assert _asks_a_registry({}) == []


@pytest.mark.parametrize("name", _agent_services())
def test_each_agent_service_is_on_the_agents_network_only_with_no_logs(name):
    service = _fragment()["services"][name]
    assert "demo" in service["profiles"]
    assert service["networks"] == ["agents"]
    assert service["labels"]["librerun.agent_id"]
    assert service["logging"] == {"driver": "none"}


@pytest.mark.parametrize("name", _agent_services())
def test_each_agent_service_holds_its_gateway_key_and_no_other_credential(name):
    """The ONE credential an agent container holds is its LibreRun gateway
    key (blueprint S4a, D10) — never a provider key, and never a
    telemetry one. It sits in OPENAI_API_KEY because that is the variable
    a framework reads, and it expands from LIBRERUN_AGENT_KEY_<ID> with
    ``:?``, so an unprovisioned agent stops ``up`` by name instead of
    starting with every model call refused."""
    service = _fragment()["services"][name]
    env = service["environment"]

    secrets = {k for k in env if "TOKEN" in k or "KEY" in k or "SECRET" in k}
    assert secrets == {"OPENAI_API_KEY"}, name

    variable = _key_variable(service["labels"]["librerun.agent_id"])
    assert env["OPENAI_API_KEY"].startswith("${" + variable + ":?"), name
    assert "lr_agent_" not in env["OPENAI_API_KEY"], name
    # …and it is pointed at the gateway, not at a provider. Both
    # spellings, so an SDK agent and a framework agent in one container
    # agree on where the gateway is.
    assert env["OPENAI_BASE_URL"] == "http://gateway:8090/v1", name
    assert env["LIBRERUN_GATEWAY_URL"] == "http://gateway:8090", name


@pytest.mark.parametrize("name", _agent_services())
def test_an_agent_that_exports_telemetry_exports_to_the_relay(name):
    """Stated as a conditional rather than as a list of the containers
    that happen to export today. The TypeScript example ships no OTLP
    exporter (the package that would is v1.1, L20) and therefore sets no
    endpoint; what must never happen is an endpoint pointing anywhere but
    the chassis relay, or carrying a credential — the run token travels
    with each invocation instead.

    Asserted rather than skipped for the absent case: a skip is a test
    that did not run, and this one has something to say about a container
    that exports nothing — namely that setting no endpoint is the only
    other allowed answer.
    """
    endpoint = _fragment()["services"][name]["environment"].get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    assert endpoint in (None, "http://backend:8000/api/v1/_o/otlp"), (name, endpoint)


def test_at_least_one_example_exports_its_own_telemetry():
    """The guard above skips a container that exports nothing, so a
    fragment where NOTHING exported would pass it three times over."""
    exporting = [
        name
        for name, service in _fragment()["services"].items()
        if service["environment"].get("OTEL_EXPORTER_OTLP_ENDPOINT")
    ]
    assert exporting, "no example container exports telemetry at all"


@pytest.mark.parametrize("name", _agent_services())
def test_the_backend_is_given_the_variable_the_manifest_names(name):
    """The manifest says where the chassis finds the agent; compose has to
    make that name resolve. Derived from the manifest rather than
    restated, so an example whose ``container.url`` variable nobody
    passed is caught here instead of as a silent "registration skipped"
    line in a boot log."""
    service = _fragment()["services"][name]
    manifest = load_manifest(_agent_dir(service))
    reference = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", manifest.container.url)
    assert reference, (
        f"{name}: container.url is {manifest.container.url!r}; the examples "
        f"name an environment variable so a deployment can retarget them"
    )
    variable = reference.group(1)
    backend_env = _compose()["services"]["backend"]["environment"]
    assert variable in backend_env, (
        f"{name}: the backend is never given {variable}, so the chassis "
        f"skips registering this agent and the demo is a card short"
    )
    assert backend_env[variable].endswith(f"http://{name}:8090}}"), backend_env[variable]


@pytest.mark.parametrize("name", _agent_services())
def test_each_manifest_and_its_service_agree_about_egress(name):
    service = _fragment()["services"][name]
    manifest = load_manifest(_agent_dir(service))
    assert manifest.network.egress is False, (
        f"{name} declares egress; no shipped example should need the Internet"
    )
    assert "egress" not in service["networks"], name


def _agent_dir(service) -> Path:
    """The agent directory a service builds: the Dockerfile's, under the
    build context (the repository root, so the SDK installs from its
    source tree). A service that ships no ``dockerfile`` builds the
    context itself."""
    build = service["build"]
    dockerfile = build.get("dockerfile")
    agent_dir = REPO / build["context"]
    if dockerfile:
        agent_dir = agent_dir / pathlib.Path(dockerfile).parent
    return agent_dir


def test_an_egress_optout_must_be_declared_in_both_places():
    """The rule the CLI template and reviewers hold: a fragment on the
    egress network without the manifest flag (or the reverse) is a
    mismatch this check would catch for the shipped agents."""
    fragment = _fragment()
    for name, service in fragment["services"].items():
        agent_id = service["labels"]["librerun.agent_id"]
        manifest = load_manifest(_agent_dir(service))
        assert manifest.id == agent_id
        assert ("egress" in service["networks"]) == manifest.network.egress, name


# ---------------------------------------------------------------------------
# compose.sh and the agent keys (blueprint S4a, D10)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_SH = REPO_ROOT / "compose.sh"


def _hermetic_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The caller's shell minus any LIBRERUN_AGENT_KEY_* it happens to
    export. Since K3 the derivation reads the environment as a source,
    so a developer's own exported key would otherwise land in every
    assertion below; what a probe wants in the environment it passes."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("LIBRERUN_AGENT_KEY_")
    }
    environment.update(extra or {})
    return environment


def _compose_functions(
    tmp_path, script: str, env_text: str | None = "", environment: dict[str, str] | None = None
):
    """Run one of compose.sh's own functions in a scratch directory and
    return the completed process.

    The functions are sourced out of the real script rather than
    reimplemented, so a change to the script is a change to what this
    asserts. ``env_text=None`` writes no .env at all.
    """
    import subprocess

    (tmp_path / "compose.sh").write_text(COMPOSE_SH.read_text())
    (tmp_path / "agents.compose.yaml").write_text(
        (REPO_ROOT / "agents.compose.yaml").read_text()
    )
    if env_text is not None:
        (tmp_path / ".env").write_text(env_text)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_hermetic_environment(environment),
    )


def _run_compose_functions(
    tmp_path, script: str, env_text: str | None = "", environment: dict[str, str] | None = None
) -> str:
    result = _compose_functions(tmp_path, script, env_text, environment)
    assert result.returncode == 0, result.stderr
    return result.stdout


_SOURCE = (
    'source <(sed -n "/^agent_key_requirements()/,/^}/p" compose.sh); '
    'source <(sed -n "/^placeholder_missing_agent_keys()/,/^}/p" compose.sh); '
    'source <(sed -n "/^derive_agent_keys()/,/^}/p" compose.sh); '
)


def test_the_gateway_receives_the_agent_keys_and_nothing_else(tmp_path):
    """compose.sh derives agent-keys.env from the LIBRERUN_AGENT_KEY_*
    lines of .env and from nothing else: the gateway holds the provider
    credentials, so handing it the whole file would hand it
    APP_SECRET_KEY and the bootstrap credentials too.

    It reads by line match rather than `source`, so a value containing
    $(...), backticks or ; is data.
    """
    env = (
        "APP_SECRET_KEY=super-secret\n"
        "INITIAL_ADMIN_PASSWORD=hunter2\n"
        "OPENAI_API_KEY=sk-a-real-provider-key\n"
        "# a comment\n"
        'LIBRERUN_AGENT_KEY_ECHO_V1="lr_agent_bbb"\n'
        "LIBRERUN_AGENT_KEY_ECHO_V1_PREVIOUS=lr_agent_ccc\n"
        "LIBRERUN_AGENT_KEY_EVIL=lr_agent_$(touch pwned)\n"
    )
    out = _run_compose_functions(
        tmp_path, _SOURCE + "derive_agent_keys; cat agent-keys.env", env
    )

    lines = [line for line in out.splitlines() if line.strip()]
    assert lines == [
        "LIBRERUN_AGENT_KEY_ECHO_V1=lr_agent_bbb",
        "LIBRERUN_AGENT_KEY_ECHO_V1_PREVIOUS=lr_agent_ccc",
        "LIBRERUN_AGENT_KEY_EVIL=lr_agent_$(touch pwned)",
    ]
    assert not (tmp_path / "pwned").exists(), "the derivation evaluated the file"


def test_two_agents_sharing_a_key_value_stop_compose_by_name(tmp_path):
    """A presented key maps to exactly one agent. The unique index would
    catch it too — as a constraint name in a boot log nobody is watching,
    after `up`."""
    result = _compose_functions(
        tmp_path,
        _SOURCE + "derive_agent_keys",
        "LIBRERUN_AGENT_KEY_ONE=lr_agent_same\nLIBRERUN_AGENT_KEY_TWO=lr_agent_same\n",
    )

    assert result.returncode != 0
    assert "LIBRERUN_AGENT_KEY_ONE" in result.stderr
    assert "LIBRERUN_AGENT_KEY_TWO" in result.stderr
    assert "same agent key value" in result.stderr


# The environment as a second source (K blueprint K3, decision L30).
#
# Compose reads the shell before .env when it expands the fragment's
# `${LIBRERUN_AGENT_KEY_<ID>:?}`, so a key exported by the caller is the
# one the agent container presents — and before K3 it was NOT the one
# the gateway registered, because agent-keys.env was derived from the
# file alone: every call from that agent came back 401. It is also the
# whole delivery when there is no .env on disk at all, which is what
# `sops exec-env` arranges (docs/platform/Install.md, "Encrypting .env at rest").


def test_an_agent_key_in_the_environment_alone_reaches_the_gateway(tmp_path):
    """Both sources land in the file, the environment's lines first; the
    derivation still evaluates nothing, whichever source a value came
    from."""
    out = _run_compose_functions(
        tmp_path,
        _SOURCE + "derive_agent_keys; cat agent-keys.env",
        "APP_SECRET_KEY=super-secret\nLIBRERUN_AGENT_KEY_FROM_FILE=lr_agent_file\n",
        environment={
            "LIBRERUN_AGENT_KEY_FROM_SHELL": "lr_agent_shell",
            "LIBRERUN_AGENT_KEY_EVIL": "lr_agent_$(touch pwned)",
        },
    )

    lines = [line for line in out.splitlines() if line.strip()]
    assert lines == [
        "LIBRERUN_AGENT_KEY_EVIL=lr_agent_$(touch pwned)",
        "LIBRERUN_AGENT_KEY_FROM_SHELL=lr_agent_shell",
        "LIBRERUN_AGENT_KEY_FROM_FILE=lr_agent_file",
    ]
    assert not (tmp_path / "pwned").exists(), "the derivation evaluated a value"


def test_the_environment_is_the_whole_delivery_when_there_is_no_env_file(tmp_path):
    """`sops exec-env` leaves no .env on disk: the environment is all
    there is, and the gateway must still get the keys."""
    out = _run_compose_functions(
        tmp_path,
        _SOURCE + "derive_agent_keys; cat agent-keys.env",
        env_text=None,
        environment={"LIBRERUN_AGENT_KEY_ECHO_V1": "lr_agent_from_the_shell"},
    )

    assert not (tmp_path / ".env").exists()
    assert out.splitlines() == ["LIBRERUN_AGENT_KEY_ECHO_V1=lr_agent_from_the_shell"]


def test_the_same_value_under_two_names_across_environment_and_file_stops_by_name(tmp_path):
    """The duplicate check spans both sources, and the message says
    which is which."""
    result = _compose_functions(
        tmp_path,
        _SOURCE + "derive_agent_keys",
        "LIBRERUN_AGENT_KEY_TWO=lr_agent_same\n",
        environment={"LIBRERUN_AGENT_KEY_ONE": "lr_agent_same"},
    )

    assert result.returncode != 0
    assert "LIBRERUN_AGENT_KEY_ONE (environment)" in result.stderr
    assert "LIBRERUN_AGENT_KEY_TWO (.env)" in result.stderr
    assert "same agent key value" in result.stderr
    assert not (tmp_path / "agent-keys.env").exists(), "a refused derivation wrote a file"


def test_the_environment_outranks_the_file_for_one_name(tmp_path):
    """Compose's own precedence: a name set in both is the shell's. The
    file's value must be absent, not merely second — the gateway would
    otherwise register a key the agent container never presents."""
    out = _run_compose_functions(
        tmp_path,
        _SOURCE + "derive_agent_keys; cat agent-keys.env",
        "LIBRERUN_AGENT_KEY_ECHO_V1=lr_agent_from_the_file\n",
        environment={"LIBRERUN_AGENT_KEY_ECHO_V1": "lr_agent_from_the_shell"},
    )

    assert out.splitlines() == ["LIBRERUN_AGENT_KEY_ECHO_V1=lr_agent_from_the_shell"]


def test_the_placeholder_never_becomes_a_registered_key(tmp_path):
    """The stand-in `placeholder_missing_agent_keys` exports so that an
    unprovisioned agent does not block `./compose.sh up -d` is a public
    string. Now that the derivation reads the environment, the wrong call
    order would write it into agent-keys.env and the gateway would
    register it as a credential. The script derives first and
    placeholders second; this runs them the other way round on purpose
    and the file must still be empty."""
    out = _run_compose_functions(
        tmp_path,
        _SOURCE
        + "placeholder_missing_agent_keys up -d; "
        + 'echo "exported=${LIBRERUN_AGENT_KEY_ECHO_V1:-UNSET}"; '
        + "derive_agent_keys; echo 'file:'; cat agent-keys.env; echo '(end)'",
    )

    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    assert values["exported"].startswith("unprovisioned-agent-key"), (
        "the placeholder was not exported, so the probe proved nothing"
    )
    assert out.split("file:\n", 1)[1].strip() == "(end)", out


def test_a_key_with_a_newline_in_it_is_refused_by_name(tmp_path):
    """A shell value may hold anything and a file line may not: a newline
    would put a second line into the gateway's file. Refused by name,
    never echoing the value."""
    result = _compose_functions(
        tmp_path,
        _SOURCE + "derive_agent_keys",
        "",
        environment={"LIBRERUN_AGENT_KEY_ECHO_V1": "lr_agent_ok\nAPP_SECRET_KEY=smuggled"},
    )

    assert result.returncode != 0
    assert "LIBRERUN_AGENT_KEY_ECHO_V1" in result.stderr
    assert "smuggled" not in result.stderr
    assert not (tmp_path / "agent-keys.env").exists()


def test_an_unprovisioned_checkout_can_still_run_infra_but_not_an_agent(tmp_path):
    """The fragment's `:?` must stop `up` for an agent with no key — and
    must NOT stop `./compose.sh up -d`, the infra-only command a
    developer runs for a local uvicorn, which starts no agent container
    at all. Compose interpolates the whole file before it filters by
    profile, so compose.sh fills the gap when no agent profile is asked
    for and leaves it open when one is.
    """
    probe = (
        'echo "no-profile=${LIBRERUN_AGENT_KEY_ECHO_V1:-UNSET}"; '
        "unset LIBRERUN_AGENT_KEY_ECHO_V1; "
        "placeholder_missing_agent_keys --profile demo up -d; "
        'echo "demo-profile=${LIBRERUN_AGENT_KEY_ECHO_V1:-UNSET}"'
    )
    out = _run_compose_functions(
        tmp_path, _SOURCE + "placeholder_missing_agent_keys up -d; " + probe
    )

    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    assert values["no-profile"].startswith("unprovisioned-agent-key")
    assert values["demo-profile"] == "UNSET", (
        "the demo profile was requested, so an unprovisioned agent must stop "
        "`up` with the fragment's named error"
    )


def test_starting_one_agent_does_not_demand_another_agents_key(tmp_path):
    """Per agent, not per file. With two agents installed, `--profile demo
    up` must not be blocked by the key of an agent it never asked to
    start — the same defect the placeholder exists to fix, one level in.

    Three shapes, because a fragment may be written any of them and
    guessing wrong puts the bug back: inline profiles, a block list, and
    a service with no profiles at all (it always starts, so its key is
    always required). The block-list agent also declares its key ABOVE
    its profiles, which is where reading line by line goes wrong.
    """
    import subprocess

    (tmp_path / "compose.sh").write_text(COMPOSE_SH.read_text())
    (tmp_path / "agents.compose.yaml").write_text(
        "services:\n"
        "  inline-agent:\n"
        '    profiles: ["demo", "extra"]\n'
        "    environment:\n"
        "      OPENAI_API_KEY: ${LIBRERUN_AGENT_KEY_INLINE_V1:?not provisioned}\n"
        "  block-agent:\n"
        "    environment:\n"
        "      OPENAI_API_KEY: ${LIBRERUN_AGENT_KEY_BLOCK_V1:?not provisioned}\n"
        "    profiles:\n"
        "      - other\n"
        "      - spare\n"
        "  always-agent:\n"
        "    environment:\n"
        "      OPENAI_API_KEY: ${LIBRERUN_AGENT_KEY_ALWAYS_V1:?not provisioned}\n"
    )
    (tmp_path / ".env").write_text("")

    def probe(command: str) -> dict[str, str]:
        script = (
            _SOURCE
            + f"placeholder_missing_agent_keys {command}; "
            + 'for v in INLINE_V1 BLOCK_V1 ALWAYS_V1; do '
            + 'n=LIBRERUN_AGENT_KEY_$v; echo "$v=${!n:-UNSET}"; done'
        )
        result = subprocess.run(
            ["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        return dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )

    # Infra only: every agent that a profile gates gets a placeholder, so
    # `./compose.sh up -d` works on an unprovisioned checkout.
    values = probe("up -d")
    assert values["INLINE_V1"].startswith("unprovisioned-agent-key")
    assert values["BLOCK_V1"].startswith("unprovisioned-agent-key")
    assert values["ALWAYS_V1"] == "UNSET", (
        "a service under no profile always starts, so its key is always "
        "required — placeholdering it would start a container whose every "
        "model call is refused"
    )

    # One agent asked for: only that agent's key is left open.
    values = probe("--profile other up -d")
    assert values["BLOCK_V1"] == "UNSET", (
        "the block-agent's profile was requested, so its unprovisioned key "
        "must stop `up` with the fragment's named error"
    )
    assert values["INLINE_V1"].startswith("unprovisioned-agent-key"), (
        "the inline-agent was not asked for; its missing key must not block "
        "the command"
    )
    assert values["ALWAYS_V1"] == "UNSET"


def test_a_provisioned_key_is_never_overridden(tmp_path):
    out = _run_compose_functions(
        tmp_path,
        _SOURCE + 'placeholder_missing_agent_keys up -d; echo "v=${LIBRERUN_AGENT_KEY_ECHO_V1:-FROM_ENV_FILE}"',
        "LIBRERUN_AGENT_KEY_ECHO_V1=lr_agent_real\n",
    )

    # compose reads .env itself, so the script must leave the variable
    # alone rather than shadow a real key with a placeholder.
    assert "v=FROM_ENV_FILE" in out


# ---------------------------------------------------------------------------
# scripts/demo.sh and the agent keys (blueprint S4a, D10; S5-R)
# ---------------------------------------------------------------------------

DEMO_SH = REPO / "scripts" / "demo.sh"


def _demo_tree(tmp_path, env_text: str | None):
    """A scratch checkout with the REAL demo.sh and the REAL agent
    directories, so a newly added example is covered by these checks the
    day it lands rather than when someone remembers to list it."""
    import shutil

    (tmp_path / "scripts").mkdir()
    shutil.copy(DEMO_SH, tmp_path / "scripts" / "demo.sh")
    shutil.copytree(REPO / "backend" / "agents", tmp_path / "backend" / "agents")
    if env_text is not None:
        (tmp_path / ".env").write_text(env_text)
        (tmp_path / ".env").chmod(0o600)
    return tmp_path


def _run_demo(tmp_path) -> str:
    """``./scripts/demo.sh --env-only``: everything up to, and not
    including, starting containers."""
    import subprocess

    result = subprocess.run(
        ["bash", "scripts/demo.sh", "--env-only"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _key_lines(tmp_path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in (tmp_path / ".env").read_text().splitlines()
        if line.startswith("LIBRERUN_AGENT_KEY_")
    )


def _expected_key_variables() -> set[str]:
    """One per discovered agent, derived from the manifests themselves."""
    import yaml as _yaml

    names = set()
    for manifest in sorted((REPO / "backend" / "agents").glob("*/agent.yaml")) + sorted(
        (REPO / "backend" / "agents" / "_examples").glob("*/agent.yaml")
    ):
        if manifest.parent.name.startswith("_"):
            continue
        agent_id = (_yaml.safe_load(manifest.read_text()) or {}).get("id")
        if agent_id:
            names.add(_key_variable(str(agent_id)))
    return names


def test_a_fresh_checkout_gets_one_key_per_agent(tmp_path):
    """The zero-config promise (L22) is per agent: every agent the demo
    starts must have a key before `up`, because compose expands the
    fragment's `:?` while no LibreRun service is running and nothing
    inside the platform can mint one then."""
    tree = _demo_tree(tmp_path, env_text=None)

    _run_demo(tree)

    keys = _key_lines(tree)
    assert set(keys) == _expected_key_variables(), set(keys)
    assert all(value.startswith("lr_agent_") for value in keys.values()), keys
    # Distinct: a key names exactly one agent, and compose.sh refuses two
    # agents sharing a value.
    assert len(set(keys.values())) == len(keys)
    assert (tree / ".env").stat().st_mode & 0o777 == 0o600


def test_an_env_written_before_an_agent_existed_is_topped_up(tmp_path):
    """The upgrade path S5-R adds two agents to (blueprint S5-R).

    Compose expands `${LIBRERUN_AGENT_KEY_<ID>:?…}` BEFORE it filters by
    profile, so an .env written before an agent existed stops `up` with a
    named error about a variable its author never chose to omit. Right
    for a deleted key, wrong for a new agent — and indistinguishable from
    inside compose, so the script closes the gap instead.
    """
    tree = _demo_tree(
        tmp_path,
        env_text=(
            "APP_SECRET_KEY=already-chosen\n"
            "LIBRERUN_AGENT_KEY_VITA_V1=lr_agent_the_operators_own\n"
        ),
    )

    output = _run_demo(tree)

    keys = _key_lines(tree)
    assert set(keys) == _expected_key_variables(), set(keys)
    # An existing key is SOMEBODY'S DECISION — possibly a rotation in
    # flight — so it is never rewritten.
    assert keys["LIBRERUN_AGENT_KEY_VITA_V1"] == "lr_agent_the_operators_own"
    assert "APP_SECRET_KEY=already-chosen" in (tree / ".env").read_text()
    assert "provisioned" in output, output


def test_topping_up_is_idempotent(tmp_path):
    """A second run must add nothing: an .env that grew a duplicate key
    line on every start would eventually hand two agents one value, which
    compose.sh refuses by name."""
    tree = _demo_tree(tmp_path, env_text=None)
    _run_demo(tree)
    first = _key_lines(tree)

    output = _run_demo(tree)

    assert _key_lines(tree) == first
    assert "provisioned" not in output, output


def test_a_key_in_the_environment_is_not_duplicated_into_the_file(tmp_path):
    """Compose reads the shell before the file, so a key exported by the
    caller is already provisioned. Writing a second, different value for
    the same agent into .env would be inert at best and confusing at
    worst."""
    import subprocess

    tree = _demo_tree(tmp_path, env_text="APP_SECRET_KEY=already-chosen\n")
    environment = dict(os.environ)
    for name in _expected_key_variables():
        environment[name] = "lr_agent_from_the_shell"

    result = subprocess.run(
        ["bash", "scripts/demo.sh", "--env-only"],
        cwd=tree,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert _key_lines(tree) == {}, (tree / ".env").read_text()


@pytest.mark.parametrize("args", [["--pull"], ["--env-only", "--pull"]])
def test_demo_sh_refuses_pull_and_writes_nothing(tmp_path, args):
    """``--pull`` retired with image publishing (A2; D26): one line, exit
    2, and nothing written — not even the ``.env`` ``--env-only`` would
    have written first. The refusal stays until 1.1.0."""
    import subprocess

    tree = _demo_tree(tmp_path, env_text=None)
    before = sorted(str(p.relative_to(tree)) for p in tree.rglob("*"))

    result = subprocess.run(
        ["bash", "scripts/demo.sh", *args], cwd=tree, capture_output=True, text=True
    )

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stderr.splitlines() == [
        "demo.sh: --pull is gone: a LibreRun release is source only, and ./scripts/demo.sh builds it"
    ]
    assert result.stdout == ""
    assert not (tree / ".env").exists()
    assert sorted(str(p.relative_to(tree)) for p in tree.rglob("*")) == before
