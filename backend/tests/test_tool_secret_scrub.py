"""The run's tool secrets are scrubbed from what it persists (K8a, D20).

A value ``caps.secrets.get`` delivers is the one exception L31 makes: it
goes to the declaring run, and from there nowhere the chassis writes. The
runner holds ``run_boundary.scrubbing`` through its phases and their error
path — every declared value, re-resolved before each output walk, and each
value the façade hands over — so the output, the report, the progress, the
error text, the audit and run-store writes, and a container's ``log`` and
``failed`` text carry ``[REDACTED_SECRET]`` in its place. An identifier or
an object key holding one is refused with ``secret_in_output``, never a
PII reason.

On the runner's harness (``test_agent_runner``): the store's ``resolve`` is
stubbed to one value, so these hold the scrub, not the store.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import traceback
import uuid

import pytest

from app.agents import registry
from app.agents.manifest import AgentManifest
from app.agents.protocol import AgentProtocol, AnalysisResult, InvestigationResult, StepProgress
from app.services import agent_runner, run_boundary, run_errors, secrets_service
from app.services import tool_secrets_service as tool_secrets
from app.services.pii_service import PiiRefused
from tests.test_agent_runner import (  # noqa: E402
    _FakeRun,
    _FakeSession,
    patch_runner,  # noqa: F401  (fixture)
)
from tests.test_container_runner import _write_container_dir
from tests.test_one_trace_per_run import exporter  # noqa: F401  (fixture)

VALUE = "tvly-a-delivered-value-5b8e41"
AGENT = "scrub-probe"


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


@pytest.fixture
def the_store_holds_one_value(monkeypatch):
    """``resolve`` answers every declared name with ``VALUE`` from this
    tenant's row; ``stamp`` records nothing."""
    calls: list[tuple] = []

    async def _resolve(tenant_id, agent_id, name, *, env_fallback):
        calls.append((name, env_fallback))
        return tool_secrets.Resolved(
            value=VALUE,
            source="tenant",
            row=secrets_service.Owner("tenant", tenant_id, agent_id),
        )

    async def _stamp(row, name):
        return None

    monkeypatch.setattr(tool_secrets, "resolve", _resolve)
    monkeypatch.setattr(tool_secrets, "stamp", _stamp)
    return calls


def _manifest(runtime: str = "python-package") -> AgentManifest:
    return AgentManifest.model_validate(
        {
            "id": AGENT,
            "name": "scrub probe",
            "runtime": runtime,
            "phases": [{"name": "analyze"}],
            "output": {"mode": "structured"},
            "secrets": ["search_key"],
        }
    )


class _Echo(AgentProtocol):
    """Asks for its secret and puts it everywhere the chassis persists."""

    agent_id = AGENT
    display_name = "scrub probe"
    description = "d"

    def __init__(self, *, in_a_task: bool = False, fail: bool = False, as_key: bool = False):
        self._in_a_task = in_a_task
        self._fail = fail
        self._as_key = as_key

    async def analyze(self, inp, on_progress):
        get = inp.capabilities.secrets.get("search_key")
        value = await (asyncio.create_task(get) if self._in_a_task else get)
        await on_progress(StepProgress(step_id="search", status="running", detail=f"using {value}"))
        if self._fail:
            raise RuntimeError(f"the provider refused key {value}")
        structured = {value: 1} if self._as_key else {"echo": f"key={value}", "all": [value]}
        return InvestigationResult(
            status="complete",
            structured=structured,
            report_html=f"<p>searched with {value}</p>",
        )


