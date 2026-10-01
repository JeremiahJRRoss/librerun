"""The HTTPS edge's loopback guard and its topology (K blueprint T1; L35, D37).

The `tls` profile puts an HTTPS edge in front of LibreRun, and that is only
true while nothing else answers off the host: Docker publishes a port with
its own iptables rules, ahead of ufw's and firewalld's, and no override file
can remove a publish. So `compose.sh`'s `tls_loopback_guard` refuses the
profile — exit 4, every line to set named, nothing started — unless
BACKEND_PORT and FRONTEND_PORT are `127.0.0.1:<port>` or `[::1]:<port>`,
NEXT_PUBLIC_API_URL is `/api/v1` and BACKEND_INTERNAL_URL is
`http://backend:8000`, each resolved as compose resolves it.

The guard is sourced out of the real script, as K3's probes source
`derive_agent_keys` (`test_agents_network.py`), so a change to the script is
a change to what this asserts; the whole script runs once on a fake engine
(`test_compose_engine_detection.py`'s harness) to show the refusal comes
before compose is asked to do anything. The topology tests read
`compose.yaml` itself. Each test drives the rule against the violation it
exists for, beside the case that must pass: on a clean tree a guard that
never looks and one that works are indistinguishable.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE_SH = REPO / "compose.sh"
COMPOSE_YAML = REPO / "compose.yaml"
COMPOSE_FILES = ("compose.yaml", "agents.compose.yaml")

# The four lines the guard holds, as the HTTPS section of Install.md and
# the tls-edge job write them.
GOOD = {
    "BACKEND_PORT": "127.0.0.1:8000",
    "FRONTEND_PORT": "127.0.0.1:3000",
    "NEXT_PUBLIC_API_URL": "/api/v1",
    "BACKEND_INTERNAL_URL": "http://backend:8000",
}
REFUSED = 4
TLS_UP = ("--profile", "app", "--profile", "tls", "up", "-d")

_SOURCE = (
    'source <(sed -n "/^tls_env_file_value()/,/^}/p" compose.sh); '
    'source <(sed -n "/^tls_loopback_guard()/,/^}/p" compose.sh); '
    'tls_loopback_guard "$@"'
)

# Names a caller's own shell must not leak into a probe: the guard reads the
# environment first, as compose does.
_GUARDED = ("BACKEND_PORT", "FRONTEND_PORT", "NEXT_PUBLIC_API_URL", "BACKEND_INTERNAL_URL", "COMPOSE_PROFILES")


def _lines(values: dict[str, str]) -> str:
    return "".join(f"{name}={value}\n" for name, value in values.items())


def _guard(
    tmp_path: Path,
    *args: str,
    env_text: str | None = "",
    environment: dict[str, str] | None = None,
    compose_text: str | None = None,
) -> subprocess.CompletedProcess:
    """Run the guard as compose.sh would, in a scratch checkout: `compose.sh`
    and `compose.yaml` copied (the guard reads compose's defaults from the
    file), `.env` written unless ``env_text`` is None."""
    (tmp_path / "compose.sh").write_text(COMPOSE_SH.read_text())
    (tmp_path / "compose.yaml").write_text(compose_text if compose_text is not None else COMPOSE_YAML.read_text())
    env_file = tmp_path / ".env"
    if env_text is None:
        env_file.unlink(missing_ok=True)
    else:
        env_file.write_text(env_text)
    shell = {name: value for name, value in os.environ.items() if name not in _GUARDED}
    shell.update(environment or {})
    return subprocess.run(
        ["bash", "-c", _SOURCE, "guard", *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=shell,
        timeout=60,
    )


def _refused(result: subprocess.CompletedProcess, *named: str) -> None:
    assert result.returncode == REFUSED, (result.returncode, result.stdout, result.stderr)
    assert "nothing was started" in result.stderr, result.stderr
    for line in named:
        assert line in result.stderr, (line, result.stderr)


def _passed(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert result.stderr == "", result.stderr


# ---------------------------------------------------------------------------
# the guard, sourced
# ---------------------------------------------------------------------------


def test_loopback_and_relative_url_pass(tmp_path):
    """The layout the HTTPS section writes passes — on IPv4 and IPv6 loopback,
    on ports of the operator's choosing, and with the port lines left to
    compose's own default, which is loopback since T1."""
    _passed(_guard(tmp_path, *TLS_UP, env_text=_lines(GOOD)))
    _passed(_guard(tmp_path, *TLS_UP, env_text=_lines({**GOOD, "BACKEND_PORT": "[::1]:8000", "FRONTEND_PORT": "[::1]:3000"})))
    _passed(_guard(tmp_path, *TLS_UP, env_text=_lines({**GOOD, "BACKEND_PORT": "127.0.0.1:8001", "FRONTEND_PORT": "127.0.0.1:3001"})))
    urls_only = {name: GOOD[name] for name in ("NEXT_PUBLIC_API_URL", "BACKEND_INTERNAL_URL")}
    _passed(_guard(tmp_path, *TLS_UP, env_text=_lines(urls_only)))


