#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Gate S — the canary secret sweep (K blueprint §3.2).

Boot the stack with a distinct **canary** value in every secret slot,
drive it the way a customer does, then look for those canaries in every
place a value could have leaked to: the JSONL log file, each container's
stdout and stderr (the Vector console sink among them), every API
response body, ``activity_audit_log`` details, ``app_settings`` values,
the exported trace, a settings dump out of each process, and every file
under ``./data``. A canary found anywhere is a failure, named by sink.

That is the easy half. The hard half is proving the sweep LOOKED — a
sweep that read nothing finds nothing and exits 0, which is the failure
mode CLAUDE.md names: a gate that reports success by not looking is
worse than no gate. Three devices, all of them load-bearing:

* **Every sink must yield bytes.** A sink that came back empty is an
  error, not a pass. ``docker compose logs`` against a service name
  nobody spelled right returns nothing at all, and it looks exactly like
  a clean container.
* **A positive control.** ``--marker`` is a value the caller planted in
  the running system on purpose — an admin settings write puts one in an
  API response, in ``app_settings`` and in the audit log at once. The
  sweep must FIND it, in at least ``--marker-sinks`` distinct sinks, or
  its searching is not believed.
* **A report, verified separately.** ``--report`` writes what was read
  and ``--verify-report`` re-reads it and applies floors, the way
  ``scripts/assert_suite_ran.py`` refuses a suite that shrank. The
  workflow runs both, so "the sweep passed" and "the sweep ran" are two
  different assertions.

From K6 a secret can be set through the API, so that is a slot too:
``--secret-setting KEY:SLOT`` PUTs the ``SLOT`` canary to the secret
setting ``KEY``, requires ``200`` with ``value`` null and a fingerprint,
and keeps the fingerprint. The ``secrets`` table then holds the canary
only as ciphertext, so its plaintext must be in no sink — and the
fingerprint must be in a ``sql:`` sink, or the sweep never read the row
it is vouching for: ``verify()`` refuses a report whose planted
fingerprint no ``sql:`` sink shows.

From K7 a provider key can be pasted in the admin UI, sealed in the
browser to the gateway's public key, so that is a slot too:
``--sealed-key NAME:FILE`` POSTs the blob in ``FILE`` — made OUTSIDE the
sweep, by ``openssl pkeyutl`` with the browser's parameters, since the
sweep stays stdlib — to ``/api/v1/admin/providers/NAME/key``, requires
``202`` and ``pending``, polls the list until the gateway has adopted it
(``runtime``), and keeps the fingerprint the gateway gave it. The key's
plaintext must be in no sink, and the fingerprint must be in
``sql:gateway_status``, the row the gateway writes: ``verify()`` refuses a
report whose sealed fingerprint that sink does not show
(``_require_fingerprint``).

From K8a an agent's tool secret can be set for a tenant through the
agents API, so that is a slot too: ``--tool-secret AGENT:NAME:SLOT`` PUTs
the ``SLOT`` canary as this tenant's value of the agent's declared
``NAME`` (``/api/v1/agents/AGENT/secrets/tenant/NAME``), requires ``200``,
no value in the answer and a fingerprint, and keeps the fingerprint, which
must then be in ``sql:secrets`` (``_require_tool_secret``). The one place
a tool secret's value leaves the chassis on purpose — ``secret_get``'s
result to the declaring run — goes to the container and is recorded by no
sink, so the sweep needs no exemption for it (D20) and ``verify()``
refuses a report that claims one.

From K8b a container fetches its tool secret over MCP, so the fetch is a
slot too: ``--tool-secret-run AGENT:JSON``, after that agent's one
``--tool-secret`` has been planted, submits one run of AGENT with the JSON
as its input, polls it to ``complete`` (120 s at most) and requires the
planted row's ``last_used_at`` to have moved during the run — read before
the run is submitted and again after it. The chassis stamps the row when
``secret_get`` answers, so a stamp the run moved is the proof the value
really crossed to the container; a stamp left by an earlier fetch proves
nothing, and neither does a row the chassis did not stamp again because it
stamped it within the hour (a sweep re-run on the same backend), which
fails closed. The report's run slot carries the run's id, its status and
both timestamps, never the run's output, and ``verify()`` refuses a report
whose run slot shows no stamp the run moved (``_require_tool_secret_run``).
An agent with more than one planted secret is refused a run slot, since
the slot could not say which one the run fetched. The run's own detail
goes into the API sink, where the canary must not be.

And the negative double, which is the workflow's job rather than this
script's: a canary written into a real sink on purpose must turn the job
red (see ``.github/workflows/librerun-smoke.yml``, the ``secrets-as-
files`` job). A sweep nobody has watched fail is a sweep nobody knows
works.

Stdlib only — this runs on a bare CI runner beside the compose stack.

    python3 scripts/secret_sweep.py --canaries canaries.json \\
        --data-dir ./data --compose-service backend --compose-service gateway \\
        --api-url http://localhost:8000 --admin-email a@b.c --admin-password … \\
        --report sweep-report.json --expect-sinks 8 --marker-sinks 3
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# A file this big under ./data is a model, an index or an artifact, not
# somewhere a 30-character canary hides in a way that matters. Read in
# chunks so the sweep does not need the file in memory either way.
CHUNK = 1 << 20


class SweepFailure(RuntimeError):
    """The sweep found a canary, or could not prove it looked."""


# --------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------


class Sink:
    """One place a secret could have leaked to, and what was read there.

    ``text`` is what the sweep searched. ``note`` is how it was obtained,
    for the report — an operator reading a red job needs to know whether
    "0 bytes" means a clean container or a misspelled service name.
    """

    def __init__(self, name: str, text: str, note: str = ""):
        self.name = name
        self.text = text or ""
        self.note = note

    @property
    def bytes_read(self) -> int:
        return len(self.text.encode("utf-8", "replace"))


