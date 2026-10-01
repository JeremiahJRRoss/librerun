"""``init``: a new agent from a template (gaps C3, C4, C8).

Three templates, each derived from a shipped example and each the
smallest complete agent of its kind:

* ``langgraph`` — a compiled LangGraph graph running in the backend
  process through ``librerun-langgraph`` (the LangGraph example's shape);
* ``container-python`` — one async handler on the ``librerun-agent`` SDK,
  served as a Run Contract v1 container (the echo example's shape);
* ``container-ts`` — the Run Contract served directly from one
  TypeScript file with the Vercel AI SDK making the model call (the
  TypeScript reference server's shape).

Every template grants ``llm`` and ``pii``, declares one ``llm.steps``
entry and calls it — through the in-process capability, the SDK or the
gateway environment — so the admin page can retarget a scaffolded
agent's model exactly as it retargets an example's (D13). The container
templates ship a Dockerfile and append a service to
``agents.compose.yaml`` in the examples' exact shape: the gateway
environment, a freshly issued agent key in ``.env``, the internal
``agents`` network, the ``librerun.agent_id`` label,
``logging: driver: none`` and the ``egress`` line commented.

Files are copied with token substitution — ``__AGENT_ID__``,
``__AGENT_NAME__``, ``__PACKAGE__``, ``__CLASS_NAME__``,
``__KEY_VARIABLE__``, ``__SERVICE__``, ``__TEMPLATE__`` — and nothing
else is generated at run time, so what a template ships is what an
author reads.
"""
from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

from ._agents import find_agent, read_fragment
from ._common import CliError, say, validate_agent_id
from ._env import DotEnv, agent_key_variable, mint_agent_key

TEMPLATES = ("langgraph", "container-python", "container-ts")
CONTAINER_TEMPLATES = ("container-python", "container-ts")

# The OTLP line of the fragment, per runtime: the Python SDK exports its
# spans and log records to the chassis relay; the TypeScript server ships
# no exporter (``@librerun/agent`` is v1.1), so it sets no endpoint
# rather than one nothing reads — the same choice the examples make.
OTEL_LINES = {
    "container-python": "      OTEL_EXPORTER_OTLP_ENDPOINT: http://backend:8000/api/v1/_o/otlp",
    "container-ts": (
        "      # No OTLP exporter in this container: it serves the Run Contract\n"
        "      # directly, and the TypeScript package that would export for it is\n"
        "      # v1.1. Its work is still in the run's one trace — the chassis\n"
        "      # phase span and the gateway's LLM span under it."
    ),
}


def display_name(agent_id: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_]+", agent_id) if part)


def class_name(agent_id: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[-_]+", agent_id) if part) + "Agent"


def tokens(agent_id: str, name: str, template: str) -> dict[str, str]:
    values = {
        "__AGENT_ID__": agent_id,
        "__AGENT_NAME__": name,
        "__PACKAGE__": agent_id.replace("-", "_"),
        "__CLASS_NAME__": class_name(agent_id),
        "__SERVICE__": agent_id,
        "__TEMPLATE__": template,
        "__OTEL_LINE__": OTEL_LINES.get(template, ""),
    }
    if template in CONTAINER_TEMPLATES:
        values["__KEY_VARIABLE__"] = agent_key_variable(agent_id)
    return values


def render(text: str, values: dict[str, str]) -> str:
    for token, value in values.items():
        text = text.replace(token, value)
    return text


def template_files(template: str):
    """``(relative path, text)`` for every file of the template, from the
    package data — the CLI has no checkout of its own."""
    base = resources.files("librerun").joinpath("templates", template)
    if not base.is_dir():
        raise CliError(f"unknown template {template!r}; choose one of {', '.join(TEMPLATES)}")
    out = []
    stack = [(base, Path())]
    while stack:
        node, rel = stack.pop()
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            # A byte-compiled cache beside a template's agent.py is not
            # part of the template.
            if child.name == "__pycache__" or child.name.endswith((".pyc", ".pyo")):
                continue
            child_rel = rel / child.name
            if child.is_dir():
                stack.append((child, child_rel))
            else:
                out.append((child_rel, child.read_text(encoding="utf-8")))
    return sorted(out, key=lambda item: str(item[0]))


def fragment_text(values: dict[str, str]) -> str:
    text = resources.files("librerun").joinpath("templates", "_fragment.yaml").read_text(encoding="utf-8")
    return render(text, values)


def scaffold(root: Path, agent_id: str, template: str, name: str | None) -> dict:
    """Write the agent, the fragment service and the key. Returns what was
    written, for the summary and for tests."""
    validate_agent_id(agent_id)
    if template not in TEMPLATES:
        raise CliError(f"unknown template {template!r}; choose one of {', '.join(TEMPLATES)}")
    name = (name or display_name(agent_id)).strip()
    if not name:
        raise CliError("--name must not be empty")
    values = tokens(agent_id, name, template)
    package = values["__PACKAGE__"]
    target = root / "backend" / "agents" / package
    if target.exists():
        raise CliError(f"{target.relative_to(root)} already exists; pick another name or remove it")
    existing = find_agent(root, agent_id)
    if existing is not None:
        raise CliError(f"an agent with id {agent_id!r} already exists at {existing.directory.relative_to(root)}")
    if template in CONTAINER_TEMPLATES:
        for service in read_fragment(root):
            if service.name == agent_id or service.agent_id == agent_id:
                raise CliError(
                    f"agents.compose.yaml already carries a service {service.name!r} "
                    f"(librerun.agent_id: {service.agent_id}); pick another name"
                )

    written = []
    for rel, text in template_files(template):
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(text, values), encoding="utf-8")
        written.append(str(path.relative_to(root)))

    result = {"directory": target, "files": written, "template": template, "agent_id": agent_id, "name": name}

    if template in CONTAINER_TEMPLATES:
        fragment = root / "agents.compose.yaml"
        text = fragment.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        fragment.write_text(text + fragment_text(values), encoding="utf-8")
        result["service"] = agent_id
        env = DotEnv(root / ".env")
        variable = values["__KEY_VARIABLE__"]
        result["key_variable"] = variable
        if env.exists and not env.is_set(variable):
            env.append_block(
                [
                    "",
                    f"# Added by librerun init {agent_id}: this agent's LibreRun gateway key",
                    "# (blueprint S4a, D10) — not a provider key. Rotate it with",
                    f"# `librerun key rotate {agent_id}`.",
                    f"{variable}={mint_agent_key()}",
                ]
            )
            env.write()
            result["key_written"] = True
        else:
            result["key_written"] = False
    return result


def cmd_init(root: Path, args) -> int:
    result = scaffold(root, args.name, args.template, args.display_name)
    rel = result["directory"].relative_to(root)
    say(f"created {rel} from the {result['template']} template ({len(result['files'])} files)")
    for path in result["files"]:
        say(f"  {path}")
    if "service" in result:
        say(f"appended service {result['service']} to agents.compose.yaml (profile `agents`, network `agents`, logging off)")
        if result["key_written"]:
            say(f"provisioned {result['key_variable']} in .env")
        else:
            say(f"{result['key_variable']}: {'already set' if (root / '.env').is_file() else 'no .env yet — `librerun up` (or `librerun demo`) provisions it'}")
    say()
    say("next:")
    say("  librerun up                        rebuild and start; the agent appears on the new-run page")
    say(f"  librerun run --agent {result['agent_id']} --wait     submit its sample")
    say(f"  librerun battery --agent {result['agent_id']}        the conformance battery")
    say(f"  {rel}/README.md        what to edit, and how to break it on purpose")
    return 0
