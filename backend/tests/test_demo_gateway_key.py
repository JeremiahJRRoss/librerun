"""The demo's gateway store key (K7-06; D33).

``scripts/demo.sh`` and ``librerun demo`` put ``LIBRERUN_GATEWAY_SECRETS_KEY``
in the demo's own ``gateway.env`` — the file the gateway alone reads — so a
provider key pasted in Admin -> Settings has somewhere to be kept. JR's
decision of 2026-09-20 stands: once K7 has merged, the demo generates both
store keys (D14, D33).

What these hold, on ``test_agents_network.py``'s scratch checkout with the
REAL demo.sh:

* a fresh demo writes ``gateway.env`` owner-only, holding one FERNET key —
  the gateway refuses to boot on anything else — which is not the backend's
  and is never in ``.env``, the file the backend reads;
* an existing ``gateway.env`` without the key gets one dated line, once;
* every choice already made is left alone: the key or its ``_FILE`` in the
  file, a blank one included (which means "unconfigured"), a file named in
  ``LIBRERUN_GATEWAY_ENV_FILE``, and anything outside demo mode;
* ``librerun demo`` does the same, and ``librerun up`` never does.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

from app import secrets_keyring as keyring
from tests.test_agents_network import _demo_tree, _run_demo

REPO = Path(__file__).resolve().parents[2]
CLI_SRC = REPO / "cli" / "src"
if str(CLI_SRC) not in sys.path:
    sys.path.insert(0, str(CLI_SRC))

from librerun import _env, _stack  # noqa: E402

VARIABLE = "LIBRERUN_GATEWAY_SECRETS_KEY"
BACKEND = "LIBRERUN_BACKEND_SECRETS_KEY"
DEMO_ENV = "LIBRERUN_DEMO=true\nLIBRERUN_STUB_LLM=true\n"


def _tree(path: Path, env_text: str | None, gateway_text: str | None = None) -> Path:
    path.mkdir(parents=True)
    root = _demo_tree(path, env_text)
    if gateway_text is not None:
        (root / "gateway.env").write_text(gateway_text)
        (root / "gateway.env").chmod(0o600)
    return root


def _gateway_key(root: Path) -> str:
    (line,) = [l for l in (root / "gateway.env").read_text().splitlines() if l.startswith(f"{VARIABLE}=")]
    return line.split("=", 1)[1]


def _assert_fernet(value: str) -> None:
    assert len(value) == 44 and value.endswith("="), "not the 44 characters of a Fernet key"
    Fernet(value.encode())
    assert len(keyring.parse(value, variable=VARIABLE)) == 1  # the gateway's own boot check


def _demo(root: Path, **environ: str) -> str:
    env = {
        k: v for k, v in os.environ.items()
        if k not in (VARIABLE, f"{VARIABLE}_FILE", "LIBRERUN_GATEWAY_ENV_FILE", "LIBRERUN_DEMO")
    }
    env.update(environ)
    result = subprocess.run(
        ["bash", "scripts/demo.sh", "--env-only"], cwd=root, capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_demo_writes_its_own_gateway_env(tmp_path):
    root = _tree(tmp_path / "demo", None)
    out = _run_demo(root)

    gateway = root / "gateway.env"
    assert "wrote gateway.env (the gateway's store key, mode 600)" in out
    assert stat.S_IMODE(gateway.stat().st_mode) == 0o600, "gateway.env holds a key: owner-only"
    key = _gateway_key(root)
    _assert_fernet(key)

    env_text = (root / ".env").read_text()
    assert VARIABLE not in env_text, "the gateway's key reached the file the backend reads"
    (backend_line,) = [l for l in env_text.splitlines() if l.startswith(f"{BACKEND}=")]
    assert backend_line.split("=", 1)[1] != key, "the two store keys must differ (D33)"

    before = gateway.read_text()
    assert "gateway.env" not in _run_demo(root)
    assert gateway.read_text() == before, "a second run rewrote gateway.env"


def test_an_existing_gateway_env_gets_one_line_and_a_choice_is_left_alone(tmp_path):
    operators = "OPENAI_API_KEY=sk-the-operators-own-key\n"
    root = _tree(tmp_path / "topped", DEMO_ENV, operators)
    out = _demo(root)
    assert "provisioned the gateway's store key in gateway.env" in out
    text = (root / "gateway.env").read_text()
    assert text.startswith(operators), "a line the file already had was rewritten"
    assert text.splitlines()[-2].startswith("# Added by scripts/demo.sh on ")
    _assert_fernet(_gateway_key(root))
    assert stat.S_IMODE((root / "gateway.env").stat().st_mode) == 0o600
    assert "gateway.env" not in _demo(root)
    assert (root / "gateway.env").read_text() == text

    choices = {
        "an explicit blank": (DEMO_ENV, operators + f"{VARIABLE}=\n", {}),
        "a _FILE": (DEMO_ENV, operators + f"{VARIABLE}_FILE=/run/secrets/gateway_secrets_key\n", {}),
        "not the demo": (DEMO_ENV.replace("LIBRERUN_DEMO=true", "LIBRERUN_DEMO=false"), operators, {}),
    }
    for label, (env_text, gateway_text, environ) in choices.items():
        root = _tree(tmp_path / label.replace(" ", "-"), env_text, gateway_text)
        assert "gateway.env" not in _demo(root, **environ), label
        assert (root / "gateway.env").read_text() == gateway_text, label

    # A file the operator names elsewhere is theirs: nothing is written.
    for label, env_text, environ in (
        ("named in .env", DEMO_ENV + "LIBRERUN_GATEWAY_ENV_FILE=/run/decrypted/gateway.env\n", {}),
        ("named in the environment", DEMO_ENV, {"LIBRERUN_GATEWAY_ENV_FILE": "/run/decrypted/gateway.env"}),
    ):
        root = _tree(tmp_path / label.replace(" ", "-"), env_text)
        assert "gateway.env" not in _demo(root, **environ), label
        assert not (root / "gateway.env").exists(), label


def test_a_readable_gateway_env_is_made_owner_only_before_the_key(tmp_path, monkeypatch):
    """Codex on #170: a line appended keeps the file's mode, and a
    ``gateway.env`` copied from the example is usually 0644, so the store key
    would sit in a file every local account can read. The script and the CLI
    make the file owner-only first, and a file this user may not make
    owner-only gets no key at all."""
    operators = "OPENAI_API_KEY=sk-the-operators-own-key\n"
    script = _tree(tmp_path / "script", DEMO_ENV, operators)
    (script / "gateway.env").chmod(0o644)
    assert "provisioned the gateway's store key in gateway.env" in _demo(script)
    assert stat.S_IMODE((script / "gateway.env").stat().st_mode) == 0o600
    _assert_fernet(_gateway_key(script))

    cli = _tree(tmp_path / "cli", DEMO_ENV, operators)
    (cli / "gateway.env").chmod(0o644)
    assert _stack.top_up_gateway_env(cli, _env.DotEnv(cli / ".env", environ={})) == "added"
    assert stat.S_IMODE((cli / "gateway.env").stat().st_mode) == 0o600
    _assert_fernet(_gateway_key(cli))

    # Not this user's to make owner-only: a chmod that fails. The script's
    # through a `chmod` on PATH that refuses, the CLI's through os.chmod.
    refusing = tmp_path / "refusing-bin"
    refusing.mkdir()
    (refusing / "chmod").write_text("#!/bin/sh\nexit 1\n")
    (refusing / "chmod").chmod(0o755)
    theirs = _tree(tmp_path / "theirs-script", DEMO_ENV, operators)
    (theirs / "gateway.env").chmod(0o644)
    env = {k: v for k, v in os.environ.items() if k not in (VARIABLE, f"{VARIABLE}_FILE", "LIBRERUN_GATEWAY_ENV_FILE", "LIBRERUN_DEMO")}
    env["PATH"] = f"{refusing}{os.pathsep}{env['PATH']}"
    result = subprocess.run(
        ["bash", "scripts/demo.sh", "--env-only"], cwd=theirs, capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
    assert "not yours to make owner-only" in result.stderr
    assert (theirs / "gateway.env").read_text() == operators, "a key went into a file others can read"

    theirs = _tree(tmp_path / "theirs-cli", DEMO_ENV, operators)
    (theirs / "gateway.env").chmod(0o644)

    def refused(path, mode, *args, **kwargs):
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(_stack.os, "chmod", refused)
    assert _stack.top_up_gateway_env(theirs, _env.DotEnv(theirs / ".env", environ={})) == "not-owner"
    assert (theirs / "gateway.env").read_text() == operators, "a key went into a file others can read"


def test_the_cli_writes_as_the_script_does_and_up_never_does(tmp_path, monkeypatch):
    root = _tree(tmp_path / "cli", DEMO_ENV)
    env = _env.DotEnv(root / ".env", environ={})
    assert _stack.top_up_gateway_env(root, env) == "wrote"
    assert stat.S_IMODE((root / "gateway.env").stat().st_mode) == 0o600
    _assert_fernet(_gateway_key(root))
    assert _stack.top_up_gateway_env(root, env) is None  # once

    (root / "gateway.env").write_text("OPENAI_API_KEY=sk-x\n")
    assert _stack.top_up_gateway_env(root, env) == "added"
    _assert_fernet(_gateway_key(root))

    for label, env_text, gateway_text, environ in (
        ("an explicit blank", DEMO_ENV, f"{VARIABLE}=\n", {}),
        ("a _FILE", DEMO_ENV, f"{VARIABLE}_FILE=/run/secrets/k\n", {}),
        ("named elsewhere", DEMO_ENV, None, {"LIBRERUN_GATEWAY_ENV_FILE": "/elsewhere.env"}),
        ("not the demo", DEMO_ENV.replace("true\nLIBRERUN_STUB", "false\nLIBRERUN_STUB"), None, {}),
    ):
        case = _tree(tmp_path / f"cli-{label.replace(' ', '-')}", env_text, gateway_text)
        assert _stack.top_up_gateway_env(case, _env.DotEnv(case / ".env", environ=environ)) is None, label
        if gateway_text is None:
            assert not (case / "gateway.env").exists(), label
        else:
            assert (case / "gateway.env").read_text() == gateway_text, label

    # `librerun demo --env-only` writes it; `librerun up` never does.
    up = _tree(tmp_path / "up", DEMO_ENV)
    for name in (VARIABLE, f"{VARIABLE}_FILE", "LIBRERUN_GATEWAY_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_stack, "compose_up", lambda *a, **k: None)
    assert _stack.cmd_up(up, SimpleNamespace(no_build=True, no_wait=True, quiet=True)) == 0
    assert not (up / "gateway.env").exists()
    assert _stack.cmd_demo(up, SimpleNamespace(env_only=True, pull=False, no_wait=True)) == 0
    _assert_fernet(_gateway_key(up))
