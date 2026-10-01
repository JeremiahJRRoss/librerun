"""compose.sh names the engine problem a newcomer actually has (S10).

`./scripts/demo.sh`, `librerun demo`, `librerun up` and `librerun
battery` all reach the container engine through `compose.sh`'s
`detect_engine`. It accepted docker only when `docker info` answered, and
otherwise fell through to "neither docker nor podman found. Install one
of them first" — so a newcomer whose Docker Desktop was simply not
started yet (the likeliest first-run state), or a Linux user outside the
`docker` group, was told to install what they had. S10's first-contact
walk of the README hit exactly that on its first command.

Each case runs the real script from a scratch copy, under a PATH holding
only the ordinary tools it uses plus a fake engine, so the host's own
Docker cannot answer for it.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# What compose.sh calls on the way to and just past `detect_engine`. A
# missing one would surface as a failure naming it, not as a false pass.
_TOOLS = (
    "bash", "sh", "sed", "tr", "cat", "dirname", "basename", "grep", "awk",
    "mktemp", "chmod", "rm", "mv", "cp", "head", "tail", "cut", "sort",
    "env", "id", "date", "ls", "mkdir", "touch", "wc", "tee", "sleep",
    "readlink", "realpath", "stat", "uname",
)

DAEMON_DOWN = "daemon is not answering"
NOT_INSTALLED = "neither docker nor podman found"


def _checkout(tmp_path: Path) -> Path:
    # A copy, because the success path derives agent-keys.env beside the
    # script, and a test must not write into the tree it is testing.
    work = tmp_path / "checkout"
    work.mkdir()
    for name in ("compose.sh", "compose.yaml", "agents.compose.yaml"):
        shutil.copy2(ROOT / name, work / name)
    return work


def _bin(tmp_path: Path, **fakes: str) -> Path:
    b = tmp_path / "bin"
    b.mkdir()
    for tool in _TOOLS:
        real = shutil.which(tool)
        if real:
            (b / tool).symlink_to(real)
    for name, body in fakes.items():
        script = b / name
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o755)
    return b


def _run(tmp_path: Path, bin_dir: Path, *args: str) -> subprocess.CompletedProcess:
    work = _checkout(tmp_path)
    return subprocess.run(
        [shutil.which("bash"), str(work / "compose.sh"), *args],
        cwd=work,
        env={"PATH": str(bin_dir), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_a_docker_whose_daemon_does_not_answer_is_named_as_such(tmp_path):
    bin_dir = _bin(tmp_path, docker='[ "$1" = info ] && exit 1\nexit 0\n')
    result = _run(tmp_path, bin_dir, "ps")
    assert result.returncode == 1
    assert DAEMON_DOWN in result.stderr
    assert "docker group" in result.stderr
    # The message this replaces told the person to install what they had.
    assert NOT_INSTALLED not in result.stderr


def test_with_no_engine_at_all_it_still_says_install_one(tmp_path):
    result = _run(tmp_path, _bin(tmp_path), "ps")
    assert result.returncode == 1
    assert NOT_INSTALLED in result.stderr
    assert DAEMON_DOWN not in result.stderr


def test_podman_is_still_the_fallback_when_docker_does_not_answer(tmp_path):
    # The new message must not pre-empt the fallback it sits after.
    log = tmp_path / "podman.log"
    bin_dir = _bin(
        tmp_path,
        docker='[ "$1" = info ] && exit 1\nexit 0\n',
        podman=f'echo "$@" >> {log}\nexit 0\n',
        **{"podman-compose": f'echo "podman-compose $@" >> {log}\nexit 0\n'},
    )
    result = _run(tmp_path, bin_dir, "ps")
    assert DAEMON_DOWN not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    assert "podman-compose" in log.read_text()


@pytest.mark.parametrize("verb", ["ps", "config"])
def test_a_docker_that_answers_is_used(tmp_path, verb):
    # The positive control: the same harness, a daemon that answers, and
    # the command reaches `docker compose`.
    log = tmp_path / "docker.log"
    bin_dir = _bin(tmp_path, docker=f'echo "$@" >> {log}\nexit 0\n')
    result = _run(tmp_path, bin_dir, verb)
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert "info" in calls
    assert any(c.startswith("compose ") and c.endswith(verb) for c in calls), calls


def test_compose_sh_derives_no_registry_from_origin(tmp_path):
    """A release is source only (A2; L37, D26), so compose.sh no longer
    turns this checkout's ``origin`` into ``ghcr.io/<owner>``: compose
    receives no LIBRERUN_IMAGE_PREFIX it was not given, and its own
    ``localhost/librerun`` default names the build. ``git`` is on PATH and
    ``origin`` names an owner — everything the derivation used to need."""
    work = _checkout(tmp_path)
    home = {"PATH": "", "HOME": str(tmp_path)}
    git = shutil.which("git")
    for args in (["init", "-q"], ["remote", "add", "origin", "git@github.com:Some-Owner/librerun.git"]):
        subprocess.run([git, *args], cwd=work, env={**home, "PATH": str(Path(git).parent)}, check=True)
    log = tmp_path / "docker.log"
    bin_dir = _bin(tmp_path, docker=f'echo "ARGS $*" >> {log}\nenv >> {log}\nexit 0\n')
    (bin_dir / "git").symlink_to(git)

    result = subprocess.run(
        [shutil.which("bash"), str(work / "compose.sh"), "config"],
        cwd=work, env={**home, "PATH": str(bin_dir)}, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    seen = log.read_text()
    assert "ARGS compose " in seen, seen
    assert "LIBRERUN_IMAGE_PREFIX" not in seen
    assert "ghcr.io" not in seen and "some-owner" not in seen.lower()
