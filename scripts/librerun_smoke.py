#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Scenario-driven smoke: the §3 demo parity gate, automated (blueprint B14).

Drives a booted stack the way a customer does — log in, load the agent's
demo scenario, submit it, watch live progress, approve at the gate, wait
for the report, read it back — and fails loudly if any step doesn't
happen. Stdlib only: it runs on a bare CI runner against the compose
stack, and on a laptop against `uvicorn` just as well.

    python scripts/librerun_smoke.py --base-url http://localhost:8000 \
        --email admin@example.com --password ... --agent vita-v1

Behind the HTTPS edge (K blueprint T1) the base is the edge's origin, and
`--cacert` names the CA its certificate is verified against — for the
edge's local CA, the root copied out of `librerun-edge`:

    python scripts/librerun_smoke.py --base-url https://librerun.test:8443 \
        --cacert edge-root.crt --email ... --password ... --agent vita-v1

Credentials are passed explicitly, and only that way. A `--env-file`
convenience lived here briefly and drew seven review findings in six
rounds, every one an instance of the same false premise: that a script
talking to a backend over HTTP can work out what that backend loaded.
It cannot — the backend may be a container, on another host, or started
from any directory. Pass the credentials you configured.

Do NOT `set -a && . ./.env` to fill them either: bash evaluates the file,
so `pw$(echo BAD)x` RUNS and `pa$$word1` becomes the shell's PID. Read
the values and type them, quoted.

Exit code 0 means: the stack booted, the agent was discovered, intake
validated and persisted, the pipeline ran both phases through a human
gate, a report exists and contains what the agent promised, and the run
carries a trace id. That is the gate — not a mock of it.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

TERMINAL_OK = "complete"
TERMINAL_BAD = "error"


class SmokeFailure(RuntimeError):
    """A gate step did not happen. The message is the CI failure line."""


# The context an https base is verified against: `--cacert`'s file alone,
# as curl's --cacert, or the system's trust when it is not given. An http
# URL (the base, or the trace viewer's) never uses it.
_TLS_CONTEXT: ssl.SSLContext | None = None


def _request(method: str, url: str, *, token: str | None = None, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_TLS_CONTEXT) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:2000]}


def wait_for_health(base: str, attempts: int = 60, delay: float = 2.0) -> None:
    for i in range(attempts):
        try:
            status, body = _request("GET", f"{base}/api/v1/health", timeout=5)
            if status == 200:
                print(f"health ok after {i * delay:.0f}s: {body}")
                assert_detector_ready(body)
                return
        except Exception as e:  # noqa: BLE001 — the stack is still coming up
            if isinstance(e, SmokeFailure):
                raise
            if i == 0:
                print(f"waiting for {base} … ({e})")
        time.sleep(delay)
    raise SmokeFailure(f"backend never became healthy at {base}")


def assert_detector_ready(health: dict) -> None:
    """Blueprint S4c: the deployment this smoke just certified has a
    WORKING named-entity detector.

    A 200 from /health proves the process is up, not that stage 3 of the
    redaction pipeline can run — and a deployment whose spaCy model is
    missing now refuses every intake, so a smoke that did not look here
    would report the failure as a confusing 503 three steps later, or
    (with the operator opt-out set) would pass while every run was
    stored regex-only.
    """
    detector = health.get("pii_detector")
    if not isinstance(detector, dict):
        raise SmokeFailure(
            "/health carries no pii_detector block: this build predates "
            "blueprint S4c, or the field was dropped"
        )
    state = detector.get("state")
    if state != "ready":
        raise SmokeFailure(
            f"pii_detector.state is {state!r}, not 'ready' "
            f"(coverage={detector.get('coverage')!r}, "
            f"error={detector.get('error')!r}): the PII detector did not "
            "come up, so this deployment cannot redact names, places or "
            "organisations"
        )
    print(f"pii detector ready: coverage={detector.get('coverage')}")


def login(base: str, email: str, password: str) -> str:
    status, body = _request(
        "POST",
        f"{base}/api/v1/auth/login",
        body={"email": email, "password": password},
    )
    if status != 200 or not body or "access_token" not in body:
        raise SmokeFailure(f"login failed ({status}): {body}")
    print(f"logged in as {email}")
    return body["access_token"]


