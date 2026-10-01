"""Two obligations the container battery drove past.

Both were recorded in the "Deliberately not in this PR" section of the
S5-F pull request (`PR #56`) as follow-ups, never filed as issues. The
commit that closed them cites "#56, #57" as if they were issue numbers;
they are not — those are two merged pull requests, and the numbers came
from an internal task list. The prose here names the obligations rather
than a number, which is what should have been done.

The battery's POST body was not the body the chassis sends. `run` carried
`{id, case_id, tenant_id, rerun}` and stopped there, while `container.py`
advertises `run.mcp.url` — the one door to chassis capabilities
(`Run_Contract_v1.md:118`) — and every request the chassis makes carries
a W3C `traceparent` whose ABSENCE the contract says "must never be an
error" (`Run_Contract_v1.md:409-411`).

Neither omission was merely an unchecked obligation:

* **`run.mcp.url` made the battery unable to drive a conformant agent.**
  With no
  `run.mcp.url`, the SDK raises `CapabilityError(-32601)` at the first
  `ctx.capabilities.*`, `ctx.pii.redact` or `ctx.config.step`, so every
  agent that uses a capability its manifest grants failed inside the
  battery for a reason the battery created. Measured, and kept here as
  `test_the_body_this_replaced_cannot_drive_an_agent_that_uses_its_grant`.
* **The `traceparent` gap left the likeliest v1 defect invisible.** The
  battery sent
  `traceparent` on every request it ever made, so an agent that parses
  the header and raises on its absence — the mistake a framework makes
  by default — passed clean.

The checks these add are held to the repository's rule that a gate which
reports success by not looking is worse than no gate: `mcp` reports
`skip` when nothing called and never `pass`, and each verdict here is
negative-tested by injecting the defect it exists to catch.
"""
from __future__ import annotations

import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from adapter_kit.run_contract import (
    CapabilityRecorder,
    RelayRecorder,
    run_contract_battery,
)
from app.agents.manifest import load_manifest

ECHO_DIR = Path(__file__).resolve().parents[1] / "agents" / "_examples" / "echo_container"

#: An address with a domain a recognizer really fires on, so a `redact`
#: answer that came from the chassis pipeline is distinguishable from one
#: a stub echoed back.
FIXTURE = "pii.fixture@example.com"


@pytest.fixture
def capabilities():
    recorder = CapabilityRecorder(0, host="127.0.0.1", advertise_host="127.0.0.1")
    yield recorder
    recorder.stop()