def test_non_loopback_binding_refused_by_name(tmp_path):
    """A bare port, the IPv4 and IPv6 wildcards and a LAN address are each
    refused, and the message names the line to set — keeping the operator's
    port — and why the binding is the guard."""
    for binding, port in (("8000", "8000"), ("0.0.0.0:8000", "8000"), ("192.168.1.10:8000", "8000"),
                          ("[::]:8000", "8000"), ("0.0.0.0:8001", "8001")):
        result = _guard(tmp_path, *TLS_UP, env_text=_lines({**GOOD, "BACKEND_PORT": binding}))
        _refused(result, f"BACKEND_PORT=127.0.0.1:{port}", f"it is {binding}")
        assert "FRONTEND_PORT=" not in result.stderr, "a line that holds was named"
        assert "iptables" in result.stderr and "ufw" in result.stderr and "firewalld" in result.stderr
    result = _guard(tmp_path, *TLS_UP, env_text=_lines({**GOOD, "FRONTEND_PORT": "0.0.0.0:3000"}))
    _refused(result, "FRONTEND_PORT=127.0.0.1:3000")
    assert "BACKEND_PORT=" not in result.stderr


def test_absolute_api_url_refused_rebuild_named(tmp_path):
    """The browser must call the edge's own origin: an absolute URL — http,
    which an https page blocks as mixed content, or even https to another
    origin — is refused, and the message says to rebuild, because the value
    is baked into the web UI."""
    for url in ("http://localhost:8000/api/v1", "https://librerun.test:8443/api/v1", "/api/v1/"):
        result = _guard(tmp_path, *TLS_UP, env_text=_lines({**GOOD, "NEXT_PUBLIC_API_URL": url}))
        _refused(result, "NEXT_PUBLIC_API_URL=/api/v1", f"it is {url}")
        assert "--build" in result.stderr and "mixed content" in result.stderr, result.stderr
    # Left to compose's default, the absolute URL the example ships.
    no_url = {name: value for name, value in GOOD.items() if name != "NEXT_PUBLIC_API_URL"}
    _refused(_guard(tmp_path, *TLS_UP, env_text=_lines(no_url)),
             "NEXT_PUBLIC_API_URL=/api/v1", "compose.yaml's default")