def pick_scenario(base: str, token: str, agent_id: str) -> dict:
    status, agents = _request("GET", f"{base}/api/v1/agents", token=token)
    if status != 200 or not agents:
        raise SmokeFailure(f"no agents discovered ({status}): {agents}")
    ids = [a["agent_id"] for a in agents]
    if agent_id not in ids:
        raise SmokeFailure(f"agent {agent_id!r} not discovered; found {ids}")
    print(f"agents discovered: {ids}")

    status, scenarios = _request(
        "GET", f"{base}/api/v1/agents/{agent_id}/scenarios", token=token
    )
    if status != 200 or not scenarios:
        raise SmokeFailure(
            f"agent {agent_id!r} serves no demo scenario ({status}): {scenarios}"
        )
    print(f"scenario: {scenarios[0].get('name')!r}")
    return scenarios[0]


def submit(base: str, token: str, agent_id: str, user_inputs: dict) -> dict:
    status, body = _request(
        "POST",
        f"{base}/api/v1/runs?agent_id={agent_id}",
        token=token,
        body=user_inputs,
    )
    if status not in (200, 201, 202) or not body:
        raise SmokeFailure(f"run submission failed ({status}): {body}")
    run_id = body.get("run_id")
    number = body.get("run_number")
    if not run_id or not number:
        raise SmokeFailure(f"run submission returned no run_id/run_number: {body}")
    # The deprecated duplicates (blueprint S1: one release, gone at v1.1)
    # must be byte-identical to the authoritative fields while they exist.
    for new, old in (("run_id", "case_id"), ("run_number", "case_number")):
        if body.get(old) != body.get(new):
            raise SmokeFailure(
                f"deprecated duplicate {old}={body.get(old)!r} does not "
                f"mirror {new}={body.get(new)!r}"
            )
    print(f"submitted {number} ({run_id}) status={body.get('status')}")
    return {"id": run_id, "number": number}


def poll(
    base: str,
    token: str,
    run_id: str,
    until,
    attempts: int,
    delay: float,
    on_tick=None,
) -> dict:
    last = {}
    for i in range(attempts):
        status, body = _request("GET", f"{base}/api/v1/runs/{run_id}", token=token)
        if status != 200 or not body:
            raise SmokeFailure(f"run fetch failed ({status}): {body}")
        last = body
        state = body.get("status")
        # Sampled before the exit checks so the last observation happens with
        # the run in its terminal state too.
        if on_tick is not None:
            on_tick()
        if state == TERMINAL_BAD:
            raise SmokeFailure(f"run errored after {i * delay:.0f}s")
        if until(state):
            print(f"  -> {state} after {i * delay:.0f}s")
            return body
        time.sleep(delay)
    raise SmokeFailure(
        f"run stuck in {last.get('status')!r} after {attempts * delay:.0f}s"
    )


def _fetch_progress(base: str, token: str, run_id: str) -> dict:
    status, body = _request("GET", f"{base}/api/v1/runs/{run_id}/progress", token=token)
    if status != 200:
        raise SmokeFailure(f"progress endpoint failed ({status}): {body}")
    return body or {}


def _progress_records(body: dict) -> dict[str, tuple]:
    return {
        s["step_id"]: (s.get("status"), s.get("duration_ms"), s.get("detail"))
        for s in (body or {}).get("steps", [])
        if s.get("step_id")
    }


