"""The ``librerun`` CLI and its three templates (blueprint S6).

The CLI lives in ``cli/`` and is standard-library only; the chassis is
what checks its output. So this module puts ``cli/src`` on ``sys.path``
the way ``conftest.py`` puts the SDK there, renders every template into
a scratch checkout, and holds the result to the rules the chassis
already enforces on the shipped examples: the manifest loads, the grants
are ``llm`` and ``pii``, one ``llm.steps`` entry is declared, the
appended compose service has the examples' exact shape
(``tests/test_agents_network.py``'s rules, applied to the fragment the
template appends), the key is provisioned once and never rewritten, and
— the part that matters — the rendered agents pass the same batteries
the examples pass and go red WITH A REASON when broken the way each
README says to (D9, D10).

Every guard has its negative probe beside it: a rotation that refuses
what it must refuse, a doctor that fails without an engine, a break that
turns a green battery red. On a clean tree a checker that never looks
and one that works are indistinguishable.
"""
from __future__ import annotations

import datetime
import importlib.util
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.agents.manifest import load_manifest

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
CLI_SRC = REPO / "cli" / "src"
if str(CLI_SRC) not in sys.path:
    sys.path.insert(0, str(CLI_SRC))

from librerun import _agents, _env, _init, _rotate, cli as cli_module  # noqa: E402
from librerun._common import CliError  # noqa: E402

TEMPLATES = ("langgraph", "container-python", "container-ts")


# ---------------------------------------------------------------------------
# a scratch checkout: the compose files and the agents tree, nothing else
# ---------------------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    (root / "backend").mkdir(parents=True)
    for name in ("compose.yaml", "compose.sh", "agents.compose.yaml"):
        shutil.copy(REPO / name, root / name)
    shutil.copytree(
        REPO / "backend" / "agents",
        root / "backend" / "agents",
        ignore=shutil.ignore_patterns("__pycache__", "tests", "*.pyc", "node_modules"),
    )
    return root


def _args(**kwargs):
    defaults = {"template": "langgraph", "display_name": None}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _service_named(root: Path, name: str) -> dict:
    fragment = yaml.safe_load((root / "agents.compose.yaml").read_text())
    return fragment["services"][name]


# ---------------------------------------------------------------------------
# .env: read as compose reads it, edited line by line
# ---------------------------------------------------------------------------


def test_parse_follows_compose_semantics():
    text = textwrap.dedent(
        """
        # a comment
        PLAIN=one # an inline comment
        export EXPORTED=two
        QUOTED="a \\"b\\" ${PLAIN}"
        SINGLE='${PLAIN} stays literal'
        DEFAULTED=${UNSET_NAME:-fallback}
        SETBUT=${EMPTY-not-used}${EMPTY:-used}
        DOLLARS=$$notavar
        YAMLISH: three
        PLAIN=overridden
        """
    )
    values = _env.parse(text, environ={"EMPTY": ""})
    assert values["PLAIN"] == "overridden"
    assert values["EXPORTED"] == "two"
    assert values["QUOTED"] == 'a "b" one'
    assert values["SINGLE"] == "${PLAIN} stays literal"
    assert values["DEFAULTED"] == "fallback"
    assert values["SETBUT"] == "used"
    assert values["DOLLARS"] == "$notavar"
    assert values["YAMLISH"] == "three"


def test_the_process_environment_wins_even_when_empty(tmp_path):
    path = tmp_path / ".env"
    path.write_text("BACKEND_PORT=8001\nOTHER=x\n")
    env = _env.DotEnv(path, environ={"BACKEND_PORT": ""})
    assert env.effective("BACKEND_PORT") == ""
    assert env.effective("OTHER") == "x"
    assert env.effective("MISSING") is None
    assert env.file_has("OTHER") and not env.file_has("BACKEND_PORT_X")


def test_set_and_remove_touch_only_their_own_lines(tmp_path):
    path = tmp_path / ".env"
    original = "# keep me\nA=1\n# A=commented\nB=2\nA=3\n"
    path.write_text(original)
    env = _env.DotEnv(path, environ={})
    env.set("A", "9")
    assert env.text == "# keep me\nA=9\n# A=commented\nB=2\n"
    env.set("C", "new")
    assert env.text.endswith("B=2\nC=new\n")
    assert env.remove("B") == 1
    assert "B=2" not in env.text and "# keep me" in env.text and "# A=commented" in env.text
    assert env.remove("NOPE") == 0


def test_write_is_owner_only_when_new_and_keeps_an_existing_mode(tmp_path):
    path = tmp_path / ".env"
    env = _env.DotEnv(path, environ={})
    env.text = "A=1\n"
    env.write()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    os.chmod(path, 0o640)
    env = _env.DotEnv(path, environ={})
    env.set("A", "2")
    env.write()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.read_text() == "A=2\n"


def test_agent_key_variable_is_the_gateway_rule():
    assert _env.agent_key_variable("vita-v1") == "LIBRERUN_AGENT_KEY_VITA_V1"
    assert _env.agent_key_variable("langgraph-triage") == "LIBRERUN_AGENT_KEY_LANGGRAPH_TRIAGE"
    assert _env.agent_key_variable("a1") == "LIBRERUN_AGENT_KEY_A1"
    with pytest.raises(CliError):
        _env.agent_key_variable("foo-previous")
    with pytest.raises(CliError):
        _env.agent_key_variable("Not_Valid")


def test_minted_keys_carry_the_prefix_the_gateway_demands():
    key = _env.mint_agent_key()
    assert key.startswith("lr_agent_") and len(key) == len("lr_agent_") + 48
    assert _env.mint_agent_key() != key


# ---------------------------------------------------------------------------
# the agents on disk and the fragment, read without the chassis
# ---------------------------------------------------------------------------


def test_scan_agents_finds_every_bundled_agent_with_its_runtime():
    found = {s.id: s for s in _agents.scan_agents(REPO)}
    assert set(found) == {"vita-v1", "echo-v1", "langgraph-triage", "llamaindex-summarize", "vercel-answer"}
    assert found["echo-v1"].runtime == "container"
    assert found["echo-v1"].container_url == "${ECHO_AGENT_URL}"
    assert found["langgraph-triage"].runtime == "python-package"
    assert found["vita-v1"].relative_to_backend == "agents/vita_v1"


def test_the_fragment_reader_maps_labels_to_services():
    services = {s.name: s for s in _agents.read_fragment(REPO)}
    assert services["echo-agent"].agent_id == "echo-v1"
    assert services["echo-agent"].container_name == "librerun-echo-agent"
    assert services["echo-agent"].profiles == ["demo"]
    assert _agents.service_for(REPO, "vercel-answer").name == "vercel-agent"
    assert _agents.service_for(REPO, "nobody") is None


def test_the_fragment_reader_takes_block_profiles_and_quoted_labels(tmp_path):
    (tmp_path / "agents.compose.yaml").write_text(
        textwrap.dedent(
            """
            services:
              one:
                profiles:
                  - "agents"
                  - demo
                labels:
                  librerun.agent_id: "one-v1"
              two:
                profiles: ['x']
                labels:
                  librerun.agent_id: two-v1  # trailing comment
            """
        )
    )
    services = {s.name: s for s in _agents.read_fragment(tmp_path)}
    assert services["one"].profiles == ["agents", "demo"] and services["one"].agent_id == "one-v1"
    assert services["two"].profiles == ["x"] and services["two"].agent_id == "two-v1"


# ---------------------------------------------------------------------------
# init: every template renders an agent the chassis accepts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template", TEMPLATES)
def test_every_template_renders_a_manifest_the_chassis_loads(checkout, template):
    result = _init.scaffold(checkout, "my-agent", template, None)
    manifest = load_manifest(result["directory"])
    assert manifest.id == "my-agent" and manifest.name == "My Agent"
    assert "llm" in manifest.capabilities and "pii" in manifest.capabilities
    assert len(manifest.llm.steps) == 1 and manifest.llm.steps[0].id == "answer"
    assert manifest.output.mode == "structured"
    # S7's card badge and progress labels: a framework each, and a label
    # for every row the template's code reports — the adapter's
    # `<phase>:<node>` rows for the graph, the SDK/server's STEP for the
    # containers.
    assert manifest.framework, "the card would fall back to the runtime"
    expected_rows = {"answer:answer", "answer:summarise"} if template == "langgraph" else {"answer"}
    assert set(manifest.step_labels()) == expected_rows
    assert all(label.strip() for label in manifest.step_labels().values())
    if template == "langgraph":
        assert manifest.runtime == "python-package"
        assert manifest.framework == "langgraph"
    else:
        assert manifest.runtime == "container"
        assert manifest.container.url == "http://my-agent:8090"
        assert (result["directory"] / manifest.input_schema).is_file()
        assert (result["directory"] / "Dockerfile").is_file()
    scenarios = sorted((result["directory"] / manifest.scenarios).glob("*.json"))
    assert scenarios, "a template without a sample has nothing for the new-run page"
    for path in scenarios:
        data = json.loads(path.read_text())
        assert data["name"] and isinstance(data["user_inputs"], dict)


@pytest.mark.parametrize("template", TEMPLATES)
def test_no_token_survives_rendering(checkout, template):
    result = _init.scaffold(checkout, "my-agent", template, None)
    for path in result["directory"].rglob("*"):
        if path.is_file():
            leftovers = re.findall(r"__[A-Z_]+__", path.read_text())
            assert not leftovers, (path, leftovers)
    if "service" in result:
        assert not re.findall(r"__[A-Z_]+__", (checkout / "agents.compose.yaml").read_text())


def test_the_templates_shipped_use_only_the_tokens_init_knows():
    """A token typed in a template that `tokens()` does not define would
    survive rendering as literal text; the check above catches it on the
    rendered tree, this one at the source, with the message naming it."""
    known = set(_init.tokens("a-b", "A B", "container-python"))
    base = CLI_SRC / "librerun" / "templates"
    for path in base.rglob("*"):
        if path.is_file():
            used = set(re.findall(r"__[A-Z_]+__", path.read_text()))
            assert used <= known, (path.relative_to(base), used - known)


@pytest.mark.parametrize("template", ("container-python", "container-ts"))
def test_container_templates_append_a_service_in_the_examples_shape(checkout, template):
    """The rules `tests/test_agents_network.py` holds the shipped
    fragment to, applied to what `init` appends."""
    result = _init.scaffold(checkout, "my-agent", template, None)
    service = _service_named(checkout, "my-agent")
    env = service["environment"]
    assert service["labels"]["librerun.agent_id"] == "my-agent"
    assert service["networks"] == ["agents"]
    assert service["logging"] == {"driver": "none"}
    assert service["profiles"] == ["agents"]
    assert service["container_name"] == "librerun-my-agent"
    assert service["build"] == {"context": ".", "dockerfile": "backend/agents/my_agent/Dockerfile"}
    # Built from the checkout and never pulled, like every service that
    # builds (A2, #133): a scaffolded agent has no image anywhere to pull.
    assert service["pull_policy"] == "build"
    secrets = {k for k in env if "TOKEN" in k or "KEY" in k or "SECRET" in k}
    assert secrets == {"OPENAI_API_KEY"}
    assert env["OPENAI_API_KEY"].startswith("${LIBRERUN_AGENT_KEY_MY_AGENT:?")
    assert "lr_agent_" not in env["OPENAI_API_KEY"]
    assert env["OPENAI_BASE_URL"] == "http://gateway:8090/v1"
    assert env["LIBRERUN_GATEWAY_URL"] == "http://gateway:8090"
    assert env.get("OTEL_EXPORTER_OTLP_ENDPOINT") in (None, "http://backend:8000/api/v1/_o/otlp")
    if template == "container-python":
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://backend:8000/api/v1/_o/otlp"
    else:
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    # The egress line is carried commented, as the manifest docs say.
    text = (checkout / "agents.compose.yaml").read_text()
    assert "# - egress" in text.split("my-agent:", 1)[1]
    assert result["service"] == "my-agent"
    # …and the three example services are exactly as they were.
    assert set(yaml.safe_load(text)["services"]) == {"echo-agent", "llamaindex-agent", "vercel-agent", "my-agent"}


def test_init_provisions_the_key_once_and_never_rewrites_it(checkout):
    env_path = checkout / ".env"
    env_path.write_text("APP_SECRET_KEY=x\nLIBRERUN_AGENT_KEY_ECHO_V1=lr_agent_existing\n")
    first = _init.scaffold(checkout, "one", "container-python", None)
    assert first["key_written"] is True
    values = _env.parse(env_path.read_text(), environ={})
    assert values["LIBRERUN_AGENT_KEY_ONE"].startswith("lr_agent_")
    assert values["LIBRERUN_AGENT_KEY_ECHO_V1"] == "lr_agent_existing"
    # A second agent adds its own line and leaves the first's alone.
    _init.scaffold(checkout, "two", "container-ts", "Two")
    again = _env.parse(env_path.read_text(), environ={})
    assert again["LIBRERUN_AGENT_KEY_ONE"] == values["LIBRERUN_AGENT_KEY_ONE"]
    assert again["LIBRERUN_AGENT_KEY_TWO"].startswith("lr_agent_")
    assert stat.S_IMODE(env_path.stat().st_mode) == stat.S_IMODE(env_path.stat().st_mode)


