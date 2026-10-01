"""Tests for ``app.services.agent_runner``.

No real DB or Redis — ``async_session`` / ``get_redis`` / registry lookups
are monkeypatched with in-memory stubs. Focus is on the runner's control
flow, which since blueprint B7 is manifest-driven: ``start_run`` walks the
agent's declared phase list, parks at approval gates, auto-advances
ungated transitions; ``resume_run`` continues after approval;
``rerun_current_phase`` repeats the phase whose output was edited.
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from typing import Any

import pytest

from app.agents import registry
from app.agents.manifest import AgentManifest
from app.agents.protocol import (
    AgentInput,
    AgentProtocol,
    AnalysisResult,
    InvestigationResult,
    StepProgress,
)
from app.services import agent_runner, run_errors


# --------------------------- minimal in-memory stubs -------------------------


class _FakePipeline:
    """A transaction that APPLIES what it was queued, and REMEMBERS the
    ttl it was given.

    The remembering is the point. `test_capability_boundaries` already
    had a double whose `expire` was `pass` — against that, a production
    change that drops the expiry can never fail a test, however the code
    moves. A double that discards what it is told measures nothing
    (§12 185(a)), so this one records, and the guard below reads it back.
    """

    def __init__(self, parent: "_FakeRedis") -> None:
        self._parent = parent
        self._queued: list = []

    def hset(self, key: str, field: str, value: str) -> "_FakePipeline":
        self._queued.append(("hset", key, field, value))
        return self

    def expire(self, key: str, ttl: int) -> "_FakePipeline":
        self._queued.append(("expire", key, ttl))
        return self

    async def execute(self) -> list:
        replies: list = []
        for command in self._queued:
            self._parent.commands.append(command)
            if command[0] == "hset":
                _, key, field, value = command
                self._parent.hashes.setdefault(key, {})[field] = value
                replies.append(1)
            elif command[0] == "expire":
                _, key, ttl = command
                self._parent.ttls[key] = ttl
                replies.append(True)
        self._queued = []
        # A LIST, because `hset` + `expire` really replies
        # `[fields_added, True]`. The read-pipeline doubles elsewhere in
        # this suite return one too; copying them without noticing they
        # are read-shaped is how a write-pipeline double ends up
        # asserting nothing.
        return replies


class _FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        # What the key's expiry was last set to, and every command the
        # pipeline actually issued — both read back by the guards.
        self.ttls: dict[str, int] = {}
        self.commands: list = []

    def pipeline(self, transaction: bool = False) -> _FakePipeline:
        assert transaction, "run_hash_write must ask for a transaction"
        return _FakePipeline(self)

    async def hset(self, key: str, field: str, value: str) -> None:
        self.commands.append(("hset", key, field, value))
        self.hashes.setdefault(key, {})[field] = value

    async def expire(self, key: str, ttl: int) -> None:
        self.commands.append(("expire", key, ttl))
        self.ttls[key] = ttl

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.hashes.pop(key, None)

    async def get(self, key: str):
        return None

    async def set(self, *a, **k):
        return None


class _FakeRun:
    """Mirror of the fields the runner touches on the Run ORM object."""

    def __init__(
        self,
        *,
        run_id,
        tenant_id,
        user_id=None,
        run_number="C-001",
        status="submitted",
        vendor_a_name="A",
        vendor_b_name="B",
        user_inputs=None,
        current_phase=None,
    ) -> None:
        self.id = run_id
        self.tenant_id = tenant_id
        self.user_id = user_id or uuid.uuid4()
        self.run_number = run_number
        self.status = status
        self.vendor_a_name = vendor_a_name
        self.vendor_a_product = None
        self.vendor_a_feature = None
        self.vendor_a_observation = None
        self.vendor_b_name = vendor_b_name
        self.vendor_b_product = None
        self.vendor_b_feature = None
        self.vendor_b_observation = None
        self.logs_a = None
        self.logs_b = None
        self.use_case = "u"
        self.problem_statement = "p"
        self.impact_statement = None
        self.severity = None
        self.trace_id = None
        self.root_traceparent = None
        self.root_tracestate = None
        self.phase2_span_id = None
        self.agent_id = "vita-v1"
        self.user_inputs = user_inputs
        self.current_phase = current_phase


class _FakeSnapshot:
    def __init__(self, run_id, tenant_id) -> None:
        self.run_id = run_id
        self.tenant_id = tenant_id
        self.analysis = None
        self.structured_data = None
        self.report_html = None


class _FakeSession:
    """Enough of AsyncSession to satisfy the runner's calls."""

    def __init__(self, run: _FakeRun | None) -> None:
        self._run = run
        self.snapshot: _FakeSnapshot | None = None
        self.commits = 0
        self.flushes = 0
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, key):
        # Handles get(Run, id)
        return self._run

    async def execute(self, stmt):
        # Only used to look up the snapshot — return an object with
        # ``scalar_one_or_none``.
        class _R:
            def __init__(self, value):
                self._value = value

            def scalar_one_or_none(self):
                return self._value

        return _R(self.snapshot)

    def add(self, obj):
        # The runner creates real RunSnapshot instances; treat anything
        # snapshot-shaped as the current snapshot so subsequent attribute
        # writes land on one object.
        if hasattr(obj, "analysis") and hasattr(obj, "structured_data"):
            self.snapshot = obj

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rolled_back = True


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