def check_progress(base: str, token: str, run_id: str) -> dict[str, tuple]:
    """Live progress is part of the gate — the endpoint must serve without
    blowing up on anything the agent wrote into it.

    Returns ``{step_id: record}`` so the caller can prove a *later* phase
    reported something. ``run:{id}:progress`` is one cumulative hash keyed by
    step id with no per-step phase, so "are there steps?" asked after approval
    would be satisfied by the earlier phase's entries alone.

    The value is the whole record, not just the id: nothing requires step ids
    to be unique across phases (``StepProgress.step_id`` is a bare ``str``),
    and the write is an ``hset`` on that id, so a phase reusing an id updates
    the existing entry instead of adding one."""
    body = _fetch_progress(base, token, run_id)
    steps = body.get("steps", [])
    if not steps:
        # 200-with-nothing used to pass. An agent that streams no progress
        # at all would reach the gate, finish, and be certified as having
        # "watched live progress" — the endpoint was checked for not
        # blowing up, which is not the same as checking that anything was
        # reported.
        #
        # This asserts the API, not the UI, and the two differ here: the run
        # page renders ProgressList only in the ``investigating`` state (the
        # final phase), so phase 1's steps are served by this endpoint while
        # the page shows "Analyzing your inputs…". An agent that reports
        # nothing is still broken — the steps drive the page for every later
        # phase, and they are the record of what phase 1 actually did.
        raise SmokeFailure(
            "the progress endpoint returned 200 with no steps — the agent "
            "reported nothing for this phase, so there is no record of what "
            "it did and later phases would render an empty progress list. "
            "This is the live-progress half of the parity gate."
        )
    print(f"progress: phase={body.get('phase')} steps={len(steps)}")
    return _progress_records(body)


class FinalPhaseProgressWatch:
    """The final phase must report progress of its own.

    Checking only before approval left the one phase whose progress the run
    page actually renders — the final one, where it mounts ProgressList —
    unchecked. Because the progress hash is cumulative, an agent that emitted
    nothing after the gate still looked green: the earlier phase's entries
    were still sitting there.

    Terminal state alone cannot settle it, though. ``run:{id}:progress`` is
    an ``hset`` keyed by step id with no per-step phase, and nothing requires
    ids to be unique across phases, so an agent that reuses an id and takes
    it ``running`` -> ``complete`` in *both* phases leaves an identical record
    at the gate and at the end. Comparing only the final snapshot would fail
    that healthy run, even though the phase-2 ``running`` update was on
    screen for the user.

    So sample while the final phase runs as well, and accept a change seen at
    any point. Only an agent that never reports anything leaves every
    observation identical.

    Residual, stated rather than hidden: if the final phase is faster than
    the poll interval *and* reuses ids *and* ends in the same record, no
    sample catches the transition and this still fails. Closing that needs
    phase attribution in the progress data itself, which is a chassis change,
    not a smoke-script one."""

    def __init__(self, base: str, token: str, run_id: str, before: dict[str, tuple]):
        self._base = base
        self._token = token
        self._run_id = run_id
        self._before = before
        self._observed: tuple[str, set] | None = None

    def _changed(self, now: dict[str, tuple]) -> set:
        return {k for k, v in now.items() if self._before.get(k) != v}

    def sample(self) -> None:
        """Cheap mid-flight observation; called on each poll tick."""
        if self._observed:
            return
        try:
            now = _progress_records(_fetch_progress(self._base, self._token, self._run_id))
        except SmokeFailure:
            return  # transient read; the final assertion still runs
        changed = self._changed(now)
        if changed:
            self._observed = ("while the phase was running", changed)

    def assert_reported(self) -> None:
        # Also re-runs the serves-and-is-non-empty assertion on the final state.
        changed = self._changed(check_progress(self._base, self._token, self._run_id))
        if changed:
            self._observed = self._observed or ("at completion", changed)
        if not self._observed:
            raise SmokeFailure(
                "the final phase reported no progress — nothing was added or "
                "updated at any point after the gate, sampled every poll and "
                f"again at the end, leaving the same {len(self._before)} step "
                "record(s) present at approval. That is the phase whose "
                "progress the run page displays, so a user would watch a "
                "stale list from the previous phase and see nothing happen "
                "for the whole investigation."
            )
        when, what = self._observed
        print(f"final-phase progress: {len(what)} step(s) changed ({when})")


def read_report(base: str, token: str, run_id: str, expected: list[str]) -> str:
    status, body = _request(
        "GET", f"{base}/api/v1/runs/{run_id}/report/embedded", token=token
    )
    if status != 200 or not body or not body.get("html"):
        raise SmokeFailure(f"report missing ({status}): {str(body)[:400]}")
    html = body["html"]
    missing = [probe for probe in expected if probe not in html]
    if missing:
        raise SmokeFailure(
            f"report is missing expected content {missing}; got {len(html)} chars"
        )
    print(f"report ok: {len(html)} chars, all {len(expected)} probes present")
    return html


