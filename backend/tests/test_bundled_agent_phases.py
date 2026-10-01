"""Every bundled agent implements the phases its manifest declares.

The `_template` scaffold was the documented way to start an agent until
blueprint S6 replaced it with `librerun init`. A batch once removed its
obsolete step-config methods — correct, the gateway owns that now — and
took `analyze` and `investigate` out with them, which have nothing to do
with step config. The manifest still declared both phases, so a copy of
the scaffold raised `NotImplementedError` on its first phase and no run
from the documented template could ever complete (Codex round 18, P1).
The scaffold is gone; the templates `librerun init` renders are held to
the same rule in `tests/test_librerun_cli.py`.

Two guards, because each misses what the other catches:

- **the manifests**, read from disk, against the agent classes' own
  methods. Scoped to every bundled agent, so the same over-deletion
  anywhere else fails here too;
- **the dispatch**, in `test_run_phase_requires_an_override` below,
  because `AgentProtocol` DEFINES `analyze` and `investigate`. `getattr`
  finds them for an agent implementing neither, so `run_phase`'s own
  "declares phase X but defines no matching method" — written for
  exactly this case — was unreachable for the two phase names most
  agents declare. What an author got instead was a bare
  `NotImplementedError`. That is why this was quiet rather than loud.
"""
from __future__ import annotations

import ast
import pathlib

import pytest
import yaml

AGENTS = pathlib.Path(__file__).resolve().parents[1] / "agents"


def _runtime(manifest: pathlib.Path) -> str:
    loaded = yaml.safe_load(manifest.read_text()) or {}
    return str(loaded.get("runtime") or "python-package")


def _bundled():
    """Every IN-PROCESS agent directory: a manifest, an ``agent.py``, and
    a runtime this process actually executes.

    The runtime is read from the manifest rather than inferred from the
    presence of ``agent.py``, which is what this used to do. A container
    agent may perfectly well call its module ``agent.py`` — the
    LlamaIndex example does — and it has no ``AgentProtocol`` subclass at
    all, because the chassis reaches it over HTTP and the phase is
    implemented on the other side of that wire. The old rule reported
    that as a broken agent; the echo example escaped only because its
    module happens to be called ``echo_agent.py``, which is an accident
    and not a rule.

    Container agents are not thereby unchecked. That a container answers
    the phases its manifest declares is what the container battery
    drives (`.github/workflows/container-battery.yml`, one matrix entry
    per example) and what `test_container_runner.py` exercises against a
    live one — neither of which this in-process guard could do.
    """
    for manifest in sorted(AGENTS.rglob("agent.yaml")):
        module = manifest.parent / "agent.py"
        if module.exists() and _runtime(manifest) == "python-package":
            yield manifest, module


def test_the_excluded_directories_are_excluded_for_their_runtime():
    """A selection rule that narrowed for the wrong reason would take
    agents out of the guard silently — which is how the rule it replaced
    behaved. So state what is left out, and why.

    ``rglob`` is deliberately not re-derived here: this walks the same
    tree and asserts that every directory with an ``agent.py`` is either
    under the guard or a container.
    """
    on_disk = {
        m.parent.name: _runtime(m)
        for m in sorted(AGENTS.rglob("agent.yaml"))
        if (m.parent / "agent.py").exists()
    }
    guarded = {m.parent.name for m, _ in _bundled()}
    excluded = {name: runtime for name, runtime in on_disk.items() if name not in guarded}

    assert on_disk, "no agent directories at all — this guard checked nothing"
    assert all(runtime == "container" for runtime in excluded.values()), (
        f"these directories have an agent.py but are outside the guard for "
        f"a reason other than being containers: {excluded}"
    )


def _declared_phases(manifest: pathlib.Path) -> list[str]:
    loaded = yaml.safe_load(manifest.read_text()) or {}
    return [
        p["name"]
        for p in (loaded.get("phases") or [])
        if isinstance(p, dict) and p.get("name")
    ]


def _methods_defined_in(module: pathlib.Path) -> set[str]:
    """Methods the agent class writes ITSELF, read from source."""
    tree = ast.parse(module.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names.update(
                f.name
                for f in node.body
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return names


def _agent_class(module: pathlib.Path):
    """The AgentProtocol subclass in this module, imported.

    Source alone is not enough and the LangGraph example is why: it
    extends the adapter's `LangGraphAgent`, which overrides `run_phase`
    to map phase names onto compiled graphs — the escape hatch
    `run_phase`'s own docstring offers. An AST guard sees a class with
    no `analyze` and calls it broken. Resolving the class is what
    distinguishes "dispatches differently" from "does not dispatch".
    """
    import importlib.util

    from app.agents.protocol import AgentProtocol

    spec = importlib.util.spec_from_file_location(
        f"_bundled_{module.parent.name}", module
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    for value in vars(loaded).values():
        if (
            isinstance(value, type)
            and issubclass(value, AgentProtocol)
            and value is not AgentProtocol
            and value.__module__ == loaded.__name__
        ):
            return value
    raise AssertionError(f"no AgentProtocol subclass in {module}")


def test_there_are_bundled_agents_to_check():
    """A collector that finds nothing passes every test below it."""
    found = list(_bundled())
    assert len(found) >= 2, f"expected the template and at least one example: {found}"


@pytest.mark.parametrize(
    "manifest,module",
    list(_bundled()),
    ids=lambda p: p.parent.name if isinstance(p, pathlib.Path) else str(p),
)
def test_every_declared_phase_can_actually_run(manifest, module):
    """Either a method per phase, or `run_phase` overridden — which is
    the documented alternative for an adapter mapping phases onto
    something else."""
    from app.agents.protocol import AgentProtocol

    declared = _declared_phases(manifest)
    assert declared, f"{manifest} declares no phases"

    try:
        agent_class = _agent_class(module)
    except ImportError as exc:  # an optional framework is not installed
        pytest.skip(f"{module.parent.name} needs {exc.name!r}")

    if agent_class.run_phase is not AgentProtocol.run_phase:
        return  # dispatches its own way; `run_phase` is the contract

    unimplemented = []
    for phase in declared:
        own = getattr(agent_class, phase, None)
        base = AgentProtocol.__dict__.get(phase)
        if own is None or own is base:
            unimplemented.append(phase)
    assert not unimplemented, (
        f"{module.parent.name} declares {unimplemented} in {manifest.name}, "
        f"overrides neither those methods nor run_phase, and so raises "
        f"NotImplementedError on that phase for every run"
    )


@pytest.mark.asyncio
async def test_run_phase_requires_an_override_not_merely_an_attribute():
    """The dispatch half. `AgentProtocol` defines `analyze`, so an agent
    that implements nothing still has the attribute."""
    from app.agents.protocol import AgentProtocol

    class Bare(AgentProtocol):
        agent_id, display_name, description = "bare", "Bare", "-"

    class Real(AgentProtocol):
        agent_id, display_name, description = "real", "Real", "-"

        async def analyze(self, inp, on_progress):
            return "analysed"

    with pytest.raises(NotImplementedError) as caught:
        await Bare().run_phase("analyze", None, None)
    assert "defines no matching method" in str(caught.value), (
        "an agent with no analyze() got the base class's bare "
        "NotImplementedError instead of the message naming the phase"
    )

    assert await Real().run_phase("analyze", None, None) == "analysed"