@pytest.fixture
def patch_runner(monkeypatch):
    """Install fake async_session + redis into agent_runner."""

    state: dict[str, Any] = {"session": None, "redis": _FakeRedis()}

    def _session_factory():
        return state["session"]

    async def _get_redis():
        return state["redis"]

    def install(session: _FakeSession):
        state["session"] = session

    monkeypatch.setattr(agent_runner, "async_session", _session_factory)
    monkeypatch.setattr(agent_runner, "get_redis", _get_redis)
    return install


def _manifest(agent_id: str, phases: list[dict], mode: str = "html_report") -> AgentManifest:
    """Build a registrable manifest from a compact phase spec list."""
    return AgentManifest.model_validate(
        {
            "id": agent_id,
            "name": "stub",
            "runtime": "python-package",
            "phases": phases,
            "output": {"mode": mode},
        }
    )


# --------------------------- stub agents -------------------------------------


class _HappyVita(AgentProtocol):
    """Two-phase agent registered WITHOUT a manifest — exercises the
    ``default_manifest`` back-compat shape (analyze + gated investigate)."""

    agent_id = "vita-v1"
    display_name = "v"
    description = "d"

    def __init__(self) -> None:
        self.analyze_calls: list[AgentInput] = []
        self.investigate_calls: list[AgentInput] = []

    async def analyze(self, inp, on_progress):  # noqa: ARG002
        self.analyze_calls.append(inp)
        return AnalysisResult(
            display={"refined_problem_statement": "rp"},
            structured={
                "refined_problem": {"refined_problem_statement": "rp"},
                "classified_inputs": {"valid": True},
                "_drifts": [{"step_id": "validate", "drift_type": "x"}],
            },
            status="awaiting_approval",
        )

    async def investigate(self, inp, on_progress):  # noqa: ARG002
        self.investigate_calls.append(inp)
        return InvestigationResult(
            status="complete",
            report_html="<p>rendered</p>",
            structured={
                "works_cited_a": [],
                "works_cited_b": [],
                "skills_cited": [],
                "resolution_plan": {},
                "followup_questions": [],
                "_drifts": [],
            },
        )


class _BlockedVita(AgentProtocol):
    agent_id = "vita-v1"
    display_name = "v"
    description = "d"

    async def analyze(self, inp, on_progress):  # noqa: ARG002
        return AnalysisResult(
            display={"rejected": True, "reason": "no"},
            structured={"classified_inputs": {"valid": False, "rejection_reason": "no"}},
            status="blocked",
        )

    async def investigate(self, inp, on_progress):  # noqa: ARG002
        return InvestigationResult(status="error", error="not reachable")


class _Boom(AgentProtocol):
    agent_id = "vita-v1"
    display_name = "v"
    description = "d"

    async def analyze(self, inp, on_progress):  # noqa: ARG002
        raise RuntimeError("boom")

    async def investigate(self, inp, on_progress):  # noqa: ARG002
        raise RuntimeError("boom")


class _SinglePhase(AgentProtocol):
    """Manifest declares one phase — submission runs straight to complete."""

    agent_id = "solo-v1"
    display_name = "s"
    description = "d"

    def __init__(self) -> None:
        self.calls: list[AgentInput] = []

    async def analyze(self, inp, on_progress):  # noqa: ARG002
        self.calls.append(inp)
        return InvestigationResult(
            status="complete", report_html="<p>one-shot</p>", structured={"answer": 42}
        )


