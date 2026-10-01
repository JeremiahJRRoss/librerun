"""The agent → chassis boundary (blueprint S4; gaps H7, J3, J6).

Every agent-supplied value the chassis persists or forwards is walked:
terminal output (the Accept's number cases end the run ``error`` with
``pii_in_output``; the clean number object completes unchanged; the
fixture in ``report_html`` is stored with the placeholder), progress
records (a flagged step id refuses the write, a detail is redacted),
Run Contract event text (``log.message`` and ``failed.error`` redacted,
a ``progress`` event with a flagged ``step_id`` dropped with a warning),
and the audit attribution the chassis stamps.
"""
from __future__ import annotations

import json
import logging
import uuid

import pytest

from app.agents import registry
from app.agents.container import ContainerAgent, ContainerAgentError
from app.agents.manifest import load_manifest
from app.agents.protocol import (
    AgentProtocol,
    AnalysisResult,
    InvestigationResult,
    StepProgress,
)
from app.services import agent_runner, run_boundary
from app.services.pii_service import PiiRefused

from tests.test_agent_runner import (  # noqa: E402
    _FakeRedis,
    _FakeRun,
    _FakeSession,
    _FakeSnapshot,
    _manifest,
    patch_runner,  # noqa: F401  (fixture)
)
from tests.test_container_runner import _write_container_dir

FIXTURE_EMAIL = "pii.fixture@example.com"
FIXTURE_PHONE = 2125551234


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


# ------------------------------------------------------------ the helpers --


def test_redact_text_uses_the_intake_placeholders_but_keeps_dates_and_places():
    out = run_boundary.redact_text(
        f"mail {FIXTURE_EMAIL} from Palo Alto on 2024-01-15"
    )
    assert FIXTURE_EMAIL not in out
    assert "[REDACTED_EMAIL_ADDRESS_1]" in out
    assert "Palo Alto" in out and "2024-01-15" in out
    assert run_boundary.redact_text(None) is None
    assert run_boundary.redact_text(12) == "12"


def test_the_pii_capability_is_the_intake_pipeline_unchanged():
    """``ctx.pii.redact`` strips what intake strips — dates and places
    included — because that is what an agent asks it for."""
    import asyncio

    from app.capabilities import PiiCapability
    from app.services import pii_service

    text = f"mail me at {FIXTURE_EMAIL} from Palo Alto"
    assert asyncio.run(PiiCapability().redact(text)) == pii_service.redact(text)[0]
    assert "Palo Alto" not in asyncio.run(PiiCapability().redact(text))


def test_check_name_refuses_and_names_only_the_path(caplog):
    with caplog.at_level(logging.WARNING):
        with pytest.raises(PiiRefused) as exc:
            run_boundary.check_name(
                FIXTURE_EMAIL, argument="key", reason="pii_in_store", path="run_store.key"
            )
    assert exc.value.reason == "pii_in_store"
    assert exc.value.argument == "key"
    assert exc.value.finding.path == "run_store.key"
    assert FIXTURE_EMAIL not in str(exc.value)
    assert FIXTURE_EMAIL not in caplog.text
    assert "boundary_refused" in caplog.text
    assert run_boundary.check_name("marker", argument="key", reason="pii_in_store") == "marker"


def test_walk_value_redacts_strings_and_refuses_keys_and_numbers():
    walked = run_boundary.walk_value(
        {"note": f"call {FIXTURE_EMAIL}", "count": 3}, argument="detail", reason="pii_in_audit"
    )
    assert FIXTURE_EMAIL not in walked["note"] and walked["count"] == 3
    with pytest.raises(PiiRefused) as exc:
        run_boundary.walk_value({FIXTURE_EMAIL: 1}, argument="detail", reason="pii_in_audit")
    assert exc.value.finding.kind == "identifier"
    with pytest.raises(PiiRefused) as exc:
        run_boundary.walk_value({"phone": FIXTURE_PHONE}, argument="detail", reason="pii_in_audit")
    assert exc.value.finding.kind == "number" and exc.value.finding.path == "$.phone"