def _tree_has_settled(spans: list, *, require_llm: bool) -> bool:
    """Has everything the assertions below need actually arrived?

    Two processes export into this trace on their own batch schedules —
    the backend its phase and step spans, the gateway its LLM spans — so
    a trace read too early is a trace with holes in it. The old wait was
    "at least two spans", which the backend alone satisfies almost
    immediately; the gate then read a half-assembled tree and reported
    an LLM span whose parent had not landed yet as a SECOND ROOT. That
    is the pre-S4 defect's exact signature, announced on a run that
    never had it.

    So the wait condition is the assertions' own precondition: one root,
    that root the submission's ``run`` span, and an LLM span present when
    one is required. Waiting changes WHEN the gate looks, never what it
    accepts — a run that is genuinely more than one tree still has more
    than one root after the last attempt, and still fails with the same
    message.

    The root's name is part of that precondition. Read before any of the
    backend's spans has landed, the trace can be the gateway's first LLM
    span alone — its step not arrived yet, so it is the only root, and it
    is an LLM span. "One root and an LLM span" held, and the gate
    reported "the root span is 'chat gpt-4o', not 'run'" on a correct
    run (main's push run on 74930ca). The browser walk's poll
    (frontend/e2e/delight.spec.ts) already waited for a root named
    ``run``; this is the same condition.
    """
    if not spans:
        return False
    by_id = {s["spanID"]: s for s in spans}
    roots = [
        s
        for s in spans
        if not [r for r in s.get("references") or [] if r.get("spanID") in by_id]
    ]
    if len(roots) != 1 or roots[0].get("operationName") != "run":
        return False
    if require_llm and not [
        s
        for s in spans
        if str(s.get("operationName", "")).startswith(("chat ", "embeddings "))
    ]:
        return False
    return True


def assert_one_tree(
    viewer_base: str,
    trace_id: str,
    attempts: int = 20,
    delay: float = 3.0,
    require_llm: bool = True,
) -> dict:
    """The run is ONE tree in the viewer: a single root, both phases under it.

    A stable trace id says the chassis kept one id; it does not say the
    spans are one tree. They were not, before S4 — each phase was its
    own trace joined by a link, so a gated run rendered as two traces
    and "one tree from intake" was untrue for exactly the runs a human
    had looked at. That is invisible from the API and obvious in the
    viewer, so it is asserted where the viewer reads: the trace itself.

    Jaeger's HTTP API, since Jaeger is what the demo ships. Returns a
    summary of what it found.
    """
    url = f"{viewer_base.rstrip('/')}/api/traces/{trace_id}"
    spans = []
    for attempt in range(attempts):
        try:
            status, body = _request("GET", url)
        except SmokeFailure:
            status, body = 0, None
        if status == 200 and body and body.get("data"):
            spans = body["data"][0].get("spans") or []
            if _tree_has_settled(spans, require_llm=require_llm):
                break
        if attempt < attempts - 1:
            time.sleep(delay)
    if not spans:
        raise SmokeFailure(
            f"the viewer has no spans for trace {trace_id} — traces are part "
            f"of the gate. Tried {url}"
        )

    by_id = {s["spanID"]: s for s in spans}
    roots = [s for s in spans if not [r for r in s.get("references") or [] if r.get("spanID") in by_id]]
    if len(roots) != 1:
        names = sorted(s["operationName"] for s in roots)
        raise SmokeFailure(
            f"the run is {len(roots)} trees, not one: roots {names}. A gated "
            f"run whose phases are separate traces is the defect S4 closed."
        )
    root = roots[0]
    if root["operationName"] != "run":
        raise SmokeFailure(
            f"the root span is {root['operationName']!r}, not 'run' — the "
            f"submission request is supposed to open the tree"
        )
    if any(s.get("traceID") != root.get("traceID") for s in spans):
        raise SmokeFailure("the viewer returned spans from more than one trace id")

    names = sorted(s["operationName"] for s in spans)
    # A run that produced a report without an LLM span called a model
    # some other way — which is the one thing the gateway exists to make
    # impossible. ``require_llm=False`` is for callers asserting the
    # SHAPE of a tree that never called a model.
    llm = assert_llm_spans(spans) if require_llm else {"count": 0, "spans": []}
    print(f"one tree: root {root['operationName']!r}, {len(spans)} spans {names}")
    return {
        "spans": len(spans),
        "root": root["operationName"],
        "names": names,
        "llm_spans": llm,
    }


