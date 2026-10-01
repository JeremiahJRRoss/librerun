"""``doctor``: what this machine and this checkout can and cannot do.

Every line is a fact with a verdict — ``[ok]``, ``[warn]`` or ``[FAIL]``
— and the exit status is 1 when anything FAILED, so a script can rely on
it. The first section is the container engine, and it fails loudly: a
machine without Docker or Podman can run none of the other commands, and
the report says so before anything else.

What is reported, in order: the engine; the checkout; ``.env`` and its
posture (demo, stub), with the secrets store's key — set, blank (the store
unconfigured) or an entry that is not a Fernet key, never the key itself
(K6); ``gateway.env`` — present or absent, and which
provider keys it names (names only, never a value; K1 §4) — with a warning
for a provider key left in the root ``.env``, which reaches nothing since
K1, and named through ``LIBRERUN_GATEWAY_ENV_FILE`` when that is set and
this ``compose.yaml`` reads it (K3: whether the named file exists and is
owner-only); a ``.env`` absent from disk while the ``LIBRERUN_AGENT_KEY_*``
names are in the environment, read as ``sops exec-env`` running as
designed rather than as a missing file (K3, mode B), and a key line the
shell outranks; the agents on disk, each with its runtime, its compose service and
whether its gateway key is provisioned (and whether a rotation is in
flight); the host ports; the backend, the gateway and the trace endpoint
when the stack is up, including which agents on disk the backend has
NOT registered; and, given credentials (``--email``, ``--password-stdin``
or the two variables — never a password on argv), who the sign-in is and
whether they administer the platform, and for a platform admin the secret
settings whose row no configured key opens (K6). A password goes only to the base
URL named with ``--base-url``, or to the stack the engine says is this
checkout's, and only once ``/api/v1/meta`` answers as LibreRun (K4b,
``_credentials.py``). Behind the HTTPS edge (T1) the base is ``--base-url``
or ``LIBRERUN_URL``, verified against ``--cacert`` or ``LIBRERUN_CA_FILE``;
with no ``--base-url`` the engine is asked about this checkout's ``edge``,
and the password goes to the address it publishes on, under the URL's host
name — the name the certificate carries.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import stat
from pathlib import Path
from urllib.parse import urlsplit

from . import _compose, _credentials, _http
from ._agents import read_fragment, scan_agents
from ._common import CliError, say
from ._env import AGENT_KEY_PREFIX, AGENT_KEY_PREVIOUS_SUFFIX, DotEnv, agent_key_variable, bind_host, bind_port
from ._stack import addresses

PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_AI_API_KEY")
GATEWAY_ENV = "gateway.env"
GATEWAY_ENV_FILE_VARIABLE = "LIBRERUN_GATEWAY_ENV_FILE"
STORE_KEY_VARIABLE = "LIBRERUN_BACKEND_SECRETS_KEY"
# K7 (D33): the gateway's own store key, in gateway.env and never the backend's.
GATEWAY_STORE_KEY_VARIABLE = "LIBRERUN_GATEWAY_SECRETS_KEY"
# A Fernet key: 32 bytes as url-safe base64, 43 characters and one '='.
# The backend's own rule (app/secrets_keyring.py), restated with the stdlib
# because the CLI depends on nothing.
_FERNET_KEY = re.compile(r"^[A-Za-z0-9_-]{43}=$")


class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.warned = 0

    def ok(self, text: str) -> None:
        say(f"  [ok]   {text}")

    def warn(self, text: str) -> None:
        self.warned += 1
        say(f"  [warn] {text}")

    def fail(self, text: str) -> None:
        self.failed += 1
        say(f"  [FAIL] {text}")

    def section(self, title: str) -> None:
        say(f"\n{title}")


def check_engine(report: Report) -> bool:
    report.section("Container engine")
    engine = _compose.engine_report()
    tried = ", ".join(
        f"{t['name']}: {'not on PATH' if not t['binary'] else ('daemon answers' if t['daemon'] else 'daemon does not answer')}"
        for t in engine["tried"]
    )
    if not engine["engine"] or not engine["binary"]:
        report.fail(
            f"no container engine: {tried}. LibreRun runs under Docker or "
            f"Podman and nothing here works without one — install Docker "
            f"(https://docs.docker.com/get-docker/) or Podman, then run "
            f"`librerun doctor` again."
        )
        return False
    if not engine["daemon"]:
        report.fail(
            f"{engine['engine']} is installed at {engine['binary']} but its "
            f"daemon does not answer `{engine['engine']} info` ({tried}). "
            f"Start it (`sudo systemctl start docker`; Podman on Linux has no "
            f"daemon, so `{engine['engine']} info` names the fault) and run "
            f"`librerun doctor` again."
        )
        return False
    report.ok(f"{engine['engine']} at {engine['binary']}, daemon answers")
    if engine["compose"]:
        report.ok(f"compose: {engine['compose']}")
    else:
        report.fail(
            f"{engine['engine']} has no compose: install the compose plugin "
            f"(Docker) or podman-compose (`pip install podman-compose`)"
        )
        return False
    return True


def check_checkout(report: Report, root: Path) -> None:
    report.section(f"Checkout: {root}")
    for name in ("compose.yaml", "compose.sh", "agents.compose.yaml", "backend/agents"):
        if (root / name).exists():
            report.ok(name)
        else:
            report.fail(f"{name} is missing — is this a complete LibreRun clone?")


def check_env(report: Report, root: Path, env: DotEnv) -> None:
    report.section(".env")
    exported = env.environment_names(AGENT_KEY_PREFIX)
    if not env.exists:
        if not exported:
            report.warn("no .env: `librerun demo` writes the zero-config one (demo mode, stub LLM, generated credentials)")
            return
        # K3, mode B: `sops exec-env` hands the file's values to compose.sh
        # through the environment and nothing plaintext is on disk.
        report.ok(
            f"no .env on disk, and {len(exported)} agent key variable(s) in the environment "
            f"({', '.join(exported)}): the values arrive through the shell — `sops exec-env`, "
            f"docs/platform/Install.md \"Encrypting .env at rest\" — which is that mode running as designed, "
            f"not a missing file"
        )
    else:
        mode = stat.S_IMODE(env.path.stat().st_mode)
        if mode & 0o077:
            report.warn(f".env is mode {mode:04o}; it holds the secret and the admin password — `chmod 600 .env`")
        else:
            report.ok(".env present, owner-only")
        for name in exported:
            if not env.file_has(name):
                report.ok(f"{name} comes from this shell and not from .env: the environment outranks the file (K3)")
            elif (env.values().get(name) or "").strip() == (env.effective(name) or "").strip():
                report.ok(f"{name} is also exported in this shell, with the same value as its .env line")
            else:
                report.warn(
                    f"{name} is exported in this shell and differs from its .env line: the environment wins "
                    f"(K3) — the containers start on the shell's value, and `librerun key rotate` refuses "
                    f"while it does. Unset it, or make .env agree."
                )
    demo = _truthy(env.effective("LIBRERUN_DEMO"))
    stub = _truthy(env.effective("LIBRERUN_STUB_LLM"))
    report.ok(f"LIBRERUN_DEMO={'true' if demo else 'not true'}   LIBRERUN_STUB_LLM={'true' if stub else 'not true'}")
    if not demo and not (env.effective("APP_SECRET_KEY") or "").strip():
        report.fail("APP_SECRET_KEY is blank and LIBRERUN_DEMO is not true: the backend refuses the default secret outside demo mode")
    check_store_key(report, env)
    if not demo and not stub and not _gateway_env_names(root, env):
        # K7: a provider key may be pasted in Admin -> Settings instead, which
        # this read cannot see; the signed-in check below lists them.
        report.ok(
            "no provider key in gateway.env and LIBRERUN_STUB_LLM is not true: model calls use "
            "the keys pasted in Admin -> Settings, or are refused without one"
        )
    stale = [k for k in PROVIDER_KEYS if (env.values().get(k) or "").strip()]
    if stale:
        report.warn(
            f"{', '.join(stale)} set in the root .env: since K1 no service reads a "
            f"provider key from there — it reaches nothing. Move it to {GATEWAY_ENV} "
            f"(cp gateway.env.example gateway.env) and delete it here."
        )


def check_store_key(report: Report, env: DotEnv) -> None:
    """The secrets store's key (K6, D33): set, blank or malformed. Never
    the key — an entry is named by its position."""
    raw = (env.effective(STORE_KEY_VARIABLE) or "").strip()
    path = (env.effective(STORE_KEY_VARIABLE + "_FILE") or "").strip()
    if not raw and path:
        report.ok(
            f"{STORE_KEY_VARIABLE} is set, from a file ({STORE_KEY_VARIABLE}_FILE={path}); "
            f"the backend reads it at boot and refuses a malformed one"
        )
        return
    if not raw:
        report.warn(
            f"{STORE_KEY_VARIABLE} is blank (unless a compose overlay delivers it as a file): "
            f"the secrets store is unconfigured, so a secret set in Admin -> Settings answers "
            f"503 secrets_store_unconfigured and each secret setting reads its environment "
            f"variable instead. Generate one: head -c 32 /dev/urandom | base64 | tr '+/' '-_'"
        )
        return
    entries = [entry.strip() for entry in raw.split(",")]
    bad = [str(position) for position, entry in enumerate(entries, 1) if not _FERNET_KEY.match(entry)]
    if bad:
        report.fail(
            f"{STORE_KEY_VARIABLE}: entr{'y' if len(bad) == 1 else 'ies'} {', '.join(bad)} of "
            f"{len(entries)} {'is' if len(bad) == 1 else 'are'} not a Fernet key (44 characters of "
            f"url-safe base64 ending in '='), so the backend refuses to boot — the value is not "
            f"shown here"
        )
        return
    rotation = "; the first seals, the others still open what they sealed until the rewrap" if len(entries) > 1 else ""
    report.ok(f"{STORE_KEY_VARIABLE} is set: {len(entries)} Fernet key(s){rotation}")


def check_store_rows(
    report: Report, base: str, token: str, *, cafile: str | None = None, address: str | None = None
) -> None:
    """The secret settings whose row no configured key opens, counted from
    the settings API with the sign-in (K6): names, never a value."""
    status, rows = _http.get(
        f"{base}/api/v1/admin/settings", token=token, follow_redirects=False, cafile=cafile, address=address
    )
    if status != 200 or not isinstance(rows, list):
        report.warn(f"the secret settings could not be read (/api/v1/admin/settings answered {status})")
        return
    secrets = [row for row in rows if isinstance(row, dict) and row.get("value_type") == "secret"]
    unreadable = [
        str(row.get("key"))
        for row in secrets
        if isinstance(row.get("secret"), dict) and row["secret"].get("source") == "unreadable"
    ]
    if unreadable:
        report.warn(
            f"{len(unreadable)} secret setting(s) have a row no configured key opens: "
            f"{', '.join(unreadable)} — the environment applies meanwhile; Replace or Clear each in "
            f"Admin -> Settings (`python -m app.scripts.rewrap_secrets --dry-run` names them too)"
        )
    else:
        report.ok(f"secret settings: {len(secrets)}, none with a row the configured key cannot open")


def sealing_fingerprint(public_key_pem: str) -> str | None:
    """``SHA256:<hex>`` of the public key's SubjectPublicKeyInfo DER — the
    gateway's boot log and the admin page compute the same (D34). Stdlib: the
    PEM's body is the DER in base64."""
    body = "".join(
        line.strip() for line in public_key_pem.splitlines() if line.strip() and not line.startswith("-----")
    )
    try:
        der = base64.b64decode(body, validate=True)
    except (ValueError, TypeError):
        return None
    return "SHA256:" + hashlib.sha256(der).hexdigest() if der else None