def test_values_resolve_as_compose_does(tmp_path):
    """Docker Compose's precedence: the environment wins even when empty,
    then the env file (the last assignment, in compose's dotenv syntax, or
    the `--env-file` arguments instead of .env), then compose.yaml's own
    default — read from the file — which is also what a blank becomes.
    (podman-compose below 1.6 reads no COMPOSE_PROFILES; the guard does, so
    its refusal there errs toward the binding.)"""
    wildcard = "0.0.0.0:8000"
    # The environment outranks .env, both ways.
    _refused(_guard(tmp_path, *TLS_UP, env_text=_lines(GOOD), environment={"BACKEND_PORT": wildcard}),
             "BACKEND_PORT=127.0.0.1:8000", "this shell's environment")
    _passed(_guard(tmp_path, *TLS_UP, env_text=_lines({**GOOD, "BACKEND_PORT": wildcard}),
                   environment={"BACKEND_PORT": "127.0.0.1:8000"}))
    # …even when empty: compose's ${BACKEND_PORT:-…} then takes its default.
    _passed(_guard(tmp_path, *TLS_UP, env_text=_lines({**GOOD, "BACKEND_PORT": wildcard}),
                   environment={"BACKEND_PORT": ""}))
    # The last assignment wins; a commented one is not one.
    good_last = _lines(GOOD).replace("BACKEND_PORT=127.0.0.1:8000\n", f"BACKEND_PORT={wildcard}\nBACKEND_PORT=127.0.0.1:8000\n")
    bad_last = _lines(GOOD).replace("BACKEND_PORT=127.0.0.1:8000\n", f"BACKEND_PORT=127.0.0.1:8000\nBACKEND_PORT={wildcard}\n")
    _passed(_guard(tmp_path, *TLS_UP, env_text=good_last))
    _refused(_guard(tmp_path, *TLS_UP, env_text=bad_last), "BACKEND_PORT=127.0.0.1:8000")
    commented = _lines(GOOD).replace("BACKEND_PORT=127.0.0.1:8000\n", f"# BACKEND_PORT={wildcard}\n")
    _passed(_guard(tmp_path, *TLS_UP, env_text=commented))
    # compose's dotenv syntax: quotes, a comment after an unquoted value,
    # `export`, and the YAML-style colon — the last two are assignments.
    for line, holds in (
        ('BACKEND_PORT="127.0.0.1:8000"', True),
        ("BACKEND_PORT='[::1]:8000'", True),
        ("BACKEND_PORT=127.0.0.1:8000   # loopback", True),
        ("  BACKEND_PORT = 127.0.0.1:8000", True),
        (f"export BACKEND_PORT={wildcard}", False),
        (f"BACKEND_PORT: {wildcard}", False),
        (f'BACKEND_PORT="{wildcard}"', False),
    ):
        text = _lines(GOOD).replace("BACKEND_PORT=127.0.0.1:8000\n", line + "\n")
        result = _guard(tmp_path, *TLS_UP, env_text=text)
        if holds:
            _passed(result)
        else:
            _refused(result, "BACKEND_PORT=127.0.0.1:8000")
    # --env-file replaces .env, in either spelling, the last file winning.
    (tmp_path / "other.env").write_text(_lines(GOOD))
    bad_dotenv = _lines({**GOOD, "BACKEND_PORT": wildcard})
    _passed(_guard(tmp_path, "--env-file", "other.env", *TLS_UP, env_text=bad_dotenv))
    _passed(_guard(tmp_path, "--env-file=other.env", *TLS_UP, env_text=bad_dotenv))
    (tmp_path / "later.env").write_text(f"BACKEND_PORT={wildcard}\n")
    _refused(_guard(tmp_path, "--env-file", "other.env", "--env-file", "later.env", *TLS_UP, env_text=_lines(GOOD)),
             "BACKEND_PORT=127.0.0.1:8000", "other.env later.env")
    # The defaults are compose.yaml's own, read from the file: move one and
    # the guard follows it.
    urls_only = {name: GOOD[name] for name in ("NEXT_PUBLIC_API_URL", "BACKEND_INTERNAL_URL")}
    compose = COMPOSE_YAML.read_text()
    assert "${BACKEND_PORT:-127.0.0.1:8000}" in compose and "${FRONTEND_PORT:-127.0.0.1:3000}" in compose
    moved = compose.replace("${BACKEND_PORT:-127.0.0.1:8000}", "${BACKEND_PORT:-0.0.0.0:8000}")
    _refused(_guard(tmp_path, *TLS_UP, env_text=_lines(urls_only), compose_text=moved),
             "BACKEND_PORT=127.0.0.1:8000", "it is 0.0.0.0:8000, from compose.yaml's default")
    _passed(_guard(tmp_path, *TLS_UP, env_text=_lines(urls_only)))
    default = re.search(r"\$\{NEXT_PUBLIC_API_URL:-([^}]*)\}", compose).group(1)
    no_url = {name: value for name, value in GOOD.items() if name != "NEXT_PUBLIC_API_URL"}
    _refused(_guard(tmp_path, *TLS_UP, env_text=_lines(no_url)), f"it is {default}, from compose.yaml's default")


def test_a_comment_never_stands_for_a_default(tmp_path):
    """compose.yaml's default is read from its lines, never from a comment:
    one that quotes `${BACKEND_PORT:-…}` above the real default neither
    refuses a loopback default nor lets a wildcard one through."""
    urls_only = {name: GOOD[name] for name in ("NEXT_PUBLIC_API_URL", "BACKEND_INTERNAL_URL")}
    compose = COMPOSE_YAML.read_text()
    real = '      - "${BACKEND_PORT:-127.0.0.1:8000}:8000"\n'
    assert compose.count(real) == 1, "the backend's port line moved; follow it here"
    wildcard_comment = '      # "${BACKEND_PORT:-0.0.0.0:8000}:8000" would publish on every interface\n'
    loopback_comment = '# "${BACKEND_PORT:-127.0.0.1:8000}:8000" is the default this file ships\n'
    # A wildcard in a comment, just above the loopback default: that default is judged.
    _passed(_guard(tmp_path, *TLS_UP, env_text=_lines(urls_only),
                   compose_text=compose.replace(real, wildcard_comment + real)))
    # A loopback in a comment, at the top, over a wildcard default: refused.
    moved = compose.replace(real, real.replace("127.0.0.1:8000", "0.0.0.0:8000"))
    _refused(_guard(tmp_path, *TLS_UP, env_text=_lines(urls_only), compose_text=loopback_comment + moved),
             "BACKEND_PORT=127.0.0.1:8000", "it is 0.0.0.0:8000, from compose.yaml's default")