def test_init_without_an_env_writes_no_key_and_says_so(checkout):
    result = _init.scaffold(checkout, "one", "container-python", None)
    assert result["key_written"] is False
    assert not (checkout / ".env").exists()
    # The in-process template holds no key at all.
    result = _init.scaffold(checkout, "two", "langgraph", None)
    assert "key_variable" not in result and "service" not in result


def test_init_refuses_a_duplicate_a_bundled_id_and_a_bad_name(checkout):
    _init.scaffold(checkout, "my-agent", "langgraph", None)
    with pytest.raises(CliError, match="already exists"):
        _init.scaffold(checkout, "my-agent", "container-python", None)
    with pytest.raises(CliError, match="already exists"):
        _init.scaffold(checkout, "echo-v1", "container-python", None)
    with pytest.raises(CliError, match="not a valid agent id"):
        _init.scaffold(checkout, "Bad_Name", "langgraph", None)
    with pytest.raises(CliError, match="previous"):
        _init.scaffold(checkout, "foo-previous", "langgraph", None)
    with pytest.raises(CliError, match="unknown template"):
        _init.scaffold(checkout, "fine", "crewai", None)
    # A service name already in the fragment is refused too, even for a
    # fresh agent id: compose would merge the two blocks into one.
    with pytest.raises(CliError, match="already carries a service"):
        _init.scaffold(checkout, "echo-agent", "container-python", None)


def test_display_and_class_names_derive_from_the_id():
    assert _init.display_name("my-agent") == "My Agent"
    assert _init.class_name("my-agent") == "MyAgentAgent"
    assert _init.class_name("triage") == "TriageAgent"


# ---------------------------------------------------------------------------
# the rendered agents pass the batteries the examples pass — and fail
# with a reason when broken the way their READMEs say (D9, D10)
# ---------------------------------------------------------------------------


def _run_in_process_battery(agent_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "adapter_kit.in_process", "--agent-dir", str(agent_dir), "--json"],
        cwd=str(BACKEND), capture_output=True, text=True, timeout=600,
    )


@pytest.mark.skipif(
    importlib.util.find_spec("langgraph") is None,
    reason="langgraph is an adapter-side dependency; the chassis does not ship it",
)
def test_the_langgraph_template_passes_the_adapter_battery_and_fails_broken(checkout):
    """Through the in-process driver `librerun battery` runs — the
    subprocess is the CI command, not a re-implementation of it."""
    directory = _init.scaffold(checkout, "my-agent", "langgraph", None)["directory"]
    green = _run_in_process_battery(directory)
    assert green.returncode == 0, green.stdout + green.stderr
    assert '"passed": true' in green.stdout
    assert "[PASS] my-agent" in green.stdout

    # The README's break: an empty structured result.
    agent = directory / "agent.py"
    text = agent.read_text()
    anchor = r'    return \{\n        "structured": \{\n(?:.*\n)*?    \}\n'
    assert len(re.findall(anchor, text)) == 1
    agent.write_text(re.sub(anchor, '    return {"structured": {}}\n', text, count=1))
    red = _run_in_process_battery(directory)
    assert red.returncode == 1, red.stdout + red.stderr
    assert '"passed": false' in red.stdout
    assert "produced no structured output" in red.stdout, red.stdout


def test_the_driver_refuses_what_it_cannot_run(checkout):
    """Exit 2 and a reason, for a directory that is not an agent, a
    container agent, and an agent with no sample."""
    nothing = checkout / "backend" / "agents" / "nothing"
    nothing.mkdir()
    result = _run_in_process_battery(nothing)
    assert result.returncode == 2 and "carries no agent.yaml" in result.stderr

    container = _init.scaffold(checkout, "boxed", "container-python", None)["directory"]
    result = _run_in_process_battery(container)
    assert result.returncode == 2 and "cannot run in-process" in result.stderr

    if importlib.util.find_spec("langgraph") is not None:
        directory = _init.scaffold(checkout, "silent", "langgraph", None)["directory"]
        shutil.rmtree(directory / "scenarios")
        result = _run_in_process_battery(directory)
        assert result.returncode == 2 and "no scenario" in result.stderr