# What a gateway span must carry (blueprint S4a): the model that answered,
# the tokens it used and what it cost. Asserted in the viewer rather than
# in a unit test because the claim is that an OPERATOR can see it.
LLM_SPAN_ATTRIBUTES = (
    "gen_ai.request.model",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "librerun.cost_usd",
)


def assert_llm_spans(spans: list) -> dict:
    """Every LLM span in the tree names its model, its tokens and its cost.

    Keyless runs included: the gateway's stub is costed against the model
    it stands in for, precisely so this assertion is not one that only
    holds where somebody has a provider account.
    """
    llm = [
        s
        for s in spans
        if str(s.get("operationName", "")).startswith(("chat ", "embeddings "))
    ]
    if not llm:
        raise SmokeFailure(
            "the run's trace has no LLM span. Every model call goes through "
            "the gateway, which writes exactly one span per call — a run that "
            "produced a report without one called a model some other way."
        )
    seen = []
    for span in llm:
        tags = {t.get("key"): t.get("value") for t in span.get("tags") or []}
        missing = [a for a in LLM_SPAN_ATTRIBUTES if a not in tags]
        if missing:
            raise SmokeFailure(
                f"LLM span {span['operationName']!r} is missing {missing}. "
                f"Model, tokens and cost on every call is promise 3."
            )
        model = str(tags["gen_ai.request.model"]).strip()
        if not model:
            raise SmokeFailure(
                f"LLM span {span['operationName']!r} names an empty model"
            )
        # A redacted name is not a model, and "non-empty" would not have
        # noticed: the export walks every string it was not told the
        # chassis wrote, and the recognizers read a dated model name as a
        # person's. An assertion that accepts [REDACTED_PERSON_1] is an
        # assertion that stopped looking.
        if (
            "REDACTED" in model
            or "REDACTED" in str(tags.get("gen_ai.response.model", ""))
            or "REDACTED" in str(span.get("operationName", ""))
        ):
            raise SmokeFailure(
                f"LLM span {span['operationName']!r} names the model {model!r} — "
                f"the export redacted what the chassis resolved. The resolved "
                f"provider and model are stamped (S4's chassis record) so the "
                f"walk leaves them alone — the span's NAME included, which "
                f"is the first thing an operator reads. This says a stamp is "
                f"missing."
            )
        if float(tags["librerun.cost_usd"]) <= 0:
            raise SmokeFailure(
                f"LLM span {span['operationName']!r} reports a cost of "
                f"{tags['librerun.cost_usd']} — an unpriced call is a call "
                f"nobody can budget for"
            )
        seen.append(
            {
                "name": span["operationName"],
                "model": tags["gen_ai.request.model"],
                "cost_usd": tags["librerun.cost_usd"],
            }
        )
    print(f"{len(seen)} LLM span(s), each with a model, tokens and a cost")
    return {"count": len(seen), "spans": seen}


def assert_step_models(base: str, token: str, run_id: str) -> list[str]:
    """The run page can say which model answered each step (D13).

    The gateway writes it to ``run:{id}:step_models`` and the progress
    endpoint joins it in, which is what makes "change the model in the UI
    and see it on the next run" a thing an operator can check without
    opening a trace viewer.
    """
    body = _fetch_progress(base, token, run_id)
    models = [
        s.get("model")
        for s in (body or {}).get("steps", [])
        if s.get("model")
    ]
    if not models:
        steps = [s.get("step_id") for s in (body or {}).get("steps", [])]
        raise SmokeFailure(
            f"no step reports the model that answered it (steps: {steps}). "
            f"The gateway writes it to run:{{id}}:step_models and "
            f"GET /runs/{{id}}/progress joins it in, so the run page can "
            f"show it. An empty join means the gateway never wrote one — "
            f"or that the two halves disagree about the key."
        )
    print(f"steps reporting a model: {sorted(set(models))}")
    return models