def test_the_fourth_line_is_required(tmp_path):
    """BACKEND_INTERNAL_URL is the guard's fourth line (REC-09): with the
    bundle calling a relative /api/v1, a start WITHOUT the profile serves the
    UI on plain loopback only through the web UI's own rewrite to the
    backend. Blank, or aimed anywhere but the compose network's backend,
    it is refused by name — and the three lines that hold are not named."""
    no_rewrite = {name: value for name, value in GOOD.items() if name != "BACKEND_INTERNAL_URL"}
    for text in (_lines(no_rewrite), _lines({**no_rewrite, "BACKEND_INTERNAL_URL": ""}),
                 _lines({**no_rewrite, "BACKEND_INTERNAL_URL": "http://host.containers.internal:8000"})):
        result = _guard(tmp_path, *TLS_UP, env_text=text)
        _refused(result, "BACKEND_INTERNAL_URL=http://backend:8000")
        for holds in ("BACKEND_PORT=", "FRONTEND_PORT=", "NEXT_PUBLIC_API_URL="):
            assert holds not in result.stderr, (holds, result.stderr)
    _passed(_guard(tmp_path, *TLS_UP, env_text=_lines(GOOD)))


def test_check_runs_only_when_tls_requested(tmp_path):
    """Every line wrong, and the guard still says nothing until the profile
    is requested — then it refuses, for any subcommand, however the profile
    was asked for, `*` included: compose reads it as every profile, `tls`
    among them (Compose v5.1.1 resolves the edge under `--profile '*'` and
    `COMPOSE_PROFILES=app,*`). COMPOSE_PROFILES follows compose's
    precedence: the environment, even empty, else the env file."""
    everything_wrong = _lines({"BACKEND_PORT": "0.0.0.0:8000", "FRONTEND_PORT": "3000",
                               "NEXT_PUBLIC_API_URL": "http://localhost:8000/api/v1", "BACKEND_INTERNAL_URL": ""})
    for args, environment in (
        (("up", "-d"), {}),
        (("--profile", "app", "up", "-d"), {}),
        (("--profile", "tlsx", "up", "-d"), {}),
        (("--profile=viewer", "ps"), {}),
        (("up", "-d"), {"COMPOSE_PROFILES": "app,viewer"}),
        (("up", "-d"), {"COMPOSE_PROFILES": ""}),
    ):
        _passed(_guard(tmp_path, *args, env_text=everything_wrong + "COMPOSE_PROFILES=\n", environment=environment))
    # The environment wins even when empty, over an .env that names tls.
    _passed(_guard(tmp_path, "up", "-d", env_text=everything_wrong + "COMPOSE_PROFILES=app,tls\n",
                   environment={"COMPOSE_PROFILES": ""}))
    for args, environment, extra in (
        (TLS_UP, {}, ""),
        (("--profile=tls", "up", "-d"), {}, ""),
        (("--profile", "app,tls", "up", "-d"), {}, ""),
        (("up", "-d"), {"COMPOSE_PROFILES": "app,tls"}, ""),
        (("up", "-d"), {"COMPOSE_PROFILES": " app , tls "}, ""),
        (("up", "-d"), {}, "COMPOSE_PROFILES=app,tls\n"),
        (("--profile", "tls", "down"), {}, ""),
        (("--profile", "tls", "ps", "--format", "json"), {}, ""),
        (("--profile", "*", "up", "-d"), {}, ""),
        (("--profile=*", "up", "-d"), {}, ""),
        (("--profile", "app", "--profile", "*", "config"), {}, ""),
        (("up", "-d"), {"COMPOSE_PROFILES": "*"}, ""),
        (("up", "-d"), {"COMPOSE_PROFILES": "app, *"}, ""),
        (("up", "-d"), {}, "COMPOSE_PROFILES=*\n"),
    ):
        _refused(_guard(tmp_path, *args, env_text=everything_wrong + extra, environment=environment),
                 "BACKEND_PORT=127.0.0.1:8000", "FRONTEND_PORT=127.0.0.1:3000",
                 "NEXT_PUBLIC_API_URL=/api/v1", "BACKEND_INTERNAL_URL=http://backend:8000")


# ---------------------------------------------------------------------------
# the whole script, on a fake engine
# ---------------------------------------------------------------------------