def _serve_rendered_agent(agent_dir: Path):
    """The rendered container-python agent, served in-thread on the SDK,
    exactly as `tests/test_run_contract_battery.py` serves the echo agent."""
    from librerun_agent.testing import serve_in_thread

    spec = importlib.util.spec_from_file_location(f"rendered_{agent_dir.name}_{os.getpid()}", agent_dir / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return serve_in_thread(module.app)


@pytest.mark.asyncio
async def test_the_container_python_template_passes_the_run_contract_battery_and_fails_broken(checkout, monkeypatch):
    from adapter_kit.run_contract import run_contract_battery

    # No gateway and no MCP server here: the template must degrade to
    # its rule, never die — that is what the battery drives.
    monkeypatch.setenv("LIBRERUN_GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("OPENAI_API_KEY", "lr_agent_battery")
    directory = _init.scaffold(checkout, "my-agent", "container-python", None)["directory"]
    manifest = load_manifest(directory)
    scenario = json.loads(next((directory / "scenarios").glob("*.json")).read_text())["user_inputs"]

    handle = _serve_rendered_agent(directory)
    try:
        result = await run_contract_battery(handle.url, manifest=manifest, scenario=scenario)
    finally:
        handle.stop()
    assert result.passed, result.summary()
    assert result.checks["completed"] == "pass" and result.checks["token_binding"] == "pass"
    assert result.output["answer_source"] == "rules"
    assert result.output["question"] == scenario["question"]

    # The README's break: a list where the phase output should be.
    agent = directory / "agent.py"
    text = agent.read_text()
    anchor = r'    return \{\n        "question": question,\n(?:.*\n)*?    \}\n'
    assert len(re.findall(anchor, text)) == 1
    agent.write_text(re.sub(anchor, '    return ["broken"]\n', text, count=1))
    handle = _serve_rendered_agent(directory)
    try:
        broken = await run_contract_battery(handle.url, manifest=manifest, scenario=scenario)
    finally:
        handle.stop()
    assert not broken.passed
    assert any("must return a dict" in failure for failure in broken.failures), broken.failures


@pytest.mark.asyncio
async def test_the_container_python_template_reads_an_unset_tool_secret_as_none(checkout):
    """K8b: the template's ``_tool_secret`` is ``ctx.secrets.get`` with the
    one refusal that is a state — a declared name nobody set — read as
    ``None``; a name the manifest does not declare is a typo and still
    raises. The template declares no name (the manifest's line is a
    comment), so the handler never calls it."""
    from librerun_agent import SecretNotDeclared, SecretNotSet

    directory = _init.scaffold(checkout, "my-agent", "container-python", None)["directory"]
    assert load_manifest(directory).secrets == []
    assert "# secrets: [search_api_key]" in (directory / "agent.yaml").read_text()
    spec = importlib.util.spec_from_file_location(f"rendered_secret_{os.getpid()}", directory / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class _Secrets:
        def __init__(self, answer):
            self.answer, self.asked = answer, []

        async def get(self, name):
            self.asked.append(name)
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    def ctx(answer):
        return SimpleNamespace(secrets=_Secrets(answer))

    held = ctx("a-value-of-the-tenant")
    assert await module._tool_secret(held, "search_api_key") == "a-value-of-the-tenant"
    assert held.secrets.asked == ["search_api_key"]
    assert await module._tool_secret(ctx(SecretNotSet(-32006, "secret_not_set: search_api_key")), "search_api_key") is None
    with pytest.raises(SecretNotDeclared):
        await module._tool_secret(ctx(SecretNotDeclared(-32005, "secret_not_declared: typo")), "typo")
    assert "_tool_secret(" not in (directory / "agent.py").read_text().split("async def handler", 1)[1]


def test_the_container_ts_template_ships_what_its_battery_needs(checkout):
    """Node is not part of the chassis suite; the CI matrix builds and
    drives the TypeScript template. What can be held here is its shape:
    the four endpoints, the per-invocation client, the step id, the
    type-checker script the matrix runs, and the derivation from the
    reference server."""
    directory = _init.scaffold(checkout, "my-agent", "container-ts", None)["directory"]
    server = (directory / "server.ts").read_text()
    for endpoint in ('path === "/healthz"', 'path === "/v1/runs"', "(events|output)"):
        assert endpoint in server
    assert "createOpenAI({ baseURL: GATEWAY_URL, apiKey: AGENT_KEY, headers, fetch:" in server
    assert 'const STEP = "answer"' in server and "librerun/${STEP}" in server
    assert '"X-LibreRun-Run-Token": bearer' in server
    assert "zod" not in server
    # K8b: the tool-secret helper — MCP secret_get with the invocation's
    # bearer, null for -32006 secret_not_set, an Error naming any other
    # code (-32005 secret_not_declared by name) — logs, emits and returns
    # nothing of the value but to its caller, and nothing calls it until
    # a name is declared.
    helper = re.search(r"\nasync function secretGet\((.*?)\n}\n", server, re.S)
    assert helper, "server.ts has no secretGet helper"
    body = helper.group(0)
    assert "mcpUrl: string | undefined, bearer: string, name: string): Promise<string | null>" in body
    assert 'params: { name: "secret_get", arguments: { name } }' in body
    assert "authorization: `Bearer ${bearer}`" in body
    assert "body.error?.code === -32006) return null" in body
    assert "body.error.code === -32005 ? \"-32005 secret_not_declared\"" in body
    for leak in ("console.", "emit(", "inv.output"):
        assert leak not in body, leak
    assert server.count("secretGet(") == 1, "the template declares no secret, so nothing calls the helper"
    manifest = (directory / "agent.yaml").read_text()
    assert "# secrets: [search_api_key]" in manifest and load_manifest(directory).secrets == []
    package = json.loads((directory / "package.json").read_text())
    assert package["scripts"]["typecheck"] == "tsc --noEmit"
    reference = json.loads((REPO / "backend/agents/_examples/vercel_ai_answer_ts/package.json").read_text())
    assert package["dependencies"] == reference["dependencies"], "the template pins what the reference server pins"
    dockerfile = (directory / "Dockerfile").read_text()
    assert "backend/agents/my_agent/server.ts" in dockerfile and "--omit=dev" in dockerfile


# ---------------------------------------------------------------------------
# key rotate: the .env half, pure
# ---------------------------------------------------------------------------


def _env_with(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text)
    return _env.DotEnv(path, environ={})


def test_rotate_moves_the_old_value_to_previous_and_mints_a_new_one(tmp_path):
    env = _env_with(tmp_path, "A=1\nLIBRERUN_AGENT_KEY_ECHO_V1=lr_agent_old\nB=2\n")
    variable, fresh = _rotate.rotate(env, "echo-v1")
    assert variable == "LIBRERUN_AGENT_KEY_ECHO_V1" and fresh.startswith("lr_agent_") and fresh != "lr_agent_old"
    values = _env.parse(env.text, environ={})
    assert values["LIBRERUN_AGENT_KEY_ECHO_V1"] == fresh
    assert values["LIBRERUN_AGENT_KEY_ECHO_V1_PREVIOUS"] == "lr_agent_old"
    assert values["A"] == "1" and values["B"] == "2"
    # In flight: a second rotation is refused until --finish.
    with pytest.raises(CliError, match="already in flight"):
        _rotate.rotate(env, "echo-v1")
    assert _rotate.finish(env, "echo-v1") == "LIBRERUN_AGENT_KEY_ECHO_V1_PREVIOUS"
    values = _env.parse(env.text, environ={})
    assert "LIBRERUN_AGENT_KEY_ECHO_V1_PREVIOUS" not in values
    assert values["LIBRERUN_AGENT_KEY_ECHO_V1"] == fresh
    with pytest.raises(CliError, match="no rotation"):
        _rotate.finish(env, "echo-v1")


def test_rotate_refuses_an_unprovisioned_key(tmp_path):
    env = _env_with(tmp_path, "A=1\n")
    with pytest.raises(CliError, match="not provisioned"):
        _rotate.rotate(env, "echo-v1")
    env = _env_with(tmp_path, "LIBRERUN_AGENT_KEY_ECHO_V1=\n")
    with pytest.raises(CliError, match="not provisioned"):
        _rotate.rotate(env, "echo-v1")


def test_rotate_and_finish_refuse_a_key_the_shell_exports(tmp_path):
    """Since K3 the environment outranks .env for compose and compose.sh
    alike: an edit to the file under an exported name would recreate both
    containers on the old key and report a rotation that never happened.
    Both steps refuse, and the file is left as it was."""
    path = tmp_path / ".env"
    path.write_text("LIBRERUN_AGENT_KEY_ECHO_V1=lr_agent_old\n")
    env = _env.DotEnv(path, environ={"LIBRERUN_AGENT_KEY_ECHO_V1": "lr_agent_shell"})
    with pytest.raises(CliError, match="exported in this shell"):
        _rotate.rotate(env, "echo-v1")
    assert env.text == "LIBRERUN_AGENT_KEY_ECHO_V1=lr_agent_old\n"
    path.write_text("LIBRERUN_AGENT_KEY_ECHO_V1=lr_agent_new\nLIBRERUN_AGENT_KEY_ECHO_V1_PREVIOUS=lr_agent_old\n")
    for exported in (
        {"LIBRERUN_AGENT_KEY_ECHO_V1_PREVIOUS": "lr_agent_old"},
        {"LIBRERUN_AGENT_KEY_ECHO_V1": "lr_agent_new"},
    ):
        env = _env.DotEnv(path, environ=exported)
        with pytest.raises(CliError, match="exported in this shell"):
            _rotate.finish(env, "echo-v1")
        assert "LIBRERUN_AGENT_KEY_ECHO_V1_PREVIOUS=lr_agent_old" in env.text
    # Another agent's exported key is no reason to refuse this one.
    env = _env.DotEnv(path, environ={"LIBRERUN_AGENT_KEY_OTHER": "lr_agent_x"})
    assert _rotate.finish(env, "echo-v1") == "LIBRERUN_AGENT_KEY_ECHO_V1_PREVIOUS"


def test_the_service_is_resolved_from_the_label_or_refused_by_reason():
    assert _rotate.resolve_service(REPO, "echo-v1", None) == "echo-agent"
    assert _rotate.resolve_service(REPO, "echo-v1", "custom") == "custom"
    with pytest.raises(CliError, match="python-package agent"):
        _rotate.resolve_service(REPO, "langgraph-triage", None)
    with pytest.raises(CliError, match="Pass --service"):
        _rotate.resolve_service(REPO, "nobody", None)


def test_rotate_recreates_without_building(checkout, monkeypatch):
    """A key changes a container's environment, not its image: both steps
    recreate with compose's ``--no-build`` (A2), which every service that
    builds would otherwise do on ``up`` (``pull_policy: build``, #133)."""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(_rotate._compose, "run", lambda root, *args, **kwargs: calls.append(args))
    (checkout / ".env").write_text("LIBRERUN_AGENT_KEY_ECHO_V1=lr_agent_old\n")

    def rotate(**flags) -> None:
        args = SimpleNamespace(agent_id="echo-v1", finish=False, service=None, no_up=False)
        assert _rotate.cmd_key_rotate(checkout, SimpleNamespace(**{**vars(args), **flags})) == 0

    rotate()
    rotate(finish=True)
    assert calls == [
        ("up", "-d", "--no-build", "gateway", "echo-agent"),
        ("up", "-d", "--no-build", "gateway"),
    ]


def test_prefix_of_never_shows_more_than_the_admin_page():
    assert _rotate.prefix_of("lr_agent_abcdefghijklmnop") == "abcdefgh"


# ---------------------------------------------------------------------------
# doctor: loud without an engine, quiet about values
# ---------------------------------------------------------------------------


def _cli(root: Path, *args: str, env: dict | None = None, input: str | None = None) -> subprocess.CompletedProcess:
    merged = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"), "PYTHONPATH": str(CLI_SRC), "PYTHONDONTWRITEBYTECODE": "1"}
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-m", "librerun", "--root", str(root), *args],
        capture_output=True, text=True, timeout=120, env=merged, input=input,
    )


@pytest.mark.parametrize("args", [("--pull",), ("--env-only", "--pull")])
def test_demo_refuses_pull_with_one_line(checkout, args):
    """``--pull`` retired with image publishing (A2; D26). Hidden from the
    help and refused in one line with exit 2 before ``.env`` is read or
    written — until 1.1.0, when the flag goes."""
    before = sorted(str(p.relative_to(checkout)) for p in checkout.rglob("*"))
    result = _cli(checkout, "demo", *args)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert result.stderr.splitlines() == [
        "librerun: --pull is gone: a LibreRun release is source only, and `librerun demo` builds it"
    ]
    assert result.stdout == ""
    assert not (checkout / ".env").exists()
    assert sorted(str(p.relative_to(checkout)) for p in checkout.rglob("*")) == before
    assert "--pull" not in _cli(checkout, "demo", "--help").stdout


def test_doctor_fails_loudly_without_an_engine(checkout):
    """PATH with no docker and no podman: exit 1, and the first section
    says why. The rest of the report still runs, so the failure names
    the engine rather than hiding behind a later section."""
    result = _cli(checkout, "doctor", env={"PATH": "/nonexistent"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "[FAIL] no container engine" in result.stdout
    assert "Agents on disk" in result.stdout and "vita-v1" in result.stdout
    assert "the stack checks are skipped" in result.stdout


def test_doctor_names_provider_keys_but_never_their_values(checkout):
    (checkout / "gateway.env").write_text("OPENAI_API_KEY=sk-the-secret-value\nANTHROPIC_API_KEY=\n")
    (checkout / ".env").write_text("LIBRERUN_DEMO=true\nLIBRERUN_STUB_LLM=true\nGOOGLE_AI_API_KEY=AIza-stale\n")
    result = _cli(checkout, "doctor", env={"PATH": "/nonexistent"})
    assert "provider keys named: OPENAI_API_KEY" in result.stdout
    assert "sk-the-secret-value" not in result.stdout and "AIza-stale" not in result.stdout
    assert "GOOGLE_AI_API_KEY set in the root .env" in result.stdout
    assert "reaches nothing" in result.stdout


def test_doctor_flags_a_rotation_in_flight_and_a_provider_key_in_an_agents_variable(checkout):
    (checkout / ".env").write_text(
        "LIBRERUN_AGENT_KEY_ECHO_V1=lr_agent_a\nLIBRERUN_AGENT_KEY_ECHO_V1_PREVIOUS=lr_agent_b\n"
        "LIBRERUN_AGENT_KEY_VERCEL_ANSWER=sk-not-an-agent-key\n"
    )
    result = _cli(checkout, "doctor", env={"PATH": "/nonexistent"})
    assert "rotation is in flight" in result.stdout
    assert "[FAIL] vercel-answer" in result.stdout and "does not begin lr_agent_" in result.stdout
    assert "sk-not-an-agent-key" not in result.stdout


def test_doctor_reads_mode_b_as_designed_and_not_as_a_missing_env(checkout):
    """K3's note to S6: no .env on disk with the LIBRERUN_AGENT_KEY_* names
    in the environment is `sops exec-env` running as designed. Without the
    names it is a missing file, as before."""
    assert not (checkout / ".env").exists()
    bare = _cli(checkout, "doctor", env={"PATH": "/nonexistent"})
    assert "[warn] no .env:" in bare.stdout
    assert any("echo-v1" in line and "not provisioned" in line for line in bare.stdout.splitlines())
    mode_b = _cli(
        checkout,
        "doctor",
        env={
            "PATH": "/nonexistent",
            "LIBRERUN_DEMO": "true",
            "LIBRERUN_STUB_LLM": "true",
            "LIBRERUN_AGENT_KEY_ECHO_V1": "lr_agent_from-the-shell-0000",
            "LIBRERUN_AGENT_KEY_LLAMAINDEX_SUMMARIZE": "lr_agent_from-the-shell-0001",
            "LIBRERUN_AGENT_KEY_VERCEL_ANSWER": "lr_agent_from-the-shell-0002",
        },
    )
    assert "no .env on disk, and 3 agent key variable(s) in the environment" in mode_b.stdout
    assert "running as designed, not a missing file" in mode_b.stdout
    assert "[warn] no .env:" not in mode_b.stdout
    assert any("echo-v1" in line and "key provisioned" in line for line in mode_b.stdout.splitlines())
    assert "not provisioned" not in mode_b.stdout
    assert "from-the-shell" not in mode_b.stdout


def test_doctor_says_when_the_shell_outranks_a_key_line_in_env(checkout):
    (checkout / ".env").write_text("LIBRERUN_DEMO=true\nLIBRERUN_STUB_LLM=true\nLIBRERUN_AGENT_KEY_ECHO_V1=lr_agent_file\n")
    differs = _cli(checkout, "doctor", env={"PATH": "/nonexistent", "LIBRERUN_AGENT_KEY_ECHO_V1": "lr_agent_shell"})
    assert "[warn] LIBRERUN_AGENT_KEY_ECHO_V1 is exported in this shell and differs" in differs.stdout
    assert "the environment wins" in differs.stdout and "key rotate` refuses" in differs.stdout
    assert "lr_agent_shell" not in differs.stdout and "lr_agent_file" not in differs.stdout
    same = _cli(checkout, "doctor", env={"PATH": "/nonexistent", "LIBRERUN_AGENT_KEY_ECHO_V1": "lr_agent_file"})
    assert "with the same value as its .env line" in same.stdout and "differs" not in same.stdout
    extra = _cli(checkout, "doctor", env={"PATH": "/nonexistent", "LIBRERUN_AGENT_KEY_SOMEONE_ELSE": "lr_agent_x"})
    assert "LIBRERUN_AGENT_KEY_SOMEONE_ELSE comes from this shell and not from .env" in extra.stdout


def test_doctor_follows_the_gateway_env_file_variable(checkout, tmp_path):
    """K3's note to S6: gateway.env present, absent, or supplied through
    LIBRERUN_GATEWAY_ENV_FILE — and, when the variable is set, whether the
    file it names exists and is owner-only. Honoured only when this
    compose.yaml reads the variable; an older one leaves the gateway on
    gateway.env, and the report says so instead of pretending."""
    (checkout / ".env").write_text("LIBRERUN_DEMO=true\nLIBRERUN_STUB_LLM=true\n")
    missing = _cli(checkout, "doctor", env={"PATH": "/nonexistent", "LIBRERUN_GATEWAY_ENV_FILE": str(tmp_path / "nowhere.env")})
    assert "[warn] LIBRERUN_GATEWAY_ENV_FILE names" in missing.stdout and "which does not exist" in missing.stdout
    assert "sops exec-file" in missing.stdout
    named = tmp_path / "decrypted.env"
    named.write_text("OPENAI_API_KEY=sk-through-the-variable\n")
    named.chmod(0o644)
    loose = _cli(checkout, "doctor", env={"PATH": "/nonexistent", "LIBRERUN_GATEWAY_ENV_FILE": str(named)})
    assert f"supplied through LIBRERUN_GATEWAY_ENV_FILE={named}" in loose.stdout
    assert "provider keys named: OPENAI_API_KEY" in loose.stdout and "sk-through-the-variable" not in loose.stdout
    assert f"chmod 600 {named}" in loose.stdout
    named.chmod(0o600)
    tight = _cli(checkout, "doctor", env={"PATH": "/nonexistent", "LIBRERUN_GATEWAY_ENV_FILE": str(named)})
    assert f"chmod 600 {named}" not in tight.stdout and "provider keys named: OPENAI_API_KEY" in tight.stdout
    compose = checkout / "compose.yaml"
    older = compose.read_text().replace("${LIBRERUN_GATEWAY_ENV_FILE:-gateway.env}", "gateway.env")
    assert older != compose.read_text(), "the repo's compose.yaml reads the variable (K3); the probe removes that"
    compose.write_text(older)
    unread = _cli(checkout, "doctor", env={"PATH": "/nonexistent", "LIBRERUN_GATEWAY_ENV_FILE": str(named)})
    assert "[warn] LIBRERUN_GATEWAY_ENV_FILE is set but this compose.yaml does not read it" in unread.stdout
    assert "supplied through" not in unread.stdout and "gateway.env absent" in unread.stdout


def test_the_root_is_found_from_a_subdirectory_or_named_or_refused(checkout, tmp_path):
    deep = checkout / "backend" / "agents"
    result = subprocess.run(
        [sys.executable, "-m", "librerun", "doctor"], cwd=str(deep), capture_output=True, text=True, timeout=120,
        env={"PATH": "/nonexistent", "HOME": "/tmp", "PYTHONPATH": str(CLI_SRC), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert f"librerun doctor — {checkout}" in result.stdout
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "librerun", "doctor"], cwd=str(elsewhere), capture_output=True, text=True, timeout=120,
        env={"PATH": "/nonexistent", "HOME": "/tmp", "PYTHONPATH": str(CLI_SRC), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 1 and "not inside a LibreRun checkout" in result.stderr


# ---------------------------------------------------------------------------
# run: against a stand-in backend
# ---------------------------------------------------------------------------


class _FakeBackend(BaseHTTPRequestHandler):
    """Enough of the API for `run` and for doctor's sign-in: health, meta,
    login, /auth/me, agents, scenarios, submit, and a run that goes
    running -> complete (or error) on the second read. A stand-in, not a
    mock of the chassis: it answers the same routes with the same shapes.
    Every request is recorded, so a test can say what was NOT sent."""

    outcome = "complete"
    reads = 0
    # K4b: the password it accepts, what /api/v1/meta answers as, what
    # /auth/me says about the platform, and paths answered with a 302.
    password = "pw"
    name = "LibreRun"
    platform_admin = True
    redirects: dict = {}
    requests: list = []
    signed_in = None
    # K6: what GET /api/v1/admin/settings answers a platform admin.
    settings: list = []
    # K7: what GET /api/v1/admin/providers answers a platform admin.
    providers: dict = {}
    # K9: what GET /api/v1/admin/deployment answers a platform admin.
    deployment: dict = {}

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _seen(self, body=None) -> bool:
        """Record the request; answer the redirect configured for it."""
        type(self).requests.append((self.command, self.path, self.headers.get("Authorization"), body))
        target = type(self).redirects.get(self.path)
        if target:
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
        return bool(target)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self._seen(body):
            return
        if self.path == "/api/v1/auth/login":
            if body.get("password") != type(self).password:
                return self._json(401, {"detail": "bad credentials"})
            type(self).signed_in = body.get("email")
            return self._json(200, {"access_token": "tok"})
        if self.path.startswith("/api/v1/runs?agent_id=my-agent"):
            if self.headers.get("Authorization") != "Bearer tok":
                return self._json(401, {})
            type(self).submitted = body
            return self._json(201, {"run_id": "11111111-1111-1111-1111-111111111111", "run_number": "RUN-7", "status": "running"})
        self._json(404, {})

    def do_GET(self):
        if self._seen():
            return
        if self.path == "/api/v1/health":
            return self._json(200, {"status": "ok", "pii_detector": {"state": "ready", "coverage": "full"}})
        if self.path == "/api/v1/meta":
            return self._json(200, {"name": type(self).name, "version": "0", "demo": True, "stub_llm": True,
                                    "gateway": "ok", "agents": [{"id": "my-agent"}], "trace_viewer_configured": False})
        if self.path == "/api/v1/auth/me":
            if self.headers.get("Authorization") != "Bearer tok":
                return self._json(401, {"detail": "Missing bearer token"})
            return self._json(200, {"id": "22222222-2222-2222-2222-222222222222", "email": type(self).signed_in,
                                    "role": "admin", "tenant_id": "33333333-3333-3333-3333-333333333333",
                                    "display_name": None, "is_platform_admin": type(self).platform_admin})
        if self.path == "/api/v1/admin/settings":
            if self.headers.get("Authorization") != "Bearer tok":
                return self._json(401, {"detail": "Missing bearer token"})
            return self._json(200, type(self).settings)
        if self.path == "/api/v1/admin/providers":
            if self.headers.get("Authorization") != "Bearer tok":
                return self._json(401, {"detail": "Missing bearer token"})
            return self._json(200, type(self).providers)
        if self.path == "/api/v1/admin/deployment":
            if self.headers.get("Authorization") != "Bearer tok":
                return self._json(401, {"detail": "Missing bearer token"})
            if not type(self).platform_admin:
                return self._json(403, {"detail": "Platform operator only"})
            return self._json(200, type(self).deployment)
        if self.path == "/api/v1/agents":
            return self._json(200, [{"agent_id": "my-agent", "display_name": "My Agent"}])
        if self.path == "/api/v1/agents/my-agent/scenarios":
            return self._json(200, [{"id": "demo", "name": "Demo", "user_inputs": {"question": "q"}}])
        if self.path.startswith("/api/v1/runs/11111111"):
            type(self).reads += 1
            state = "running" if type(self).reads < 2 else type(self).outcome
            return self._json(200, {"run_id": "11111111-1111-1111-1111-111111111111", "status": state, "error": "boom" if state == "error" else None})
        self._json(404, {})


@pytest.fixture
def fake_backend():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBackend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _FakeBackend.reads = 0
    _FakeBackend.outcome = "complete"
    _FakeBackend.password = "pw"
    _FakeBackend.name = "LibreRun"
    _FakeBackend.platform_admin = True
    _FakeBackend.redirects = {}
    _FakeBackend.requests = []
    _FakeBackend.signed_in = None
    _FakeBackend.settings = []
    _FakeBackend.providers = {}
    _FakeBackend.deployment = {}
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


def test_run_submits_the_sample_and_follows_it_to_complete(checkout, fake_backend, monkeypatch):
    (checkout / ".env").write_text("INITIAL_ADMIN_EMAIL=a@b\nINITIAL_ADMIN_PASSWORD=pw\nFRONTEND_PORT=3001\n")
    monkeypatch.setattr("librerun._run.time.sleep", lambda s: None)
    from librerun._run import cmd_run

    args = SimpleNamespace(agent="my-agent", scenario="demo", wait=True, approve=False, timeout=30, base_url=fake_backend, email=None, password=None, password_stdin=False)
    assert cmd_run(checkout, args) == 0
    assert _FakeBackend.submitted == {"question": "q"}

    _FakeBackend.reads = 0
    _FakeBackend.outcome = "error"
    assert cmd_run(checkout, args) == 1

    with pytest.raises(CliError, match="no scenario 'other'"):
        cmd_run(checkout, SimpleNamespace(**{**vars(args), "scenario": "other"}))
    with pytest.raises(CliError, match="login failed"):
        cmd_run(checkout, SimpleNamespace(**{**vars(args), "password": "wrong"}))


def test_run_needs_credentials_it_never_guesses(checkout, fake_backend, monkeypatch):
    from librerun._run import cmd_run

    monkeypatch.delenv("LIBRERUN_EMAIL", raising=False)
    monkeypatch.delenv("LIBRERUN_PASSWORD", raising=False)
    args = SimpleNamespace(agent="my-agent", scenario=None, wait=False, approve=False, timeout=30, base_url=fake_backend, email=None, password=None, password_stdin=False)
    with pytest.raises(CliError, match="no credentials"):
        cmd_run(checkout, args)


# ---------------------------------------------------------------------------
# up: the backend's health is not the gateway's
# ---------------------------------------------------------------------------


class _GatewayLate(BaseHTTPRequestHandler):
    """A backend that is healthy at once and hears from its gateway only
    from its `answers_from`-th /api/v1/meta read on — the order the
    template-matrix race of 2026-09-28 met (the K blueprint's §11, T1)."""

    metas = 0
    answers_from = 4

    def log_message(self, *args):
        pass

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/v1/health":
            return self._json(200, {"status": "ok"})
        if self.path == "/api/v1/meta":
            type(self).metas += 1
            up = type(self).metas >= type(self).answers_from
            return self._json(200, {"name": "LibreRun", "version": "0", "demo": True,
                                    "stub_llm": True if up else None, "gateway": "ok" if up else "unreachable",
                                    "trace_viewer_configured": False})
        self._json(404, {})


def test_up_returns_only_once_the_gateway_answers(checkout, no_proxy, monkeypatch, capsys):
    """`librerun up` waits for the gateway as it waits for the backend, so a
    run started the moment it returns reaches a gateway that is up; one that
    never answers is waited for a bounded time and reported, never fatal."""
    from librerun import _stack

    server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayLate)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for name in ("BACKEND_PORT", "LIBRERUN_DEMO", "INITIAL_ADMIN_EMAIL"):
            monkeypatch.delenv(name, raising=False)
        (checkout / ".env").write_text(f"BACKEND_PORT=127.0.0.1:{server.server_address[1]}\n")
        monkeypatch.setattr(_stack, "compose_up", lambda *args, **kwargs: None)
        monkeypatch.setattr(_stack.time, "sleep", lambda seconds: None)
        _GatewayLate.metas, _GatewayLate.answers_from = 0, 4
        assert _stack.cmd_up(checkout, SimpleNamespace(no_build=True, no_wait=False, quiet=False)) == 0
        out = capsys.readouterr().out
        assert "gateway is up" in out and "The LLM is a stub" in out, out
        assert "did not answer" not in out, out
        assert _GatewayLate.metas == 5, _GatewayLate.metas  # four waits, then the summary's read
        # A gateway that never answers: the wait ends at its bound and says so.
        _GatewayLate.metas, _GatewayLate.answers_from = 0, 10**6
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert _stack.wait_for_gateway(base, seconds=0) is False
        assert "the gateway did not answer within 0 seconds (/api/v1/meta says unreachable)" in capsys.readouterr().err
    finally:
        server.shutdown()


def test_up_no_build_passes_compose_no_build(checkout, monkeypatch):
    """``librerun up --no-build`` builds nothing and pulls nothing: with
    every service at ``pull_policy: build`` (#133) a plain ``up`` builds,
    so the flag is compose's own ``--no-build`` (A2), and without it
    ``--build`` says out loud what compose does anyway."""
    from librerun import _stack

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(_stack._compose, "run", lambda root, *args, **kwargs: calls.append(args))
    monkeypatch.setattr(_stack, "top_up_keys", lambda env, root: [])
    (checkout / ".env").write_text("APP_SECRET_KEY=x\n")
    for no_build in (True, False):
        assert _stack.cmd_up(checkout, SimpleNamespace(no_build=no_build, no_wait=True, quiet=True)) == 0
    assert calls == [("up", "-d", "--no-build"), ("up", "-d", "--build")]
    help_text = _cli(checkout, "up", "--help").stdout
    assert "build nothing, pull nothing" in " ".join(help_text.split())


# ---------------------------------------------------------------------------
# credentials (K4b, #134): no password on argv for doctor, nothing sent
# until the answer is LibreRun's and this checkout's, no redirect followed
# ---------------------------------------------------------------------------

SECRET = "K4b-s3cret-Passw0rd"


def _credentials_sent(requests) -> list:
    """The requests that carried a credential: the sign-in, or a token."""
    return [r for r in requests if (r[0], r[1]) == ("POST", "/api/v1/auth/login") or r[2]]


def test_doctor_refuses_an_argv_password(checkout, fake_backend):
    """A command line is in the process list, readable by every user of
    the machine, so doctor has no --password. With abbreviations off,
    `--password` is refused outright rather than read as a prefix of
    `--password-stdin` — and refused before anything runs or is sent."""
    for extra in (["--password", "x"], ["--password"], ["--passw"]):
        result = _cli(checkout, "doctor", "--base-url", fake_backend, "--email", "a@b", *extra,
                      env={"PATH": "/nonexistent"})
        assert result.returncode == 2, (extra, result.stdout, result.stderr)
        assert "unrecognized arguments" in result.stderr, result.stderr
        assert "librerun doctor —" not in result.stdout
    assert _FakeBackend.requests == []


def test_doctor_signs_in_and_names_the_user(checkout, fake_backend):
    _FakeBackend.password = SECRET
    piped = _cli(checkout, "doctor", "--base-url", fake_backend, "--email", "a@b", "--password-stdin",
                 env={"PATH": "/nonexistent"}, input=SECRET + "\n")
    assert "[ok]   signed in as a@b; platform admin: yes" in piped.stdout, piped.stdout + piped.stderr
    assert SECRET not in piped.stdout + piped.stderr
    # The meta check, then the sign-in, then /auth/me — in that order.
    calls = [(method, path) for method, path, *_ in _FakeBackend.requests]
    meta, login, me = ("GET", "/api/v1/meta"), ("POST", "/api/v1/auth/login"), ("GET", "/api/v1/auth/me")
    assert calls.index(meta) < calls.index(login) < calls.index(me), calls
    # The named stack needs no engine here: only this machine's checks skip.
    assert "the gateway and trace checks are skipped" in piped.stdout

    # The two variables, and an admin of another tenant.
    _FakeBackend.platform_admin = False
    from_env = _cli(checkout, "doctor", "--base-url", fake_backend,
                    env={"PATH": "/nonexistent", "LIBRERUN_EMAIL": "ops@b", "LIBRERUN_PASSWORD": SECRET})
    assert "signed in as ops@b; platform admin: no" in from_env.stdout, from_env.stdout
    assert SECRET not in from_env.stdout + from_env.stderr

    # No credentials: a line on how to sign in, and nothing carrying one.
    before = len(_FakeBackend.requests)
    bare = _cli(checkout, "doctor", "--base-url", fake_backend, env={"PATH": "/nonexistent"})
    assert "[ok]   not signed in (no credentials given)" in bare.stdout and "--password-stdin" in bare.stdout
    assert _credentials_sent(_FakeBackend.requests[before:]) == []


def test_doctor_reports_the_store_key(checkout, fake_backend):
    """K6 (D33): the secrets store's key is set, blank or malformed — said
    in any mode, and never the key — and, signed in as a platform admin,
    the secret settings whose row no configured key opens are counted from
    the API, by name. No value is printed on any path."""
    import base64

    def key() -> str:
        return base64.urlsafe_b64encode(os.urandom(32)).decode()

    def doctor(env_text: str, *extra: str, **kwargs) -> subprocess.CompletedProcess:
        (checkout / ".env").write_text("LIBRERUN_DEMO=true\nLIBRERUN_STUB_LLM=true\n" + env_text)
        return _cli(checkout, "doctor", *extra, env={"PATH": "/nonexistent"}, **kwargs)

    one, two = key(), key()
    result = doctor(f"LIBRERUN_BACKEND_SECRETS_KEY={one}\n")
    assert "[ok]   LIBRERUN_BACKEND_SECRETS_KEY is set: 1 Fernet key(s)" in result.stdout, result.stdout
    result = doctor(f"LIBRERUN_BACKEND_SECRETS_KEY={two},{one}\n")
    assert "is set: 2 Fernet key(s); the first seals" in result.stdout
    assert one not in result.stdout and two not in result.stdout

    result = doctor("LIBRERUN_BACKEND_SECRETS_KEY_FILE=/run/secrets/backend_secrets_key\n")
    assert "[ok]   LIBRERUN_BACKEND_SECRETS_KEY is set, from a file" in result.stdout

    result = doctor("LIBRERUN_BACKEND_SECRETS_KEY=\n")
    assert "[warn] LIBRERUN_BACKEND_SECRETS_KEY is blank" in result.stdout
    assert "503 secrets_store_unconfigured" in result.stdout

    hex_key = "ab" * 32
    result = doctor(f"LIBRERUN_BACKEND_SECRETS_KEY={one},{hex_key}\n")
    assert "[FAIL] LIBRERUN_BACKEND_SECRETS_KEY: entry 2 of 2 is not a Fernet key" in result.stdout
    assert result.returncode == 1 and hex_key not in result.stdout and one not in result.stdout

    # Signed in as a platform admin: the rows no configured key opens.
    _FakeBackend.password = SECRET
    _FakeBackend.settings = [
        {"key": "auth.azure_client_secret", "value_type": "secret", "value": None,
         "secret": {"set": True, "source": "unreadable", "fingerprint": None}},
        {"key": "kb.embed_model", "value_type": "string", "value": "openai/x", "secret": None},
    ]
    signed = ("--base-url", fake_backend, "--email", "a@b", "--password-stdin")
    result = doctor(f"LIBRERUN_BACKEND_SECRETS_KEY={one}\n", *signed, input=SECRET + "\n")
    assert "[warn] 1 secret setting(s) have a row no configured key opens: auth.azure_client_secret" in result.stdout, result.stdout
    assert SECRET not in result.stdout + result.stderr
    _FakeBackend.settings[0]["secret"] = {"set": True, "source": "runtime", "fingerprint": "0123456789ab"}
    result = doctor(f"LIBRERUN_BACKEND_SECRETS_KEY={one}\n", *signed, input=SECRET + "\n")
    assert "[ok]   secret settings: 1, none with a row the configured key cannot open" in result.stdout

    # Only a platform admin is asked; and the token follows no redirect.
    before = len(_FakeBackend.requests)
    _FakeBackend.platform_admin = False
    doctor(f"LIBRERUN_BACKEND_SECRETS_KEY={one}\n", *signed, input=SECRET + "\n")
    assert ("GET", "/api/v1/admin/settings") not in [(m, p) for m, p, *_ in _FakeBackend.requests[before:]]
    _FakeBackend.platform_admin = True
    elsewhere_handler = type("_Elsewhere", (_FakeBackend,), {"requests": [], "redirects": {}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), elsewhere_handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _FakeBackend.redirects = {
            "/api/v1/admin/settings": f"http://127.0.0.1:{server.server_port}/api/v1/admin/settings"
        }
        result = doctor(f"LIBRERUN_BACKEND_SECRETS_KEY={one}\n", *signed, input=SECRET + "\n")
        assert "the secret settings could not be read (/api/v1/admin/settings answered 302)" in result.stdout
        assert elsewhere_handler.requests == []
    finally:
        server.shutdown()


def test_doctor_reports_the_gateway_key(checkout, fake_backend):
    """K7 (D33, D34): the gateway's store key in gateway.env is set, blank,
    not a Fernet key, or one the backend's list also holds — never the key
    — and a key-less gateway.env is no warning, since the admin page can
    hold the provider keys. Signed in as a platform admin, doctor lists what
    the gateway holds and the fingerprint of the key it seals to, from the
    same address and token as the settings read, following no redirect."""
    import base64

    def key() -> str:
        return base64.urlsafe_b64encode(os.urandom(32)).decode()

    backend = key()

    def doctor(gateway_text: str | None, *extra: str, **kwargs) -> subprocess.CompletedProcess:
        (checkout / ".env").write_text(
            f"LIBRERUN_DEMO=true\nLIBRERUN_STUB_LLM=true\nLIBRERUN_BACKEND_SECRETS_KEY={backend}\n"
        )
        gateway = checkout / "gateway.env"
        if gateway_text is None:
            gateway.unlink(missing_ok=True)
        else:
            gateway.write_text(gateway_text)
            gateway.chmod(0o600)
        return _cli(checkout, "doctor", *extra, env={"PATH": "/nonexistent"}, **kwargs)

    one, two = key(), key()
    result = doctor(f"LIBRERUN_GATEWAY_SECRETS_KEY={one}\n")
    assert "[ok]   LIBRERUN_GATEWAY_SECRETS_KEY is set: 1 Fernet key(s), none of them the backend's" in result.stdout, result.stdout
    assert "naming no provider key: the keys pasted in Admin -> Settings serve" in result.stdout
    assert "[warn] " + str(checkout / "gateway.env") + " present but" not in result.stdout
    result = doctor(f"LIBRERUN_GATEWAY_SECRETS_KEY={two},{one}\nOPENAI_API_KEY=sk-in-the-file\n")
    assert "is set: 2 Fernet key(s), none of them the backend's; the first seals" in result.stdout
    assert "provider keys named: OPENAI_API_KEY" in result.stdout
    assert one not in result.stdout and two not in result.stdout and "sk-in-the-file" not in result.stdout

    result = doctor("LIBRERUN_GATEWAY_SECRETS_KEY_FILE=/run/secrets/gateway_secrets_key\n")
    assert "[ok]   LIBRERUN_GATEWAY_SECRETS_KEY is set, from a file" in result.stdout
    for blank in ("LIBRERUN_GATEWAY_SECRETS_KEY=\n", None):
        result = doctor(blank)
        assert "[warn] LIBRERUN_GATEWAY_SECRETS_KEY is blank" in result.stdout, result.stdout
        assert "cannot be pasted in Admin -> Settings" in result.stdout

    hex_key = "cd" * 32
    result = doctor(f"LIBRERUN_GATEWAY_SECRETS_KEY={one},{hex_key}\n")
    assert "[FAIL] LIBRERUN_GATEWAY_SECRETS_KEY: entry 2 of 2 is not a Fernet key" in result.stdout
    assert result.returncode == 1 and hex_key not in result.stdout
    result = doctor(f"LIBRERUN_GATEWAY_SECRETS_KEY={one},{backend}\n")
    assert "[FAIL] LIBRERUN_GATEWAY_SECRETS_KEY and LIBRERUN_BACKEND_SECRETS_KEY share a key" in result.stdout
    assert result.returncode == 1 and backend not in result.stdout and one not in result.stdout

    # Signed in as a platform admin: what the gateway holds, and its key.
    _FakeBackend.password = SECRET
    _FakeBackend.providers = _reported_providers()
    signed = ("--base-url", fake_backend, "--email", "a@b", "--password-stdin")
    result = doctor(f"LIBRERUN_GATEWAY_SECRETS_KEY={one}\n", *signed, input=SECRET + "\n")
    assert ("[ok]   model providers: openai from gateway.env; anthropic runtime · f00dfacecafe; "
            "google not set") in result.stdout, result.stdout
    assert f"[ok]   the gateway seals provider keys to {PROVIDERS_FINGERPRINT}" in result.stdout
    _FakeBackend.providers = _reported_providers(public_key_pem=None)
    _FakeBackend.providers["providers"][1].update(row="rejected", reason="unsealable", source="env")
    result = doctor(f"LIBRERUN_GATEWAY_SECRETS_KEY={one}\n", *signed, input=SECRET + "\n")
    assert "anthropic rejected (unsealable)" in result.stdout
    assert "[warn] anthropic: a stored key the gateway could not open" in result.stdout
    assert "[warn] the gateway published no key to seal to" in result.stdout
    _FakeBackend.providers = {"reported": False}
    result = doctor(f"LIBRERUN_GATEWAY_SECRETS_KEY={one}\n", *signed, input=SECRET + "\n")
    assert "[warn] the gateway has not reported what it holds yet" in result.stdout

    # Only a platform admin is asked; and the token follows no redirect.
    before = len(_FakeBackend.requests)
    _FakeBackend.platform_admin = False
    doctor(f"LIBRERUN_GATEWAY_SECRETS_KEY={one}\n", *signed, input=SECRET + "\n")
    assert ("GET", "/api/v1/admin/providers") not in [(m, p) for m, p, *_ in _FakeBackend.requests[before:]]
    _FakeBackend.platform_admin = True
    elsewhere_handler = type("_Elsewhere", (_FakeBackend,), {"requests": [], "redirects": {}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), elsewhere_handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _FakeBackend.redirects = {
            "/api/v1/admin/providers": f"http://127.0.0.1:{server.server_port}/api/v1/admin/providers"
        }
        result = doctor(f"LIBRERUN_GATEWAY_SECRETS_KEY={one}\n", *signed, input=SECRET + "\n")
        assert "the model providers could not be read (/api/v1/admin/providers answered 302)" in result.stdout
        assert elsewhere_handler.requests == []
    finally:
        server.shutdown()
    assert SECRET not in result.stdout + result.stderr


def test_nothing_is_sent_before_meta_says_librerun(checkout, fake_backend, monkeypatch):
    """Another program on the port is never handed a password: until
    /api/v1/meta answers 200 as LibreRun, neither doctor nor run signs in."""
    from librerun import _credentials
    from librerun._run import cmd_run

    _FakeBackend.password = SECRET
    _FakeBackend.name = "SomethingElse"
    result = _cli(checkout, "doctor", "--base-url", fake_backend, "--email", "a@b", "--password-stdin",
                  env={"PATH": "/nonexistent"}, input=SECRET + "\n")
    assert "[FAIL] not signed in:" in result.stdout, result.stdout
    assert "answered as 'SomethingElse', not as LibreRun; nothing was sent" in result.stdout
    assert _credentials_sent(_FakeBackend.requests) == []

    monkeypatch.setenv("LIBRERUN_PASSWORD", SECRET)
    args = SimpleNamespace(agent="my-agent", scenario=None, wait=False, approve=False, timeout=30,
                           base_url=fake_backend, email="a@b", password=None, password_stdin=False)
    with pytest.raises(_credentials.NothingSent, match="nothing was sent"):
        cmd_run(checkout, args)
    closed = f"http://127.0.0.1:{_free_port()}"
    with pytest.raises(_credentials.NothingSent, match="nothing answered"):
        _credentials.sign_in(closed, "a@b", SECRET)
    assert _credentials_sent(_FakeBackend.requests) == []

    # The control: the same stand-in, answering as LibreRun, takes the sign-in.
    _FakeBackend.name = "LibreRun"
    assert cmd_run(checkout, args) == 0
    assert len(_credentials_sent(_FakeBackend.requests)) >= 1


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _ps_record(working_dir, port: int, *, state: str = "running", service: str = "backend") -> dict:
    """One `docker compose ps --format json` record, as Compose 2.21+ prints
    it — the backend's by default, or the HTTPS edge's (T1)."""
    target = 8443 if service == "edge" else 8000
    return {
        "Service": service,
        "State": state,
        "Labels": f"com.docker.compose.project={Path(working_dir).name.lower()},"
                  f"com.docker.compose.project.working_dir={working_dir},"
                  f"com.docker.compose.service={service}",
        "Publishers": [{"URL": "127.0.0.1", "TargetPort": target, "PublishedPort": port, "Protocol": "tcp"}],
    }


def test_nothing_is_sent_to_a_stack_that_is_not_this_checkouts(checkout, fake_backend, monkeypatch, capsys, tmp_path):
    """#134 §1: another checkout's stack on the same port answers /meta as
    LibreRun too. With no --base-url, doctor asks the engine (`compose ps`,
    through compose.sh) whether this checkout's backend is running on the
    port before it sends a password — this project, this working directory
    (two clones with one directory name are one compose project), this
    port. The engine and compose are stood in; the stack answering is the
    stand-in backend."""
    from librerun import _compose

    port = int(fake_backend.rsplit(":", 1)[1])
    (checkout / ".env").write_text(f"BACKEND_PORT=127.0.0.1:{port}\nLIBRERUN_DEMO=true\nLIBRERUN_STUB_LLM=true\n")
    _FakeBackend.password = SECRET
    engine = {"name": "docker", "binary": "/usr/bin/docker", "daemon": True, "compose": "docker compose"}
    monkeypatch.setattr(_compose, "engine_report", lambda: {**engine, "engine": "docker", "tried": [engine]})
    ps = {"stdout": "", "returncode": 0}
    asked = []

    def fake_compose(root, *args, **kwargs):
        asked.append((args, kwargs.get("profiles")))
        return SimpleNamespace(returncode=ps["returncode"], stdout="Using: docker compose\n" + ps["stdout"],
                               stderr="no such service" if ps["returncode"] else "")

    monkeypatch.setattr(_compose, "run", fake_compose)

    def doctor() -> tuple[str, list]:
        monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
        before = len(_FakeBackend.requests)
        cli_module.main(["--root", str(checkout), "doctor", "--email", "a@b", "--password-stdin"])
        return capsys.readouterr().out, _FakeBackend.requests[before:]

    other_clone = tmp_path / "older" / checkout.name
    refused = {
        "no container of this project": ("", 0),
        "another clone of the same name": (json.dumps(_ps_record(other_clone, port)), 0),
        "this checkout's backend on another port": (json.dumps(_ps_record(checkout, port + 1)), 0),
        "this checkout's backend, stopped": (json.dumps(_ps_record(checkout, port, state="exited")), 0),
        "compose ps failing": ("", 1),
    }
    for label, (stdout, code) in refused.items():
        ps.update(stdout=stdout, returncode=code)
        out, seen = doctor()
        assert "[warn] not signed in:" in out and "so nothing was sent" in out, (label, out)
        assert "--base-url" in out, label
        assert _credentials_sent(seen) == [], label

    # The control, in each shape an engine prints: this checkout's backend
    # on this port is signed in to.
    mine = _ps_record(checkout, port)
    podman = {"Labels": {"com.docker.compose.service": "backend",
                         "com.docker.compose.project.working_dir": str(checkout)},
              "Ports": [{"host_ip": "127.0.0.1", "container_port": 8000, "host_port": port, "range": 1, "protocol": "tcp"}],
              "State": "running"}
    shapes = {
        "compose json lines": json.dumps({"Service": "frontend", "State": "running"}) + "\n" + json.dumps(mine),
        "compose array": json.dumps([mine]),
        "podman-compose array": "['podman', '--version', '']\n" + json.dumps([podman], indent=4),
    }
    for label, stdout in shapes.items():
        ps.update(stdout=stdout, returncode=0)
        out, seen = doctor()
        assert "[ok]   signed in as a@b; platform admin: yes" in out, (label, out)
        assert len(_credentials_sent(seen)) >= 1, label
    assert asked and all(args == ("ps", "--format", "json") and profiles == ("app",) for args, profiles in asked)

    # The port is not the stack (Codex, PR #158): an .env edited since `up`
    # can name another HOST on the same port. The container must publish on
    # an address the base URL's host reaches, and the credential goes to
    # that address. 192.0.2.10 is TEST-NET-1, never this machine's.
    from librerun import _credentials

    elsewhere = f"http://192.0.2.10:{port}"
    wildcard = {**mine, "Publishers": [{"URL": "0.0.0.0", "TargetPort": 8000, "PublishedPort": port, "Protocol": "tcp"}]}
    for label, record in (("published on 127.0.0.1", mine), ("published on every address", wildcard)):
        ps.update(stdout=json.dumps(record), returncode=0)
        pinned, why = _credentials.this_checkouts_backend(checkout, elsewhere)
        assert pinned is None and "192.0.2.10 does not reach" in why, (label, why)
    ps.update(stdout=json.dumps(wildcard), returncode=0)
    pinned, _ = _credentials.this_checkouts_backend(checkout, f"http://localhost:{port}")
    assert pinned == f"http://127.0.0.1:{port}"
    v6_only = {**mine, "Publishers": [{"URL": "::", "TargetPort": 8000, "PublishedPort": port, "Protocol": "tcp"}]}
    ps.update(stdout=json.dumps(v6_only), returncode=0)
    pinned, why = _credentials.this_checkouts_backend(checkout, f"http://127.0.0.1:{port}")
    assert pinned is None, why

    # A base URL named on the command line is the operator's say-so: no
    # engine question at all.
    asked.clear()
    ps.update(stdout="", returncode=0)
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
    cli_module.main(["--root", str(checkout), "doctor", "--base-url", fake_backend, "--email", "a@b", "--password-stdin"])
    assert "signed in as a@b; platform admin: yes" in capsys.readouterr().out
    assert asked == []


def test_credentials_never_follow_a_redirect(fake_backend):
    """urllib follows a 3xx by copying the request's headers to whatever
    host the Location names — Authorization included — so the three
    calls that carry a credential follow nothing."""
    from librerun import _credentials, _http

    elsewhere_handler = type("_Elsewhere", (_FakeBackend,), {"requests": [], "redirects": {}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), elsewhere_handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    elsewhere = f"http://127.0.0.1:{server.server_port}"
    _FakeBackend.password = SECRET
    try:
        _FakeBackend.redirects = {"/api/v1/meta": f"{elsewhere}/api/v1/meta"}
        with pytest.raises(_credentials.NothingSent, match="redirected"):
            _credentials.sign_in(fake_backend, "a@b", SECRET)
        _FakeBackend.redirects = {"/api/v1/auth/login": f"{elsewhere}/api/v1/auth/login"}
        with pytest.raises(CliError, match="redirected"):
            _credentials.sign_in(fake_backend, "a@b", SECRET)
        _FakeBackend.redirects = {"/api/v1/auth/me": f"{elsewhere}/api/v1/auth/me"}
        token = _credentials.sign_in(fake_backend, "a@b", SECRET)
        with pytest.raises(CliError, match="not sent onward"):
            _credentials.whoami(fake_backend, token)
        assert elsewhere_handler.requests == []

        # The probe bites: urllib's own default hands the token onward.
        status, _ = _http.get(f"{fake_backend}/api/v1/auth/me", token=token)
        assert status == 200
        assert [auth for _, _, auth, _ in elsewhere_handler.requests] == ["Bearer tok"]
    finally:
        server.shutdown()


def test_run_reads_stdin_or_the_environment(checkout, fake_backend, monkeypatch, capsys):
    """`run`'s password: --password-stdin, else LIBRERUN_PASSWORD, else the
    demo's INITIAL_ADMIN_PASSWORD, which stays for the demo."""
    from librerun import _credentials
    from librerun._run import cmd_run

    _FakeBackend.password = SECRET
    (checkout / ".env").write_text("INITIAL_ADMIN_EMAIL=a@b\nINITIAL_ADMIN_PASSWORD=not-this-one\n")
    base = dict(agent="my-agent", scenario="demo", wait=False, approve=False, timeout=30,
                base_url=fake_backend, email=None, password=None, password_stdin=False)
    monkeypatch.delenv("LIBRERUN_EMAIL", raising=False)

    # One line from a pipe outranks the variable and the file.
    monkeypatch.setenv("LIBRERUN_PASSWORD", "not-this-either")
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
    assert cmd_run(checkout, SimpleNamespace(**{**base, "password_stdin": True})) == 0
    # The variables outrank the file.
    monkeypatch.setenv("LIBRERUN_PASSWORD", SECRET)
    monkeypatch.setenv("LIBRERUN_EMAIL", "ops@b")
    assert cmd_run(checkout, SimpleNamespace(**base)) == 0
    # At a terminal, getpass reads it with no echo.
    terminal = io.StringIO()
    terminal.isatty = lambda: True
    monkeypatch.setattr("sys.stdin", terminal)
    monkeypatch.setattr(_credentials.getpass, "getpass", lambda prompt="": SECRET)
    monkeypatch.delenv("LIBRERUN_PASSWORD")
    assert cmd_run(checkout, SimpleNamespace(**{**base, "password_stdin": True})) == 0
    logins = [body for method, path, _, body in _FakeBackend.requests if path == "/api/v1/auth/login"]
    assert [b["email"] for b in logins] == ["a@b", "ops@b", "ops@b"]
    assert all(b["password"] == SECRET for b in logins)

    # An empty pipe is refused before anything is sent.
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    before = len(_FakeBackend.requests)
    with pytest.raises(CliError, match="empty password"):
        cmd_run(checkout, SimpleNamespace(**{**base, "password_stdin": True}))
    assert _FakeBackend.requests[before:] == []
    # And the two ways to give a password are one or the other.
    with pytest.raises(SystemExit) as exc:
        cli_module.build_parser().parse_args(["run", "--agent", "a", "--password", "x", "--password-stdin"])
    assert exc.value.code == 2
    capsys.readouterr()


def test_run_warns_about_an_argv_password(checkout, fake_backend, monkeypatch, capsys):
    """`run --password` still works for one release, and says on stderr
    why it goes."""
    from librerun._run import cmd_run

    monkeypatch.delenv("LIBRERUN_EMAIL", raising=False)
    monkeypatch.delenv("LIBRERUN_PASSWORD", raising=False)
    (checkout / ".env").write_text("INITIAL_ADMIN_EMAIL=a@b\n")
    args = dict(agent="my-agent", scenario="demo", wait=False, approve=False, timeout=30,
                base_url=fake_backend, email=None, password="pw", password_stdin=False)
    assert cmd_run(checkout, SimpleNamespace(**args)) == 0
    err = capsys.readouterr().err
    assert "--password puts the password on the command line" in err
    assert "process list" in err and "--password-stdin" in err and "after this release" in err

    monkeypatch.setenv("LIBRERUN_PASSWORD", "pw")
    assert cmd_run(checkout, SimpleNamespace(**{**args, "password": None})) == 0
    assert "--password puts" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# HTTPS through the edge (T1): --cacert, LIBRERUN_URL, and doctor's sign-in
# to this checkout's edge, at the address the engine vouched for, under the
# URL's host name
# ---------------------------------------------------------------------------

EDGE_HOST = "librerun.test"


class _PKI:
    """A test CA and the server certificates it issues, made with
    `cryptography` (in the backend's lock since K6): the edge's local CA,
    stood in. `ca_file` is its root, as copied out of `librerun-edge`."""

    def __init__(self, directory: Path, common_name: str = "LibreRun T1 test root"):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        directory.mkdir(parents=True, exist_ok=True)
        self.directory, self._issued = directory, 0
        self._key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        now = datetime.datetime.now(datetime.timezone.utc)
        self._cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self._key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                              data_encipherment=False, key_agreement=False, key_cert_sign=True,
                              crl_sign=True, encipher_only=False, decipher_only=False),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self._key.public_key()), critical=False)
            .sign(self._key, hashes.SHA256())
        )
        self.ca_file = directory / "root.crt"
        self.ca_file.write_bytes(self._cert.public_bytes(serialization.Encoding.PEM))

    def leaf(self, *, dns=(), ips=()) -> tuple[str, str]:
        """A server certificate for exactly these names; ``(cert, key)`` paths."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        names = [x509.DNSName(n) for n in dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, (list(dns) + list(ips))[0])]))
            .issuer_name(self._cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                              data_encipherment=False, key_agreement=False, key_cert_sign=False,
                              crl_sign=False, encipher_only=False, decipher_only=False),
                critical=True,
            )
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(self._key, hashes.SHA256())
        )
        self._issued += 1
        cert_path = self.directory / f"leaf{self._issued}.crt"
        key_path = self.directory / f"leaf{self._issued}.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        return str(cert_path), str(key_path)


@pytest.fixture
def pki(tmp_path):
    return _PKI(tmp_path / "pki")


@pytest.fixture
def no_proxy(monkeypatch):
    """Every local TLS server here is reached directly, whatever proxy the
    host's shell names — which a pinned request must ignore anyway."""
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")


# A SubjectPublicKeyInfo DER stand-in, and what doctor must print for it:
# SHA256 over the DER the PEM's body decodes to (D34). The CLI checks the
# digest, not the key, so any bytes will do.
PROVIDERS_DER = bytes(range(256)) * 2
PROVIDERS_FINGERPRINT = "SHA256:" + __import__("hashlib").sha256(PROVIDERS_DER).hexdigest()


def _reported_providers(**overrides) -> dict:
    import base64

    body = base64.b64encode(PROVIDERS_DER).decode()
    pem = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64)) + "\n-----END PUBLIC KEY-----\n"
    report = {
        "reported": True, "stub": True, "gateway_version": "0", "updated_at": None, "public_key_pem": pem,
        "providers": [
            {"name": "openai", "aliases": ["openai", "azure"], "source": "env", "fingerprint": None,
             "set_by": None, "set_at": None, "row": None, "reason": None},
            {"name": "anthropic", "aliases": ["anthropic"], "source": "runtime", "fingerprint": "f00dfacecafe",
             "set_by": None, "set_at": None, "row": "runtime", "reason": None},
            {"name": "google", "aliases": ["gemini", "google", "vertex_ai"], "source": "unset",
             "fingerprint": None, "set_by": None, "set_at": None, "row": None, "reason": None},
        ],
    }
    report.update(overrides)
    return report


def _deployment_view(**overrides) -> dict:
    """What GET /api/v1/admin/deployment answers (K9): an allowlist of
    names and values, the header variables by presence, the gateway's
    report and the transport — never a secret."""
    view = {
        "version": "1.0.0", "license": "AGPL-3.0-only", "source_url": "https://example.invalid/src",
        "demo": True, "stub": True,
        "gateway": {"reachable": True, "reported": True, "version": "1.0.0",
                    "updated_at": "2026-09-30T12:00:00Z", "providers": []},
        "settings": [
            {"name": "LIBRERUN_DEMO", "env_class": 2, "value": True, "source": "env",
             "hint": "Change it in .env, then restart the backend."},
            {"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "env_class": 2, "value": "http://vector:4317",
             "source": "env", "hint": "Change it in .env, then restart the backend."},
            {"name": "LOG_LEVEL", "env_class": 2, "value": "INFO", "source": "default",
             "hint": "Change it in .env, then restart the backend."},
        ],
        "otlp_headers": [
            {"name": "OTEL_EXPORTER_OTLP_HEADERS", "set": True},
            {"name": "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "set": False},
            {"name": "OTEL_EXPORTER_OTLP_LOGS_HEADERS", "set": False},
        ],
        "transport": {"scheme": "http", "host": "127.0.0.1"},
    }
    view.update(overrides)
    return view


def _tls_backend_class(**attributes):
    """A `_FakeBackend` of its own — its own requests, password and settings
    — that also records each request's Host header."""

    class _Backend(_FakeBackend):
        hosts: list = []

        def _seen(self, body=None) -> bool:
            type(self).hosts.append(self.headers.get("Host"))
            return super()._seen(body)

    defaults = dict(reads=0, outcome="complete", password="pw", name="LibreRun", platform_admin=True,
                    redirects={}, requests=[], hosts=[], signed_in=None, settings=[],
                    providers=_reported_providers(), deployment=_deployment_view())
    for name, value in {**defaults, **attributes}.items():
        setattr(_Backend, name, value)
    return _Backend


def _serve_tls(handler, cert: str, key: str, *, host: str = "127.0.0.1", port: int = 0):
    """``handler`` over TLS with that certificate on host:port; returns the
    server, its port and the SNI names clients sent."""
    server = ThreadingHTTPServer((host, port), handler)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert, key)
    sni: list = []
    context.sni_callback = lambda _sock, name, _context: sni.append(name)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_port, sni


def _resolve(monkeypatch, table: dict[str, list[str]]) -> None:
    """Names resolved as an /etc/hosts alias would, in the order given —
    what `tls-edge` does for librerun.test, in-process."""
    real = socket.getaddrinfo

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host in table:
            return [info for address in table[host] for info in real(address, port, family, type, proto, flags)]
        return real(host, port, family, type, proto, flags)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def _run_args(**overrides) -> SimpleNamespace:
    args = dict(agent="my-agent", scenario="demo", wait=True, approve=False, timeout=30, base_url=None,
                email=None, password=None, password_stdin=False, cacert=None)
    args.update(overrides)
    return SimpleNamespace(**args)


def test_https_base_url_for_run_and_doctor(checkout, pki, no_proxy, monkeypatch, capsys):
    """LIBRERUN_URL — the shell's, never an .env line — is the base `run` and
    `doctor` reach with no --base-url, scheme and all: `addresses()` no longer
    writes a fixed http://, and behind the edge one origin serves the UI and
    the API. `demo` and `up` still start, wait for and print the published
    ports (they never start the edge), naming a loopback binding `localhost`.
    Over https `run` and `doctor` verify against --cacert or
    LIBRERUN_CA_FILE and sign in; without it the certificate fails the meta
    check, nothing is sent, and the message says what to pass."""
    from librerun import _credentials, _run, _stack

    # addresses(): LIBRERUN_URL from the process environment alone.
    (checkout / ".env").write_text("LIBRERUN_URL=https://from-the-file.test:8443\nBACKEND_PORT=127.0.0.1:8000\n")
    plain = _stack.addresses(_env.DotEnv(checkout / ".env", environ={}))
    assert (plain["backend"], plain["frontend"]) == ("http://localhost:8000", "http://localhost:3000"), plain
    shell = {"LIBRERUN_URL": "https://librerun.test:8443/"}
    edge = _stack.addresses(_env.DotEnv(checkout / ".env", environ=shell))
    assert edge["backend"] == edge["frontend"] == "https://librerun.test:8443", edge
    assert _stack.published(_env.DotEnv(checkout / ".env", environ=shell))["backend"] == "http://localhost:8000"
    assert _stack.derived_env(_env.DotEnv(checkout / ".env", environ=shell))["NEXT_PUBLIC_API_URL"] == (
        "http://localhost:8000/api/v1"
    )
    assert cli_module.build_parser().parse_args(["run", "--agent", "a", "--cacert", "root.crt"]).cacert == "root.crt"
    assert cli_module.build_parser().parse_args(["doctor", "--cacert", "root.crt"]).cacert == "root.crt"

    backend = _tls_backend_class(password=SECRET)
    cert, key = pki.leaf(dns=("localhost",), ips=("127.0.0.1",))
    server, port, sni = _serve_tls(backend, cert, key)
    base = f"https://127.0.0.1:{port}"
    try:
        # run: LIBRERUN_URL and LIBRERUN_CA_FILE, both from the shell.
        (checkout / ".env").write_text("LIBRERUN_DEMO=true\n")
        monkeypatch.setenv("LIBRERUN_URL", base)
        monkeypatch.setenv("LIBRERUN_CA_FILE", str(pki.ca_file))
        monkeypatch.setenv("LIBRERUN_EMAIL", "a@b")
        monkeypatch.setenv("LIBRERUN_PASSWORD", SECRET)
        monkeypatch.setattr("librerun._run.time.sleep", lambda s: None)
        assert _run.cmd_run(checkout, _run_args()) == 0
        assert backend.submitted == {"question": "q"}
        out = capsys.readouterr().out
        assert f"run page: {base}/runs/" in out and f"api:      {base}/api/v1/runs/" in out, out
        # …and --cacert on the command line, with --base-url.
        monkeypatch.delenv("LIBRERUN_CA_FILE")
        monkeypatch.delenv("LIBRERUN_URL")
        backend.reads = 0
        assert _run.cmd_run(checkout, _run_args(base_url=base, cacert=str(pki.ca_file))) == 0
        # …and an https --base-url is the edge's origin, UI included: the run
        # page is printed there, not at the published web UI's port.
        out = capsys.readouterr().out
        assert f"run page: {base}/runs/" in out and "localhost:3000" not in out, out

        # doctor --base-url https://… --cacert: signed in over TLS.
        result = _cli(checkout, "doctor", "--base-url", base, "--cacert", str(pki.ca_file), "--email", "a@b",
                      "--password-stdin", env={"PATH": "/nonexistent", "no_proxy": "*"}, input=SECRET + "\n")
        assert "[ok]   signed in as a@b; platform admin: yes" in result.stdout, result.stdout + result.stderr
        assert f"Backend at {base}" in result.stdout

        # No CA file: the certificate does not verify, and nothing is sent.
        before = len(backend.requests)
        with pytest.raises(_credentials.NothingSent, match="--cacert"):
            _run.cmd_run(checkout, _run_args(base_url=base))
        result = _cli(checkout, "doctor", "--base-url", base, "--email", "a@b", "--password-stdin",
                      env={"PATH": "/nonexistent", "no_proxy": "*"}, input=SECRET + "\n")
        assert "--cacert" in result.stdout and "signed in as" not in result.stdout, result.stdout
        assert _credentials_sent(backend.requests[before:]) == []
    finally:
        server.shutdown()


def test_ca_file_builds_tls_context(pki, tmp_path, no_proxy):
    """--cacert's file builds the TLS context an https request verifies
    against — `ssl.create_default_context(cafile=…)`, hostname checked — and
    it is trusted ALONE, as curl's --cacert: the system's trust does not know
    the edge's local CA, and a certificate another CA issued is refused though
    it names the host. The no-redirect rule holds on the same path."""
    from librerun import _http

    context = _http.tls_context(str(pki.ca_file))
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert [dict(pair[0] for pair in ca["subject"]) for ca in context.get_ca_certs()] == [
        {"commonName": "LibreRun T1 test root"}
    ]
    backend = _tls_backend_class()
    cert, key = pki.leaf(dns=("localhost",), ips=("127.0.0.1",))
    stranger = _PKI(tmp_path / "stranger", "Another root")
    other_cert, other_key = stranger.leaf(dns=("localhost",), ips=("127.0.0.1",))
    server, port, _ = _serve_tls(backend, cert, key)
    other, other_port, _ = _serve_tls(_tls_backend_class(), other_cert, other_key)
    try:
        url = f"https://127.0.0.1:{port}/api/v1/meta"
        status, meta = _http.get(url, cafile=str(pki.ca_file))
        assert status == 200 and meta["name"] == "LibreRun", meta
        status, body = _http.get(url)
        assert status == 0 and "CERTIFICATE_VERIFY_FAILED" in body["error"], body
        status, body = _http.get(f"https://127.0.0.1:{other_port}/api/v1/meta", cafile=str(pki.ca_file))
        assert status == 0 and "CERTIFICATE_VERIFY_FAILED" in body["error"], body
        backend.redirects = {"/api/v1/meta": "https://elsewhere.test/api/v1/meta"}
        status, body = _http.get(url, cafile=str(pki.ca_file), follow_redirects=False)
        assert (status, body) == (302, {"location": "https://elsewhere.test/api/v1/meta"})
    finally:
        server.shutdown()
        other.shutdown()


def test_missing_ca_file_refused_by_path(checkout, fake_backend, tmp_path, monkeypatch):
    """A CA file that is not there, or holds no certificate, is refused by
    its path — from --cacert or LIBRERUN_CA_FILE, by doctor, run and the
    smoke client — before anything is checked or sent."""
    from librerun import _http
    from librerun._run import cmd_run

    missing = tmp_path / "nowhere" / "root.crt"
    garbage = tmp_path / "not-a-certificate.pem"
    garbage.write_text("not a certificate\n")
    for path in (missing, garbage):
        with pytest.raises(CliError, match=re.escape(str(path))):
            _http.tls_context(str(path))
    for extra, env in (((["--cacert", str(missing)]), {}), ([], {"LIBRERUN_CA_FILE": str(missing)})):
        result = _cli(checkout, "doctor", "--base-url", fake_backend, "--email", "a@b", "--password-stdin", *extra,
                      env={"PATH": "/nonexistent", **env}, input=SECRET + "\n")
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert str(missing) in result.stderr and "does not exist" in result.stderr, result.stderr
        assert "librerun doctor —" not in result.stdout
    monkeypatch.setenv("LIBRERUN_PASSWORD", SECRET)
    with pytest.raises(CliError, match=re.escape(str(garbage))):
        cmd_run(checkout, _run_args(base_url=fake_backend, email="a@b", cacert=str(garbage)))
    smoke = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "librerun_smoke.py"), "--base-url", fake_backend,
         "--cacert", str(missing), "--email", "a@b", "--password", SECRET],
        capture_output=True, text=True, timeout=60,
    )
    assert smoke.returncode == 1 and str(missing) in smoke.stderr, (smoke.stdout, smoke.stderr)
    assert _FakeBackend.requests == [], "a request left before the CA file was refused"