async def _run(patch_runner, agent: _Echo):  # noqa: F811
    registry.register(agent, _manifest())
    run = _FakeRun(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    run.agent_id = AGENT
    session = _FakeSession(run)
    patch_runner(session)
    await agent_runner.start_run(run.id, run.tenant_id, AGENT)
    return run, session


def _progress(runner_redis) -> str:
    return json.dumps(runner_redis.hashes)


@pytest.mark.asyncio
async def test_a_delivered_value_is_scrubbed(patch_runner, the_store_holds_one_value):  # noqa: F811
    run, session = await _run(patch_runner, _Echo())

    assert run.status == "complete", run.error_detail
    stored = json.dumps(session.snapshot.structured_data) + session.snapshot.report_html
    assert VALUE not in stored
    assert session.snapshot.structured_data == {
        "echo": "key=[REDACTED_SECRET]",
        "all": ["[REDACTED_SECRET]"],
    }
    assert "[REDACTED_SECRET]" in session.snapshot.report_html
    redis = await agent_runner.get_redis()
    assert VALUE not in _progress(redis) and "[REDACTED_SECRET]" in _progress(redis)
    # An in-process agent's reads may fall back to the environment.
    assert ("search_key", True) in the_store_holds_one_value


@pytest.mark.asyncio
async def test_a_value_delivered_in_a_task_the_agent_spawned_is_scrubbed(
    patch_runner, the_store_holds_one_value, monkeypatch  # noqa: F811
):
    """The runner's own resolution finds nothing here, so only the value
    the façade adds as it hands it over can be scrubbed — from inside a
    task the agent created, whose context is a copy of the runner's."""

    async def _nothing(*args, **kwargs):
        return []

    monkeypatch.setattr(tool_secrets, "scrub_values", _nothing)
    run, session = await _run(patch_runner, _Echo(in_a_task=True))

    assert run.status == "complete", run.error_detail
    assert VALUE not in json.dumps(session.snapshot.structured_data)
    assert VALUE not in session.snapshot.report_html


@pytest.mark.asyncio
async def test_the_error_text_is_scrubbed(patch_runner, the_store_holds_one_value):  # noqa: F811
    run, _ = await _run(patch_runner, _Echo(fail=True))

    assert run.status == "error"
    assert VALUE not in (run.error_detail or "")
    assert "[REDACTED_SECRET]" in run.error_detail


@pytest.mark.asyncio
async def test_a_key_holding_a_value_is_refused_by_its_own_reason(
    patch_runner, the_store_holds_one_value, caplog  # noqa: F811
):
    with caplog.at_level(logging.WARNING):
        run, session = await _run(patch_runner, _Echo(as_key=True))

    assert run.status == "error"
    assert run.error_code == run_errors.OUTPUT_REFUSED
    assert "secret_in_output" in run.error_detail and "pii_in_output" not in run.error_detail
    assert session.snapshot is None or session.snapshot.structured_data is None
    assert VALUE not in caplog.text and VALUE not in run.error_detail


# -- the boundary's own rules ------------------------------------------------------


def test_names_and_keys_holding_a_value_are_refused_and_content_is_replaced():
    with run_boundary.scrubbing([VALUE]):
        with pytest.raises(PiiRefused) as raised:
            run_boundary.check_name(
                f"k-{VALUE}", argument="key", reason=run_boundary.REASON_STORE, path="run_store.key"
            )
        assert raised.value.reason == run_boundary.REASON_SECRET
        assert VALUE not in str(raised.value)

        with pytest.raises(PiiRefused) as raised:
            run_boundary.walk_value(
                {"ok": 1, VALUE: 2}, argument="detail", reason=run_boundary.REASON_AUDIT
            )
        assert raised.value.reason == run_boundary.REASON_SECRET
        assert raised.value.finding.path == "$.<key 1>"

        walked = run_boundary.walk_value(
            {"note": f"sent {VALUE} twice: {VALUE}", "deep": [[{"x": VALUE}]]},
            argument="value",
            reason=run_boundary.REASON_STORE,
        )
        assert VALUE not in json.dumps(walked)
        assert walked["note"] == "sent [REDACTED_SECRET] twice: [REDACTED_SECRET]"


def test_the_longest_value_is_replaced_first():
    short, long = "abcd-1234", "abcd-1234-and-more"
    with run_boundary.scrubbing([short, long]):
        assert run_boundary.redact_text(f"x {long} y {short}") == (
            "x [REDACTED_SECRET] y [REDACTED_SECRET]"
        )


def test_a_value_cut_by_a_length_cap_is_replaced_whole():
    """``safe_detail`` and the container adapter cap text before it is
    walked: the tail a cap leaves of a value is the start of it."""
    text = ("x" * (run_errors.MAX_DETAIL_CHARS - 10)) + f" key {VALUE}"
    with run_boundary.scrubbing([VALUE]):
        detail = run_errors.safe_detail(text)
    assert detail.endswith("[REDACTED_SECRET]")
    assert VALUE[:5] not in detail[-40:]


def test_outside_a_run_nothing_is_replaced():
    """The set is the run's and ends with it: nothing leaks into the next."""
    with run_boundary.scrubbing([VALUE]):
        pass
    assert run_boundary.redact_text(f"key {VALUE}") == f"key {VALUE}"
    run_boundary.add_scrub(VALUE)  # a no-op outside a block
    assert run_boundary.redact_text(f"key {VALUE}") == f"key {VALUE}"


@pytest.mark.asyncio
async def test_a_containers_log_and_failed_text_are_scrubbed(tmp_path, caplog):
    from app.agents.container import ContainerAgent, ContainerAgentError
    from app.agents.manifest import load_manifest

    d = _write_container_dir(tmp_path)
    agent = ContainerAgent(load_manifest(d), "http://127.0.0.1:9", d)

    async def _collect(progress):
        return None

    with run_boundary.scrubbing([VALUE]):
        with caplog.at_level(logging.INFO):
            await agent._handle_event("log", {"level": "info", "message": f"got {VALUE}"}, _collect)
        assert "container_agent_log" in caplog.text and VALUE not in caplog.text
        # Cut at the adapter's own cap, mid-value.
        long_error = ("e" * 1990) + VALUE
        with pytest.raises(ContainerAgentError) as raised:
            await agent._handle_event("failed", {"error": long_error}, _collect)
    assert VALUE[:5] not in str(raised.value)[-40:]
    assert str(raised.value).endswith("[REDACTED_SECRET]")


# -- what a run was delivered, across requests (Codex on #173) --------------------

ROTATED_AWAY = "tvly-the-value-a-container-read-before-9c2d"


def test_blocks_of_one_run_share_what_it_was_delivered():
    """An MCP request is a block of its own. What one block of a run was
    delivered is scrubbed by the next — and by no other run's — until the
    run is forgotten."""
    run_id, other = uuid.uuid4(), uuid.uuid4()
    try:
        with run_boundary.scrubbing((), run_id=run_id):
            run_boundary.add_scrub(ROTATED_AWAY)
        with run_boundary.scrubbing((), run_id=run_id):
            assert run_boundary.scrub_secrets(f"k={ROTATED_AWAY}") == "k=[REDACTED_SECRET]"
        with run_boundary.scrubbing((), run_id=other):
            assert ROTATED_AWAY in run_boundary.scrub_secrets(f"k={ROTATED_AWAY}")
        run_boundary.forget_run(run_id)
        with run_boundary.scrubbing((), run_id=run_id):
            assert ROTATED_AWAY in run_boundary.scrub_secrets(f"k={ROTATED_AWAY}")
    finally:
        run_boundary.forget_run(run_id)
        run_boundary.forget_run(other)


class _ReadOverMcp(AgentProtocol):
    """A container's shape, in-process: the value arrives in a request of
    its own — a fresh context, as an HTTP request has — and the rows no
    longer hold it when the output is walked (the store answers VALUE)."""

    agent_id = AGENT
    display_name = "scrub probe"
    description = "d"

    async def analyze(self, inp, on_progress):
        async def _secret_get_request():
            with run_boundary.scrubbing((), run_id=inp.run_id):
                run_boundary.add_scrub(ROTATED_AWAY)

        await asyncio.create_task(_secret_get_request(), context=contextvars.Context())
        return InvestigationResult(
            status="complete",
            structured={"echo": f"key={ROTATED_AWAY}"},
            report_html=f"<p>searched with {ROTATED_AWAY}</p>",
        )


@pytest.mark.asyncio
async def test_a_value_read_over_mcp_is_scrubbed_from_the_output(
    patch_runner, the_store_holds_one_value  # noqa: F811
):
    """The runner's set is the run's: a value its container was handed in
    an MCP request is scrubbed from the output even once the row moved on,
    and the set is dropped when the run ends."""
    run, session = await _run(patch_runner, _ReadOverMcp())

    assert run.status == "complete", run.error_detail
    persisted = json.dumps(session.snapshot.structured_data) + (session.snapshot.report_html or "")
    assert ROTATED_AWAY not in persisted and "[REDACTED_SECRET]" in persisted
    assert str(run.id) not in run_boundary._DELIVERED


class _ParksAfterReading(AgentProtocol):
    agent_id = AGENT
    display_name = "scrub probe"
    description = "d"

    async def analyze(self, inp, on_progress):
        await inp.capabilities.secrets.get("search_key")
        return AnalysisResult(display={}, structured={"gathered": True}, status="awaiting_approval")

    async def investigate(self, inp, on_progress):
        return InvestigationResult(status="complete", structured={"done": True})


@pytest.mark.asyncio
async def test_a_parked_run_keeps_what_it_was_delivered_and_an_ended_one_drops_it(
    patch_runner, the_store_holds_one_value  # noqa: F811
):
    manifest = AgentManifest.model_validate(
        {
            "id": AGENT,
            "name": "scrub probe",
            "runtime": "python-package",
            "phases": [{"name": "analyze"}, {"name": "investigate", "approval": True}],
            "output": {"mode": "structured"},
            "secrets": ["search_key"],
        }
    )
    registry.register(_ParksAfterReading(), manifest)
    run = _FakeRun(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    run.agent_id = AGENT
    patch_runner(_FakeSession(run))
    try:
        await agent_runner.start_run(run.id, run.tenant_id, AGENT)
        assert run.status == "awaiting_approval", run.error_detail
        assert VALUE in run_boundary._DELIVERED[str(run.id)][0].values

        await agent_runner.resume_run(run.id, run.tenant_id, AGENT)
        assert run.status == "complete", run.error_detail
        assert str(run.id) not in run_boundary._DELIVERED
    finally:
        run_boundary.forget_run(run.id)


# -- an agent's exception (Codex on #173) ----------------------------------------


def test_the_whole_chain_is_scrubbed_and_the_type_kept():
    """Arguments and notes of the exception, its cause and its context are
    scrubbed in place, so the runner still classifies it by its type."""
    try:
        try:
            raise ValueError(f"inner {VALUE}")
        except ValueError as inner:
            outer = RuntimeError(f"outer {VALUE}")
            outer.add_note(f"note {VALUE}")
            raise outer from inner
    except RuntimeError as exc:
        caught = exc
    with run_boundary.scrubbing((VALUE,)):
        scrubbed = run_boundary.scrub_exception(caught)
    assert scrubbed is caught and type(scrubbed) is RuntimeError
    rendered = "".join(traceback.format_exception(scrubbed))
    assert VALUE not in rendered and rendered.count("[REDACTED_SECRET]") == 3
    # Outside a run, or with nothing to find, it is left alone.
    clean = RuntimeError("nothing here")
    with run_boundary.scrubbing((VALUE,)):
        assert run_boundary.scrub_exception(clean) is clean


class _OwnStr(Exception):
    def __init__(self, key: str):
        super().__init__("the provider refused")
        self.key = key

    def __str__(self) -> str:
        return f"the provider refused key {self.key}"


class _Raises(AgentProtocol):
    agent_id = AGENT
    display_name = "scrub probe"
    description = "d"

    def __init__(self, *, own_str: bool = False):
        self._own_str = own_str

    async def analyze(self, inp, on_progress):
        value = await inp.capabilities.secrets.get("search_key")
        if self._own_str:
            raise _OwnStr(value)
        raise RuntimeError(f"the provider refused key {value}")


def _texts(exporter, caplog) -> tuple[str, str]:
    events = [
        event
        for span in exporter.get_finished_spans()
        for event in span.events
        if event.name == "exception"
    ]
    assert events, "no span recorded the exception, so this proves nothing"
    spans = json.dumps([dict(event.attributes) for event in events])
    records = [r for r in caplog.records if "run_failed" in r.getMessage()]
    assert records, "run_failed was not logged, so this proves nothing"
    logs = caplog.text + "".join(
        "".join(traceback.format_exception(*r.exc_info)) for r in records if r.exc_info
    )
    return spans, logs


@pytest.mark.asyncio
@pytest.mark.parametrize("own_str", [False, True])
async def test_an_agents_exception_reaches_neither_span_nor_log(
    patch_runner, the_store_holds_one_value, exporter, caplog, own_str  # noqa: F811
):
    """The phase span records what leaves it and ``run_failed`` logs it,
    and neither walks it. An exception whose ``__str__`` is its own comes
    out as a ``ScrubbedError`` naming it; the run fails as a phase either way."""
    with caplog.at_level(logging.ERROR):
        run, _ = await _run(patch_runner, _Raises(own_str=own_str))

    spans, logs = _texts(exporter, caplog)
    assert VALUE not in spans and VALUE not in logs
    assert "[REDACTED_SECRET]" in spans
    assert run.status == "error" and run.error_code == run_errors.PHASE_FAILED
    assert VALUE not in (run.error_detail or "")
    if own_str:
        assert "ScrubbedError" in run.error_detail and "_OwnStr" in run.error_detail