# What compose.sh calls on its way to the guard and past it
# (test_compose_engine_detection.py's list, which this harness shares).
_TOOLS = (
    "bash", "sh", "sed", "tr", "cat", "dirname", "basename", "grep", "awk",
    "mktemp", "chmod", "rm", "mv", "cp", "head", "tail", "cut", "sort",
    "env", "id", "date", "ls", "mkdir", "touch", "wc", "tee", "sleep",
    "readlink", "realpath", "stat", "uname", "printf",
)


def _fake_engine(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A scratch checkout, and a PATH whose `docker` answers `info` and logs
    every call — so the host's own engine cannot answer for it."""
    work = tmp_path / "checkout"
    work.mkdir()
    for name in ("compose.sh", "compose.yaml", "agents.compose.yaml"):
        shutil.copy2(REPO / name, work / name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in _TOOLS:
        real = shutil.which(tool)
        if real:
            (bin_dir / tool).symlink_to(real)
    log = tmp_path / "docker.log"
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 0\n')
    docker.chmod(0o755)
    return work, bin_dir, log


def test_compose_sh_refuses_before_engine_runs(tmp_path):
    """The Accept's negative, on a fake engine: with BACKEND_PORT=0.0.0.0:8000
    exported, `./compose.sh --profile app --profile tls up -d` exits 4 naming
    BACKEND_PORT=127.0.0.1:8000, the engine is never asked to run compose,
    and nothing is written — not even agent-keys.env. The same command with
    the line fixed reaches the engine: the refusal is the guard's, not the
    harness's."""
    work, bin_dir, log = _fake_engine(tmp_path)
    (work / ".env").write_text(_lines(GOOD))
    shell = {"PATH": str(bin_dir), "HOME": str(tmp_path)}

    def compose_sh(extra: dict[str, str]) -> subprocess.CompletedProcess:
        log.write_text("")
        return subprocess.run(
            [shutil.which("bash"), str(work / "compose.sh"), *TLS_UP],
            cwd=work, env={**shell, **extra}, capture_output=True, text=True, timeout=60,
        )

    result = compose_sh({"BACKEND_PORT": "0.0.0.0:8000"})
    assert result.returncode == REFUSED, (result.returncode, result.stdout, result.stderr)
    assert "BACKEND_PORT=127.0.0.1:8000" in result.stderr, result.stderr
    calls = log.read_text().splitlines()
    assert "info" in calls, "the harness never reached the engine check — the test proves nothing"
    assert not any(call.startswith("compose -f") for call in calls), calls
    assert not (work / "agent-keys.env").exists(), "a refused command wrote agent-keys.env"

    result = compose_sh({})
    assert result.returncode == 0, result.stderr
    assert f"compose -f compose.yaml {' '.join(TLS_UP)}" in log.read_text().splitlines()


# ---------------------------------------------------------------------------
# compose.yaml: what is published, and who the backend believes
# ---------------------------------------------------------------------------


def _compose(name: str = "compose.yaml") -> dict:
    return yaml.safe_load((REPO / name).read_text())


def _with_defaults(text: str, **env: str) -> str:
    """A compose string interpolated as compose would with only ``env`` set:
    a name in ``env`` is its value, and otherwise each ``${NAME:-default}``
    or ``${NAME-default}`` is its default and ``${NAME}`` is blank."""
    return re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}",
        lambda m: env[m.group(1)] if m.group(1) in env else (m.group(2) or ""),
        text,
    )


def _published(entry) -> tuple[str, str]:
    """``(host ip, host port)`` of a ports entry, in the short syntax
    (``[HOST:]HOST_PORT:CONTAINER_PORT``) or the long one."""
    if isinstance(entry, dict):
        return str(entry.get("host_ip") or ""), str(entry.get("published") or "")
    text = _with_defaults(str(entry)).split("/", 1)[0]
    match = re.fullmatch(r"(?:(\[[^\]]*\]|[^:\[\]]*):)?(\d+):(\d+)", text) or re.fullmatch(r"(\d+)", text)
    assert match, f"a ports entry this test cannot read: {entry!r}"
    if match.re.pattern == r"(\d+)":
        return "", ""
    return (match.group(1) or "").strip("[]"), match.group(2)