def assert_meta_gateway(
    base: str, attempts: int = 60, delay: float = 2.0
) -> dict:
    """``/api/v1/meta`` sources keyless mode from the gateway (S4a).

    The backend holds no such switch any more, so a ``stub_llm`` that is
    null means the gateway is unreachable — which the smoke must fail on
    rather than read as "not stubbed".

    …but must WAIT for before failing on, which it did not until issue
    #85. ``wait_for_health`` above returns when the BACKEND is healthy,
    and `compose.yaml` gives the backend ``gateway: condition:
    service_started`` **deliberately** — "a run that reaches a model
    before the gateway is up fails that phase with a named error, which
    beats holding the whole API closed behind it". So the backend is
    healthy and answering while the gateway may still be coming up, and
    the gateway's healthcheck carries ``start_period: 60s``. Reading
    ``/meta`` once, right after health, made every caller of this smoke
    race that window: observed on one commit as a ~29s boot reporting
    ``ok`` and a ~4m45s boot reporting ``unreachable``.

    A non-200 is still immediate: ``wait_for_health`` has already
    returned by here, so the backend not answering is a real failure
    rather than something to sit through.
    """
    # Carried out of the loop rather than read from `body` after it: with
    # `attempts` at 0 the loop never binds `body`, and the failure path
    # would raise NameError over whatever really went wrong.
    last_seen = None
    for attempt in range(attempts):
        status, body = _request("GET", f"{base}/api/v1/meta")
        if status != 200 or not isinstance(body, dict):
            raise SmokeFailure(f"/api/v1/meta answered {status}")
        last_seen = body.get("gateway")
        if last_seen == "ok":
            if attempt:
                print(f"meta: gateway ok after {attempt * delay:.0f}s")
            break
        if attempt == 0:
            print(f"waiting for the gateway … (gateway={last_seen!r})")
        time.sleep(delay)
    else:
        raise SmokeFailure(
            f"/api/v1/meta reports gateway={last_seen!r} after "
            f"{attempts * delay:.0f}s: the backend could not reach the "
            f"gateway, so it cannot say whether the deployment is keyless"
        )
    if not isinstance(body.get("stub_llm"), bool):
        raise SmokeFailure(
            f"/api/v1/meta reports stub_llm={body.get('stub_llm')!r}, which is "
            f"not a fact about the deployment"
        )
    print(f"meta: stub_llm={body['stub_llm']} (sourced from the gateway)")
    return {"stub_llm": body["stub_llm"], "gateway": body["gateway"]}