class _UngatedTwoPhase(AgentProtocol):
    """Two phases, no approval gate — the runner auto-advances."""

    agent_id = "duo-v1"
    display_name = "duo"
    description = "d"

    def __init__(self) -> None:
        self.order: list[str] = []

    async def gather(self, inp, on_progress):  # noqa: ARG002
        self.order.append("gather")
        return AnalysisResult(
            display={}, structured={"gathered": True}, status="awaiting_approval"
        )

    async def conclude(self, inp, on_progress):  # noqa: ARG002
        self.order.append("conclude")
        return InvestigationResult(
            status="complete", report_html="<p>done</p>", structured={"final": True}
        )


# --------------------------- start_run ---------------------------------------


@pytest.mark.asyncio
async def test_start_run_unknown_agent_sets_error(patch_runner):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(run_id=run_id, tenant_id=tenant_id)
    session = _FakeSession(run)
    patch_runner(session)

    await agent_runner.start_run(run_id, tenant_id, "does-not-exist")

    assert run.status == "error"
    assert session.commits >= 1
    # Blueprint S7: the reason lands on the row, as a code the customer
    # page has a sentence for and a detail the admin view shows.
    assert run.error_code == run_errors.AGENT_UNAVAILABLE
    assert run.error_detail == "agent 'does-not-exist' is not registered in this process"


@pytest.mark.asyncio
async def test_start_run_parks_at_approval_gate(patch_runner):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(
        run_id=run_id,
        tenant_id=tenant_id,
        user_inputs={"vendor_a_name": "A"},
    )
    session = _FakeSession(run)
    patch_runner(session)
    agent = _HappyVita()
    registry.register(agent)  # default manifest: analyze + gated investigate

    await agent_runner.start_run(run_id, tenant_id, "vita-v1")

    assert run.status == "awaiting_approval"
    assert run.current_phase == "analyze"
    assert session.snapshot is not None
    # Generic column written (no _drifts key).
    assert session.snapshot.analysis == {
        "refined_problem": {"refined_problem_statement": "rp"},
        "classified_inputs": {"valid": True},
    }
    assert agent.analyze_calls[0].user_inputs["vendor_a_name"] == "A"
    # The gate means investigate must NOT have run.
    assert agent.investigate_calls == []


@pytest.mark.asyncio
async def test_start_run_blocked_agent_marks_error(patch_runner):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(run_id=run_id, tenant_id=tenant_id)
    session = _FakeSession(run)
    patch_runner(session)
    registry.register(_BlockedVita())

    await agent_runner.start_run(run_id, tenant_id, "vita-v1")

    assert run.status == "error"
    # The agent said so, in its own status word; the code says "the
    # agent failed" and the detail keeps what it said (operator-facing).
    assert run.error_code == run_errors.AGENT_FAILED
    assert run.error_detail == "phase 'analyze' ended with status 'blocked'"


@pytest.mark.asyncio
async def test_start_run_agent_raises_sets_error(patch_runner):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(run_id=run_id, tenant_id=tenant_id)
    session = _FakeSession(run)
    patch_runner(session)
    registry.register(_Boom())

    await agent_runner.start_run(run_id, tenant_id, "vita-v1")

    assert run.status == "error"
    # An exception the runner did not expect: the class and the message,
    # redacted, for the admin view; a code the customer page maps.
    assert run.error_code == run_errors.PHASE_FAILED
    assert run.error_detail == "RuntimeError: boom"


@pytest.mark.asyncio
async def test_a_failed_final_phase_keeps_the_agents_error_text_redacted(patch_runner):
    """``InvestigationResult.error`` is the agent's own sentence — kept
    for the operator, through the boundary's redaction, never shown to
    the customer (who gets the code's sentence)."""

    class _FailsWithText(AgentProtocol):
        agent_id = "solo-v1"
        display_name = "s"
        description = "d"

        async def analyze(self, inp, on_progress):  # noqa: ARG002
            return InvestigationResult(
                status="error", error="upstream refused the call for pii.fixture@example.com"
            )

    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(run_id=run_id, tenant_id=tenant_id)
    session = _FakeSession(run)
    patch_runner(session)
    registry.register(_FailsWithText(), _manifest("solo-v1", [{"name": "analyze"}]))

    await agent_runner.start_run(run_id, tenant_id, "solo-v1")

    assert run.status == "error"
    assert run.error_code == run_errors.AGENT_FAILED
    assert run.error_detail.startswith("upstream refused the call for ")
    assert "pii.fixture@example.com" not in run.error_detail
    assert "REDACTED" in run.error_detail