def _edge_stage(checkout, pki, monkeypatch, *, ps_stdout, returncode: int = 0):
    """This checkout under the HTTPS edge, stood in: LIBRERUN_URL and
    LIBRERUN_CA_FILE in the shell; `librerun.test` resolving FIRST to
    127.0.0.2, where another edge answers with a certificate from the same
    CA (#134: two clones with one directory name share the edge's volume,
    so its local CA), and then to 127.0.0.1, where the edge the engine
    reports answers; the engine and `compose ps` stood in."""
    from librerun import _compose

    cert, key = pki.leaf(dns=(EDGE_HOST,))
    mine = _tls_backend_class(password=SECRET)
    other = _tls_backend_class(password=SECRET)
    server, port, sni = _serve_tls(mine, cert, key, host="127.0.0.1")
    other_server, _, other_sni = _serve_tls(other, cert, key, host="127.0.0.2", port=port)
    _resolve(monkeypatch, {EDGE_HOST: ["127.0.0.2", "127.0.0.1"]})
    (checkout / ".env").write_text("LIBRERUN_DEMO=true\nLIBRERUN_STUB_LLM=true\n")
    monkeypatch.setenv("LIBRERUN_URL", f"https://{EDGE_HOST}:{port}")
    monkeypatch.setenv("LIBRERUN_CA_FILE", str(pki.ca_file))
    engine = {"name": "docker", "binary": "/usr/bin/docker", "daemon": True, "compose": "docker compose"}
    monkeypatch.setattr(_compose, "engine_report", lambda: {**engine, "engine": "docker", "tried": [engine]})
    asked: list = []

    def fake_compose(root, *args, **kwargs):
        asked.append((args, kwargs.get("profiles")))
        stdout = ps_stdout(port) if callable(ps_stdout) else ps_stdout
        return SimpleNamespace(returncode=returncode, stdout="Using: docker compose\n" + stdout,
                               stderr="compose.sh: the tls profile is requested" if returncode else "")

    monkeypatch.setattr(_compose, "run", fake_compose)
    return SimpleNamespace(mine=mine, other=other, sni=sni, other_sni=other_sni, port=port, asked=asked,
                           servers=(server, other_server))