@pytest.fixture(scope="module")
def echo():
    """The echo agent on the SDK, in-process, with no exporter configured."""
    from librerun_agent import _otel
    from librerun_agent.testing import serve_in_thread

    _otel._reset_for_tests()
    spec = importlib.util.spec_from_file_location(
        "echo_for_mcp_battery", ECHO_DIR / "echo_agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handle = serve_in_thread(module.app)
    yield handle.url
    handle.stop()
    _otel._reset_for_tests()


def _uses_its_grant() -> dict:
    """A scenario that makes the echo agent use the `pii` grant it declares."""
    return {"message": "battery", "redact": f"write to {FIXTURE}"}


# ---------------------------------------------------------------------------
# run.mcp.url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_that_uses_its_grant_reaches_the_advertised_url(echo, capabilities):
    manifest = load_manifest(ECHO_DIR)
    result = await run_contract_battery(
        echo, manifest=manifest, scenario=_uses_its_grant(),
        capabilities=capabilities, expect_mcp=True)
    assert result.passed, result.summary()
    assert result.checks["mcp"] == "pass"
    # THE CALL, not just a verdict about it.
    assert [c["tool"] for c in result.mcp_calls] == ["redact"] * len(result.mcp_calls)
    assert result.mcp_calls, "expect_mcp passed with no calls recorded"
    assert all(c["why"] is None for c in result.mcp_calls)
    # And the answer came from the chassis's own pipeline rather than a
    # stub echoing the argument: the address is gone and the placeholder
    # intake would have written is in its place.
    assert FIXTURE not in result.output["redacted"]
    assert "[REDACTED_EMAIL_ADDRESS_1]" in result.output["redacted"]
    # The examined count beside the verdict, so "no findings" and "looked
    # at nothing" cannot read the same.
    assert any("request(s) to run.mcp.url" in n for n in result.notes), result.notes


@pytest.mark.asyncio
async def test_the_body_this_replaced_cannot_drive_an_agent_that_uses_its_grant(echo):
    """THE DEFECT, injected by restoring the pre-fix body.

    Passing no recorder sends exactly the `run` object the battery sent
    before this change — no `mcp` key at all — and the agent dies on its
    own granted capability. That is the measurement that makes this a
    defect rather than a missing check.
    """
    manifest = load_manifest(ECHO_DIR)
    result = await run_contract_battery(
        echo, manifest=manifest, scenario=_uses_its_grant())
    assert not result.passed
    assert result.checks["completed"] == "fail"
    assert any("advertised no run.mcp.url" in f for f in result.failures), result.failures
    # …and the check says `skip`, never `pass`: nothing was served, so
    # nothing was observed, and an unobserved obligation is not a met one.
    assert result.checks["mcp"] == "skip"


@pytest.mark.asyncio
async def test_no_recorder_is_skip_and_expecting_one_anyway_fails(echo):
    manifest = load_manifest(ECHO_DIR)
    quiet = await run_contract_battery(
        echo, manifest=manifest, scenario={"message": "battery"})
    assert quiet.checks["mcp"] == "skip"

    # `incomplete`, not `skip` and not `fail`: the caller demanded the
    # check and nothing was serving, which is neither "nothing to look
    # at" nor a defect in the agent.
    demanded = await run_contract_battery(
        echo, manifest=manifest, scenario={"message": "battery"}, expect_mcp=True)
    assert demanded.checks["mcp"] == "incomplete"
    assert not demanded.passed
    assert any("no capability recorder" in f for f in demanded.failures)


@pytest.mark.asyncio
async def test_an_agent_that_never_calls_is_skip_until_the_caller_expects_one(
        echo, capabilities):
    """Silence is conformant — the contract says an agent "may ignore the
    key entirely" — so the CALLER decides whether it is acceptable."""
    manifest = load_manifest(ECHO_DIR)
    silent = await run_contract_battery(
        echo, manifest=manifest, scenario={"message": "battery"},
        capabilities=capabilities)
    assert silent.checks["mcp"] == "skip"
    assert silent.passed, silent.summary()
    assert not silent.mcp_calls


@pytest.mark.asyncio
async def test_expecting_a_call_from_a_silent_agent_fails(echo, capabilities):
    manifest = load_manifest(ECHO_DIR)
    result = await run_contract_battery(
        echo, manifest=manifest, scenario={"message": "battery"},
        capabilities=capabilities, expect_mcp=True)
    assert result.checks["mcp"] == "fail"
    assert any("made no MCP request" in f for f in result.failures), result.failures


def _mcp_call(url: str, token: str | None, tool: str, arguments: dict) -> tuple[int, dict]:
    """One JSON-RPC `tools/call` against the recorder, as an agent makes it."""
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}}).encode()
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_a_bearer_the_battery_never_minted_is_refused_and_recorded(capabilities):
    """THE FINDING the check exists for, injected directly.

    An agent that invents a bearer — or replays one from another run —
    must not read this run's capabilities, and the refusal must be
    RECORDED rather than dropped: a dropped one reads as silence, and
    silence is `skip`.
    """
    capabilities.expect(tokens={"minted"}, granted=frozenset({"pii"}), steps=[])

    good, _ = _mcp_call(capabilities.url, "minted", "redact", {"text": FIXTURE})
    assert good == 200
    invented, body = _mcp_call(capabilities.url, "invented", "redact", {"text": FIXTURE})
    assert invented == 401
    assert body["error"]["code"] == -32001
    missing, _ = _mcp_call(capabilities.url, None, "redact", {"text": FIXTURE})
    assert missing == 401

    whys = [c["why"] for c in capabilities.calls]
    assert whys == [None,
                    "it carried a bearer this battery never minted",
                    "it carried no bearer token"], capabilities.calls