@pytest.mark.asyncio
async def test_resume_with_a_cursor_the_manifest_lost_names_the_phase(patch_runner):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(
        run_id=run_id, tenant_id=tenant_id, status="awaiting_approval", current_phase="vanished"
    )
    session = _FakeSession(run)
    patch_runner(session)
    registry.register(_HappyVita())

    await agent_runner.resume_run(run_id, tenant_id, "vita-v1")

    assert run.status == "error"
    assert run.error_code == run_errors.PHASE_INVALID
    assert run.error_detail == (
        "cannot resume after phase 'vanished': the manifest declares ['analyze', 'investigate']"
    )


@pytest.mark.asyncio
async def test_start_run_single_phase_completes_directly(patch_runner):
    """A one-phase manifest has no gate: submit → complete, results in the
    final-output columns."""
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(run_id=run_id, tenant_id=tenant_id)
    session = _FakeSession(run)
    patch_runner(session)
    agent = _SinglePhase()
    registry.register(agent, _manifest("solo-v1", [{"name": "analyze"}]))

    await agent_runner.start_run(run_id, tenant_id, "solo-v1")

    assert run.status == "complete"
    assert run.current_phase == "analyze"
    assert session.snapshot.structured_data == {"answer": 42}
    assert session.snapshot.report_html == "<p>one-shot</p>"
    assert session.snapshot.analysis is None
    assert len(agent.calls) == 1


@pytest.mark.asyncio
async def test_start_run_ungated_phases_auto_advance(patch_runner):
    """Consecutive phases without an approval gate run in one background
    task — no parking, one submission ends complete."""
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(run_id=run_id, tenant_id=tenant_id)
    session = _FakeSession(run)
    patch_runner(session)
    agent = _UngatedTwoPhase()
    registry.register(
        agent,
        _manifest("duo-v1", [{"name": "gather"}, {"name": "conclude"}]),
    )

    await agent_runner.start_run(run_id, tenant_id, "duo-v1")

    assert agent.order == ["gather", "conclude"]
    assert run.status == "complete"
    assert run.current_phase == "conclude"
    # Intermediate output landed in analysis, final output in structured_data.
    assert session.snapshot.analysis == {"gathered": True}
    assert session.snapshot.structured_data == {"final": True}
    assert session.snapshot.report_html == "<p>done</p>"


# --------------------------- resume_run --------------------------------------


@pytest.mark.asyncio
async def test_resume_run_completes_final_phase(patch_runner):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(
        run_id=run_id, tenant_id=tenant_id, current_phase="analyze"
    )
    session = _FakeSession(run)
    session.snapshot = _FakeSnapshot(run_id, tenant_id)
    session.snapshot.analysis = {
        "refined_problem": {"refined_problem_statement": "rp"},
        "classified_inputs": {"valid": True},
    }
    patch_runner(session)
    agent = _HappyVita()
    registry.register(agent)

    await agent_runner.resume_run(run_id, tenant_id, "vita-v1")

    assert run.status == "complete"
    assert run.current_phase == "investigate"
    assert session.snapshot.report_html == "<p>rendered</p>"
    # structured_data is the generic col; _drifts stripped.
    assert session.snapshot.structured_data is not None
    assert "_drifts" not in session.snapshot.structured_data
    # The resumed phase received the parked analysis as prior context.
    assert agent.investigate_calls[0].prior_analysis == session.snapshot.analysis
    # phase2_span_id only populated when a real OTEL span is active;
    # in tests OTEL is uninitialized so span.get_span_context() is invalid.
    # trace_id may be None for the same reason — don't assert on it.


@pytest.mark.asyncio
async def test_resume_run_null_cursor_falls_back_to_second_phase(patch_runner):
    """A row parked before migration 009 has no cursor; the old runner's
    approve always launched the second phase, so that's the fallback."""
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(run_id=run_id, tenant_id=tenant_id, current_phase=None)
    session = _FakeSession(run)
    session.snapshot = _FakeSnapshot(run_id, tenant_id)
    session.snapshot.analysis = {"refined_problem": {}}
    patch_runner(session)
    agent = _HappyVita()
    registry.register(agent)

    await agent_runner.resume_run(run_id, tenant_id, "vita-v1")

    assert run.status == "complete"
    assert agent.analyze_calls == []
    assert len(agent.investigate_calls) == 1


@pytest.mark.asyncio
async def test_resume_run_cursor_not_in_manifest_errors(patch_runner):
    """An agent author renamed phases while a run was parked — resuming
    can't guess, so the case errors with a clear log."""
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(
        run_id=run_id, tenant_id=tenant_id, current_phase="no-such-phase"
    )
    session = _FakeSession(run)
    patch_runner(session)
    registry.register(_HappyVita())

    await agent_runner.resume_run(run_id, tenant_id, "vita-v1")

    assert run.status == "error"