def _run(cmd: list[str], *, timeout: int = 120) -> tuple[int, str]:
    try:
        done = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"[secret_sweep: {type(exc).__name__}: {exc}]"
    # stdout AND stderr: a container's stderr is exactly where a
    # traceback carrying a settings dump would land.
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def _compose(args) -> list[str]:
    cmd = ["docker", "compose"]
    for path in args.compose_file:
        cmd += ["-f", path]
    for profile in args.compose_profile:
        cmd += ["--profile", profile]
    return cmd


def sink_container_logs(args, service: str) -> Sink:
    code, text = _run(_compose(args) + ["logs", "--no-color", "--timestamps", service])
    return Sink(
        f"container-logs:{service}",
        text,
        note=f"docker compose logs {service} (rc={code})",
    )


def sink_file_tree(root: str, *, skip: set[str]) -> Sink:
    """Every file under ``root``, concatenated.

    ``skip`` holds the sweep's OWN files — the canary list and the report
    it is about to write. Reading its own canary file back and calling it
    a leak would be a gate that can only fail, which is no more useful
    than one that can only pass.
    """
    base = Path(root)
    chunks: list[str] = []
    files = 0
    if base.exists():
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if str(path.resolve()) in skip:
                continue
            files += 1
            chunks.append(f"\n===== {path} =====\n")
            try:
                with path.open("rb") as handle:
                    while True:
                        block = handle.read(CHUNK)
                        if not block:
                            break
                        chunks.append(block.decode("utf-8", "replace"))
            except OSError as exc:
                chunks.append(f"[secret_sweep: unreadable: {exc}]")
    return Sink(f"file-tree:{root}", "".join(chunks), note=f"{files} file(s)")


def sink_settings_dump(args, service: str, module: str, attribute: str) -> Sink:
    """``repr(settings)`` from inside a running container.

    The sink a masked field exists for, and the one no other check
    covers: every secret this process holds is in one string, so if
    ``SecretStr`` were missing from even one field, this is where it
    shows. The dump is searched for the canaries like anything else, and
    the workflow additionally asserts it is full of ``**********``.
    """
    program = (
        f"import {module} as m;"
        f"print(repr(m.{attribute}))"
    )
    code, text = _run(
        _compose(args) + ["exec", "-T", service, "python", "-c", program]
    )
    return Sink(
        f"settings-dump:{service}",
        text,
        note=f"repr({module}.{attribute}) inside {service} (rc={code})",
    )


def sink_sql(args, label: str, statement: str) -> Sink:
    code, text = _run(
        _compose(args)
        + [
            "exec",
            "-T",
            args.psql_service,
            "psql",
            "-U",
            args.psql_user,
            "-d",
            args.psql_db,
            "-A",
            "-t",
            "-c",
            statement,
        ]
    )
    return Sink(f"sql:{label}", text, note=f"psql in {args.psql_service} (rc={code})")


# --------------------------------------------------------------------------
# The API sink: drive the stack, keep every response body
# --------------------------------------------------------------------------