def _doctor_in_process(checkout, monkeypatch, capsys) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
    cli_module.main(["--root", str(checkout), "doctor", "--email", "a@b", "--password-stdin"])
    return capsys.readouterr().out


def test_doctor_signs_in_through_this_checkouts_edge(checkout, pki, no_proxy, monkeypatch, capsys):
    """LIBRERUN_URL=https://librerun.test:<port> and no --base-url: doctor asks
    the engine for this checkout's EDGE — `compose ps` with `tls` beside `app`
    — and the meta check, the sign-in, /auth/me and the settings read go to
    the address it publishes on, 127.0.0.1, under the URL's host name (SNI and
    Host `librerun.test`). The name resolves first to 127.0.0.2, where another
    edge holding a certificate from the same CA answers: it gets doctor's
    unauthenticated health and meta reads, and no credential."""
    stage = _edge_stage(
        checkout, pki, monkeypatch,
        ps_stdout=lambda port: json.dumps(_ps_record(checkout, port, service="edge")) + "\n"
        + json.dumps(_ps_record(checkout, 8000)) + "\n",
    )
    try:
        out = _doctor_in_process(checkout, monkeypatch, capsys)
        assert "[ok]   signed in as a@b; platform admin: yes" in out, out
        assert "secret settings: 0" in out, out
        # K7: the providers read, on the same vetted address and token.
        assert f"the gateway seals provider keys to {PROVIDERS_FINGERPRINT}" in out, out
        assert stage.asked == [(("ps", "--format", "json"), ("app", "tls"))], stage.asked
        sent = _credentials_sent(stage.mine.requests)
        assert [(m, p) for m, p, *_ in sent] == [("POST", "/api/v1/auth/login"), ("GET", "/api/v1/auth/me"),
                                                ("GET", "/api/v1/admin/settings"),
                                                ("GET", "/api/v1/admin/providers"),
                                                ("GET", "/api/v1/admin/deployment")], sent
        # K9: the deployment view, read the same way.
        assert "[ok]   LIBRERUN_DEMO=true (env)" in out, out
        assert set(stage.mine.hosts) == {f"{EDGE_HOST}:{stage.port}"} and set(stage.sni) == {EDGE_HOST}
        assert _credentials_sent(stage.other.requests) == []
        assert {path for _, path, *_ in stage.other.requests} <= {"/api/v1/health", "/api/v1/meta"}
        assert stage.other.requests, "the other edge answered nothing — the name did not resolve to it first"
    finally:
        for server in stage.servers:
            server.shutdown()


