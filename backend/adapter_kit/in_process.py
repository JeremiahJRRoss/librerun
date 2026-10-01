"""``python -m adapter_kit.in_process --agent-dir DIR``: the adapter
battery for one ``python-package`` agent, from its directory (blueprint S6).

The battery's Python API (``run_battery``) takes an agent OBJECT, which
is what a test holds and what ``librerun battery`` does not: the CLI has
a directory. This driver is the missing step — it loads that one
directory the way the chassis's discovery does, in isolation, and runs
the battery with the manifest's own phases and output mode, so an agent
that passes here is an agent the runner will accept.

Isolation matters: ``discover_agents`` scans a whole roots directory, and
the agent under test sits beside the bundled ones. So the directory is
linked alone into a scratch root and that root is scanned; the agent's
imports resolve through the link and every other agent stays out of the
process.

Exit status 0 when the battery passed, 1 when it did not, 2 when it could
not run at all (no manifest, no scenario, the agent failed to load) —
each with the reason printed, because "red" without a reason is a
battery nobody can act on. ``--json`` prints the report as JSON after the
summary, on its own block, for a CI step to read.

Tracing is initialised with an SDK ``TracerProvider`` when none is
installed, because ``require_traces`` (the battery's third promise) needs
spans to be recordable at all.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from adapter_kit import Scenario, run_battery
from app.agents import registry
from app.agents.manifest import ManifestError, load_manifest


def _refuse(reason: str) -> None:
    """Could not run at all — exit 2 with the reason, distinct from a
    battery that ran and failed (1)."""
    print(reason, file=sys.stderr)
    sys.exit(2)


def _tracing() -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())


def _scenario_path(agent_dir: Path, manifest, wanted: str | None) -> Path:
    scen_dir = agent_dir / manifest.scenarios
    files = sorted(scen_dir.glob("*.json")) if scen_dir.is_dir() else []
    if wanted:
        for path in files:
            if path.stem == wanted:
                return path
        _refuse(f"the battery cannot run: no scenario {wanted!r} under {scen_dir} (found {[p.stem for p in files]})")
    if not files:
        _refuse(
            f"the battery cannot run: no scenario under {scen_dir}. Add one — a JSON "
            f"file with a name and a user_inputs object that satisfies the agent's "
            f"input schema; it is also the sample the new-run page offers."
        )
    return files[0]


def load_agent(agent_dir: Path):
    """Discover exactly this agent, through the chassis's own discovery,
    and return ``(agent, manifest)`` or exit with the reason."""
    try:
        manifest = load_manifest(agent_dir)
    except ManifestError as exc:
        _refuse(f"the battery cannot run: {exc}")
    if manifest.runtime != "python-package":
        _refuse(
            f"the battery cannot run in-process: {manifest.id} is a "
            f"{manifest.runtime!r} agent. Drive its URL with "
            f"`python -m adapter_kit.run_contract --url ... --agent-dir {agent_dir}` "
            f"(`librerun battery --agent {manifest.id}` does)."
        )
    scratch = Path(tempfile.mkdtemp(prefix="librerun-battery-"))
    link = scratch / agent_dir.name
    try:
        os.symlink(agent_dir.resolve(), link, target_is_directory=True)
    except (OSError, NotImplementedError):
        shutil.copytree(agent_dir, link)
    registry._clear_registry_for_tests()
    registry.discover_agents(scratch)
    agent = registry.get_agent(manifest.id)
    if agent is None:
        _refuse(
            f"the battery cannot run: discovery did not register {manifest.id!r} "
            f"from {agent_dir}. The log lines above name the cause — a manifest "
            f"the loader refused, an import that failed, an agent_id that "
            f"disagrees with the manifest id, or no AgentProtocol subclass in "
            f"agent.py."
        )
    return agent, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the adapter conformance battery against one python-package agent directory."
    )
    parser.add_argument("--agent-dir", required=True, help="the agent directory (agent.yaml, agent.py, scenarios/)")
    parser.add_argument("--scenario", help="a scenario file stem under scenarios/ (default: the first)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON too")
    args = parser.parse_args(argv)

    agent_dir = Path(args.agent_dir)
    if not (agent_dir / "agent.yaml").is_file():
        _refuse(f"the battery cannot run: {agent_dir} carries no agent.yaml")
    _tracing()
    agent, manifest = load_agent(agent_dir)
    scenario_file = _scenario_path(agent_dir, manifest, args.scenario)
    scenario = Scenario.from_file(scenario_file)

    result = asyncio.run(
        run_battery(
            agent,
            scenario,
            phases=manifest.phase_names(),
            output_mode=manifest.output.mode,
        )
    )
    print(result.summary())
    if args.json:
        print(
            json.dumps(
                {
                    "passed": result.passed,
                    "agent_id": result.agent_id,
                    "scenario": scenario_file.stem,
                    "phases_run": result.phases_run,
                    "failures": result.failures,
                    "progress": [list(p) for p in result.progress],
                    "span_names": result.span_names,
                    "redactions": result.redactions,
                },
                indent=2,
                default=str,
            )
        )
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