@pytest.mark.asyncio
async def test_progress_write_is_the_one_path_and_refuses_a_flagged_step_id():
    redis = _FakeRedis()
    run_id = uuid.uuid4()
    await run_boundary.progress_write(
        redis, run_id, "gather", "running", f"contacting {FIXTURE_EMAIL}"
    )
    (record,) = redis.hashes[f"run:{run_id}:progress"].values()
    assert FIXTURE_EMAIL not in record
    assert json.loads(record)["status"] == "running"
    with pytest.raises(PiiRefused) as exc:
        await run_boundary.progress_write(redis, run_id, FIXTURE_EMAIL, "running")
    assert exc.value.reason == "pii_in_progress"
    assert exc.value.finding.path == "progress.step_id"
    assert FIXTURE_EMAIL not in " ".join(redis.hashes[f"run:{run_id}:progress"])


# ------------------------------------------------------ terminal output --


class _OutputAgent(AgentProtocol):
    agent_id = "out-v1"
    display_name = "out"
    description = "d"

    def __init__(self, structured, report_html=None):
        self._structured = structured
        self._report_html = report_html

    async def analyze(self, inp, on_progress):  # noqa: ARG002
        return InvestigationResult(
            status="complete", structured=self._structured, report_html=self._report_html
        )


async def _run_output(patch_runner, structured, report_html=None, caplog=None):
    registry.register(
        _OutputAgent(structured, report_html),
        _manifest("out-v1", [{"name": "analyze"}], mode="structured"),
    )
    run = _FakeRun(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    run.agent_id = "out-v1"
    session = _FakeSession(run)
    patch_runner(session)
    await agent_runner.start_run(run.id, run.tenant_id, "out-v1")
    return run, session


@pytest.mark.parametrize(
    "structured, path",
    [
        ({"phone": FIXTURE_PHONE}, "$.phone"),
        ({"contacts": [FIXTURE_PHONE]}, "$.contacts[0]"),
        ({"contacts": [442071838750]}, "$.contacts[0]"),  # valid only as +44…
        ({"anything": 4111111111111111}, "$.anything"),  # Luhn-valid card
        ({FIXTURE_EMAIL: "x"}, "$.<key 0>"),  # the address as a key: named by ordinal
        ({"v": 1234567890123452}, "$.v"),  # Luhn-valid, leading 1, no context
    ],
)
@pytest.mark.asyncio
async def test_flagged_output_ends_the_run_error_with_pii_in_output(
    patch_runner, caplog, structured, path
):
    with caplog.at_level(logging.ERROR):
        run, session = await _run_output(patch_runner, structured)
    assert run.status == "error"
    assert session.snapshot is None or session.snapshot.structured_data is None
    assert "run_output_refused" in caplog.text
    assert "pii_in_output" in caplog.text
    assert path in caplog.text
    assert FIXTURE_EMAIL not in caplog.text
    assert str(FIXTURE_PHONE) not in caplog.text


@pytest.mark.asyncio
async def test_the_clean_number_object_completes_and_is_stored_unchanged(patch_runner):
    clean = {
        "created_at": 1788998400005,  # Luhn-valid epoch under a time-named key
        "ts_ns": 1757534400000000000,
        "id": 9876543,
        "message_id": 1788998400005123456,
        "ratio": 2125551234.5,
    }
    run, session = await _run_output(patch_runner, clean)
    assert run.status == "complete"
    assert session.snapshot.structured_data == clean


@pytest.mark.asyncio
async def test_the_fixture_in_output_strings_and_report_html_is_stored_redacted(
    patch_runner,
):
    run, session = await _run_output(
        patch_runner,
        {"summary": f"escalate to {FIXTURE_EMAIL} today"},
        report_html=f"<p>Contact {FIXTURE_EMAIL} on 2024-01-15 in Palo Alto</p>",
    )
    assert run.status == "complete"
    assert FIXTURE_EMAIL not in json.dumps(session.snapshot.structured_data)
    assert "[REDACTED_EMAIL_ADDRESS_1]" in session.snapshot.structured_data["summary"]
    assert FIXTURE_EMAIL not in session.snapshot.report_html
    assert "2024-01-15" in session.snapshot.report_html
    assert "Palo Alto" in session.snapshot.report_html


class _TwoPhase(AgentProtocol):
    agent_id = "two-v1"
    display_name = "two"
    description = "d"

    async def analyze(self, inp, on_progress):  # noqa: ARG002
        return AnalysisResult(
            display={}, structured={"note": f"ask {FIXTURE_EMAIL}"}, status="awaiting_approval"
        )

    async def investigate(self, inp, on_progress):  # noqa: ARG002
        return InvestigationResult(status="complete", structured={})


@pytest.mark.asyncio
async def test_the_analysis_text_of_a_non_final_phase_is_walked_too(patch_runner):
    registry.register(
        _TwoPhase(),
        _manifest("two-v1", [{"name": "analyze"}, {"name": "investigate", "approval": True}]),
    )
    run = _FakeRun(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    run.agent_id = "two-v1"
    session = _FakeSession(run)
    patch_runner(session)
    await agent_runner.start_run(run.id, run.tenant_id, "two-v1")
    assert run.status == "awaiting_approval"
    assert FIXTURE_EMAIL not in json.dumps(session.snapshot.analysis)


# ------------------------------------------------------ container events --


class _Collector:
    def __init__(self):
        self.events: list[StepProgress] = []

    async def __call__(self, p):
        self.events.append(p)


def _container(tmp_path) -> ContainerAgent:
    d = _write_container_dir(tmp_path)
    return ContainerAgent(load_manifest(d), "http://127.0.0.1:9", d)


@pytest.mark.asyncio
async def test_container_log_and_failed_text_are_redacted(tmp_path, caplog):
    agent = _container(tmp_path)
    with caplog.at_level(logging.INFO):
        await agent._handle_event(
            "log", {"level": "info", "message": f"fetched {FIXTURE_EMAIL}"}, _Collector()
        )
    assert "container_agent_log" in caplog.text
    assert FIXTURE_EMAIL not in caplog.text
    with pytest.raises(ContainerAgentError) as exc:
        await agent._handle_event("failed", {"error": f"no route to {FIXTURE_EMAIL}"}, _Collector())
    assert FIXTURE_EMAIL not in str(exc.value)
    assert "[REDACTED_EMAIL_ADDRESS_1]" in str(exc.value)


@pytest.mark.asyncio
async def test_container_progress_detail_is_redacted_and_a_flagged_step_id_drops_the_event(
    tmp_path, caplog
):
    agent = _container(tmp_path)
    collector = _Collector()
    await agent._handle_event(
        "progress",
        {"step_id": "gather", "status": "running", "detail": f"mailing {FIXTURE_EMAIL}"},
        collector,
    )
    (event,) = collector.events
    assert event.step_id == "gather" and FIXTURE_EMAIL not in event.detail

    with caplog.at_level(logging.WARNING):
        result = await agent._handle_event(
            "progress", {"step_id": FIXTURE_EMAIL, "status": "running"}, collector
        )
    assert result is None
    assert len(collector.events) == 1  # dropped, never handed on
    assert "container_progress_dropped" in caplog.text
    assert "progress.step_id" in caplog.text
    assert FIXTURE_EMAIL not in caplog.text


@pytest.mark.asyncio
async def test_token_record_carries_the_run_owner(tmp_path):
    from tests.test_container_runner import _agent_input

    agent = _container(tmp_path)
    owner = uuid.uuid4()
    record = agent._run_token_record(_agent_input(user_id=owner), {}, 30)
    assert record["user_id"] == str(owner)
    assert agent._run_token_record(_agent_input(), {}, 30)["user_id"] is None


# ---------------------------------------------------- audit attribution --


class _AuditSession:
    """A session that hands back the run and the user rows and records
    what was added."""

    def __init__(self, rows: dict):
        self.rows = rows
        self.added: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def begin(self):
        return self

    async def get(self, model, key):
        return self.rows.get((model.__name__, key))

    def add(self, obj):
        self.added.append(obj)


@pytest.mark.asyncio
async def test_audit_rows_are_attributed_to_the_run_owner_by_the_chassis(monkeypatch):
    from types import SimpleNamespace

    import app.database
    from app.capabilities import for_run

    run_id, tenant_id, owner = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session = _AuditSession(
        {
            ("Run", run_id): SimpleNamespace(user_id=owner),
            ("User", owner): SimpleNamespace(email="owner@example.com"),
        }
    )
    monkeypatch.setattr(app.database, "async_session", lambda: session)

    # In-process: the runner hands the owner over.
    caps = for_run(
        run_id=run_id, tenant_id=tenant_id, agent_id="probe-agent", grants=["audit"], user_id=owner
    )
    assert await caps.audit.log("blocked_request", {"reason": f"see {FIXTURE_EMAIL}"}) is True
    (row,) = session.added
    assert row.user_id == owner and row.user_email == "owner@example.com"
    assert row.action_type == "blocked_request"
    assert FIXTURE_EMAIL not in json.dumps(row.detail)

    # A token record from before the owner travelled: resolved from the run row.
    caps = for_run(run_id=run_id, tenant_id=tenant_id, agent_id="probe-agent", grants=["audit"])
    await caps.audit.log("demo_event", {"a": 1})
    assert session.added[-1].user_id == owner
    assert session.added[-1].user_email == "owner@example.com"


@pytest.mark.asyncio
async def test_audit_log_has_no_attribution_parameter_and_refuses_flagged_arguments():
    from app.capabilities import for_run

    caps = for_run(
        run_id=uuid.uuid4(), tenant_id=uuid.uuid4(), agent_id="probe-agent", grants=["audit"]
    )
    with pytest.raises(TypeError):
        await caps.audit.log("x", {}, user_email=FIXTURE_EMAIL)
    with pytest.raises(TypeError):
        await caps.audit.log("x", {}, user_id=uuid.uuid4())
    with pytest.raises(PiiRefused) as exc:
        await caps.audit.log(FIXTURE_EMAIL, {})
    assert exc.value.reason == "pii_in_audit" and exc.value.argument == "action_type"
    with pytest.raises(PiiRefused) as exc:
        await caps.audit.log("event", {FIXTURE_EMAIL: 1})
    assert exc.value.reason == "pii_in_audit" and exc.value.argument == "detail"
    with pytest.raises(PiiRefused):
        await caps.audit.log("event", {"phone": FIXTURE_PHONE})


@pytest.mark.asyncio
async def test_schema_drift_reports_are_walked(monkeypatch):
    import app.database
    from app.capabilities import for_run

    session = _AuditSession({})

    async def _flush():
        pass

    session.flush = _flush
    monkeypatch.setattr(app.database, "async_session", lambda: session)

    class _Report:
        def __init__(self, d):
            self.d = d

        def to_dict(self):
            return self.d

    caps = for_run(
        run_id=uuid.uuid4(), tenant_id=uuid.uuid4(), agent_id="probe-agent", grants=["audit"]
    )
    await caps.audit.log_schema_drift([_Report({"details": {"got": f"x {FIXTURE_EMAIL}"}})])
    (row,) = session.added
    assert FIXTURE_EMAIL not in json.dumps(row.detail)
    with pytest.raises(PiiRefused):
        await caps.audit.log_schema_drift([_Report({FIXTURE_EMAIL: 1})])


@pytest.mark.asyncio
async def test_an_agent_chosen_progress_status_is_normalized_not_stored():
    """`status` was agent-controlled, written verbatim and never walked.

    Two consequences, both real. An address in it was stored in Redis.
    And `StepProgress.status` is a Literal of five values, so any other
    word made `GET /runs/{id}/progress` fail response validation — every
    later read of that run's progress a 500, from one bad write.

    Normalized at the one write path: five constants or nothing, so the
    status needs no walk. Unknown words degrade to `running` rather than
    refusing, because progress is cosmetic and the terminal event
    decides the run's fate.
    """
    redis = _FakeRedis()
    run_id = uuid.uuid4()
    for sent, stored in (
        ("running", "running"),
        ("completed", "complete"),
        ("failed", "error"),
        ("SKIPPED", "skipped"),
        (FIXTURE_EMAIL, "running"),
        ("<script>alert(1)</script>", "running"),
        (None, "running"),
        (42, "running"),
    ):
        await run_boundary.progress_write(redis, run_id, "gather", sent)
        record = json.loads(redis.hashes[f"run:{run_id}:progress"]["gather"])
        assert record["status"] == stored, sent
        assert record["status"] in run_boundary.STORED_STATUSES

    blob = json.dumps(redis.hashes[f"run:{run_id}:progress"])
    assert FIXTURE_EMAIL not in blob
    assert "script" not in blob