# --------------------------- rerun_current_phase -----------------------------


@pytest.mark.asyncio
async def test_rerun_passes_user_edits(patch_runner):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(
        run_id=run_id, tenant_id=tenant_id, current_phase="analyze"
    )
    session = _FakeSession(run)
    # Pre-existing snapshot with analysis from the previous attempt.
    session.snapshot = _FakeSnapshot(run_id, tenant_id)
    prior_analysis = {
        "refined_problem": {"refined_problem_statement": "old"},
        "classified_inputs": {"valid": True},
    }
    session.snapshot.analysis = prior_analysis
    patch_runner(session)
    agent = _HappyVita()
    registry.register(agent)

    await agent_runner.rerun_current_phase(run_id, "edited!", "vita-v1")

    assert run.status == "awaiting_approval"
    assert agent.analyze_calls[0].user_edits == "edited!"
    # The agent received the previous analysis as prior_analysis. After the
    # run, snap.analysis is the NEW agent output, so assert against the
    # pre-run value captured above.
    assert agent.analyze_calls[0].prior_analysis == prior_analysis
    # A rerun repeats the current phase, never the next one.
    assert agent.investigate_calls == []


@pytest.mark.asyncio
async def test_rerun_null_cursor_falls_back_to_first_phase(patch_runner):
    """Legacy parked rows (pre-009) rerun the first phase — the pre-B7
    edit semantics."""
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(run_id=run_id, tenant_id=tenant_id, current_phase=None)
    session = _FakeSession(run)
    session.snapshot = _FakeSnapshot(run_id, tenant_id)
    session.snapshot.analysis = {"refined_problem": {}}
    patch_runner(session)
    agent = _HappyVita()
    registry.register(agent)

    await agent_runner.rerun_current_phase(run_id, "edited!", "vita-v1")

    assert run.status == "awaiting_approval"
    assert len(agent.analyze_calls) == 1
    assert agent.investigate_calls == []


# --------------------------- resume_status_for -------------------------------


def test_resume_status_for_final_phase_is_investigating():
    registry.register(_HappyVita())
    assert agent_runner.resume_status_for("vita-v1", "analyze") == "investigating"


def test_resume_status_for_mid_phase_is_refining():
    agent = _UngatedTwoPhase()
    registry.register(
        agent,
        _manifest(
            "duo-v1",
            [
                {"name": "gather"},
                {"name": "middle", "approval": True},
                {"name": "conclude", "approval": True},
            ],
        ),
    )
    assert agent_runner.resume_status_for("duo-v1", "gather") == "refining"
    assert agent_runner.resume_status_for("duo-v1", "middle") == "investigating"


def test_resume_status_for_unknown_agent_defaults_investigating():
    assert agent_runner.resume_status_for("nope", "analyze") == "investigating"


# --------------------------- telemetry call shape ----------------------------


@pytest.fixture
def captured_using_attributes(monkeypatch):
    """Patch ``using_attributes`` in agent_runner with a recorder.

    OpenInference's ``using_attributes`` is a context manager that writes
    session_id / user_id / metadata into the OTEL Context API so the
    LLM auto-instrumentors propagate them onto child spans. We don't
    drive a real TracerProvider in unit tests, so just record the kwargs
    and yield to confirm the runner's call-site shape.
    """
    calls: list[dict] = []

    @contextmanager
    def _recorder(**kwargs):
        calls.append(kwargs)
        yield

    monkeypatch.setattr(agent_runner, "using_attributes", _recorder)
    return calls


@pytest.mark.asyncio
async def test_start_run_uses_using_attributes(patch_runner, captured_using_attributes):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    run = _FakeRun(run_id=run_id, tenant_id=tenant_id, user_id=user_id)
    session = _FakeSession(run)
    patch_runner(session)
    registry.register(_HappyVita())

    await agent_runner.start_run(run_id, tenant_id, "vita-v1")

    assert len(captured_using_attributes) == 1
    kwargs = captured_using_attributes[0]
    assert kwargs["session_id"] == str(run_id)
    assert kwargs["user_id"] == str(user_id)
    assert kwargs["metadata"] == {
        "tenant_id": str(tenant_id),
        "run_number": run.run_number,
        "agent_id": "vita-v1",
        "agent_name": "v",
        "phase": "analyze",
    }
    assert "agent:vita-v1" in kwargs["tags"]
    assert "phase:analyze" in kwargs["tags"]
    assert f"run:{run.run_number}" in kwargs["tags"]
    # Pre-S1 spelling rides along for one release (saved viewer filters).
    assert f"case:{run.run_number}" in kwargs["tags"]