def test_an_edge_from_another_checkout_gets_nothing(checkout, pki, tmp_path, no_proxy, monkeypatch, capsys):
    """The certificate does not stand in for the ownership check: another
    clone of the same directory name is the same compose project, shares the
    edge's local CA, and its `librerun-edge` answers on the port with a
    certificate the copied root verifies. `compose ps` naming an edge from
    another working directory — or this checkout's edge stopped, or on
    another port, or only this checkout's backend (an https base is the
    edge's), or `compose ps` refused by the loopback guard — and no
    credential goes anywhere."""
    other_clone = tmp_path / "older" / checkout.name
    cases = {
        "an edge from another clone of the same name": (
            lambda port: json.dumps(_ps_record(other_clone, port, service="edge")), 0, f"an edge from {other_clone}"),
        "this checkout's edge, stopped": (
            lambda port: json.dumps(_ps_record(checkout, port, state="exited", service="edge")), 0, "no running edge"),
        "this checkout's edge on another port": (
            lambda port: json.dumps(_ps_record(checkout, port + 1, service="edge")), 0, "this checkout's edge on port(s)"),
        "this checkout's backend and no edge": (
            lambda port: json.dumps(_ps_record(checkout, port)), 0, "no running edge"),
        "compose ps refused by the loopback guard": (lambda port: "", 4, "exit 4"),
    }
    for label, (ps_stdout, code, why) in cases.items():
        stage = _edge_stage(checkout, pki, monkeypatch, ps_stdout=ps_stdout, returncode=code)
        try:
            out = _doctor_in_process(checkout, monkeypatch, capsys)
            assert "[warn] not signed in:" in out and "so nothing was sent" in out, (label, out)
            assert why in out and "--base-url" in out, (label, out)
            assert _credentials_sent(stage.mine.requests + stage.other.requests) == [], label
            assert stage.asked == [(("ps", "--format", "json"), ("app", "tls"))], (label, stage.asked)
        finally:
            for server in stage.servers:
                server.shutdown()