def test_only_edge_publishes_off_loopback():
    """With every variable at compose's default, each published port of both
    compose files binds 127.0.0.1 or ::1 — the backend and the web UI since
    T1 — but the edge's one publish, which is the point of it."""
    loopback = {"127.0.0.1", "::1"}
    seen: dict[str, list[tuple[str, str]]] = {}
    for file in COMPOSE_FILES:
        for name, service in (_compose(file).get("services") or {}).items():
            for entry in service.get("ports") or []:
                seen.setdefault(name, []).append(_published(entry))
    off_loopback = {name: ports for name, ports in seen.items() for host, _ in ports if host not in loopback}
    assert set(off_loopback) == {"edge"}, f"published off loopback: {off_loopback}"
    assert seen["edge"] == [("", "8443")], seen["edge"]
    assert _compose()["services"]["edge"]["ports"] == ["${LIBRERUN_HTTPS_PORT:-8443}:8443"]
    # Not vacuous: the ports this rule is about were read, loopback now.
    assert seen["backend"] == [("127.0.0.1", "8000")] and seen["frontend"] == [("127.0.0.1", "3000")], seen
    assert {"postgres", "redis", "gateway", "vector", "jaeger"} <= set(seen), sorted(seen)


def _edge_network(**env: str) -> tuple[ipaddress.IPv4Network, ipaddress.IPv4Network, ipaddress.IPv4Address]:
    (config,) = _compose()["networks"]["edge"]["ipam"]["config"]
    return (ipaddress.ip_network(_with_defaults(config["subnet"], **env)),
            ipaddress.ip_network(_with_defaults(config["ip_range"], **env)),
            ipaddress.ip_address(_with_defaults(config["gateway"], **env)))


def test_forwarded_allow_ips_is_edge_address():
    """The backend believes X-Forwarded-For from the edge's fixed address and
    from nothing else: one address, never `*` and never a network (agent
    containers share the backend's `agents` network and could forge the
    headers), inside the edge's subnet and outside the range compose hands
    out dynamically, so no container started first can hold it. Every start
    creates the network, and an engine refuses a subnet another network
    holds, so `LIBRERUN_EDGE_NET` moves it — and all of this must hold
    wherever it moves, not only at the default."""
    compose = _compose()
    raw_trusted = compose["services"]["backend"]["environment"]["FORWARDED_ALLOW_IPS"]
    edge = compose["services"]["edge"]
    assert set(edge["networks"]) == {"edge"}, "the edge joins its network alone (podman-compose#1299)"
    raw_pinned = edge["networks"]["edge"]["ipv4_address"]
    for env in ({}, {"LIBRERUN_EDGE_NET": "172.16.88"}, {"LIBRERUN_EDGE_NET": "10.213.4"}):
        trusted = _with_defaults(raw_trusted, **env)
        assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", trusted), f"FORWARDED_ALLOW_IPS={trusted!r} is not one address"
        pinned = ipaddress.ip_address(_with_defaults(raw_pinned, **env))
        assert ipaddress.ip_address(trusted) == pinned, (env, trusted, pinned)
        subnet, dynamic, gateway = _edge_network(**env)
        assert pinned in subnet and pinned not in dynamic and pinned != gateway, (env, pinned, subnet, dynamic, gateway)
        assert dynamic.subnet_of(subnet) and gateway in subnet and gateway not in dynamic, env
    # …and it is one line that moves them: a second value really moved all five.
    assert _edge_network(LIBRERUN_EDGE_NET="172.16.88")[0] != _edge_network()[0]
    # The entrypoint states the intent; the value above is the trust.
    assert "--proxy-headers" in (REPO / "backend" / "entrypoint.sh").read_text()


def test_edge_network_holds_edge_backend_frontend():
    """The `edge` network carries the edge, the backend, the web UI and
    `edge-control` (T2), and no agent; it is not internal (ACME needs a
    route out); its subnet is fixed, small, and clear of both engines'
    default pools — Docker's local 172.17.0.0/16–172.31.0.0/16 and
    192.168.0.0/16 and its swarm pool 10.0.0.0/8, which also holds Podman's
    10.88.0.0/16 and up — so an engine never hands it out first. The edge
    runs under the `tls` profile alone."""
    members = set()
    for file in COMPOSE_FILES:
        for name, service in (_compose(file).get("services") or {}).items():
            networks = service.get("networks") or []
            if "edge" in (networks if isinstance(networks, list) else list(networks)):
                members.add(name)
    assert members == {"edge", "edge-control", "backend", "frontend"}, members
    network = _compose()["networks"]["edge"]
    assert not network.get("internal"), "ACME needs a route out"
    subnet, _, _ = _edge_network()
    assert subnet.num_addresses <= 256, subnet
    pools = [ipaddress.ip_network(pool) for pool in ("172.17.0.0/16", "172.18.0.0/15", "172.20.0.0/14",
                                                   "172.24.0.0/13", "192.168.0.0/16", "10.0.0.0/8")]
    assert not any(subnet.overlaps(pool) for pool in pools), subnet
    edge = _compose()["services"]["edge"]
    assert edge["profiles"] == ["tls"]
    assert edge["image"].startswith("docker.io/library/caddy:") and "@sha256:" in edge["image"], edge["image"]