def check_providers(
    report: Report, base: str, token: str, *, cafile: str | None = None, address: str | None = None
) -> None:
    """What the gateway holds for each provider, and the key it seals to
    (K7): names, sources and fingerprints, never a key — read with the
    sign-in, from the same vetted address, the way the settings are."""
    status, body = _http.get(
        f"{base}/api/v1/admin/providers", token=token, follow_redirects=False, cafile=cafile, address=address
    )
    if status != 200 or not isinstance(body, dict):
        report.warn(f"the model providers could not be read (/api/v1/admin/providers answered {status})")
        return
    if not body.get("reported"):
        report.warn("the gateway has not reported what it holds yet (is it running, and at K7 or later?)")
        return
    words = []
    rejected = []
    for entry in body.get("providers") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        row, source = entry.get("row"), entry.get("source")
        if row == "pending":
            words.append(f"{name} pending")
        elif row == "rejected":
            words.append(f"{name} rejected ({entry.get('reason')})")
            rejected.append(str(name))
        elif source == "runtime":
            words.append(f"{name} runtime · {entry.get('fingerprint')}")
        elif source == "env":
            words.append(f"{name} from gateway.env")
        else:
            words.append(f"{name} not set")
    report.ok(f"model providers: {'; '.join(words) or 'none reported'}")
    if rejected:
        report.warn(
            f"{', '.join(rejected)}: a stored key the gateway could not open — gateway.env serves "
            f"meanwhile; Replace or Clear it in Admin -> Settings"
        )
    pem = body.get("public_key_pem")
    fingerprint = sealing_fingerprint(pem) if isinstance(pem, str) else None
    if fingerprint:
        report.ok(
            f"the gateway seals provider keys to {fingerprint} — compare it once with the gateway's "
            f"boot line (gateway_sealing_key) and the admin page (D34)"
        )
    else:
        report.warn(
            f"the gateway published no key to seal to: {GATEWAY_STORE_KEY_VARIABLE} is blank in "
            f"gateway.env, so a provider key cannot be pasted in Admin -> Settings"
        )


