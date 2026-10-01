"""The demo's secrets-store key (K6-01, K6-02; D33).

``scripts/demo.sh`` and ``librerun demo`` write ``LIBRERUN_BACKEND_SECRETS_KEY``
into the demo ``.env`` so the demo can take a secret in Admin -> Settings,
and it must be a FERNET key — 32 random bytes as url-safe base64 — because
the backend refuses to boot on anything else. ``random_hex 32`` and
``token_hex(32)`` name 32 bytes as 64 hex characters, which is not one.

A demo ``.env`` written before K6 is topped up with one dated line, exactly
as a key is added for an agent the file predates — and never over a
choice already made: a key, an explicit blank (which means
"unconfigured"), a ``_FILE``, or the variable in the environment. Outside
demo mode nothing is added, and ``librerun up`` never adds one. The file
is made owner-only before the key goes in, and one this user may not make
owner-only gets no key (the review after K7).

On ``test_agents_network.py``'s scratch checkout: the REAL demo.sh and the
REAL agent directories.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app import secrets_keyring as keyring
from tests.test_agents_network import _demo_tree, _run_demo

REPO = Path(__file__).resolve().parents[2]
CLI_SRC = REPO / "cli" / "src"
if str(CLI_SRC) not in sys.path:
    sys.path.insert(0, str(CLI_SRC))

from librerun import _env, _stack  # noqa: E402

VARIABLE = "LIBRERUN_BACKEND_SECRETS_KEY"


def _tree(path: Path, env_text: str | None) -> Path:
    """``_demo_tree`` in a directory of its own, so one test can hold several."""
    path.mkdir(parents=True)
    return _demo_tree(path, env_text)


def _key_lines(root: Path) -> list[str]:
    return [line for line in (root / ".env").read_text().splitlines() if line.startswith(f"{VARIABLE}")]


def _assert_fernet(value: str) -> None:
    assert len(value) == 44 and value.endswith("="), "not the 44 characters of a Fernet key"
    Fernet(value.encode())  # raises on anything that is not one
    assert len(keyring.parse(value)) == 1  # the backend's own boot check


def _demo(root: Path, **environ: str) -> str:
    """demo.sh --env-only with the two names this module is about under
    the test's control, and nothing else of the test process's changed."""
    env = {k: v for k, v in os.environ.items() if k not in (VARIABLE, f"{VARIABLE}_FILE", "LIBRERUN_DEMO")}
    env.update(environ)
    result = subprocess.run(
        ["bash", "scripts/demo.sh", "--env-only"], cwd=root, capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _pre_k6_demo_env(tmp_path) -> str:
    """A demo .env as demo.sh wrote it before K6: today's, without the key."""
    fresh = _tree(tmp_path / "fresh", None)
    _run_demo(fresh)
    return "\n".join(
        line
        for line in (fresh / ".env").read_text().splitlines()
        if VARIABLE not in line and "secrets set in Admin" not in line
    ) + "\n"


def test_demo_writes_fernet_key(tmp_path):
    root = _tree(tmp_path / "demo", None)
    _run_demo(root)

    (line,) = _key_lines(root)
    _assert_fernet(line.split("=", 1)[1])

    # The CLI writes the same shape, and a new one each time.
    text, _ = _stack.demo_env_text(root)
    (cli_line,) = [l for l in text.splitlines() if l.startswith(f"{VARIABLE}=")]
    _assert_fernet(cli_line.split("=", 1)[1])
    assert cli_line != line
    _assert_fernet(_stack.fernet_key())


def test_top_up_leaves_a_choice_alone(tmp_path):
    pre_k6 = _pre_k6_demo_env(tmp_path)
    assert VARIABLE not in pre_k6 and "LIBRERUN_DEMO=true" in pre_k6

    # A pre-K6 demo .env gets one key, on one dated line, once.
    root = _tree(tmp_path / "topped", pre_k6)
    out = _demo(root)
    assert "provisioned the secrets store key" in out
    text = (root / ".env").read_text()
    (line,) = _key_lines(root)
    _assert_fernet(line.split("=", 1)[1])
    assert text.splitlines()[-2].startswith("# Added by scripts/demo.sh on ")
    assert text.startswith(pre_k6), "a line the file already had was rewritten"
    assert "provisioned the secrets store key" not in _demo(root)
    assert (root / ".env").read_text() == text

    # Every choice already made is left as it is.
    choices = {
        "an explicit blank": (pre_k6 + f"{VARIABLE}=\n", {}),
        "a _FILE": (pre_k6 + f"{VARIABLE}_FILE=/run/secrets/backend_secrets_key\n", {}),
        "the environment's": (pre_k6, {VARIABLE: ""}),
        "the environment's _FILE": (pre_k6, {f"{VARIABLE}_FILE": "/run/secrets/k"}),
        "not the demo": (pre_k6.replace("LIBRERUN_DEMO=true", "LIBRERUN_DEMO=false"), {}),
    }
    for label, (env_text, environ) in choices.items():
        root = _tree(tmp_path / label.replace(" ", "-").replace("'", ""), env_text)
        out = _demo(root, **environ)
        assert "provisioned the secrets store key" not in out, label
        assert (root / ".env").read_text() == env_text, label


