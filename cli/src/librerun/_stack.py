"""``demo``, ``up``, ``down`` and ``logs``: the stack under compose.

``demo`` is ``scripts/demo.sh``'s logic in Python (blueprint S3, L22; S6):
on a checkout with no ``.env`` it writes one — a generated secret and
admin password, demo mode, the stub LLM, the bundled agent and the
examples, Jaeger, one gateway key per agent — then builds and starts the
stack, waits for the backend and then the gateway, and prints the URL,
the credentials and the trace viewer's address. ``up`` is the same start
on an ``.env`` that already exists; both top up the key of any agent the
file predates, exactly as the script does, and never rewrite a key it
already has.
"""
from __future__ import annotations

import base64
import datetime as _dt
import os
import secrets
import time
from pathlib import Path

from . import _compose, _credentials, _http
from ._agents import scan_agents
from ._common import CliError, say, warn
from ._env import DotEnv, agent_key_variable, bind_host, bind_port, mint_agent_key

DEMO_ADMIN_EMAIL = "admin@librerun.example"
HEALTH_WAIT_SECONDS = 300
GATEWAY_WAIT_SECONDS = 120


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def agent_key_lines(root: Path) -> list[str]:
    """One ``LIBRERUN_AGENT_KEY_<ID>=lr_agent_…`` line per agent on disk
    (blueprint S4a, D10), generated the way the script generates them."""
    lines = []
    for summary in scan_agents(root):
        try:
            lines.append(f"{agent_key_variable(summary.id)}={mint_agent_key()}")
        except CliError as exc:
            warn(f"skipping the key for {summary.directory}: {exc}")
    return lines


STORE_KEY_VARIABLE = "LIBRERUN_BACKEND_SECRETS_KEY"


