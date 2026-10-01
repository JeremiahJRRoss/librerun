"""``compose.sh``, driven from the CLI.

The CLI never runs ``docker compose`` itself: ``compose.sh`` is the one
place that detects the engine, derives ``agent-keys.env`` from the
``LIBRERUN_AGENT_KEY_*`` lines of ``.env`` before every command, and
placeholders the keys of agents whose profiles were not requested. Going
around it would be going around the gate that stops an unprovisioned
agent from starting keyless.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ._common import CliError

# The profiles a full stack runs under: the platform (``app``), the
# bundled Jaeger (``viewer``), the example containers (``demo``) and the
# agents ``librerun init`` scaffolds (``agents``). ``scripts/demo.sh``
# starts the first three; the CLI adds the fourth so a scaffolded agent
# starts with everything else.
PROFILES = ("app", "viewer", "demo", "agents")


def profile_args(profiles=PROFILES) -> list[str]:
    args: list[str] = []
    for profile in profiles:
        args += ["--profile", profile]
    return args


def argv(root: Path, *args: str, profiles=PROFILES) -> list[str]:
    return [str(root / "compose.sh"), *profile_args(profiles), *args]


def run(
    root: Path,
    *args: str,
    profiles=PROFILES,
    env: dict | None = None,
    capture: bool = False,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run ``compose.sh`` with the profiles, from the checkout root."""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    command = argv(root, *args, profiles=profiles)
    try:
        result = subprocess.run(
            command,
            cwd=str(root),
            env=merged,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CliError(f"cannot run {command[0]}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CliError(f"{' '.join(command)} did not finish in {timeout:.0f}s") from exc
    if check and result.returncode != 0:
        detail = ""
        if capture:
            detail = (result.stderr or result.stdout or "").strip()
            detail = f": {detail[-2000:]}" if detail else ""
        raise CliError(
            f"`{' '.join(command[1:])}` failed (exit {result.returncode}){detail}",
            code=result.returncode or 1,
        )
    return result


def engine_report() -> dict:
    """Which container engine this machine has, and whether it answers.

    ``docker`` first when its daemon answers, else ``podman`` — the same
    order ``compose.sh`` uses, so the CLI reports the engine the wrapper
    is about to pick. ``COMPOSE_ENGINE`` forces one, as it does there.
    """
    forced = os.environ.get("COMPOSE_ENGINE")
    candidates = [forced] if forced else ["docker", "podman"]
    report = {"engine": None, "binary": None, "daemon": False, "compose": None, "tried": []}
    for name in candidates:
        binary = shutil.which(name)
        entry = {"name": name, "binary": binary, "daemon": False, "compose": None}
        report["tried"].append(entry)
        if not binary:
            continue
        if _answers([binary, "info"]):
            entry["daemon"] = True
        elif name == "docker" and not forced:
            # compose.sh falls through to podman when the docker daemon
            # does not answer; so does this.
            continue
        entry["compose"] = _compose_flavour(name, binary)
        if entry["daemon"] or forced or name == "podman":
            report.update(engine=name, binary=binary, daemon=entry["daemon"], compose=entry["compose"])
            break
    return report


def _answers(command: list[str]) -> bool:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _compose_flavour(name: str, binary: str) -> str | None:
    if name == "docker":
        if _answers([binary, "compose", "version"]):
            return "docker compose"
        if shutil.which("docker-compose"):
            return "docker-compose"
        return None
    if shutil.which("podman-compose"):
        return "podman-compose"
    if _answers([binary, "compose", "version"]):
        return "podman compose"
    return None
