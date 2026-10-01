"""``run``: submit one of an agent's samples and report the run.

The smoke client's loop (``scripts/librerun_smoke.py``), reduced to what
a newcomer needs: log in, load the sample, submit, and — with ``--wait``
— watch the run to its end. Prints the run number, the status and the
run page's URL; exits non-zero when the run ends in ``error``. The login
is ``_credentials.sign_in``: nothing is sent until ``/api/v1/meta``
answers as LibreRun, and no credential follows a redirect. The base is
``--base-url``, else ``LIBRERUN_URL`` (the HTTPS edge's origin, T1), else
the backend's published port; an https one is verified against
``--cacert``, else ``LIBRERUN_CA_FILE``. ``run`` asks no engine (K4b).
"""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlsplit

from . import _credentials, _http
from ._common import CliError, say, warn
from ._env import DotEnv
from ._stack import addresses

TERMINAL_OK = "complete"
TERMINAL_BAD = "error"
GATE = "awaiting_approval"


def credentials(env: DotEnv, args) -> tuple[str, str]:
    """The flags, else ``LIBRERUN_EMAIL``/``LIBRERUN_PASSWORD``, else the
    bootstrap admin's ``.env`` names, which the demo writes. The file is
    read the way compose reads it, never evaluated; the values are used
    for the login and printed nowhere."""
    if getattr(args, "password", None):
        warn(
            "--password puts the password on the command line, where the process "
            "list shows it to every user of this machine; use --password-stdin or "
            f"{_credentials.PASSWORD_VARIABLE}. --password goes after this release."
        )
    email, password = _credentials.from_args(args)
    email = email or (env.effective("INITIAL_ADMIN_EMAIL") or "").strip()
    password = password or (env.effective("INITIAL_ADMIN_PASSWORD") or "")
    if not email or not password:
        raise CliError(
            "no credentials: pass --email and --password-stdin, or export "
            f"{_credentials.EMAIL_VARIABLE} and {_credentials.PASSWORD_VARIABLE}, "
            "or set INITIAL_ADMIN_EMAIL and INITIAL_ADMIN_PASSWORD in .env "
            "(`librerun demo` writes both)"
        )
    return email, password


def _short(body) -> str:
    text = str(body)
    return text if len(text) <= 300 else text[:300] + "…"


def pick_scenario(base: str, token: str, agent_id: str, wanted: str | None, *, cafile: str | None = None) -> dict:
    status, agents = _http.get(f"{base}/api/v1/agents", token=token, cafile=cafile)
    if status != 200 or not isinstance(agents, list):
        raise CliError(f"could not list agents ({status}): {_short(agents)}")
    ids = [a.get("agent_id") for a in agents]
    if agent_id not in ids:
        raise CliError(
            f"agent {agent_id!r} is not registered; the backend knows {ids}. "
            f"A new agent appears after `librerun up` rebuilds the backend; "
            f"`librerun doctor` says whether it was discovered."
        )
    status, scenarios = _http.get(f"{base}/api/v1/agents/{agent_id}/scenarios", token=token, cafile=cafile)
    if status != 200 or not isinstance(scenarios, list) or not scenarios:
        raise CliError(
            f"agent {agent_id!r} serves no sample ({status}). Add a scenario "
            f"under its scenarios/ directory — a JSON file with a name and "
            f"a user_inputs object that satisfies the agent's input schema."
        )
    if wanted is None:
        return scenarios[0]
    for scenario in scenarios:
        if scenario.get("id") == wanted or scenario.get("name") == wanted:
            return scenario
    raise CliError(
        f"agent {agent_id!r} has no scenario {wanted!r}; it serves "
        f"{[s.get('id') for s in scenarios]}"
    )


def submit(base: str, token: str, agent_id: str, user_inputs: dict, *, cafile: str | None = None) -> dict:
    status, body = _http.post(
        f"{base}/api/v1/runs?agent_id={agent_id}", token=token, body=user_inputs, cafile=cafile
    )
    if status not in (200, 201, 202) or not isinstance(body, dict) or not body.get("run_id"):
        raise CliError(f"run submission failed ({status}): {_short(body)}")
    return body


def watch(base: str, token: str, run_id: str, *, approve: bool, timeout: float, cafile: str | None = None) -> dict:
    """Poll until a terminal state. A run parked at its approval gate is
    approved once when ``approve`` is set, and otherwise reported as
    parked — the gate is a human's, and the CLI says so instead of
    pressing the button for them."""
    deadline = time.monotonic() + timeout
    approved = False
    last = ""
    while True:
        status, detail = _http.get(f"{base}/api/v1/runs/{run_id}", token=token, cafile=cafile)
        if status != 200 or not isinstance(detail, dict):
            raise CliError(f"could not read the run ({status}): {_short(detail)}")
        state = detail.get("status") or ""
        if state != last:
            say(f"  status: {state}")
            last = state
        if state in (TERMINAL_OK, TERMINAL_BAD):
            return detail
        if state == GATE:
            if not approve:
                return detail
            if not approved:
                status, _ = _http.post(f"{base}/api/v1/runs/{run_id}/approve", token=token, cafile=cafile)
                if status not in (200, 201, 202):
                    raise CliError(f"approve failed ({status})")
                say("  approved at the gate (--approve)")
                approved = True
        if time.monotonic() >= deadline:
            raise CliError(f"the run is still {state!r} after {timeout:.0f}s")
        time.sleep(2)


def cmd_run(root: Path, args) -> int:
    env = DotEnv(root / ".env")
    urls = addresses(env)
    base = (args.base_url or urls["backend"]).rstrip("/")
    # An https --base-url is the HTTPS edge's origin, which serves the UI
    # and /api/v1 alike (T1), so the run page is there too; an http one is
    # the backend's own port, and the UI stays where the stack publishes it.
    frontend = base if args.base_url and urlsplit(base).scheme == "https" else urls["frontend"]
    # The CA file first: a missing one is refused by its path, before a
    # password is read from anywhere or anything is sent.
    cafile = _credentials.ca_file(args)
    email, password = credentials(env, args)
    token = _credentials.sign_in(base, email, password, cafile=cafile)
    scenario = pick_scenario(base, token, args.agent, args.scenario, cafile=cafile)
    started = time.monotonic()
    run = submit(base, token, args.agent, scenario.get("user_inputs") or {}, cafile=cafile)
    run_id = run["run_id"]
    say(f"submitted {run.get('run_number')} for {args.agent} — sample {scenario.get('name')!r}")
    say(f"  run page: {frontend}/runs/{run_id}")
    say(f"  api:      {base}/api/v1/runs/{run_id}")
    if not args.wait:
        say(f"  status:   {run.get('status')}   (re-run with --wait to follow it)")
        return 0
    detail = watch(base, token, run_id, approve=args.approve, timeout=args.timeout, cafile=cafile)
    state = detail.get("status")
    elapsed = time.monotonic() - started
    if state == TERMINAL_BAD:
        say(f"run {run.get('run_number')} ended in error after {elapsed:.0f}s: {detail.get('error') or '(no reason recorded)'}")
        return 1
    if state == GATE:
        say(
            f"run {run.get('run_number')} is parked at its approval gate after "
            f"{elapsed:.0f}s: approve it on the run page, or re-run with --approve"
        )
        return 0
    say(f"run {run.get('run_number')} completed in {elapsed:.0f}s")
    return 0