def test_a_readable_env_is_made_owner_only_before_the_key(tmp_path, monkeypatch):
    """The review after K7, the class Codex found in K7's gateway.env: a
    line appended keeps the file's mode, and an ``.env`` written by hand is
    usually 0644, so the store key would sit in a file every local account
    can read. The script and the CLI make the file owner-only first, and a
    file this user may not make owner-only gets no key at all."""
    pre_k6 = _pre_k6_demo_env(tmp_path)

    script = _tree(tmp_path / "script", pre_k6)
    (script / ".env").chmod(0o644)
    assert "provisioned the secrets store key" in _demo(script)
    assert stat.S_IMODE((script / ".env").stat().st_mode) == 0o600
    (line,) = _key_lines(script)
    _assert_fernet(line.split("=", 1)[1])

    cli = _tree(tmp_path / "cli", pre_k6)
    (cli / ".env").chmod(0o644)
    env = _env.DotEnv(cli / ".env", environ={})
    assert _stack.top_up_store_key(env) == "added"
    assert stat.S_IMODE((cli / ".env").stat().st_mode) == 0o600
    env.write()
    assert stat.S_IMODE((cli / ".env").stat().st_mode) == 0o600
    (line,) = _key_lines(cli)
    _assert_fernet(line.split("=", 1)[1])

    # Not this user's to make owner-only: a chmod that fails. The script's
    # through a `chmod` on PATH that refuses — the gateway's file named
    # elsewhere, so only the store key is asked for — the CLI's through
    # os.chmod.
    refusing = tmp_path / "refusing-bin"
    refusing.mkdir()
    (refusing / "chmod").write_text("#!/bin/sh\nexit 1\n")
    (refusing / "chmod").chmod(0o755)
    theirs = _tree(tmp_path / "theirs-script", pre_k6)
    (theirs / ".env").chmod(0o644)
    environ = {k: v for k, v in os.environ.items() if k not in (VARIABLE, f"{VARIABLE}_FILE", "LIBRERUN_DEMO")}
    environ["PATH"] = f"{refusing}{os.pathsep}{environ['PATH']}"
    environ["LIBRERUN_GATEWAY_ENV_FILE"] = "elsewhere.env"
    result = subprocess.run(
        ["bash", "scripts/demo.sh", "--env-only"], cwd=theirs, capture_output=True, text=True, env=environ
    )
    assert result.returncode == 0, result.stderr
    assert ".env is not yours to make owner-only" in result.stderr
    assert (theirs / ".env").read_text() == pre_k6, "a key went into a file others can read"

    theirs = _tree(tmp_path / "theirs-cli", pre_k6)
    (theirs / ".env").chmod(0o644)

    def refused(path, mode, *args, **kwargs):
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(_stack.os, "chmod", refused)
    env = _env.DotEnv(theirs / ".env", environ={})
    assert _stack.top_up_store_key(env) == "not-owner"
    assert env.text == pre_k6, "a key was queued for a file others can read"
    assert (theirs / ".env").read_text() == pre_k6


def test_the_cli_tops_up_as_the_script_does_and_up_never_does(tmp_path, monkeypatch):
    pre_k6 = _pre_k6_demo_env(tmp_path)

    env = _env.DotEnv(tmp_path / "missing.env", environ={})
    env.text = pre_k6
    assert _stack.top_up_store_key(env) == "added"
    (line,) = [l for l in env.text.splitlines() if l.startswith(f"{VARIABLE}=")]
    _assert_fernet(line.split("=", 1)[1])
    assert env.text.startswith(pre_k6)
    assert _stack.top_up_store_key(env) is None  # once

    for label, text, environ in (
        ("an explicit blank", pre_k6 + f"{VARIABLE}=\n", {}),
        ("a _FILE", pre_k6 + f"{VARIABLE}_FILE=/run/secrets/k\n", {}),
        ("the environment's", pre_k6, {VARIABLE: ""}),
        ("not the demo", pre_k6.replace("LIBRERUN_DEMO=true", "LIBRERUN_DEMO=false"), {}),
    ):
        env = _env.DotEnv(tmp_path / "missing.env", environ=environ)
        env.text = text
        assert _stack.top_up_store_key(env) is None, label
        assert env.text == text, label

    # `librerun demo --env-only` writes it into a pre-K6 file; `librerun up`
    # never touches the key.
    root = _tree(tmp_path / "cli", pre_k6)
    monkeypatch.delenv(VARIABLE, raising=False)
    monkeypatch.delenv(f"{VARIABLE}_FILE", raising=False)
    monkeypatch.setattr(_stack, "compose_up", lambda *a, **k: None)
    assert _stack.cmd_up(root, SimpleNamespace(no_build=True, no_wait=True, quiet=True)) == 0
    assert _key_lines(root) == []
    assert _stack.cmd_demo(root, SimpleNamespace(env_only=True, pull=False, no_wait=True)) == 0
    (line,) = _key_lines(root)
    _assert_fernet(line.split("=", 1)[1])


@pytest.mark.parametrize("value", ["ab" * 32, "0123456789abcdef" * 4])
def test_a_hex_key_is_not_a_fernet_key(value):
    """What `random_hex 32` and `token_hex(32)` print: refused by the
    backend's parser, which is why neither generator may be used."""
    with pytest.raises(keyring.SecretsStoreKeyInvalid):
        keyring.parse(value)