def _strip_url(value: str) -> str:
    """A URL as far as doctor prints it: scheme, host, port and path (K9, C14).

    Userinfo, query and fragment go, since each can carry a credential; a
    bare authority stays bare; what this cannot take apart prints as
    ``unparsed``. The backend's ``strip_url`` (``deployment_view.py``),
    restated with the stdlib because the CLI depends on nothing.
    """
    text = value.strip()
    if not text:
        return text
    bare = "//" not in text
    try:
        parts = urlsplit(f"//{text}" if bare else text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "unparsed"
    if not host:
        return "unparsed"
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    shown = f"{netloc}{parts.path}" if bare else f"{parts.scheme}://{netloc}{parts.path}"
    if any(mark in shown for mark in "@?#"):
        return "unparsed"
    return shown


def _shown(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def check_deployment(
    report: Report, base: str, token: str, *, cafile: str | None = None, address: str | None = None
) -> None:
    """The deployment as the backend reads it (K9, C14): each allowlisted
    setting with its source, the OTLP header variables by presence, the
    gateway's version and report, and the transport — read with the
    sign-in, from the same vetted address, as the settings and providers
    are. The view holds no secret, so none can be printed (K9-04)."""
    status, body = _http.get(
        f"{base}/api/v1/admin/deployment", token=token, follow_redirects=False, cafile=cafile, address=address
    )
    report.section("Deployment (as the backend reads it)")
    if status != 200 or not isinstance(body, dict):
        report.warn(
            f"the deployment view could not be read (/api/v1/admin/deployment answered {status}); "
            f"the Trace endpoint section reads .env instead"
        )
        return
    for row in body.get("settings") or []:
        if isinstance(row, dict):
            report.ok(f"{row.get('name')}={_shown(row.get('value'))} ({row.get('source')})")
    for header in body.get("otlp_headers") or []:
        if isinstance(header, dict):
            report.ok(f"{header.get('name')} {'set' if header.get('set') else 'not set'}")
    gateway = body.get("gateway") if isinstance(body.get("gateway"), dict) else {}
    if gateway.get("reported"):
        report.ok(f"gateway {gateway.get('version')}, reported {gateway.get('updated_at')}")
    else:
        report.warn("the gateway has not reported its version yet (is it running?)")
    transport = body.get("transport") if isinstance(body.get("transport"), dict) else {}
    if transport.get("scheme") == "https":
        report.ok(f"transport: https at {transport.get('host')}, through the edge")
    else:
        report.ok(f"transport: {transport.get('scheme')} at {transport.get('host')} — plain HTTP, no edge in front")


def _gateway_env_path(root: Path, env: DotEnv) -> tuple[Path, str]:
    """The file the gateway loads, and how it was named."""
    override = env.effective(GATEWAY_ENV_FILE_VARIABLE)
    if override and override.strip():
        compose_text = (root / "compose.yaml").read_text(encoding="utf-8")
        if GATEWAY_ENV_FILE_VARIABLE in compose_text:
            # Compose resolves a relative env_file path against the
            # compose file's directory, not the caller's — so does this.
            named = Path(override.strip())
            return (named if named.is_absolute() else root / named), f"{GATEWAY_ENV_FILE_VARIABLE}"
        return root / GATEWAY_ENV, f"{GATEWAY_ENV_FILE_VARIABLE} is set but this compose.yaml does not read it — the gateway still loads {GATEWAY_ENV}"
    return root / GATEWAY_ENV, GATEWAY_ENV


def _gateway_env_names(root: Path, env: DotEnv) -> list[str]:
    path, _ = _gateway_env_path(root, env)
    if not path.is_file():
        return []
    values = DotEnv(path, environ={}).values()
    names = []
    for key in PROVIDER_KEYS:
        if (values.get(key) or "").strip() or (values.get(key + "_FILE") or "").strip():
            names.append(key)
    return names


def check_gateway_env(report: Report, root: Path, env: DotEnv) -> None:
    report.section("gateway.env (the provider credentials, K1)")
    path, how = _gateway_env_path(root, env)
    if how == GATEWAY_ENV_FILE_VARIABLE:
        report.ok(f"supplied through {GATEWAY_ENV_FILE_VARIABLE}={path}")
    elif how != GATEWAY_ENV:
        report.warn(how)
    if not path.is_file():
        if how == GATEWAY_ENV_FILE_VARIABLE:
            report.warn(
                f"{GATEWAY_ENV_FILE_VARIABLE} names {path}, which does not exist: the gateway would start "
                f"holding no provider credential (its env_file is `required: false`). When the file exists "
                f"only inside `sops exec-file` (no plaintext on disk), run doctor inside that same command; "
                f"otherwise fix the path, or read this line as expected while LIBRERUN_STUB_LLM=true."
            )
        else:
            report.ok(f"{path.name if path.parent == root else path} absent: the gateway holds no provider credential (fine while LIBRERUN_STUB_LLM=true; copy gateway.env.example to add one)")
        check_gateway_store_key(report, env, DotEnv(path, environ={}))
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        report.warn(f"{path} is mode {mode:04o}; it holds provider keys — `chmod 600 {path}`")
    names = _gateway_env_names(root, env)
    if names:
        report.ok(f"{path} present; provider keys named: {', '.join(names)}   (names only — values are never read out)")
    else:
        # K7: a file holding only the gateway's store key is the demo's, and
        # a provider key may live in the admin page instead.
        report.ok(
            f"{path} present, naming no provider key: the keys pasted in Admin -> Settings serve, "
            f"or add one here"
        )
    check_gateway_store_key(report, env, DotEnv(path, environ={}))


def check_gateway_store_key(report: Report, env: DotEnv, gateway: DotEnv) -> None:
    """The gateway's store key (K7, D33): set, blank, not a Fernet key, or
    one the backend's list also holds. Never a key — an entry is named by
    its position."""
    values = gateway.values()
    raw = (values.get(GATEWAY_STORE_KEY_VARIABLE) or "").strip()
    path = (values.get(GATEWAY_STORE_KEY_VARIABLE + "_FILE") or "").strip()
    if not raw and path:
        report.ok(
            f"{GATEWAY_STORE_KEY_VARIABLE} is set, from a file ({GATEWAY_STORE_KEY_VARIABLE}_FILE={path}); "
            f"the gateway reads it at boot and refuses a malformed one or the backend's"
        )
        return
    if not raw:
        report.warn(
            f"{GATEWAY_STORE_KEY_VARIABLE} is blank (unless a compose overlay delivers it as a file): "
            f"the gateway publishes no key to seal to, so a provider key cannot be pasted in "
            f"Admin -> Settings and gateway.env's keys serve alone. Generate one: "
            f"head -c 32 /dev/urandom | base64 | tr '+/' '-_'"
        )
        return
    entries = [entry.strip() for entry in raw.split(",")]
    bad = [str(position) for position, entry in enumerate(entries, 1) if not _FERNET_KEY.match(entry)]
    if bad:
        report.fail(
            f"{GATEWAY_STORE_KEY_VARIABLE}: entr{'y' if len(bad) == 1 else 'ies'} {', '.join(bad)} of "
            f"{len(entries)} {'is' if len(bad) == 1 else 'are'} not a Fernet key (44 characters of "
            f"url-safe base64 ending in '='), so the gateway refuses to boot — the value is not shown here"
        )
        return
    backend = {entry.strip() for entry in (env.effective(STORE_KEY_VARIABLE) or "").split(",") if entry.strip()}
    if backend & set(entries):
        report.fail(
            f"{GATEWAY_STORE_KEY_VARIABLE} and {STORE_KEY_VARIABLE} share a key: the two store keys "
            f"must differ (D33), and the gateway refuses to boot once the backend has sealed a row "
            f"under it. Generate a new one for gateway.env — the keys are not shown here"
        )
        return
    rotation = "; the first seals, the others still open what they sealed until the rewrap" if len(entries) > 1 else ""
    report.ok(f"{GATEWAY_STORE_KEY_VARIABLE} is set: {len(entries)} Fernet key(s), none of them the backend's{rotation}")


def check_agents(report: Report, root: Path, env: DotEnv) -> list:
    report.section("Agents on disk (backend/agents, backend/agents/_examples)")
    agents = scan_agents(root)
    if not agents:
        report.warn("no agent found: `librerun init <name> --template langgraph|container-python|container-ts` scaffolds one")
        return agents
    services = {s.agent_id: s for s in read_fragment(root) if s.agent_id}
    configured_path = (env.effective("LIBRERUN_AGENTS_PATH") or "").strip() if env.exists else ""
    if configured_path and configured_path != "agents:agents/_examples":
        report.warn(f"LIBRERUN_AGENTS_PATH={configured_path}: the backend scans those roots; this report lists the demo layout's")
    for summary in agents:
        line = f"{summary.id}  ({summary.runtime}, {summary.directory.relative_to(root)})"
        if summary.runtime == "container":
            service = services.get(summary.id)
            if service is None:
                report.fail(f"{line}: no service in agents.compose.yaml carries librerun.agent_id: {summary.id} — nothing starts this container")
                continue
            line += f", service {service.name}"
            try:
                variable = agent_key_variable(summary.id)
            except Exception as exc:  # noqa: BLE001 — the id is the chassis's to judge
                report.fail(f"{line}: {exc}")
                continue
            if not env.is_set(variable):
                report.warn(f"{line}: {variable} not provisioned — `librerun up` adds it before starting anything")
            elif not (env.effective(variable) or "").startswith("lr_agent_"):
                report.fail(f"{line}: {variable} does not begin lr_agent_ — a provider key in an agent's variable is refused by the gateway at boot")
            elif env.is_set(variable + AGENT_KEY_PREVIOUS_SUFFIX):
                report.warn(f"{line}: key provisioned; a rotation is in flight ({variable}{AGENT_KEY_PREVIOUS_SUFFIX} set) — `librerun key rotate {summary.id} --finish` when every container holds the new key")
            else:
                report.ok(f"{line}: key provisioned")
        else:
            report.ok(line)
    return agents


def check_ports(report: Report, env: DotEnv) -> None:
    report.section("Host ports")
    for label, key, default in (
        ("backend", "BACKEND_PORT", "8000"),
        ("frontend", "FRONTEND_PORT", "3000"),
        ("gateway", "GATEWAY_PORT", "8090"),
        ("postgres", "POSTGRES_PORT", "5432"),
        ("redis", "REDIS_PORT", "6379"),
        ("jaeger", "JAEGER_UI_PORT", "16686"),
    ):
        binding = (env.effective(key) or "").strip() or default
        host, port = bind_host(binding), bind_port(binding)
        probe_host = "127.0.0.1" if host == "localhost" else host
        if _listening(probe_host, int(port)):
            report.ok(f"{label}: {host}:{port} is listening (LibreRun's if the stack is up; someone else's otherwise)")
        else:
            report.ok(f"{label}: {host}:{port} is free (the stack is not up, or this service is not started)")


def check_stack(
    report: Report,
    root: Path,
    env: DotEnv,
    agents: list,
    *,
    base_url: str | None = None,
    credentials: tuple[str | None, str | None] = (None, None),
    local: bool = True,
    cafile: str | None = None,
) -> None:
    """The backend — at ``base_url`` when the operator named one, else at
    ``LIBRERUN_URL`` or the published port — and, when ``local``, this
    machine's gateway and trace endpoint."""
    urls = addresses(env)
    backend = (base_url or urls["backend"]).rstrip("/")
    report.section(f"Backend at {backend}")
    status, health = _http.get(f"{backend}/api/v1/health", timeout=5, cafile=cafile)
    if status != 200 or not isinstance(health, dict):
        error = health.get("error") if isinstance(health, dict) else None
        hint = _credentials.tls_hint(backend, error)
        report.warn(
            f"not answering /api/v1/health ({status or 'no connection'})"
            + (hint or ": the stack is not up (`librerun up`), or BACKEND_PORT or LIBRERUN_URL differs")
        )
        return
    report.ok(f"/api/v1/health: {health.get('status')}")
    detector = health.get("pii_detector") or {}
    state = detector.get("state")
    if state == "ready":
        report.ok(f"PII detector ready (coverage {detector.get('coverage')})")
    else:
        report.fail(f"PII detector state is {state!r}: the platform fails closed, so intake and every run are refused until it is")
    status, meta = _http.get(f"{backend}/api/v1/meta", timeout=5, cafile=cafile)
    if status == 200 and isinstance(meta, dict):
        report.ok(f"/api/v1/meta: demo={meta.get('demo')} stub_llm={meta.get('stub_llm')} gateway={meta.get('gateway')} agents={len(meta.get('agents') or [])}")
        registered = {a.get("id") for a in (meta.get("agents") or [])}
        for summary in agents:
            if summary.id in registered:
                report.ok(f"registered: {summary.id}")
            else:
                report.warn(f"on disk but not registered: {summary.id} — a new agent appears after `librerun up` rebuilds the backend; otherwise `librerun logs backend` names the manifest error")
        if meta.get("trace_viewer_configured"):
            report.ok(f'"View trace" links render ({meta.get("trace_viewer")}, source {meta.get("trace_viewer_source")})')
        else:
            report.warn('no "View trace" links: TRACE_VIEWER is off or its viewer is not configured')
    else:
        report.warn(f"/api/v1/meta answered {status}")
    check_sign_in(report, root, backend, credentials, named=base_url is not None, cafile=cafile)

    if not local:
        say("\n(the gateway and trace checks are skipped: they are this machine's, and no engine answers here)")
        return
    report.section(f"Gateway at {urls['gateway']}")
    status, healthz = _http.get(f"{urls['gateway']}/healthz", timeout=5)
    if status == 200 and isinstance(healthz, dict):
        stub = healthz.get("stub")
        report.ok(f"/healthz: {healthz.get('status')}, stub={'true — keyless, every step answered from fixtures' if stub else 'false — routing to the configured providers'}")
        gstate = (healthz.get("pii_detector") or {}).get("state")
        if gstate == "ready":
            report.ok("outbound redaction's detector ready")
        else:
            report.fail(f"the gateway's PII detector state is {gstate!r}: outbound model calls are refused until it is ready")
    else:
        report.warn(f"not answering /healthz ({status or 'no connection'}): the gateway is not up, or GATEWAY_PORT differs")

    report.section("Trace endpoint")
    endpoint = env.effective("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint is None:
        report.ok("OTEL_EXPORTER_OTLP_ENDPOINT unset: the backend and the gateway export to the bundled Vector (http://vector:4317)")
    elif endpoint.strip() == "":
        report.warn("OTEL_EXPORTER_OTLP_ENDPOINT is explicitly empty: trace export is off")
    else:
        report.ok(f"OTEL_EXPORTER_OTLP_ENDPOINT={_strip_url(endpoint)}")
    if _truthy(env.effective("VECTOR_VIEWER")):
        status, _ = _http.get(f"{urls['jaeger']}/", timeout=5)
        if status == 200:
            report.ok(f"Jaeger UI answers at {urls['jaeger']}")
        else:
            report.warn(f"VECTOR_VIEWER=1 but nothing answers at {urls['jaeger']}: start the viewer profile (`librerun up` does)")
    else:
        report.ok("VECTOR_VIEWER unset: traces are not forwarded to the bundled Jaeger")


def check_sign_in(
    report: Report,
    root: Path,
    base: str,
    credentials: tuple[str | None, str | None],
    *,
    named: bool,
    cafile: str | None = None,
) -> None:
    """Who the credentials sign in as, and whether that is a platform admin.

    Sent only when both halves are there, only to a base URL the operator
    named or to this checkout's own stack — its backend for an http base,
    its HTTPS edge for an https one (T1) — and only once the answer is
    LibreRun's; ``_credentials.py`` holds the rules."""
    email, password = credentials
    address = None
    if not email and not password:
        report.ok(f"not signed in (no credentials given): to check a sign-in, {_credentials.HOW_TO_SIGN_IN}")
        return
    if not email or not password:
        missing = "an email" if not email else "a password"
        report.warn(f"not signed in: {missing} is missing, so nothing was sent — {_credentials.HOW_TO_SIGN_IN}")
        return
    if not named and urlsplit(base).scheme == "https":
        # The https port is the edge's (T1): ask for this checkout's edge,
        # and send to the address it publishes on, under the URL's host.
        vetted, address, why = _credentials.this_checkouts_edge(root, base)
        if not vetted:
            report.warn(
                f"not signed in: {why}, so nothing was sent — that address may be another "
                f"checkout's edge or another program. Start this checkout's edge "
                f"(`./compose.sh --profile app --profile tls up -d`), or name the URL to sign "
                f"in to with --base-url."
            )
            return
    elif not named:
        pinned, why = _credentials.this_checkouts_backend(root, base)
        if pinned is None:
            report.warn(
                f"not signed in: {why}, so nothing was sent — that address may be another "
                f"checkout's stack or another program. Start this checkout's stack "
                f"(`librerun up`), or name the backend to sign in to with --base-url."
            )
            return
        # The address the engine vouched for, and no other.
        base = pinned
    try:
        token = _credentials.sign_in(base, email, password, cafile=cafile, address=address)
        profile = _credentials.whoami(base, token, cafile=cafile, address=address)
    except _credentials.NothingSent as exc:
        report.fail(f"not signed in: {exc}")
        return
    except CliError as exc:
        report.fail(f"sign-in: {exc}")
        return
    flag = profile.get("is_platform_admin")
    admin = {True: "yes", False: "no"}.get(flag, "unknown (this backend does not report it)")
    report.ok(f"signed in as {profile.get('email') or email}; platform admin: {admin}")
    if flag is True:
        # Only a platform admin may read the settings; the same vetted
        # address and token, nothing sent anywhere new.
        check_store_rows(report, base, token, cafile=cafile, address=address)
        check_providers(report, base, token, cafile=cafile, address=address)
        check_deployment(report, base, token, cafile=cafile, address=address)
    else:
        report.warn(
            "the deployment view is a platform operator's (/api/v1/admin/deployment), so it was not asked; "
            "the Trace endpoint section reads .env instead"
        )


def _listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on", "t", "y")


def cmd_doctor(root: Path, args) -> int:
    # Read before the report, so a prompt comes first and a bad pipe
    # stops doctor before anything is checked or sent.
    credentials = _credentials.from_args(args)
    # Refused by its path, before anything is checked or sent.
    cafile = _credentials.ca_file(args)
    base_url = (getattr(args, "base_url", None) or "").strip().rstrip("/") or None
    report = Report()
    say(f"librerun doctor — {root}")
    engine_ok = check_engine(report)
    check_checkout(report, root)
    env = DotEnv(root / ".env")
    check_env(report, root, env)
    check_gateway_env(report, root, env)
    agents = check_agents(report, root, env)
    check_ports(report, env)
    if engine_ok or base_url:
        # A backend the operator named may be on another machine, which
        # needs no engine on this one.
        check_stack(report, root, env, agents, base_url=base_url, credentials=credentials, local=engine_ok, cafile=cafile)
    else:
        say("\n(the stack checks are skipped: no engine can have started it)")
    say()
    if report.failed:
        say(f"doctor: {report.failed} failure(s), {report.warned} warning(s) — fix the [FAIL] lines first")
        return 1
    say(f"doctor: no failures, {report.warned} warning(s)")
    return 0