# ---------------------------------------------------------------------------
# The edge's control (T2; L42, D44 refined): the socket and the keys live on
# a volume the edge and edge-control alone mount
# ---------------------------------------------------------------------------

CONTROL_VOLUME = "librerun-edge-control"
EDGE_VOLUMES = {"librerun-edge-data", CONTROL_VOLUME}
EDGE_START = REPO / "config" / "edge-start.sh"
CADDYFILE = REPO / "config" / "Caddyfile"


def _services() -> dict[str, dict]:
    services: dict[str, dict] = {}
    for file in COMPOSE_FILES:
        services.update(_compose(file).get("services") or {})
    return services


def _volume_sources(service: dict) -> set[str]:
    sources = set()
    for entry in service.get("volumes") or []:
        sources.add(str(entry.get("source") or "") if isinstance(entry, dict) else str(entry).split(":", 1)[0])
    return sources


def test_the_control_volume_is_the_edge_and_edge_control_alone():
    """L42 holds for an in-process agent too (D44 refined). The control
    volume — Caddy's admin socket and the keys a platform admin loads — is
    mounted by the edge and edge-control and by nothing else: never the
    backend, whose process an in-process agent shares, and which mounts
    neither of the edge's volumes; never the web UI or an agent container.
    edge-control is given no database URL, no secret and no agent, only the
    edge's address, and the admin API is a Unix socket on that volume and
    never TCP."""
    services = _services()
    holders = {name for name, service in services.items() if CONTROL_VOLUME in _volume_sources(service)}
    assert holders == {"edge", "edge-control"}, f"the control volume is mounted by {sorted(holders)}"
    for name, service in services.items():
        if name not in ("edge", "edge-control"):
            reached = EDGE_VOLUMES & _volume_sources(service)
            assert not reached, f"{name} mounts the edge's {sorted(reached)}"
    volumes = _compose()["volumes"]
    assert CONTROL_VOLUME in volumes and not (volumes[CONTROL_VOLUME] or {}).get("name"), (
        "the control volume is project-scoped, like librerun-edge-data"
    )
    control = services["edge-control"]
    assert control["profiles"] == ["tls"]
    assert list(control["networks"]) == ["edge"], control["networks"]
    assert set(control["environment"]) == {"EDGE_ADDRESS"}, sorted(control["environment"])
    assert not control.get("env_file"), "edge-control reads no env file: it holds no secret"
    for env in ({}, {"LIBRERUN_EDGE_NET": "172.16.88"}):
        assert _with_defaults(control["environment"]["EDGE_ADDRESS"], **env) == _with_defaults(
            services["backend"]["environment"]["FORWARDED_ALLOW_IPS"], **env
        ), "edge-control takes a change from the address the backend trusts forwarding headers from"
    assert control["command"] == ["python", "-m", "app.edge_control"]
    for name in ("edge", "edge-control"):
        mounts = [entry for entry in services[name]["volumes"] if str(entry).startswith("./config/Caddyfile:")]
        assert mounts == ["./config/Caddyfile:/etc/caddy/Caddyfile:ro,z"], (name, mounts)
    admin = [line.strip() for line in CADDYFILE.read_text().splitlines() if line.strip().startswith("admin")]
    assert admin == ["admin unix//control/admin.sock|0666"], f"the admin API must be the control volume's socket: {admin}"


def _run_edge_start(tmp_path: Path, ca: str | None = None) -> tuple[subprocess.CompletedProcess, Path, Path]:
    """config/edge-start.sh itself, on a temporary control and certificate
    directory, with a stand-in `caddy` on PATH that records its arguments."""
    control, certs, bin_dir = tmp_path / "control", tmp_path / "certs", tmp_path / "bin"
    for directory in (control, certs, bin_dir):
        directory.mkdir(exist_ok=True)
    fake = bin_dir / "caddy"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" > "{tmp_path}/caddy.args"\n')
    fake.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "EDGE_CONTROL_DIR": str(control),
        "EDGE_CERT_DIR": str(certs),
    }
    if ca is not None:
        env["LIBRERUN_TLS_CA"] = ca
    result = subprocess.run(["sh", str(EDGE_START)], env=env, capture_output=True, text=True, timeout=30)
    return result, control, certs