def test_the_pinned_https_request_keeps_the_host_name(pki, no_proxy, monkeypatch):
    """`_http`'s pinned request, on a local TLS server reached at 127.0.0.1
    whose certificate the test makes for `librerun.test` alone: the
    connection opens the vetted address, the request names the host — SNI
    and Host `librerun.test` — and verifies against it, and the credential
    path runs end to end. A certificate for another name at the same address
    is refused; the naive pin, a request to https://<address>, cannot verify
    this certificate at all; and no proxy the environment names carries a
    pinned request."""
    from librerun import _credentials, _http

    cert, key = pki.leaf(dns=(EDGE_HOST,))
    backend = _tls_backend_class(password=SECRET)
    wrong_cert, wrong_key = pki.leaf(dns=("other.test",))
    server, port, sni = _serve_tls(backend, cert, key)
    wrong, wrong_port, _ = _serve_tls(_tls_backend_class(), wrong_cert, wrong_key)
    ca = str(pki.ca_file)
    try:
        # The naive pin: an address is not the name the certificate carries.
        status, body = _http.get(f"https://127.0.0.1:{port}/api/v1/meta", cafile=ca)
        assert status == 0 and "certificate" in body["error"].lower(), body
        assert backend.requests == [] and sni == [None], sni  # an address sends no SNI at all
        sni.clear()

        # Pinned: the vetted address, the host's name. (`librerun.test`
        # resolves nowhere here: the connection never asks for it.)
        url = f"https://{EDGE_HOST}:{port}"
        status, meta = _http.get(f"{url}/api/v1/meta", cafile=ca, address="127.0.0.1")
        assert status == 200 and meta["name"] == "LibreRun", meta
        assert sni == [EDGE_HOST] and backend.hosts == [f"{EDGE_HOST}:{port}"], (sni, backend.hosts)
        token = _credentials.sign_in(url, "a@b", SECRET, cafile=ca, address="127.0.0.1")
        assert _credentials.whoami(url, token, cafile=ca, address="127.0.0.1")["email"] == "a@b"
        assert set(backend.hosts) == {f"{EDGE_HOST}:{port}"} and set(sni) == {EDGE_HOST}

        # And never through a proxy the environment names: nothing listens
        # at this one, so a request that went there would not answer.
        monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
        monkeypatch.delenv("no_proxy")
        monkeypatch.delenv("NO_PROXY")
        before = len(backend.requests)
        status, meta = _http.get(f"{url}/api/v1/meta", cafile=ca, address="127.0.0.1")
        assert status == 200 and len(backend.requests) == before + 1, meta

        # Another name's certificate at the vetted address: refused.
        status, body = _http.get(f"https://{EDGE_HOST}:{wrong_port}/api/v1/meta", cafile=ca, address="127.0.0.1")
        assert status == 0 and "certificate" in body["error"].lower(), body
        # An http base names its vetted address in the URL itself (K4b).
        with pytest.raises(ValueError, match="https"):
            _http.get("http://127.0.0.1:9/api/v1/meta", address="127.0.0.1")
    finally:
        server.shutdown()
        wrong.shutdown()