def _http(method: str, url: str, *, token=None, body=None, timeout=30):
    data = None
    headers = {"Accept": "*/*"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # An ERROR body is the interesting one: error paths are where a
        # value gets echoed back by something that never meant to.
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 0, f"[secret_sweep: {type(exc).__name__}: {exc}]"


def plant_secret_setting(base: str, token: str, setting: str, slot: str, value: str, record) -> dict:
    """PUT one canary to a secret setting (K6) and keep its fingerprint.

    The answer must be 200 with ``value`` null — a secret is write-only
    (L31) — and a fingerprint, which is what the ``sql:`` sinks must then
    show: proof the ``secrets`` table was read, where a canary's absence
    alone proves nothing.
    """
    status, body = _http(
        "PUT",
        f"{base}/api/v1/admin/settings/{setting}",
        token=token,
        body={"value": value},
    )
    record(f"PUT /admin/settings/{setting} (the {slot} canary)", status, body)
    if status != 200:
        raise SweepFailure(
            f"the {slot} canary could not be set through PUT /admin/settings/{setting}: "
            f"{status}. The secrets store is then not a slot this sweep tested."
        )
    try:
        answer = json.loads(body)
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or "value" not in answer or answer["value"] is not None:
        raise SweepFailure(
            f"PUT /admin/settings/{setting} did not answer \"value\": null — a secret "
            f"setting's value is never returned (L31)"
        )
    fingerprint = (answer.get("secret") or {}).get("fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 12:
        raise SweepFailure(
            f"PUT /admin/settings/{setting} answered no 12-character fingerprint, so "
            f"nothing can show the secrets table was read"
        )
    return {"setting": setting, "slot": slot, "fingerprint": fingerprint}


def plant_sealed_key(base: str, token: str, provider: str, path: str, record, *, wait: float = 90.0) -> dict:
    """POST one sealed provider key (K7) and keep the fingerprint the
    gateway gives it once it has adopted the blob.

    ``path`` holds the blob as base64 — what the browser sends — made by
    ``openssl pkeyutl`` outside the sweep. The answer must be ``202`` with
    ``row: pending``: the backend cannot open the blob, so only the gateway
    can fingerprint the key in it, and the list is polled until it has.
    """
    import time

    sealed = Path(path).read_text(encoding="utf-8").strip()
    status, body = _http(
        "POST", f"{base}/api/v1/admin/providers/{provider}/key", token=token, body={"sealed": sealed}
    )
    record(f"POST /admin/providers/{provider}/key (a sealed canary)", status, body)
    try:
        answer = json.loads(body)
    except ValueError:
        answer = None
    if status != 202 or not isinstance(answer, dict) or answer.get("row") != "pending":
        raise SweepFailure(
            f"the sealed canary for {provider} was not accepted: POST answered {status}. The "
            f"provider-key slot is then not one this sweep tested."
        )
    deadline = time.monotonic() + wait
    entry: dict = {}
    while time.monotonic() < deadline:
        status, body = _http("GET", f"{base}/api/v1/admin/providers", token=token)
        try:
            listing = json.loads(body)
        except ValueError:
            listing = {}
        entry = next(
            (e for e in (listing.get("providers") or []) if isinstance(e, dict) and e.get("name") == provider),
            {},
        )
        if entry.get("row") == "runtime":
            record(f"GET /admin/providers (after the gateway adopted {provider}'s)", status, body)
            break
        time.sleep(2)
    fingerprint = entry.get("fingerprint")
    if entry.get("row") != "runtime" or not isinstance(fingerprint, str) or len(fingerprint) != 12:
        raise SweepFailure(
            f"the gateway never adopted the sealed canary for {provider} within {int(wait)} s "
            f"(the list says row={entry.get('row')!r}, reason={entry.get('reason')!r}), so no "
            f"fingerprint can show gateway_status was read"
        )
    return {"provider": provider, "file": path, "fingerprint": fingerprint}


def plant_tool_secret(base: str, token: str, agent: str, name: str, slot: str, value: str,
                      record) -> dict:
    """PUT one canary as this tenant's value of an agent's declared tool
    secret (K8a) and keep its fingerprint.

    The answer must be 200 with no value in it — a secret is write-only
    (L31) — and the tenant row's fingerprint, which ``sql:secrets`` must
    then show.
    """
    status, body = _http(
        "PUT",
        f"{base}/api/v1/agents/{agent}/secrets/tenant/{name}",
        token=token,
        body={"value": value},
    )
    record(f"PUT /agents/{agent}/secrets/tenant/{name} (the {slot} canary)", status, body)
    if status != 200:
        raise SweepFailure(
            f"the {slot} canary could not be set through PUT /agents/{agent}/secrets/tenant/"
            f"{name}: {status}. The tool-secret slot is then not one this sweep tested."
        )
    try:
        answer = json.loads(body)
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or "value" in answer:
        raise SweepFailure(
            f"PUT /agents/{agent}/secrets/tenant/{name} answered a value field: a tool "
            f"secret's value is never returned (L31)"
        )
    fingerprint = (answer.get("tenant") or {}).get("fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 12:
        raise SweepFailure(
            f"PUT /agents/{agent}/secrets/tenant/{name} answered no 12-character fingerprint, "
            f"so nothing can show the secrets table was read"
        )
    return {"agent": agent, "name": name, "slot": slot, "fingerprint": fingerprint}


def run_with_tool_secret(base: str, token: str, agent: str, user_inputs: dict,
                         planted: dict, record, wait: float = 120.0) -> dict:
    """Submit one run of ``agent`` that fetches its planted tool secret over
    MCP (K8b), wait for it to complete, and read the row's ``last_used_at``
    before the run and after it.

    A stamp the run moved is the proof: the chassis writes it when
    ``secret_get`` answers the declaring run, so a run that completed
    without fetching — an input that did not ask, an agent that never
    called — leaves it where it was, empty or an earlier fetch's (a
    replaced value keeps its row's stamp). The slot returned holds the
    run's id, its status and both stamps, never the run's output; the run's
    detail goes into the API sink, which the canary must stay out of like
    every other.
    """
    import time

    before = _tenant_stamp(base, token, agent, planted["name"], record, "before")
    status, body = _http(
        "POST", f"{base}/api/v1/runs?agent_id={agent}", token=token, body=user_inputs
    )
    record(f"POST /runs?agent_id={agent} (a run that fetches its tool secret)", status, body)
    try:
        run_id = json.loads(body).get("run_id")
    except (ValueError, AttributeError):
        run_id = None
    if status not in (200, 201, 202) or not run_id:
        raise SweepFailure(
            f"the tool-secret run of {agent} could not be submitted: POST answered {status}. "
            f"The fetch over MCP is then not a slot this sweep tested."
        )
    deadline = time.monotonic() + wait
    state = None
    while time.monotonic() < deadline:
        status, body = _http("GET", f"{base}/api/v1/runs/{run_id}", token=token)
        try:
            state = json.loads(body).get("status")
        except (ValueError, AttributeError):
            state = None
        if state in ("complete", "error", "failed", "blocked"):
            break
        time.sleep(2)
    record(f"GET /runs/{{id}} [{run_id}] (the tool-secret run, {state})", status, body)
    if state != "complete":
        raise SweepFailure(
            f"the tool-secret run {run_id} of {agent} ended {state!r} within {int(wait)} s, "
            f"not complete, so no fetch over MCP can be vouched for"
        )
    return {
        "agent": agent,
        "name": planted["name"],
        "run_id": run_id,
        "status": state,
        "last_used_before": before,
        "last_used_at": _tenant_stamp(base, token, agent, planted["name"], record, "after"),
    }


def _tenant_stamp(base: str, token: str, agent: str, name: str, record, when: str):
    """This tenant's row of ``agent``'s ``name``: its ``last_used_at``, or
    ``None`` — the row ``--tool-secret`` plants. The answer goes into the
    API sink."""
    status, body = _http("GET", f"{base}/api/v1/agents/{agent}/secrets", token=token)
    record(f"GET /agents/{agent}/secrets ({when} the tool-secret run)", status, body)
    try:
        states = json.loads(body).get("secrets") or []
    except (ValueError, AttributeError):
        states = []
    row = next(
        (e.get("tenant") or {} for e in states if isinstance(e, dict) and e.get("name") == name),
        {},
    )
    return row.get("last_used_at")


def _planted_for_run(planted: list[dict], agent: str, spec: str) -> dict:
    """The one tool secret this sweep planted for ``agent``, which a run
    slot of ``agent`` fetches. None planted, or more than one, is refused:
    the slot would vouch for a row the run may never have read."""
    mine = [e for e in planted if e["agent"] == agent]
    if not mine:
        raise SweepFailure(
            f"--tool-secret-run {spec!r}: no --tool-secret was planted for {agent}, so the "
            f"run would fetch nothing this sweep set"
        )
    if len(mine) > 1:
        names = ", ".join(e["name"] for e in mine)
        raise SweepFailure(
            f"--tool-secret-run {spec!r}: {len(mine)} tool secrets were planted for {agent} "
            f"({names}), so its run slot cannot say which one the run fetched; plant one "
            f"per agent that a run slot drives"
        )
    return mine[0]


def sink_api(args, canaries: dict[str, str] | None = None, planted: list | None = None,
             sealed: list | None = None, tool_secrets: list | None = None,
             tool_secret_runs: list | None = None) -> Sink:
    """Sign in, read everything an operator can read, write the marker.

    The marker write is the positive control: ``PUT /admin/settings/{key}``
    puts the value in this response body, in ``app_settings.value`` and
    in an ``activity_audit_log`` detail, so one request proves three
    sinks can see what is really there. Each ``--secret-setting`` is then
    planted (``plant_secret_setting``), its fingerprint appended to
    ``planted``.
    """
    base = args.api_url.rstrip("/")
    parts: list[str] = []

    def record(label: str, status: int, body: str) -> None:
        parts.append(f"\n===== {label} -> {status} =====\n{body}\n")

    status, body = _http(
        "POST",
        f"{base}/api/v1/auth/login",
        body={"email": args.admin_email, "password": args.admin_password},
    )
    record("POST /auth/login", status, body)
    token = ""
    try:
        token = json.loads(body).get("access_token", "")
    except (ValueError, AttributeError):
        pass
    if not token:
        raise SweepFailure(
            "the sweep could not sign in, so every authenticated sink below "
            "would have been empty and the sweep would have 'found nothing' "
            f"by not looking (login returned {status})"
        )

    for label, path in (
        ("GET /meta", "/api/v1/meta"),
        ("GET /health", "/api/v1/health"),
        ("GET /agents", "/api/v1/agents"),
        ("GET /admin/settings", "/api/v1/admin/settings"),
        ("GET /admin/audit-log", "/api/v1/admin/audit-log"),
        ("GET /runs", "/api/v1/runs"),
        # K9: the trace pipeline's own report and the agent keys' list.
        ("GET /admin/otel-status", "/api/v1/admin/otel-status"),
        ("GET /admin/agent-keys", "/api/v1/admin/agent-keys"),
    ):
        status, body = _http("GET", f"{base}{path}", token=token)
        record(label, status, body)

    # K9: the deployment view is the page that shows the most of the
    # deployment, so it is read or the sweep stops: a refused read would
    # sweep an error body and "find nothing" in a view it never saw.
    status, body = _http("GET", f"{base}/api/v1/admin/deployment", token=token)
    record("GET /admin/deployment", status, body)
    if status != 200:
        raise SweepFailure(
            "the sweep could not read the deployment view "
            f"(GET /api/v1/admin/deployment returned {status}), so the page "
            "that shows the most of the deployment would go unswept"
        )

    # Error paths, which is where a process is most likely to say more
    # than it meant to.
    status, body = _http(
        "POST",
        f"{base}/api/v1/auth/login",
        body={"email": args.admin_email, "password": "definitely-not-the-password"},
    )
    record("POST /auth/login (wrong password)", status, body)
    status, body = _http(
        "GET",
        f"{base}/api/v1/runs/00000000-0000-0000-0000-000000000000",
        token=token,
    )
    record("GET /runs/{unknown}", status, body)
    status, body = _http("GET", f"{base}/api/v1/admin/settings/nope", token=token)
    record("GET /admin/settings/nope", status, body)

    # Every run's detail and report — the export path the blueprint names.
    runs = []
    status, body = _http("GET", f"{base}/api/v1/runs", token=token)
    try:
        payload = json.loads(body)
        runs = payload if isinstance(payload, list) else payload.get("items", [])
    except (ValueError, AttributeError):
        runs = []
    for run in runs[: args.max_runs]:
        run_id = run.get("run_id") or run.get("id")
        if not run_id:
            continue
        for label, path in (
            ("GET /runs/{id}", f"/api/v1/runs/{run_id}"),
            ("GET /runs/{id}/report/embedded", f"/api/v1/runs/{run_id}/report/embedded"),
        ):
            status, body = _http("GET", f"{base}{path}", token=token)
            record(f"{label} [{run_id}]", status, body)

    if args.marker and args.marker_setting:
        status, body = _http(
            "PUT",
            f"{base}/api/v1/admin/settings/{args.marker_setting}",
            token=token,
            body={"value": args.marker},
        )
        record(f"PUT /admin/settings/{args.marker_setting}", status, body)
        if status not in (200, 201):
            raise SweepFailure(
                f"the positive control could not be planted: "
                f"PUT /admin/settings/{args.marker_setting} returned {status}. "
                f"Without it nothing proves the API, app_settings and audit "
                f"sinks can see a value that IS there."
            )
        # Read it back through the list, so the API sink carries it too.
        status, body = _http("GET", f"{base}/api/v1/admin/settings", token=token)
        record("GET /admin/settings (after the marker write)", status, body)

    for spec in getattr(args, "secret_setting", None) or []:
        setting, _, slot = spec.partition(":")
        if not setting or not slot or slot not in (canaries or {}):
            raise SweepFailure(
                f"--secret-setting {spec!r}: expected KEY:SLOT with SLOT a canary in "
                f"--canaries"
            )
        entry = plant_secret_setting(base, token, setting, slot, canaries[slot], record)
        if planted is not None:
            planted.append(entry)
    if getattr(args, "secret_setting", None):
        # The list again, so the API sink carries the secret's state — and
        # must not carry its value.
        status, body = _http("GET", f"{base}/api/v1/admin/settings", token=token)
        record("GET /admin/settings (after the secret writes)", status, body)

    for spec in getattr(args, "sealed_key", None) or []:
        provider, _, path = spec.partition(":")
        if not provider or not path:
            raise SweepFailure(f"--sealed-key {spec!r}: expected NAME:FILE")
        entry = plant_sealed_key(base, token, provider, path, record)
        if sealed is not None:
            sealed.append(entry)

    planted_here: list[dict] = []
    for spec in getattr(args, "tool_secret", None) or []:
        agent, _, rest = spec.partition(":")
        name, _, slot = rest.partition(":")
        if not agent or not name or not slot or slot not in (canaries or {}):
            raise SweepFailure(
                f"--tool-secret {spec!r}: expected AGENT:NAME:SLOT with SLOT a canary in "
                f"--canaries"
            )
        entry = plant_tool_secret(base, token, agent, name, slot, canaries[slot], record)
        if tool_secrets is not None:
            tool_secrets.append(entry)
        # The agent's list again, so the API sink carries the secret's
        # state — and must not carry its value.
        status, body = _http("GET", f"{base}/api/v1/agents/{agent}/secrets", token=token)
        record(f"GET /agents/{agent}/secrets (after the {slot} write)", status, body)
        planted_here.append(entry)

    for spec in getattr(args, "tool_secret_run", None) or []:
        agent, _, raw = spec.partition(":")
        try:
            user_inputs = json.loads(raw)
        except ValueError:
            user_inputs = None
        if not agent or not isinstance(user_inputs, dict):
            raise SweepFailure(f"--tool-secret-run {spec!r}: expected AGENT:JSON, the JSON an object")
        planted_for_run = _planted_for_run(planted_here, agent, spec)
        entry = run_with_tool_secret(base, token, agent, user_inputs, planted_for_run, record)
        if tool_secret_runs is not None:
            tool_secret_runs.append(entry)

    return Sink("api-responses", "".join(parts), note=f"{len(parts)} response(s)")


def sink_trace(args) -> Sink:
    """The exported trace, read back out of the viewer.

    The blueprint's first form says "OTLP to a file sink"; the smoke
    stack already runs Jaeger and Vector already forwards to it, so the
    exported spans are readable over HTTP with nothing new deployed.
    Same spans, same question: did a secret reach a span attribute.
    """
    base = args.jaeger_url.rstrip("/")
    parts: list[str] = []
    status, body = _http("GET", f"{base}/api/services")
    parts.append(f"\n===== jaeger /api/services -> {status} =====\n{body}\n")
    services: list[str] = []
    try:
        services = json.loads(body).get("data") or []
    except (ValueError, AttributeError):
        services = []
    for service in services:
        status, body = _http(
            "GET", f"{base}/api/traces?service={service}&limit={args.trace_limit}"
        )
        parts.append(f"\n===== jaeger traces {service} -> {status} =====\n{body}\n")
    return Sink("exported-trace", "".join(parts), note=f"{len(services)} service(s)")


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------


def load_canaries(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict) or not raw:
        raise SweepFailure(f"{path}: expected a non-empty object of name -> value")
    canaries = {}
    for name, value in raw.items():
        value = (value or "").strip()
        if len(value) < 12:
            raise SweepFailure(
                f"{name}: a canary shorter than 12 characters would match by "
                f"accident, and a sweep that cries wolf gets turned off"
            )
        canaries[str(name)] = value
    return canaries


def sweep(
    sinks: list[Sink],
    canaries: dict[str, str],
    marker: str,
    planted: list | None = None,
    sealed: list | None = None,
    tool_secrets: list | None = None,
    tool_secret_runs: list | None = None,
) -> dict:
    findings = []
    marker_sinks = []
    for sink in sinks:
        for name, value in canaries.items():
            if value in sink.text:
                findings.append({"canary": name, "sink": sink.name})
        if marker and marker in sink.text:
            marker_sinks.append(sink.name)
    return {
        "sinks": [
            {"name": s.name, "bytes": s.bytes_read, "note": s.note} for s in sinks
        ],
        "canaries": sorted(canaries),
        "findings": findings,
        "marker_found_in": marker_sinks,
        # A fingerprint is a keyed digest, not the value, so the report
        # may carry it; where it was seen is what verify() needs.
        "planted": [
            {**entry, "found_in": [s.name for s in sinks if entry["fingerprint"] in s.text]}
            for entry in (planted or [])
        ],
        # K7: a sealed provider key's fingerprint, the gateway's, likewise.
        "sealed": [
            {**entry, "found_in": [s.name for s in sinks if entry["fingerprint"] in s.text]}
            for entry in (sealed or [])
        ],
        # K8a: a tool secret's fingerprint, the tenant row's, likewise.
        "tool_secrets": [
            {**entry, "found_in": [s.name for s in sinks if entry["fingerprint"] in s.text]}
            for entry in (tool_secrets or [])
        ],
        # K8b: each run that fetched a tool secret over MCP — its id, its
        # status and the row's last_used_at before and after it, never its
        # output.
        "tool_secret_runs": [dict(entry) for entry in (tool_secret_runs or [])],
    }


def _require_fingerprint(report: dict, sink: str, fingerprint: str) -> str | None:
    """A problem, unless the sealed secret with this fingerprint was seen in
    the sink named ``sink`` — the proof that sink read the row it is
    vouching for (K7: ``sql:gateway_status``). One function per requirement,
    so the K batches' hunks merge by keeping each."""
    entry = next(
        (e for e in report.get("sealed") or [] if e.get("fingerprint") == fingerprint), None
    )
    seen = (entry or {}).get("found_in") or []
    if sink in seen:
        return None
    what = f"the provider key sealed for {entry.get('provider')}" if entry else "a sealed provider key"
    return (
        f"the fingerprint of {what} is not in {sink} (seen in: {', '.join(seen) or 'none'}). "
        f"The row the gateway wrote was never read, so 'no canary in gateway_status' means nothing."
    )


def _require_tool_secret(report: dict, fingerprint: str) -> str | None:
    """A problem, unless the tool secret planted with this fingerprint was
    seen in ``sql:secrets`` — the proof the sweep read the row it vouches
    for (K8a), beside K7's ``_require_fingerprint``."""
    entry = next(
        (e for e in report.get("tool_secrets") or [] if e.get("fingerprint") == fingerprint), None
    )
    seen = (entry or {}).get("found_in") or []
    if "sql:secrets" in seen:
        return None
    what = (
        f"{entry.get('agent')}'s {entry.get('name')} ({entry.get('slot')})" if entry else "a tool secret"
    )
    return (
        f"the fingerprint of {what} is not in sql:secrets (seen in: {', '.join(seen) or 'none'}). "
        f"The row it names was never read, so 'no canary in the secrets table' means nothing."
    )


def _stamp_moved(before, after) -> bool:
    """Whether ``after`` is a stamp later than ``before`` (``None``: never
    stamped). An unreadable timestamp moved nothing."""
    from datetime import datetime

    if not isinstance(after, str) or not after:
        return False
    if before is None:
        return True
    try:
        return datetime.fromisoformat(after.replace("Z", "+00:00")) > datetime.fromisoformat(
            str(before).replace("Z", "+00:00")
        )
    except ValueError:
        return False


def _require_tool_secret_run(entry: dict) -> str | None:
    """A problem, unless the run slot shows the run complete and a stamp
    the run itself moved: the planted row's ``last_used_at`` later than it
    was before the run was submitted, the stamp the chassis writes when
    ``secret_get`` answers the run (K8b). Without one the run fetched
    nothing over MCP that the sweep can see, so a clean sweep of its output
    means nothing."""
    if (
        entry.get("status") == "complete"
        and "last_used_before" in entry
        and _stamp_moved(entry.get("last_used_before"), entry.get("last_used_at"))
    ):
        return None
    return (
        f"the run slot of {entry.get('agent')}'s {entry.get('name')} (run "
        f"{entry.get('run_id')}) shows status {entry.get('status')!r}, last_used_at "
        f"{entry.get('last_used_before', '<not recorded>')!r} before the run and "
        f"{entry.get('last_used_at')!r} after it. Without a stamp the run moved, the fetch "
        f"over MCP never happened as far as this sweep can tell, so 'no canary in the run's "
        f"output' means nothing. (The chassis stamps a row at most once an hour per process: "
        f"a sweep re-run within the hour on the same backend cannot prove the fetch.)"
    )


def _refuse_exemptions(report: dict) -> str | None:
    """No sink is exempt (D20, K8a). ``secret_get``'s result goes to the
    container and no sink records it, so the sweep needs no exemption for
    it — and a report that claims one is one where a canary could be found
    and waved through."""
    exemptions = report.get("exemptions")
    if not exemptions:
        return None
    return (
        f"the report claims exemptions ({exemptions!r}). Gate S has none: a tool secret "
        f"delivered to a run reaches no sink it reads, so an exempt sink is one where a "
        f"leak would be excused."
    )


def verify(
    report: dict,
    *,
    expect_sinks: int,
    min_bytes: int,
    marker_sinks: int,
    expect_planted: int = 0,
    expect_sealed: int = 0,
    expect_tool_secrets: int = 0,
    expect_tool_secret_runs: int = 0,
) -> None:
    """The report's own audit — run again, separately, by the workflow."""
    sinks = report.get("sinks") or []
    empty = [s["name"] for s in sinks if s.get("bytes", 0) < min_bytes]
    problems = []
    if len(sinks) < expect_sinks:
        problems.append(
            f"read {len(sinks)} sink(s) against a floor of {expect_sinks}. "
            f"Fewer means the sweep looked in fewer places than it was "
            f"asked to — find out which and why."
        )
    if empty:
        problems.append(
            f"these sinks came back with under {min_bytes} byte(s): "
            f"{', '.join(empty)}. An empty sink is not a clean sink; it is "
            f"a sink that was not read."
        )
    found = report.get("marker_found_in") or []
    if marker_sinks and len(found) < marker_sinks:
        problems.append(
            f"the positive control was found in {len(found)} sink(s) "
            f"({', '.join(found) or 'none'}) against a floor of "
            f"{marker_sinks}. The sweep cannot find a value it KNOWS is "
            f"there, so 'no canaries found' means nothing."
        )
    planted = report.get("planted") or []
    if len(planted) < expect_planted:
        problems.append(
            f"{len(planted)} secret(s) planted through the settings API against a floor "
            f"of {expect_planted}. Fewer means the secrets store was not a slot this "
            f"sweep tested."
        )
    for entry in planted:
        seen = entry.get("found_in") or []
        if not any(name.startswith("sql:") for name in seen):
            problems.append(
                f"the fingerprint of the secret planted through {entry.get('setting')} "
                f"({entry.get('slot')}) is in no sql: sink (seen in: "
                f"{', '.join(seen) or 'none'}). The row it names was never read, so "
                f"'no canary in the secrets table' means nothing."
            )
    sealed = report.get("sealed") or []
    if len(sealed) < expect_sealed:
        problems.append(
            f"{len(sealed)} provider key(s) sealed and adopted against a floor of "
            f"{expect_sealed}. Fewer means the sealed-key slot was not one this sweep tested."
        )
    for entry in sealed:
        problem = _require_fingerprint(report, "sql:gateway_status", entry.get("fingerprint", ""))
        if problem:
            problems.append(problem)
    tool_secrets = report.get("tool_secrets") or []
    if len(tool_secrets) < expect_tool_secrets:
        problems.append(
            f"{len(tool_secrets)} tool secret(s) planted through the agents API against a floor "
            f"of {expect_tool_secrets}. Fewer means the tool-secret slot was not one this sweep "
            f"tested."
        )
    for entry in tool_secrets:
        problem = _require_tool_secret(report, entry.get("fingerprint", ""))
        if problem:
            problems.append(problem)
    runs = report.get("tool_secret_runs") or []
    if len(runs) < expect_tool_secret_runs:
        problems.append(
            f"{len(runs)} run(s) fetched a tool secret over MCP against a floor of "
            f"{expect_tool_secret_runs}. Fewer means the fetch was not a slot this sweep tested."
        )
    for entry in runs:
        problem = _require_tool_secret_run(entry)
        if problem:
            problems.append(problem)
    problem = _refuse_exemptions(report)
    if problem:
        problems.append(problem)
    if report.get("findings"):
        problems.append(
            "canaries found: "
            + "; ".join(f"{f['canary']} in {f['sink']}" for f in report["findings"])
        )
    if problems:
        raise SweepFailure("\n".join(f"- {p}" for p in problems))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--canaries",
        default="",
        help="JSON file of {slot: value}. A FILE and never an argument: a "
        "value on a command line is in the process table and in the CI log.",
    )
    parser.add_argument("--report", default="sweep-report.json")
    parser.add_argument(
        "--verify-report",
        default="",
        help="read a report written by an earlier run and apply the floors; "
        "sweeps nothing itself",
    )
    parser.add_argument("--data-dir", action="append", default=[])
    parser.add_argument("--compose-service", action="append", default=[])
    parser.add_argument("--compose-file", action="append", default=[])
    parser.add_argument("--compose-profile", action="append", default=[])
    parser.add_argument(
        "--settings-dump",
        action="append",
        default=[],
        help="SERVICE:MODULE:ATTRIBUTE, e.g. backend:app.config:settings",
    )
    parser.add_argument("--psql-service", default="")
    parser.add_argument("--psql-user", default="librerun")
    parser.add_argument("--psql-db", default="librerun")
    parser.add_argument(
        "--sql",
        action="append",
        default=[],
        help="LABEL:STATEMENT, run through psql in --psql-service",
    )
    parser.add_argument("--api-url", default="")
    parser.add_argument("--admin-email", default="")
    parser.add_argument("--admin-password", default="")
    parser.add_argument("--max-runs", type=int, default=5)
    parser.add_argument("--jaeger-url", default="")
    parser.add_argument("--trace-limit", type=int, default=20)
    parser.add_argument(
        "--marker",
        default="",
        help="a value the sweep plants and must then FIND — the positive "
        "control that says the searching works",
    )
    parser.add_argument("--marker-setting", default="trace_viewer_base_url")
    parser.add_argument("--marker-sinks", type=int, default=0)
    parser.add_argument(
        "--secret-setting",
        action="append",
        default=[],
        help="KEY:SLOT — PUT the SLOT canary to the secret setting KEY through "
        "the admin API (K6); its fingerprint must then show in a sql: sink",
    )
    parser.add_argument(
        "--expect-planted",
        type=int,
        default=None,
        help="the floor on secrets planted through the settings API (default: "
        "one per --secret-setting; 0 with --verify-report)",
    )
    parser.add_argument(
        "--sealed-key",
        action="append",
        default=[],
        help="NAME:FILE — POST the provider key sealed in FILE (base64, made outside the "
        "sweep by openssl pkeyutl) to /admin/providers/NAME/key (K7); once the gateway has "
        "adopted it, its fingerprint must show in sql:gateway_status",
    )
    parser.add_argument(
        "--expect-sealed",
        type=int,
        default=None,
        help="the floor on sealed provider keys adopted (default: one per --sealed-key; "
        "0 with --verify-report)",
    )
    parser.add_argument(
        "--tool-secret",
        action="append",
        default=[],
        help="AGENT:NAME:SLOT — PUT the SLOT canary as this tenant's value of AGENT's declared "
        "tool secret NAME through the agents API (K8a); its fingerprint must then show in "
        "sql:secrets",
    )
    parser.add_argument(
        "--expect-tool-secrets",
        type=int,
        default=None,
        help="the floor on tool secrets planted (default: one per --tool-secret; 0 with "
        "--verify-report)",
    )
    parser.add_argument(
        "--tool-secret-run",
        action="append",
        default=[],
        help="AGENT:JSON — after AGENT's one --tool-secret is planted, submit one run of AGENT "
        "with JSON as its input, wait for it to complete (120 s at most) and require the row's "
        "last_used_at to have moved during the run, the proof it fetched the secret over MCP "
        "(K8b)",
    )
    parser.add_argument(
        "--expect-tool-secret-runs",
        type=int,
        default=None,
        help="the floor on runs that fetched a tool secret (default: one per "
        "--tool-secret-run; 0 with --verify-report)",
    )
    parser.add_argument("--expect-sinks", type=int, default=1)
    parser.add_argument("--min-sink-bytes", type=int, default=1)
    args = parser.parse_args(argv)
    if args.expect_planted is None:
        args.expect_planted = 0 if args.verify_report else len(args.secret_setting)
    if args.expect_sealed is None:
        args.expect_sealed = 0 if args.verify_report else len(args.sealed_key)
    if args.expect_tool_secrets is None:
        args.expect_tool_secrets = 0 if args.verify_report else len(args.tool_secret)
    if args.expect_tool_secret_runs is None:
        args.expect_tool_secret_runs = 0 if args.verify_report else len(args.tool_secret_run)

    if args.verify_report:
        with open(args.verify_report, encoding="utf-8") as handle:
            report = json.load(handle)
        print(
            f"secret-sweep: verifying {args.verify_report}: "
            f"{len(report.get('sinks') or [])} sink(s), "
            f"{len(report.get('findings') or [])} finding(s), "
            f"marker in {len(report.get('marker_found_in') or [])} sink(s), "
            f"{len(report.get('planted') or [])} planted secret(s), "
            f"{len(report.get('sealed') or [])} sealed provider key(s), "
            f"{len(report.get('tool_secrets') or [])} tool secret(s), "
            f"{len(report.get('tool_secret_runs') or [])} tool-secret run(s)"
        )
        verify(
            report,
            expect_sinks=args.expect_sinks,
            min_bytes=args.min_sink_bytes,
            marker_sinks=args.marker_sinks,
            expect_planted=args.expect_planted,
            expect_sealed=args.expect_sealed,
            expect_tool_secrets=args.expect_tool_secrets,
            expect_tool_secret_runs=args.expect_tool_secret_runs,
        )
        print("secret-sweep: the report holds up")
        return 0

    if not args.canaries:
        raise SweepFailure("--canaries is required (or --verify-report)")
    canaries = load_canaries(args.canaries)

    # The sweep's own files are not sinks. Resolved, because ./data may
    # be reached by a different spelling of the same path.
    own = {str(Path(p).resolve()) for p in (args.canaries, args.report) if p}

    sinks: list[Sink] = []
    planted: list[dict] = []
    sealed: list[dict] = []
    tool_secrets: list[dict] = []
    tool_secret_runs: list[dict] = []
    # The API first: it plants the marker the other sinks must show, and
    # the secret settings and sealed provider keys whose fingerprints the
    # sql: sinks must show.
    if args.api_url:
        sinks.append(sink_api(args, canaries, planted, sealed, tool_secrets, tool_secret_runs))
    elif args.secret_setting or args.sealed_key or args.tool_secret or args.tool_secret_run:
        raise SweepFailure(
            "--secret-setting, --sealed-key, --tool-secret and --tool-secret-run plant "
            "through the API: they need --api-url"
        )
    for service in args.compose_service:
        sinks.append(sink_container_logs(args, service))
    for spec in args.settings_dump:
        service, module, attribute = spec.split(":", 2)
        sinks.append(sink_settings_dump(args, service, module, attribute))
    if args.psql_service:
        for spec in args.sql:
            label, statement = spec.split(":", 1)
            sinks.append(sink_sql(args, label, statement))
    if args.jaeger_url:
        sinks.append(sink_trace(args))
    # The file tree LAST: the container logs and the API calls above are
    # what put things in it.
    for root in args.data_dir:
        sinks.append(sink_file_tree(root, skip=own))

    report = sweep(sinks, canaries, args.marker, planted, sealed, tool_secrets, tool_secret_runs)
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)

    for entry in report["sinks"]:
        print(f"secret-sweep: {entry['name']}: {entry['bytes']} bytes — {entry['note']}")
    for entry in report["planted"]:
        print(
            f"secret-sweep: planted {entry['slot']} through {entry['setting']}; its "
            f"fingerprint is in {', '.join(entry['found_in']) or 'no sink'}"
        )
    for entry in report["sealed"]:
        print(
            f"secret-sweep: sealed a provider key for {entry['provider']}; the gateway's "
            f"fingerprint is in {', '.join(entry['found_in']) or 'no sink'}"
        )
    for entry in report["tool_secrets"]:
        print(
            f"secret-sweep: planted {entry['slot']} as {entry['agent']}'s {entry['name']}; its "
            f"fingerprint is in {', '.join(entry['found_in']) or 'no sink'}"
        )
    for entry in report["tool_secret_runs"]:
        print(
            f"secret-sweep: run {entry['run_id']} of {entry['agent']} ended {entry['status']}; "
            f"{entry['name']}'s row was last used at {entry['last_used_before'] or 'never'} "
            f"before it and {entry['last_used_at'] or 'never'} after it"
        )
    print(
        f"secret-sweep: {len(canaries)} canary/canaries, "
        f"{len(report['findings'])} finding(s), marker in "
        f"{len(report['marker_found_in'])} sink(s) -> {args.report}"
    )

    verify(
        report,
        expect_sinks=args.expect_sinks,
        min_bytes=args.min_sink_bytes,
        marker_sinks=args.marker_sinks,
        expect_planted=args.expect_planted,
        expect_sealed=args.expect_sealed,
        expect_tool_secrets=args.expect_tool_secrets,
        expect_tool_secret_runs=args.expect_tool_secret_runs,
    )
    print("secret-sweep: no canary reached any sink")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SweepFailure as failure:
        print(f"::error::secret-sweep FAILED\n{failure}", file=sys.stderr)
        sys.exit(1)