def fernet_key() -> str:
    """A Fernet key (K6, D33): 32 random bytes as url-safe base64, the
    format the secrets store's key list takes — ``scripts/demo.sh``'s
    ``random_fernet_key``. ``token_hex(32)`` is 64 hex characters, which
    names 32 bytes and is not a Fernet key (K6-01)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def demo_env_text(root: Path) -> tuple[str, str]:
    """The demo ``.env`` and the admin password it carries."""
    password = f"Demo-{secrets.token_hex(8)}!1"
    body = "\n".join(
        [
            f"# Written by librerun demo on {_now()}: the zero-config",
            "# demo (LIBRERUN_DEMO=true). Delete this file and run the command again for",
            "# fresh credentials. To leave demo mode: set a real APP_SECRET_KEY, unset",
            "# LIBRERUN_DEMO, set LIBRERUN_STUB_LLM=false and add provider keys — see",
            "# .env.example for every setting.",
            "APP_ENV=development",
            f"APP_SECRET_KEY={secrets.token_hex(32)}",
            "# The key the secrets set in Admin -> Settings are sealed with (K6).",
            f"{STORE_KEY_VARIABLE}={fernet_key()}",
            "LIBRERUN_DEMO=true",
            "LIBRERUN_STUB_LLM=true",
            f"INITIAL_ADMIN_EMAIL={DEMO_ADMIN_EMAIL}",
            f"INITIAL_ADMIN_PASSWORD={password}",
            "# The bundled agent and the examples side by side.",
            "LIBRERUN_AGENTS_PATH=agents:agents/_examples",
            "# Vector forwards traces to the bundled Jaeger (viewer profile).",
            "VECTOR_VIEWER=1",
            '# "View trace" links point at that Jaeger (the shipped default is off:',
            "# no link until a viewer is really there).",
            "TRACE_VIEWER=jaeger",
            "# The demo shows prompts and completions in the trace viewer; the shipped",
            "# default is NO_CONTENT.",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_AND_EVENT",
            "# Per-agent gateway keys (blueprint S4a). One per bundled agent;",
            "# compose.sh derives agent-keys.env from these lines and the environment.",
            *agent_key_lines(root),
        ]
    )
    return body + "\n", password


def top_up_keys(env: DotEnv, root: Path) -> list[str]:
    """Append a key line for every agent the file has none for — an
    ``.env`` written before an agent existed would otherwise stop ``up``
    with a named error about a variable nobody chose to omit. A key the
    file or the environment already carries is never touched: it may be
    a rotation in flight."""
    missing = []
    for line in agent_key_lines(root):
        name = line.split("=", 1)[0]
        if env.is_set(name):
            continue
        missing.append(line)
    if not missing:
        return []
    env.append_block(
        [
            "",
            f"# Added by librerun on {_now()}: agents that",
            "# this file had no key for. Existing keys are never rewritten.",
            *missing,
        ]
    )
    return [line.split("=", 1)[0] for line in missing]


def top_up_store_key(env: DotEnv) -> str | None:
    """Append a store key to a demo ``.env`` that has none — one written
    before K6 — so the demo can take a secret in Admin -> Settings.
    ``scripts/demo.sh``'s ``provision_store_key``: ``"added"`` — the file
    made owner-only first — ``"not-owner"`` when an existing file cannot be
    made owner-only, so nothing was added, or ``None``. Only in demo mode,
    and never over a choice already made — a key, an explicit blank (which
    means "unconfigured") or a ``_FILE``, in the file or the environment.
    ``librerun up`` never calls it: a real deployment chooses its own key,
    or none."""
    if not _is_true(env.effective("LIBRERUN_DEMO")):
        return None
    if env.is_set(STORE_KEY_VARIABLE) or env.is_set(f"{STORE_KEY_VARIABLE}_FILE"):
        return None
    # Owner-only before the key goes in, as for gateway.env (the review
    # after K7): the write keeps an existing file's mode, and an .env
    # written by hand is usually readable by every local account.
    if env.path.is_file():
        try:
            os.chmod(env.path, 0o600)
        except OSError:
            return "not-owner"
    env.append_block(
        [
            "",
            f"# Added by librerun on {_now()}: the secrets store key (K6).",
            f"{STORE_KEY_VARIABLE}={fernet_key()}",
        ]
    )
    return "added"


GATEWAY_STORE_KEY_VARIABLE = "LIBRERUN_GATEWAY_SECRETS_KEY"
GATEWAY_ENV = "gateway.env"


def top_up_gateway_env(root: Path, env: DotEnv) -> str | None:
    """The gateway's store key in the demo's own ``gateway.env`` (K7, D33),
    so a provider key pasted in Admin -> Settings has somewhere to be kept —
    ``scripts/demo.sh``'s ``provision_gateway_env``. ``"wrote"`` a new file,
    ``"added"`` a line to one — made owner-only first — ``"not-owner"`` when
    an existing file cannot be made owner-only, so nothing was written, or
    ``None``.

    Only in demo mode; only for the file compose reads by default — an
    operator who names another in ``LIBRERUN_GATEWAY_ENV_FILE`` keeps it
    themselves; and never over a choice already made: the key or its
    ``_FILE`` on a line of ``gateway.env``, a blank one included, which
    means "unconfigured". Never in ``.env``, which the backend reads, and
    never through compose's ``environment:`` block, where a blank would
    override the file. ``librerun up`` never calls it."""
    if not _is_true(env.effective("LIBRERUN_DEMO")):
        return None
    if env.is_set("LIBRERUN_GATEWAY_ENV_FILE"):
        return None
    gateway = DotEnv(root / GATEWAY_ENV, environ={})
    variable = GATEWAY_STORE_KEY_VARIABLE
    if not gateway.exists:
        gateway.text = "\n".join(
            [
                f"# Written by librerun demo on {_now()}: the demo's gateway.env,",
                "# read by the gateway alone. It holds the key the provider keys pasted in",
                "# Admin -> Settings are sealed with (K7); add a provider key below it, as",
                "# gateway.env.example shows, or paste one in the admin page.",
                f"{variable}={fernet_key()}",
            ]
        ) + "\n"
        gateway.write(mode=0o600)
        return "wrote"
    if gateway.file_has(variable) or gateway.file_has(f"{variable}_FILE"):
        return None
    # Owner-only before the key goes in (Codex on #170): the write keeps an
    # existing file's mode, and a gateway.env copied from the example is
    # usually readable by every local account. A file this user may not make
    # owner-only gets no key.
    try:
        os.chmod(gateway.path, 0o600)
    except OSError:
        return "not-owner"
    gateway.append_block(
        [
            "",
            f"# Added by librerun on {_now()}: the gateway's store key (K7).",
            f"{variable}={fernet_key()}",
        ]
    )
    gateway.write()
    return "added"


def _url_host(binding: str) -> str:
    """The URL host for a compose host binding: ``bind_host``'s answer, and
    ``localhost`` for a loopback address too. Since T1 a binding is loopback
    by default (``127.0.0.1:8000``), and ``localhost`` is the origin the
    example's CORS line and the demo name — ``http://127.0.0.1:3000`` is a
    different origin, which that line would not allow."""
    host = bind_host(binding)
    return "localhost" if host in ("127.0.0.1", "[::1]") else host


def published(env: DotEnv) -> dict:
    """The host-side URLs, resolved as compose resolves the port bindings:
    the shell, then ``.env``, then the compose default. What `demo` and
    `up` start and wait for — never the HTTPS edge, which neither starts."""
    backend = (env.effective("BACKEND_PORT") or "").strip() or "8000"
    frontend = (env.effective("FRONTEND_PORT") or "").strip() or "3000"
    gateway = (env.effective("GATEWAY_PORT") or "").strip() or "8090"
    jaeger = (env.effective("JAEGER_UI_PORT") or "").strip() or "16686"
    return {
        "backend": f"http://{_url_host(backend)}:{bind_port(backend)}",
        "frontend": f"http://{_url_host(frontend)}:{bind_port(frontend)}",
        # The gateway and the viewer carry their own 127.0.0.1 host in
        # compose.yaml, so these are bare ports.
        "gateway": f"http://localhost:{bind_port(gateway)}",
        "jaeger": f"http://localhost:{bind_port(jaeger)}",
    }


def addresses(env: DotEnv) -> dict:
    """The URLs `doctor` and `run` reach LibreRun at: ``LIBRERUN_URL`` for
    the API and the UI alike when this shell sets it (T1) — behind the
    HTTPS edge one https origin serves both — else ``published``'s. Read
    from the process environment and never from ``.env``."""
    urls = published(env)
    variable = _credentials.URL_VARIABLE
    url = (env.effective(variable) or "").strip().rstrip("/") if env.in_environment(variable) else ""
    if url:
        urls["backend"] = urls["frontend"] = url
    return urls


def derived_env(env: DotEnv) -> dict[str, str]:
    """The three values that follow the ports unless the caller supplied
    them (shell or ``.env``): the API URL baked into the frontend bundle,
    the origin the backend's CORS allows, and the base of the "View
    trace" links. An explicitly empty value is not a URL and counts as
    unsupplied."""
    urls = published(env)
    wanted = {
        "NEXT_PUBLIC_API_URL": f"{urls['backend']}/api/v1",
        "APP_CORS_ORIGINS": urls["frontend"],
        "TRACE_VIEWER_BASE_URL": urls["jaeger"],
    }
    return {k: v for k, v in wanted.items() if not (env.effective(k) or "").strip()}


def compose_up(root: Path, env: DotEnv, *, build: bool = True) -> None:
    """``up -d``, building from this checkout unless told not to.

    Every first-party service carries ``pull_policy: build`` (#133), so
    compose builds it on each ``up`` — from cache when nothing changed —
    and never pulls it; ``--build`` says so out loud. ``build=False`` is
    ``librerun up --no-build``: compose's own ``--no-build``, which builds
    nothing and pulls nothing and, on Docker, stops on an image that is
    missing. (podman-compose hands a missing image to ``podman create``,
    whose default would pull it — which is why the images are named
    ``localhost/librerun/…``: the most that pull reaches is the loopback
    address.)
    """
    _compose.run(root, "up", "-d", "--build" if build else "--no-build", env=derived_env(env))


def wait_for_backend(base: str, seconds: int = HEALTH_WAIT_SECONDS) -> dict:
    url = f"{base}/api/v1/health"
    say(f"waiting for {url} …")
    deadline = time.monotonic() + seconds
    while True:
        status, body = _http.get(url, timeout=5)
        if status == 200 and isinstance(body, dict):
            say(f"backend is up: {body.get('status')}")
            return body
        if time.monotonic() >= deadline:
            raise CliError(
                f"the backend did not come up within {seconds // 60} minutes "
                f"(last answer: {status or 'no connection'}). "
                f"`librerun logs backend` shows why."
            )
        time.sleep(2)


def wait_for_gateway(base: str, seconds: int = GATEWAY_WAIT_SECONDS) -> bool:
    """Wait until the backend has heard from the gateway: ``/api/v1/meta``
    says ``gateway: "ok"``. The backend's health is not the gateway's, and a
    run started the moment ``up`` returns would otherwise reach a gateway
    still starting — its agent's model call then fails, or a template falls
    back to its rule. Bounded, and never fatal: a gateway that does not
    answer is the summary's to report."""
    url = f"{base}/api/v1/meta"
    deadline = time.monotonic() + seconds
    while True:
        status, meta = _http.get(url, timeout=5)
        if status == 200 and isinstance(meta, dict) and meta.get("gateway") == "ok":
            say("gateway is up")
            return True
        if time.monotonic() >= deadline:
            warn(
                f"the gateway did not answer within {seconds} seconds "
                f"(/api/v1/meta says {meta.get('gateway') if isinstance(meta, dict) else status or 'no connection'}); "
                f"`librerun logs gateway` shows why."
            )
            return False
        time.sleep(2)


def summary(root: Path, env: DotEnv, *, generated_password: str | None) -> None:
    urls = published(env)
    status, meta = _http.get(f"{urls['backend']}/api/v1/meta", timeout=5)
    meta = meta if status == 200 and isinstance(meta, dict) else {}
    say()
    say("LibreRun is up.")
    say()
    say(f"  Open      {urls['frontend']}")
    email = env.effective("INITIAL_ADMIN_EMAIL") or "<INITIAL_ADMIN_EMAIL is not set in .env>"
    say(f"  Sign in   {email}")
    if generated_password:
        say(f"  Password  {generated_password}   (generated by this run, saved in .env)")
    else:
        say("  Password  the INITIAL_ADMIN_PASSWORD in your existing .env")
    say()
    say(f"  API       {urls['backend']}/api/v1")
    say(f"  Traces    {_traces_line(env, meta, urls)}")
    say()
    if _is_true(env.effective("LIBRERUN_DEMO")):
        say("  Demo mode (LIBRERUN_DEMO=true): not for production; the UI says so.")
    else:
        say("  LIBRERUN_DEMO is not true (shell or .env): this is a regular deployment, not the demo.")
    stub = meta.get("stub_llm")
    if stub is True:
        say("  The LLM is a stub answering from fixtures: no provider calls, no cost.")
    elif stub is False:
        say("  The gateway routes to the configured providers (LIBRERUN_STUB_LLM is not true).")
    else:
        say("  The gateway did not answer /api/v1/meta yet: keyless or not is unknown.")
    say()
    say("  Stop      librerun down")
    say("  Reset     librerun down --volumes   (deletes all data)")
    say()


def _traces_line(env: DotEnv, meta: dict, urls: dict) -> str:
    configured = meta.get("trace_viewer_configured")
    viewer = meta.get("trace_viewer") or (env.effective("TRACE_VIEWER") or "off").strip().lower()
    source = meta.get("trace_viewer_source")
    base = (env.effective("TRACE_VIEWER_BASE_URL") or "").strip() or urls["jaeger"]
    template = (env.effective("TRACE_VIEWER_URL_TEMPLATE") or "").strip()
    if configured is True:
        if source == "env":
            dest = template.replace("{base}", base.rstrip("/")) if template else base.rstrip("/")
            return f'{dest}   ({viewer} — "View trace" on any run)'
        return f'"View trace" links open the {viewer} configured in the admin settings (a runtime override of .env)'
    if configured is False:
        if viewer == "off":
            return 'none (TRACE_VIEWER=off) — set jaeger with VECTOR_VIEWER=1 for "View trace" links'
        return (
            f"none: TRACE_VIEWER={viewer}, but the backend reports no usable viewer "
            f"(/api/v1/meta) — the presets need TRACE_VIEWER_BASE_URL, custom/langsmith "
            f"a TRACE_VIEWER_URL_TEMPLATE"
        )
    return f'TRACE_VIEWER={viewer}; GET /api/v1/meta reports whether "View trace" links render'


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on", "t", "y")


# ---------------------------------------------------------------------------
# the commands
# ---------------------------------------------------------------------------


def cmd_demo(root: Path, args) -> int:
    if getattr(args, "pull", False):
        # Retired with image publishing (A2; L37, D26): a release is source
        # only. Refused before .env is read, until 1.1.0.
        raise CliError("--pull is gone: a LibreRun release is source only, and `librerun demo` builds it", code=2)
    env = DotEnv(root / ".env")
    generated = None
    if not env.exists:
        env.text, generated = demo_env_text(root)
        env.write(mode=0o600)
        say("librerun demo: wrote .env (demo mode, generated credentials, mode 600)")
    else:
        say("librerun demo: using the existing .env")
    added = top_up_keys(env, root)
    if added:
        env.write()
        say(f"librerun demo: provisioned {len(added)} new agent key(s) in .env: {' '.join(added)}")
    store_key = top_up_store_key(env)
    if store_key == "added":
        env.write()
        say(f"librerun demo: provisioned the secrets store key in .env ({STORE_KEY_VARIABLE})")
    elif store_key == "not-owner":
        warn(
            f".env is not yours to make owner-only, so the secrets store key was not "
            f"added: chmod 600 .env and run this again, or add {STORE_KEY_VARIABLE} yourself"
        )
    gateway_env = top_up_gateway_env(root, env)
    if gateway_env == "wrote":
        say(f"librerun demo: wrote {GATEWAY_ENV} (the gateway's store key, mode 600)")
    elif gateway_env == "added":
        say(f"librerun demo: provisioned the gateway's store key in {GATEWAY_ENV} ({GATEWAY_STORE_KEY_VARIABLE})")
    elif gateway_env == "not-owner":
        warn(
            f"{GATEWAY_ENV} is not yours to make owner-only, so the gateway's store key "
            f"was not added: chmod 600 {GATEWAY_ENV} and run this again, or add "
            f"{GATEWAY_STORE_KEY_VARIABLE} yourself"
        )
    if args.env_only:
        say("librerun demo: --env-only — run `librerun demo` again to start the stack")
        return 0
    for key, value in derived_env(env).items():
        defaults = {
            "NEXT_PUBLIC_API_URL": "http://localhost:8000/api/v1",
            "APP_CORS_ORIGINS": "http://localhost:3000",
            "TRACE_VIEWER_BASE_URL": "http://localhost:16686",
        }
        if value != defaults[key]:
            say(f"librerun demo: {key}={value}   (follows the port in .env)")
    compose_up(root, env)
    if not args.no_wait:
        wait_for_backend(published(env)["backend"])
        wait_for_gateway(published(env)["backend"])
    summary(root, env, generated_password=generated)
    return 0


def cmd_up(root: Path, args) -> int:
    env = DotEnv(root / ".env")
    if not env.exists:
        raise CliError(
            "no .env in this checkout. `librerun demo` writes the zero-config "
            "one; for a real deployment copy .env.example to .env and fill it in. "
            "(A deployment that keeps no plaintext .env on disk runs compose "
            "through `sops exec-env`, as docs/platform/Install.md \"Encrypting .env at "
            "rest\" shows; `librerun up` reads the file.)"
        )
    added = top_up_keys(env, root)
    if added:
        env.write()
        say(f"librerun up: provisioned {len(added)} new agent key(s) in .env: {' '.join(added)}")
    compose_up(root, env, build=not args.no_build)
    if not args.no_wait:
        wait_for_backend(published(env)["backend"])
        wait_for_gateway(published(env)["backend"])
        if not args.quiet:
            summary(root, env, generated_password=None)
    return 0


def cmd_down(root: Path, args) -> int:
    extra = ["-v"] if args.volumes else []
    if args.volumes:
        say("librerun down --volumes: stopping the stack and deleting its data volumes")
    _compose.run(root, "down", *extra)
    return 0


def cmd_logs(root: Path, args) -> int:
    services = args.service or ["backend", "gateway"]
    extra = ["--tail", str(args.tail)]
    if args.follow:
        extra.append("-f")
    # Agent containers persist no logs by construction (`logging: driver:
    # none`): their lines are run-plane log records under the run's trace.
    result = _compose.run(root, "logs", "--no-color", *extra, *services, check=False)
    return result.returncode