@pytest.mark.asyncio
async def test_a_foreign_bearer_turns_the_check_red(echo, capabilities):
    """…and the verdict follows the recording, not the other way round."""
    manifest = load_manifest(ECHO_DIR)
    # One conformant run first, so the failure cannot be blamed on there
    # being no traffic at all.
    result = await run_contract_battery(
        echo, manifest=manifest, scenario=_uses_its_grant(),
        capabilities=capabilities)
    assert result.checks["mcp"] == "pass"

    _mcp_call(capabilities.url, "not-a-minted-token", "redact", {"text": FIXTURE})
    after = await run_contract_battery(
        echo, manifest=manifest, scenario=_uses_its_grant(),
        capabilities=capabilities)
    assert after.checks["mcp"] == "fail"
    assert any("were not conformant" in f for f in after.failures), after.failures


def test_the_advertised_tool_list_is_the_chassis_table_under_the_manifest(capabilities):
    """Derived from the chassis, never typed out here.

    `tools/list` is `routers/mcp._TOOLS` filtered by the same
    `_TOOL_GRANTS` the chassis filters by. Asserting the expected names
    by hand would be a second copy of the grant table — and a second copy
    is how one of them quietly stops matching.
    """
    import urllib.request

    from app.routers import mcp as chassis

    for grants in (frozenset({"pii"}), frozenset({"pii", "run_store"}), frozenset()):
        capabilities.expect(tokens={"t"}, granted=grants, steps=[])
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        request = urllib.request.Request(
            capabilities.url, data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer t"})
        with urllib.request.urlopen(request, timeout=10) as response:
            listed = json.loads(response.read())["result"]["tools"]
        assert [t["name"] for t in listed] == [
            t["name"] for t in chassis._TOOLS
            if chassis.tool_granted(t["name"], grants)
        ]


def test_a_tool_outside_the_manifest_is_refused_but_is_not_a_finding(capabilities):
    """`-32002` is an answer the agent is entitled to receive.

    An agent asking for a tool its manifest does not grant learns so here
    exactly as it would in production. Recording that as a defect would
    make the battery fail agents for handling a refusal correctly.
    """
    capabilities.expect(tokens={"t"}, granted=frozenset({"pii"}), steps=[])
    status, body = _mcp_call(capabilities.url, "t", "audit_log",
                             {"action_type": "x", "detail": {}})
    assert status == 200
    assert body["error"]["code"] == -32002
    assert [c["why"] for c in capabilities.calls] == [None]


def test_the_recorder_answers_every_tool_the_chassis_serves(capabilities):
    """DERIVED FROM THE CHASSIS, so it cannot fall behind it quietly.

    `tools/list` is the chassis's own table, so a tool added there is
    advertised by this recorder the same day — and an agent that then
    calls it would be told its arguments were bad. The names are read
    from `_TOOL_GRANTS` rather than typed here, because a list typed
    here would be the second copy this whole design exists to avoid.
    """
    from app.routers import mcp as chassis

    # Enough of an argument for each tool to get past its own parsing;
    # the assertion is that the recorder ANSWERS, not what it answers.
    arguments = {
        "kb_search": {"queries": ["x"]},
        "run_store_get": {"key": "k"},
        "run_store_set": {"key": "k", "value": 1},
        "audit_log": {"action_type": "echo_event", "detail": {}},
        "redact": {"text": "nothing to see"},
        "config_get": {},
        "secret_get": {"name": "tavily_api_key"},
    }
    unlearned = sorted(set(chassis._TOOL_GRANTS) - set(arguments))
    assert not unlearned, (
        f"the chassis serves {unlearned}, which this test does not know how "
        f"to call — add it here and to the recorder's `_answer`")

    capabilities.expect(tokens={"t"},
                        granted=frozenset(chassis._TOOL_GRANTS.values()),
                        steps=[], secrets=["tavily_api_key"])
    for tool in sorted(chassis._TOOL_GRANTS):
        status, body = _mcp_call(capabilities.url, "t", tool, arguments[tool])
        assert status == 200, (tool, status, body)
        if tool == "secret_get":
            # K8a: the battery holds no secret, so a declared name is
            # `-32006 secret_not_set` — a legal answer the agent must
            # handle, recorded below as conformant, never a finding.
            assert body["error"]["code"] == -32006, body
            continue
        assert "error" not in body, (tool, body)
        json.loads(body["result"]["content"][0]["text"])
    # …and the examined count beside the verdict, every call clean.
    assert len(capabilities.calls) == len(chassis._TOOL_GRANTS)
    assert [c["why"] for c in capabilities.calls] == [None] * len(chassis._TOOL_GRANTS)
    assert not capabilities.refusals, capabilities.refusals


def test_config_get_answers_the_manifests_steps(capabilities):
    """The shape `ctx.config.steps()` reads, built by the chassis's own
    `effective_steps` with no overrides — there is no tenant here to
    override anything, and the payload says so."""
    from app.services import agent_step_config_service as step_configs

    manifest = load_manifest(ECHO_DIR)
    expected = step_configs.effective_steps(manifest, {})
    capabilities.expect(tokens={"t"}, granted=frozenset({"llm"}), steps=expected)
    status, body = _mcp_call(capabilities.url, "t", "config_get", {})
    assert status == 200
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["steps"] == expected
    assert [s["overridden"] for s in payload["steps"]] == [[]] * len(expected)


def test_secret_get_answers_as_the_chassis_does_with_no_secret(capabilities):
    """K8a, the battery's half of `secret_get`: the recorder learns the
    manifest's declared names through `expect()` and, holding no secret,
    answers a declared one `-32006 secret_not_set` and any other
    `-32005 secret_not_declared` — both recorded as conformant, as `-32002`
    is: an agent that handles the refusal is doing exactly what it should."""
    capabilities.expect(tokens={"t"}, granted=frozenset(), steps=[],
                        secrets=["search_key"])
    status, body = _mcp_call(capabilities.url, "t", "secret_get", {"name": "search_key"})
    assert status == 200 and body["error"]["code"] == -32006, body
    status, body = _mcp_call(capabilities.url, "t", "secret_get", {"name": "other"})
    assert status == 200 and body["error"]["code"] == -32005, body
    assert [c["why"] for c in capabilities.calls] == [None, None]


def test_config_get_needs_no_grant_in_the_battery_either(capabilities):
    """K5-04, the battery's half.

    The chassis served `config_get` without the `llm` grant it listed it
    under, and this recorder refused it without one, so an agent that
    read its own configuration passed in production and was refused
    here. Both sides now ask the chassis's one predicate, `tool_granted`,
    and a run that grants nothing is offered and answered the tool — its
    steps and its settings, in the chassis's shape.
    """
    import urllib.request

    capabilities.expect(tokens={"t"}, granted=frozenset(), steps=[],
                        settings={"note": "from the manifest", "limit": 3})
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    request = urllib.request.Request(
        capabilities.url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer t"})
    with urllib.request.urlopen(request, timeout=10) as response:
        listed = json.loads(response.read())["result"]["tools"]
    # The two tools a run that grants nothing is offered: its configuration
    # and, since K8a, its tool secrets.
    assert [t["name"] for t in listed] == ["config_get", "secret_get"]

    status, body = _mcp_call(capabilities.url, "t", "config_get", {})
    assert status == 200 and "error" not in body, body
    assert json.loads(body["result"]["content"][0]["text"]) == {
        "steps": [],
        "settings": [{"key": "note", "value": "from the manifest"},
                     {"key": "limit", "value": 3}],
    }
    # Both requests clean: neither is a finding against the agent.
    assert [c["why"] for c in capabilities.calls] == [None, None]
    # And a granted tool is still refused without its grant.
    status, body = _mcp_call(capabilities.url, "t", "redact", {"text": "x"})
    assert body["error"]["code"] == -32002


@pytest.mark.asyncio
async def test_the_echo_example_reads_its_setting_over_mcp(echo, capabilities):
    """K5-10: the reference container declares `note` in its manifest's
    `settings[]` and, asked to (`show_settings`), reads it with
    `ctx.config.settings()`. Under the battery there is no tenant, so the
    value is the manifest's default — the one `run_contract_battery`
    hands the recorder from `effective_values(manifest, {})` — and the
    call is over MCP, so `--expect-mcp` has a call to find."""
    manifest = load_manifest(ECHO_DIR)
    default = manifest.setting("note").default
    result = await run_contract_battery(
        echo, manifest=manifest,
        scenario={"message": "battery", "show_settings": True},
        capabilities=capabilities, expect_mcp=True)
    assert result.passed, result.summary()
    assert result.checks["mcp"] == "pass"
    assert result.mcp_calls, "expect_mcp passed with no calls recorded"
    assert {c["tool"] for c in result.mcp_calls} == {"config_get"}
    assert all(c["why"] is None for c in result.mcp_calls)
    assert result.output["settings"] == {"note": default}
    assert result.output["note"] == default


@pytest.mark.asyncio
async def test_the_echo_example_reads_not_set_over_mcp(echo, capabilities):
    """K8b: the reference container declares `echo_token` in `secrets[]`
    and, asked to (`fetch_secret`), reads it with `ctx.secrets.get`. The
    battery has no tenant and holds no secret, so the recorder answers the
    declared name `-32006 secret_not_set` — the chassis's own answer to a
    tenant that set nothing, and conformant — and the agent reads it as
    "not set": the run completes with `secret_set: false`, and the call is
    over MCP, so `--expect-mcp` has one to find."""
    manifest = load_manifest(ECHO_DIR)
    assert manifest.secrets == ["echo_token"]
    result = await run_contract_battery(
        echo, manifest=manifest,
        scenario={"message": "battery", "fetch_secret": True},
        capabilities=capabilities, expect_mcp=True)
    assert result.passed, result.summary()
    assert result.checks["mcp"] == "pass"
    assert {c["tool"] for c in result.mcp_calls} == {"secret_get"}
    assert all(c["why"] is None for c in result.mcp_calls)
    assert result.output["secret_set"] is False


class _AnswersWithACanary(BaseHTTPRequestHandler):
    """A stand-in for the chassis's MCP endpoint in a tenant that HAS set
    the secret: `secret_get` answers `{value}` — the canary — exactly as
    the chassis answers the declaring run. Anything else is refused."""

    canary = ""
    calls: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        message = json.loads(self.rfile.read(length) or b"{}")
        params = message.get("params") or {}
        type(self).calls.append(
            (params.get("name"), params.get("arguments"), self.headers.get("Authorization"))
        )
        if params.get("name") == "secret_get":
            text = json.dumps({"value": type(self).canary})
            body = {"jsonrpc": "2.0", "id": message.get("id"),
                    "result": {"content": [{"type": "text", "text": text}]}}
        else:
            body = {"jsonrpc": "2.0", "id": message.get("id"),
                    "error": {"code": -32602, "message": "not served here"}}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.mark.asyncio
async def test_the_echo_example_never_returns_the_value(echo):
    """K8b, L31: a value `secret_get` hands the run goes to the tool that
    needs it and nowhere else — the output is persisted. With a value set,
    the echo agent says `secret_set: true`, and neither its output nor
    anything on its event stream carries the value."""
    import secrets as _secrets

    import httpx

    # Made at run time, so no literal in the tree looks like a secret.
    _AnswersWithACanary.canary = "echo-canary-" + _secrets.token_hex(12)
    _AnswersWithACanary.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AnswersWithACanary)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    mcp_url = f"http://127.0.0.1:{server.server_address[1]}/"
    token = "echo-secret-bearer"
    body = {
        "contract": "v1",
        "agent_id": "echo-v1",
        "phase": "echo",
        "deadline_seconds": 60,
        "run": {"id": "run-canary", "tenant_id": "t-1", "rerun": False,
                "mcp": {"url": mcp_url}},
        "input": {"message": "canary", "fetch_secret": True},
        "prior_output": None,
        "user_edits": None,
    }
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            started = await client.post(f"{echo}/v1/runs", json=body, headers=headers)
            assert started.status_code in (200, 201), started.text
            invocation = started.json()["invocation_id"]
            async with client.stream(
                "GET", f"{echo}/v1/runs/{invocation}/events",
                headers={**headers, "Accept": "text/event-stream"},
            ) as stream:
                events = "".join([chunk async for chunk in stream.aiter_text()])
            output = (await client.get(
                f"{echo}/v1/runs/{invocation}/output", headers=headers)).json()
    finally:
        server.shutdown()

    assert _AnswersWithACanary.calls == [
        ("secret_get", {"name": "echo_token"}, f"Bearer {token}")
    ]
    assert output["output"]["secret_set"] is True, output
    assert _AnswersWithACanary.canary not in json.dumps(output)
    assert _AnswersWithACanary.canary not in events


# ---------------------------------------------------------------------------
# the traceparent that may be absent
# ---------------------------------------------------------------------------


class _RecordsItsHeaders(BaseHTTPRequestHandler):
    """A conformant agent that keeps every request's headers.

    `seen` is class-level and reset by the fixture: the premise this
    check rests on — that ONE invocation really arrives with no trace
    headers — is asserted from what the agent received, not from what the
    battery believes it sent.
    """

    seen: list[dict] = []
    refuse_without_traceparent = False

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"status": "ok"})
        if self.path.endswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'event: progress\ndata: {"status": "running", "step": "echo"}\n\n'
                b'event: completed\ndata: {"output": {"ok": true}}\n\n')
            return
        self._json(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        traceparent = self.headers.get("traceparent")
        type(self).seen.append({
            "traceparent": traceparent,
            "tracestate": self.headers.get("tracestate"),
        })
        if self.refuse_without_traceparent and not traceparent:
            # THE INJECTION: the mistake a framework makes by default —
            # parse the header, raise on None. Conformant in every other
            # respect, which is why nothing else catches it.
            return self._json(400, {"error": "traceparent is required"})
        self._json(201, {"invocation_id": f"inv-{len(type(self).seen)}"})


class _Agent:
    def __init__(self, handler):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def header_agent():
    _RecordsItsHeaders.seen = []
    _RecordsItsHeaders.refuse_without_traceparent = False
    agent = _Agent(_RecordsItsHeaders)
    yield agent
    agent.stop()


@pytest.mark.asyncio
async def test_exactly_one_invocation_arrives_with_no_trace_headers(header_agent):
    """The premise, asked of the AGENT rather than assumed.

    A probe that believed it had stripped the headers while the client
    still sent them would pass forever and prove nothing.
    """
    manifest = load_manifest(ECHO_DIR)
    result = await run_contract_battery(
        header_agent.url, manifest=manifest, scenario={"message": "x"})
    assert result.checks["traceparent_optional"] == "pass", result.summary()
    headerless = [s for s in _RecordsItsHeaders.seen if s["traceparent"] is None]
    assert len(headerless) == 1, _RecordsItsHeaders.seen
    # Both headers, not only the one the check is named for.
    assert headerless[0]["tracestate"] is None
    # …and every other invocation still carried one, so the battery did
    # not simply stop sending trace context.
    assert len(_RecordsItsHeaders.seen) > 1
    assert all(s["traceparent"] for s in _RecordsItsHeaders.seen
               if s is not headerless[0])


@pytest.mark.asyncio
async def test_an_agent_that_requires_a_traceparent_fails(header_agent):
    """THE DEFECT, injected: 400 when the header is absent.

    The contract's words are "absence of either header must never be an
    error". Before this check, this agent passed the battery clean.
    """
    _RecordsItsHeaders.refuse_without_traceparent = True
    manifest = load_manifest(ECHO_DIR)
    result = await run_contract_battery(
        header_agent.url, manifest=manifest, scenario={"message": "x"})
    assert result.checks["traceparent_optional"] == "fail"
    assert not result.passed
    assert any("no traceparent" in f and "400" in f for f in result.failures), \
        result.failures


class _ExportsSpans(_RecordsItsHeaders):
    """A conformant agent that also exports ONE span per invocation.

    `trace_source` picks which trace id it puts on that span:

      * ``"received"`` — the id from the `traceparent` it was handed, and
        an id of its own when it was handed none. That is what a correct
        agent does, and `_ANY_TRACE` exists so the second half of it is
        not read as a defect.
      * ``"wrong"`` — an id nobody sent, on every invocation. The case
        the span check exists to catch, re-injected under an ORDINARY
        token to prove the sentinel excuses one invocation and not the
        comparison itself.
    """

    relay_port: int = 0
    trace_source: str = "received"

    def do_POST(self):
        import secrets
        import urllib.request

        from tests.test_run_contract_battery import _otlp_span

        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        traceparent = self.headers.get("traceparent")
        if self.trace_source == "wrong":
            trace_id = "f" * 32
        elif traceparent:
            trace_id = traceparent.split("-")[1]
        else:
            # No parent was sent, so the agent starts its own trace —
            # exactly what the contract permits.
            trace_id = secrets.token_hex(16)
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.relay_port}/v1/traces",
            data=_otlp_span(trace_id, "invocation"),
            headers={"Content-Type": "application/x-protobuf",
                     "Authorization": f"Bearer {token}"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10):
                pass
        except Exception:  # noqa: BLE001 — the assertion is on the verdict
            pass
        super().do_POST()