def fetch_admin_trace_id(base: str, email: str, password: str, run_id: str) -> str:
    """The run's trace id, read through the admin view.

    ``trace_id`` is deliberately admin-only — it is operator
    observability, not customer-facing — so the trace link half of the
    parity gate is checked with an admin login when one is supplied.
    """
    if not email or not password:
        print("no admin credentials supplied — skipping the trace lookup")
        return ""
    token = login(base, email, password)
    status, body = _request(
        "GET", f"{base}/api/v1/admin/runs/{run_id}", token=token
    )
    if status != 200 or not body:
        raise SmokeFailure(f"admin run detail failed ({status}): {str(body)[:300]}")
    return body.get("trace_id") or ""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument(
        "--cacert",
        default="",
        help="a PEM CA file an https --base-url is verified against, alone (K blueprint "
        "T1): the HTTPS edge's local root, copied out of librerun-edge",
    )
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--agent", default="vita-v1")
    p.add_argument(
        "--admin-email",
        default="",
        help="admin login used to read the run's trace id — that field is "
        "admin-only (operator observability), so the trace half of the gate "
        "needs it. Without it the trace assertion is skipped.",
    )
    p.add_argument("--admin-password", default="")
    p.add_argument(
        "--expect",
        action="append",
        default=None,
        help="substring the report must contain (repeatable)",
    )
    p.add_argument("--phase-attempts", type=int, default=90)
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--summary-json", default="")
    p.add_argument(
        "--trace-viewer-url",
        default="",
        help="Jaeger base URL (e.g. http://localhost:16686). When given, the "
        "run's trace is read back and asserted to be ONE tree: a single root "
        "span `run` with every phase under it. Needs --admin-email too, since "
        "the trace id is admin-only.",
    )
    p.add_argument(
        "--no-require-trace",
        action="store_true",
        help="don't fail when the run carries no trace id (for stacks booted "
        "without any OTEL endpoint; CI keeps the assertion on, since the "
        "trace link is part of the demo parity gate)",
    )
    args = p.parse_args()

    if args.cacert:
        global _TLS_CONTEXT
        if not os.path.isfile(args.cacert):
            raise SmokeFailure(f"--cacert {args.cacert}: no such file")
        _TLS_CONTEXT = ssl.create_default_context(cafile=args.cacert)

    expected = args.expect or ["Refined problem statement", "Works cited"]
    base = args.base_url.rstrip("/")
    started = time.time()

    wait_for_health(base)
    token = login(base, args.email, args.password)
    scenario = pick_scenario(base, token, args.agent)
    run = submit(base, token, args.agent, scenario["user_inputs"])

    # Phase 1 → the human gate (or straight through for ungated agents).
    detail = poll(
        base,
        token,
        run["id"],
        lambda s: s in ("awaiting_approval", TERMINAL_OK),
        args.phase_attempts,
        args.delay,
    )
    at_gate = check_progress(base, token, run["id"])

    # The trace id as it stands at the gate — before any phase after
    # approval has run. The runner used to overwrite it at each phase
    # start, so "the run has a trace id" was true and useless: the id a
    # user saw before approving was not the one their report linked to.
    trace_at_gate = detail.get("trace_id") or fetch_admin_trace_id(
        base, args.admin_email, args.admin_password, run["id"]
    )

    if detail.get("status") == "awaiting_approval":
        status, _ = _request(
            "POST", f"{base}/api/v1/runs/{run['id']}/approve", token=token
        )
        if status not in (200, 201, 202):
            raise SmokeFailure(f"approve failed ({status})")
        print("approved at the gate")
        # Only meaningful for a gated agent: an ungated one has no phase
        # after the gate to report anything.
        watch = FinalPhaseProgressWatch(base, token, run["id"], at_gate)
        detail = poll(
            base,
            token,
            run["id"],
            lambda s: s == TERMINAL_OK,
            args.phase_attempts,
            args.delay,
            on_tick=watch.sample,
        )
        watch.assert_reported()

    html = read_report(base, token, run["id"], expected)
    # Blueprint S4a: the gateway is the only door to a model, so a
    # completed run must have left its marks — the model on each step's
    # progress entry, and the deployment's keyless state coming from the
    # gateway rather than from a switch the backend no longer has.
    step_models = assert_step_models(base, token, run["id"])
    meta = assert_meta_gateway(base)

    trace_id = detail.get("trace_id") or fetch_admin_trace_id(
        base, args.admin_email, args.admin_password, run["id"]
    )
    if trace_at_gate and trace_id and trace_at_gate != trace_id:
        raise SmokeFailure(
            f"the run changed trace id across the approval gate: {trace_at_gate} "
            f"before, {trace_id} after. One run is one trace."
        )
    if trace_at_gate:
        print(f"trace id unchanged across the gate: {trace_at_gate}")
    if not trace_id and not args.no_require_trace:
        raise SmokeFailure(
            "run carries no trace id — the trace link is part of the demo "
            "parity gate. Boot the stack with an OTLP endpoint (compose "
            "defaults to the bundled Vector) or pass --no-require-trace."
        )
    print(f"trace id: {trace_id or '(not required)'}")

    elapsed = time.time() - started
    summary = {
        "run_number": run["number"],
        "run_id": run["id"],
        "agent": args.agent,
        "scenario": scenario.get("name"),
        "status": detail.get("status"),
        "report_chars": len(html),
        "trace_id": trace_id,
        "elapsed_seconds": round(elapsed, 1),
        "stub_llm": meta["stub_llm"],
        "step_models": sorted(set(step_models)),
    }
    if args.trace_viewer_url and trace_id:
        summary["trace_tree"] = assert_one_tree(args.trace_viewer_url, trace_id)
    elif args.trace_viewer_url:
        raise SmokeFailure(
            "--trace-viewer-url was given but the run carries no trace id to "
            "look up (pass --admin-email/--admin-password: the id is admin-only)"
        )
    print("\nSMOKE SUMMARY " + json.dumps(summary))
    if args.summary_json:
        with open(args.summary_json, "w") as f:
            json.dump(summary, f, indent=2)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeFailure as exc:
        print(f"\nSMOKE FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