def test_the_start_wrapper_writes_the_environment_choice(tmp_path):
    """At every start the wrapper writes what the environment says: with
    LIBRERUN_TLS_CA naming a CA's two files, an `env-<12 hex>` entry (the
    certificate file's SHA-256) in the one pki block, a CA that
    edge-control loaded kept beside it, and the internal issuer on it;
    without, LIBRERUN_TLS's snippet and no entry. With no choice recorded
    the site imports the environment's selection; a choice is left as the
    UI wrote it. A file LIBRERUN_TLS_CA names that is missing stops the
    start, naming it, before Caddy runs: a pki entry naming files that are
    gone makes `caddy run` panic."""
    import hashlib

    from app.edge_control.edge import parse_pki, pki_text

    # Without a CA: LIBRERUN_TLS's snippet, and no pki file for the glob.
    result, control, certs = _run_edge_start(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (control / "env.caddy").read_text() == "import tls_environment\n"
    assert (control / "tls.caddy").read_text() == f"import {control}/env.caddy\n"
    assert not (control / "pki.global.caddy").exists()
    assert (tmp_path / "caddy.args").read_text() == "run --config /etc/caddy/Caddyfile --adapter caddyfile\n"

    # With the CA's two files, over a pki block edge-control wrote: the
    # environment's entry is rewritten and the loaded CA stays.
    (certs / "ca.crt").write_text("a certificate file\n")
    (certs / "ca.key").write_text("a key file\n")
    env_id = "env-" + hashlib.sha256(b"a certificate file\n").hexdigest()[:12]
    loaded = {"loaded-0123456789ab": (f"{control}/ca-0123456789ab.crt", f"{control}/ca-0123456789ab.key")}
    stale = {"env-ffffffffffff": (f"{certs}/old.crt", f"{certs}/old.key")}
    (control / "pki.global.caddy").write_text(pki_text({**stale, **loaded}))
    (tmp_path / "caddy.args").unlink()
    result, control, certs = _run_edge_start(tmp_path, f"{certs}/ca.crt  {certs}/ca.key")
    assert result.returncode == 0, result.stderr
    pki = (control / "pki.global.caddy").read_text()
    assert pki.count("pki {") == 1, pki
    assert parse_pki(pki) == {env_id: (f"{certs}/ca.crt", f"{certs}/ca.key"), **loaded}, pki
    assert pki == pki_text({env_id: (f"{certs}/ca.crt", f"{certs}/ca.key"), **loaded}), "the two writers disagree"
    assert (control / "env.caddy").read_text() == f"tls {{\n\tissuer internal {{\n\t\tca {env_id}\n\t}}\n}}\n"
    assert (control / "tls.caddy").read_text() == f"import {control}/env.caddy\n"
    assert (tmp_path / "caddy.args").exists()

    # A choice recorded on Application Settings is left as the UI wrote it,
    # across the restart; a blank LIBRERUN_TLS_CA drops its entry alone.
    (control / "tls.caddy").write_text("tls admin@example.com\n")
    (control / "choice").write_text('{"kind": "acme"}\n')
    result, control, certs = _run_edge_start(tmp_path, "")
    assert result.returncode == 0, result.stderr
    assert (control / "tls.caddy").read_text() == "tls admin@example.com\n"
    assert parse_pki((control / "pki.global.caddy").read_text()) == loaded
    assert (control / "env.caddy").read_text() == "import tls_environment\n"

    # A choice with no selection beside it goes, and the environment applies.
    (control / "tls.caddy").unlink()
    result, control, certs = _run_edge_start(tmp_path, "")
    assert result.returncode == 0, result.stderr
    assert not (control / "choice").exists()
    assert (control / "tls.caddy").read_text() == f"import {control}/env.caddy\n"

    # A file LIBRERUN_TLS_CA names that is missing stops the start, by name.
    (tmp_path / "caddy.args").unlink()
    result, control, certs = _run_edge_start(tmp_path, f"{certs}/ca.crt {certs}/gone.key")
    assert result.returncode != 0
    assert f"{certs}/gone.key" in result.stderr, result.stderr
    assert not (tmp_path / "caddy.args").exists(), "Caddy ran on a CA it cannot load"
    # …as do a path outside the certificate directory, and one word.
    for value in (f"/etc/ssl/ca.crt {certs}/ca.key", f"{certs}/ca.crt"):
        result, _, _ = _run_edge_start(tmp_path, value)
        assert result.returncode != 0 and "LIBRERUN_TLS_CA" in result.stderr, (value, result.stderr)
        assert not (tmp_path / "caddy.args").exists()