@pytest.fixture
def exporting_agent():
    relay = RelayRecorder(0, host="127.0.0.1")
    _ExportsSpans.seen = []
    _ExportsSpans.refuse_without_traceparent = False
    _ExportsSpans.relay_port = relay.port
    _ExportsSpans.trace_source = "received"
    agent = _Agent(_ExportsSpans)
    yield agent, relay
    agent.stop()
    relay.stop()


@pytest.mark.asyncio
async def test_an_agent_that_starts_its_own_trace_when_none_was_sent_is_not_accused(
        exporting_agent):
    """The `_ANY_TRACE` half: the headerless invocation's span is correct
    under whatever trace the agent chose, and the check must say so."""
    agent, relay = exporting_agent
    manifest = load_manifest(ECHO_DIR)
    result = await run_contract_battery(
        agent.url, manifest=manifest, scenario={"message": "x"},
        relay=relay, expect_spans=True, settle_seconds=1.0)
    assert result.checks["traceparent_optional"] == "pass", result.summary()
    assert result.checks["spans"] == "pass", result.summary()
    # The premise: one invocation really did arrive headerless AND really
    # did export a span on a trace the battery never sent.
    sent = set(result.traces.values()) - {"*"}
    assert any(s["trace_id"] not in sent for s in result.spans), result.spans


@pytest.mark.asyncio
async def test_a_wrong_trace_on_an_ordinary_token_is_still_caught(exporting_agent):
    """THE INJECTION that proves the sentinel did not blunt the check.

    Same agent, same relay, one difference: every span now carries an id
    nobody sent — including the invocation under test, whose token is
    held to the id it WAS sent.
    """
    agent, relay = exporting_agent
    _ExportsSpans.trace_source = "wrong"
    manifest = load_manifest(ECHO_DIR)
    result = await run_contract_battery(
        agent.url, manifest=manifest, scenario={"message": "x"},
        relay=relay, expect_spans=True, settle_seconds=1.0)
    assert result.checks["spans"] == "fail", result.summary()
    assert any("another trace id" in f for f in result.failures), result.failures