# ---------------------------------------------------------------------------
# K9 (C14): the deployment as the backend reads it, and the trace endpoint
# ---------------------------------------------------------------------------


def _doctor_named(checkout, base_url, monkeypatch, capsys) -> str:
    """doctor in process against ``base_url``, the engine stood in, so this
    machine's checks — the Trace endpoint section among them — run too."""
    from librerun import _compose

    engine = {"name": "docker", "binary": "/usr/bin/docker", "daemon": True, "compose": "docker compose"}
    monkeypatch.setattr(_compose, "engine_report", lambda: {**engine, "engine": "docker", "tried": [engine]})
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
    cli_module.main(["--root", str(checkout), "doctor", "--base-url", base_url,
                     "--email", "a@b", "--password-stdin"])
    return capsys.readouterr().out


def test_doctor_reads_the_deployment_view_as_a_platform_admin(checkout, fake_backend):
    _FakeBackend.password = SECRET
    _FakeBackend.deployment = _deployment_view()
    result = _cli(checkout, "doctor", "--base-url", fake_backend, "--email", "a@b", "--password-stdin",
                  env={"PATH": "/nonexistent"}, input=SECRET + "\n")
    out = result.stdout
    assert "Deployment (as the backend reads it)" in out, out
    assert "[ok]   LIBRERUN_DEMO=true (env)" in out, out
    assert "[ok]   OTEL_EXPORTER_OTLP_ENDPOINT=http://vector:4317 (env)" in out, out
    assert "[ok]   LOG_LEVEL=INFO (default)" in out, out
    assert "[ok]   OTEL_EXPORTER_OTLP_HEADERS set" in out, out
    assert "[ok]   OTEL_EXPORTER_OTLP_TRACES_HEADERS not set" in out, out
    assert "[ok]   gateway 1.0.0, reported 2026-09-30T12:00:00Z" in out, out
    assert "[ok]   transport: http at 127.0.0.1" in out, out
    assert SECRET not in out + result.stderr
    reads = [(m, p, auth) for m, p, auth, _ in _FakeBackend.requests if p == "/api/v1/admin/deployment"]
    assert reads == [("GET", "/api/v1/admin/deployment", "Bearer tok")], reads

    # Another status says why, and points at .env.
    _FakeBackend.deployment = {}
    _FakeBackend.redirects = {"/api/v1/admin/deployment": "http://127.0.0.1:9/elsewhere"}
    result = _cli(checkout, "doctor", "--base-url", fake_backend, "--email", "a@b", "--password-stdin",
                  env={"PATH": "/nonexistent"}, input=SECRET + "\n")
    assert "the deployment view could not be read (/api/v1/admin/deployment answered 302)" in result.stdout


def test_doctor_falls_back_to_env_for_a_tenant_admin(checkout, fake_backend, monkeypatch, capsys):
    _FakeBackend.password = SECRET
    _FakeBackend.platform_admin = False
    (checkout / ".env").write_text("OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317\n")
    out = _doctor_named(checkout, fake_backend, monkeypatch, capsys)
    assert "signed in as a@b; platform admin: no" in out, out
    assert "[warn] the deployment view is a platform operator's" in out, out
    assert "/api/v1/admin/deployment" not in [p for _, p, *_ in _FakeBackend.requests]
    assert "Trace endpoint" in out and "[ok]   OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317" in out, out


def test_doctor_strips_userinfo_and_query_from_the_trace_endpoint(checkout, fake_backend, monkeypatch, capsys):
    _FakeBackend.password = SECRET
    _FakeBackend.platform_admin = False
    (checkout / ".env").write_text("OTEL_EXPORTER_OTLP_ENDPOINT=http://u:pw-k9@c:4317/?t=tok-k9#f\n")
    out = _doctor_named(checkout, fake_backend, monkeypatch, capsys)
    assert "[ok]   OTEL_EXPORTER_OTLP_ENDPOINT=http://c:4317/" in out, out
    assert "pw-k9" not in out and "tok-k9" not in out, out


# ---------------------------------------------------------------------------
# the parser, and the page that documents it
# ---------------------------------------------------------------------------


def test_every_quickstart_command_parses():
    """Every `librerun …` line inside a fenced block of the Quickstart is
    a command the parser accepts — the page is what a newcomer pastes."""
    page = (REPO / "docs" / "authoring" / "Quickstart.md").read_text()
    blocks = re.findall(r"```bash\n(.*?)```", page, re.S)
    commands = [line.strip() for block in blocks for line in block.splitlines() if line.strip().startswith("librerun ")]
    assert len(commands) >= 12, commands
    parser = cli_module.build_parser()
    for command in commands:
        tokens = command.split("#", 1)[0].split()[1:]
        if tokens[:1] in (["--help"], ["--version"]):
            continue
        parser.parse_args(tokens)  # SystemExit on a command the CLI does not have


def test_the_templates_the_parser_offers_are_the_templates_shipped():
    parser = cli_module.build_parser()
    choices = None
    for action in parser._subparsers._group_actions[0].choices["init"]._actions:
        if action.dest == "template":
            choices = tuple(action.choices)
    assert choices == _init.TEMPLATES
    shipped = sorted(p.name for p in (CLI_SRC / "librerun" / "templates").iterdir() if p.is_dir())
    assert shipped == sorted(_init.TEMPLATES)


# ---------------------------------------------------------------------------
# the driver's own container name — what `run.mcp.url` has to point at
# ---------------------------------------------------------------------------


def _battery_argv(checkout, monkeypatch, agent_id: str) -> list[str]:
    """`librerun battery`'s compose argv, with compose itself stubbed."""
    from librerun import _battery

    seen: dict[str, list[str]] = {}

    def fake_run(root, *args, **kwargs):
        seen["argv"] = list(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(_battery._compose, "run", fake_run)
    _battery.cmd_battery(
        checkout,
        SimpleNamespace(agent=agent_id, url=None, agent_dir=None, scenario=None,
                        json=False, timeout=60),
    )
    return seen["argv"]


def test_the_battery_advertises_the_container_it_is_actually_in(checkout, monkeypatch):
    """THE DEFECT: one name, two containers, and it named the wrong one.

    `compose run` does NOT give a one-off container the service's
    network aliases — `--use-aliases` exists precisely because that is
    opt-in, and this CLI has never passed it. The driver advertised
    `backend`, which from inside an agent resolves to the LONG-RUNNING
    backend service container, where nothing is listening on the
    driver's ephemeral MCP port. `backend:8000` worked for the chassis's
    own MCP, because that container really does serve it; only the
    ephemeral port was refused, which is exactly the shape that was
    observed and could not be explained from the CI log.

    So the invariant is not "the host is some resolvable name" — it is
    that the name ADVERTISED and the name the container is GIVEN are the
    same string. Asserting only the first would have passed on `backend`.
    """
    argv = _battery_argv(checkout, monkeypatch, "echo-v1")

    assert "--name" in argv, argv
    named = argv[argv.index("--name") + 1]
    assert "--mcp-advertise-host" in argv, argv
    advertised = argv[argv.index("--mcp-advertise-host") + 1]

    assert advertised == named, (
        f"the battery advertises {advertised!r} but its container is called "
        f"{named!r}; an agent calling the first reaches the second's namesake"
    )
    assert advertised != "backend", (
        "`backend` is the long-running service container, which is not the "
        "one the driver runs in — that is the bug this pins"
    )


def test_the_driver_name_is_unique_per_process(checkout, monkeypatch):
    """Two batteries may run at once, and `compose run --name` fails
    outright on a collision — so a fixed name would trade a silent skip
    for a hard error."""
    from librerun import _battery

    assert str(os.getpid()) in _battery.driver_container_name()


def test_the_in_process_battery_names_no_container_and_advertises_nothing(
    checkout, monkeypatch
):
    """The other direction, so the rule above cannot be satisfied by
    always passing a name: the adapter battery serves no callback, so
    there is nothing for an agent to reach and nothing to advertise."""
    argv = _battery_argv(checkout, monkeypatch, "langgraph-triage")

    assert "--mcp-advertise-host" not in argv, argv
    assert "--name" not in argv, argv