@pytest.mark.asyncio
async def test_rerun_uses_edit_phase_label(
    patch_runner, captured_using_attributes
):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    run = _FakeRun(
        run_id=run_id, tenant_id=tenant_id, user_id=user_id, current_phase="analyze"
    )
    session = _FakeSession(run)
    session.snapshot = _FakeSnapshot(run_id, tenant_id)
    session.snapshot.analysis = {"refined_problem": {"refined_problem_statement": "x"}}
    patch_runner(session)
    registry.register(_HappyVita())

    await agent_runner.rerun_current_phase(run_id, "edited!", "vita-v1")

    assert len(captured_using_attributes) == 1
    kwargs = captured_using_attributes[0]
    assert kwargs["session_id"] == str(run_id)
    assert kwargs["user_id"] == str(user_id)
    assert kwargs["metadata"]["phase"] == "analyze_edit"
    assert kwargs["metadata"]["agent_id"] == "vita-v1"
    assert kwargs["metadata"]["agent_name"] == "v"
    assert "phase:analyze_edit" in kwargs["tags"]
    assert f"run:{run.run_number}" in kwargs["tags"]


@pytest.mark.asyncio
async def test_resume_run_uses_using_attributes(
    patch_runner, captured_using_attributes
):
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    run = _FakeRun(
        run_id=run_id, tenant_id=tenant_id, user_id=user_id, current_phase="analyze"
    )
    session = _FakeSession(run)
    session.snapshot = _FakeSnapshot(run_id, tenant_id)
    session.snapshot.analysis = {"refined_problem": {"refined_problem_statement": "rp"}}
    patch_runner(session)
    registry.register(_HappyVita())

    await agent_runner.resume_run(run_id, tenant_id, "vita-v1")

    assert len(captured_using_attributes) == 1
    kwargs = captured_using_attributes[0]
    assert kwargs["session_id"] == str(run_id)
    assert kwargs["user_id"] == str(user_id)
    assert kwargs["metadata"]["phase"] == "investigate"
    assert kwargs["metadata"]["agent_id"] == "vita-v1"
    assert kwargs["metadata"]["agent_name"] == "v"
    assert "phase:investigate" in kwargs["tags"]
    assert f"run:{run.run_number}" in kwargs["tags"]


@pytest.mark.asyncio
async def test_using_attributes_omits_run_tag_when_run_number_missing(
    patch_runner, captured_using_attributes
):
    """A row without an allocated run_number (shouldn't happen in
    production, but be defensive) shouldn't emit a meaningless ``run:``
    tag — the prefix on its own is just noise to filter against."""
    run_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    run = _FakeRun(run_id=run_id, tenant_id=tenant_id, run_number="")
    session = _FakeSession(run)
    patch_runner(session)
    registry.register(_HappyVita())

    await agent_runner.start_run(run_id, tenant_id, "vita-v1")

    kwargs = captured_using_attributes[0]
    assert "agent:vita-v1" in kwargs["tags"]
    assert "phase:analyze" in kwargs["tags"]
    assert not any(t.startswith(("run:", "case:")) for t in kwargs["tags"])


@pytest.mark.asyncio
async def test_on_progress_writes_redis_hash():
    redis = _FakeRedis()
    run_id = uuid.uuid4()
    cb = agent_runner._on_progress(redis, run_id)
    await cb(StepProgress(step_id="s1", status="running", detail="going"))
    key = f"run:{run_id}:progress"
    stored = redis.hashes[key]["s1"]
    parsed = json.loads(stored)
    assert parsed == {"status": "running", "duration_ms": None, "detail": "going"}
    # The runner's callback is one of the three writers of this hash, and
    # the hash must not outlive the run: the field and the key's expiry
    # go out as one transaction (S5-E). Read back from the double's
    # ledger, which is why it keeps one.
    from app.services.run_boundary import RUN_KEY_TTL_SECONDS

    assert redis.ttls[key] == RUN_KEY_TTL_SECONDS, redis.ttls
    assert redis.commands == [
        ("hset", key, "s1", stored),
        ("expire", key, RUN_KEY_TTL_SECONDS),
    ], redis.commands
