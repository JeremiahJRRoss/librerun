"""Tests for the runs router after Phase 6 (no feature flag).

Verifies the agent-runner is the only background-task target, that an
unknown ``agent_id`` returns 400, and that ``agent_id`` + ``user_inputs``
are surfaced on run-detail responses.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from app.agents import registry
from app.agents.protocol import (
    AgentProtocol,
    AnalysisResult,
    InvestigationResult,
)
from app.database import get_db
from app.middleware import get_current_user
from app.routers import runs as runs_router


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


class _StubAgent(AgentProtocol):
    """VITA-shaped stub: its input schema carries the same constraints the
    real agent declares, so create-run tests exercise the B8 server-side
    schema validation."""

    agent_id = "vita-v1"
    display_name = "stub"
    description = "stub"

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["vendor_a", "vendor_b", "use_case", "problem_statement"],
            "properties": {
                "vendor_a": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string", "minLength": 1}},
                },
                "vendor_b": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string", "minLength": 1}},
                },
                "use_case": {"type": "string", "minLength": 50},
                "problem_statement": {"type": "string", "minLength": 20},
                "logs_a": {"type": "string", "x-pii": True},
                "severity": {
                    "type": "string",
                    "enum": ["critical", "high", "medium", "low"],
                },
            },
        }

    async def analyze(self, inp, on_progress):  # pragma: no cover
        return AnalysisResult(display={}, structured={})

    async def investigate(self, inp, on_progress):  # pragma: no cover
        return InvestigationResult(status="complete")


class _ToyAgent(AgentProtocol):
    """A second agent with a completely different input shape — pins that
    POST /runs really is per-agent now (blueprint B8 accept)."""

    agent_id = "toy-v1"
    display_name = "toy"
    description = "toy"

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["description"],
            "additionalProperties": False,
            "properties": {
                "description": {"type": "string", "minLength": 5},
                "urgency": {"type": "integer"},
            },
        }

    async def analyze(self, inp, on_progress):  # pragma: no cover
        return AnalysisResult(display={}, structured={})

    async def investigate(self, inp, on_progress):  # pragma: no cover
        return InvestigationResult(status="complete")


def _make_run_orm(
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    status: str = "awaiting_approval",
    agent_id: str = "vita-v1",
) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        run_number="C-000001",
        vendor_a_name="A",
        vendor_a_product=None,
        vendor_a_feature=None,
        vendor_a_observation=None,
        vendor_b_name="B",
        vendor_b_product=None,
        vendor_b_feature=None,
        vendor_b_observation=None,
        logs_a=None,
        logs_b=None,
        use_case="u",
        title="p",
        problem_statement="p",
        impact_statement=None,
        severity=None,
        status=status,
        deleted_at=None,
        trace_id=None,
        root_traceparent=None,
        root_tracestate=None,
        phase2_span_id=None,
        current_phase=None,
        agent_id=agent_id,
        user_inputs={"vendor_a_name": "A"},
        created_at=now,
        updated_at=now,
        error_code=None,
        error_detail=None,
    )


def _build_app(
    monkeypatch,
    *,
    existing_run: SimpleNamespace | None = None,
    existing_snapshot: SimpleNamespace | None = None,
) -> tuple[TestClient, dict, uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user = SimpleNamespace(
        id=user_id,
        tenant_id=tenant_id,
        email="u@example.com",
        role="customer",
    )

    calls: dict[str, list] = {"phase1": [], "phase1_edit": [], "phase2": []}

    async def _phase1(run_id, tid, agent_id):
        calls["phase1"].append((run_id, tid, agent_id))

    async def _phase1_edit(run_id, edited, agent_id):
        calls["phase1_edit"].append((run_id, edited, agent_id))

    async def _phase2(run_id, tid, agent_id):
        calls["phase2"].append((run_id, tid, agent_id))

    monkeypatch.setattr(runs_router.agent_runner, "start_run", _phase1)
    monkeypatch.setattr(runs_router.agent_runner, "rerun_current_phase", _phase1_edit)
    monkeypatch.setattr(runs_router.agent_runner, "resume_run", _phase2)

    async def _allocate_run_number(db, tid):  # noqa: ARG001
        return "C-000001"

    monkeypatch.setattr(runs_router, "allocate_run_number", _allocate_run_number)

    async def _log_audit(*args, **kwargs):  # noqa: ARG001
        return None

    monkeypatch.setattr(runs_router, "log_audit", _log_audit)

    fake_db = AsyncMock()

    def _add(obj):
        if hasattr(obj, "id") and obj.id is None:
            obj.id = uuid.uuid4()

    fake_db.add = _add
    fake_db.flush = AsyncMock()
    fake_db.commit = AsyncMock()

    async def _get(model, pk):
        if existing_run is not None and pk == existing_run.id:
            return existing_run
        return None

    fake_db.get = _get

    async def _execute(stmt):
        class _R:
            def scalar_one_or_none(self_inner):
                return existing_snapshot

            def scalar_one(self_inner):
                return 0

            def scalars(self_inner):
                return self_inner

            def all(self_inner):
                return []

        return _R()

    fake_db.execute = _execute

    async def _db_dep():
        yield fake_db

    async def _user_dep(request: Request):
        request.state.tenant_id = tenant_id
        request.state.current_user = user
        request.state.session_id = uuid.uuid4()
        return user

    app = FastAPI()
    app.include_router(runs_router.router)
    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep

    return TestClient(app), calls, tenant_id, user_id


def _post_run_payload() -> dict:
    return {
        "vendor_a": {"name": "Vendor A"},
        "vendor_b": {"name": "Vendor B"},
        "use_case": "x" * 60,
        "problem_statement": "y" * 30,
    }


def test_create_run_unknown_agent_returns_400(monkeypatch):
    client, calls, _tid, _uid = _build_app(monkeypatch)
    r = client.post("/runs?agent_id=nope", json=_post_run_payload())
    assert r.status_code == 400
    assert "Unknown agent" in r.json()["detail"]
    assert not any(calls.values())


def test_create_run_routes_through_agent_runner(monkeypatch):
    registry.register(_StubAgent())
    client, calls, tid, _uid = _build_app(monkeypatch)
    r = client.post("/runs", json=_post_run_payload())
    assert r.status_code == 202
    assert len(calls["phase1"]) == 1
    run_id, tenant_id_arg, agent_id_arg = calls["phase1"][0]
    assert tenant_id_arg == tid
    assert agent_id_arg == "vita-v1"


def test_create_run_schema_violation_returns_422(monkeypatch):
    """B8: the body is validated against the agent's input schema
    server-side — a too-short use_case never reaches the runner."""
    registry.register(_StubAgent())
    client, calls, _tid, _uid = _build_app(monkeypatch)
    payload = _post_run_payload()
    payload["use_case"] = "too short"
    r = client.post("/runs", json=payload)
    assert r.status_code == 422
    assert any("use_case" in err for err in r.json()["detail"])
    assert not any(calls.values())


def test_create_run_missing_nested_vendor_name_returns_422(monkeypatch):
    registry.register(_StubAgent())
    client, calls, _tid, _uid = _build_app(monkeypatch)
    payload = _post_run_payload()
    payload["vendor_a"] = {}
    r = client.post("/runs", json=payload)
    assert r.status_code == 422
    assert any("vendor_a" in err for err in r.json()["detail"])
    assert not any(calls.values())


def test_create_run_accepts_per_agent_shapes(monkeypatch):
    """B8 accept: a second agent with a totally different schema submits
    through the same endpoint — no vendor vocabulary required."""
    registry.register(_ToyAgent())
    client, calls, _tid, _uid = _build_app(monkeypatch)
    r = client.post(
        "/runs?agent_id=toy-v1",
        json={"description": "printer on fire", "urgency": 3},
    )
    assert r.status_code == 202
    assert calls["phase1"][0][2] == "toy-v1"
    # And the VITA shape is rejected for the toy agent, not silently
    # accepted (additionalProperties: false).
    r2 = client.post("/runs?agent_id=toy-v1", json=_post_run_payload())
    assert r2.status_code == 422


def test_approve_run_routes_through_agent_runner(monkeypatch):
    registry.register(_StubAgent())
    run_id = uuid.uuid4()
    run = _make_run_orm(run_id, uuid.uuid4(), uuid.uuid4(), status="awaiting_approval")
    client, calls, real_tid, real_uid = _build_app(
        monkeypatch, existing_run=run
    )
    run.tenant_id = real_tid
    run.user_id = real_uid
    r = client.post(f"/runs/{run_id}/approve")
    assert r.status_code == 202
    assert len(calls["phase2"]) == 1
    assert calls["phase2"][0][2] == "vita-v1"


def test_edit_statement_routes_through_agent_runner(monkeypatch):
    registry.register(_StubAgent())
    run_id = uuid.uuid4()
    run = _make_run_orm(run_id, uuid.uuid4(), uuid.uuid4(), status="awaiting_approval")
    client, calls, real_tid, real_uid = _build_app(
        monkeypatch, existing_run=run
    )
    run.tenant_id = real_tid
    run.user_id = real_uid
    r = client.post(
        f"/runs/{run_id}/edit-statement", json={"edited_statement": "new!"}
    )
    assert r.status_code == 202
    assert len(calls["phase1_edit"]) == 1
    cid, edited, agent_id_arg = calls["phase1_edit"][0]
    assert edited == "new!"
    assert agent_id_arg == "vita-v1"


def test_get_run_includes_agent_id_and_user_inputs(monkeypatch):
    run_id = uuid.uuid4()
    run = _make_run_orm(run_id, uuid.uuid4(), uuid.uuid4(), status="submitted")
    client, _calls, real_tid, real_uid = _build_app(
        monkeypatch, existing_run=run
    )
    run.tenant_id = real_tid
    run.user_id = real_uid
    r = client.get(f"/runs/{run_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_id"] == "vita-v1"
    assert body["user_inputs"] == {"vendor_a_name": "A"}


# --------------------------- run title and approval (S2) --------------------


def _fake_run(**kw) -> SimpleNamespace:
    """What the handler needs from the row it just built: an id after
    add(), the trace columns the root span writes, plus whatever it
    logs."""
    return SimpleNamespace(
        **{
            "id": uuid.uuid4(),
            "severity": None,
            "trace_id": None,
            "root_traceparent": None,
            "root_tracestate": None,
            **kw,
        }
    )


def test_create_run_stores_the_title_from_the_manifest_path(monkeypatch):
    """Blueprint S2: the title column is filled for every agent from the
    manifest's ui.list.title_path (the demo agent points it at its problem
    statement) — computed from the redacted payload."""
    from app.agents.manifest import AgentManifest

    client, calls, _tid, _uid = _build_app(monkeypatch)
    manifest = AgentManifest.model_validate({
        "id": "vita-v1", "name": "stub", "runtime": "python-package",
        "phases": [{"name": "analyze"}, {"name": "investigate", "approval": True}],
        "output": {"mode": "html_report"},
        "ui": {"list": {"title_path": "problem_statement"}},
    })
    registry.register(_StubAgent(), manifest)
    added: list = []
    monkeypatch.setattr(runs_router, "Run", lambda **kw: added.append(kw) or _fake_run(**kw))
    r = client.post("/runs", json=_post_run_payload())
    assert r.status_code == 202, r.text
    assert added[0]["title"] == "y" * 30
    assert "problem_statement" not in added[0], "the column is written once, as the title"


def test_create_run_title_defaults_to_the_first_non_pii_string_input(monkeypatch):
    client, calls, _tid, _uid = _build_app(monkeypatch)
    registry.register(_ToyAgent())  # no manifest, no title_path
    added: list = []
    monkeypatch.setattr(runs_router, "Run", lambda **kw: added.append(kw) or _fake_run(**kw))
    r = client.post("/runs?agent_id=toy-v1", json={"description": "Pager fired at 3am", "urgency": 2})
    assert r.status_code == 202, r.text
    assert added[0]["title"] == "Pager fired at 3am"


def test_approval_endpoint_serves_phase_payload_and_manifest_summary(monkeypatch):
    """GET /runs/{id}/approval (blueprint S2) is the same for every agent:
    the parked phase, its whole output, and the summary the manifest's
    ui.approval.summary_path names inside it."""
    from app.agents.manifest import AgentManifest

    manifest = AgentManifest.model_validate({
        "id": "toy-v1", "name": "toy", "runtime": "python-package",
        "phases": [{"name": "analyze"}, {"name": "investigate", "approval": True}],
        "output": {"mode": "structured"},
        "ui": {"approval": {"summary_path": "draft.summary"}},
    })
    registry.register(_ToyAgent(), manifest)
    parked = {"scores": [1, 2], "draft": {"summary": "Rotate the cert", "steps": ["a"]}}
    run_id = uuid.uuid4()
    run = _make_run_orm(run_id, uuid.uuid4(), uuid.uuid4(), status="awaiting_approval", agent_id="toy-v1")
    run.current_phase = "analyze"
    client, calls, real_tid, real_uid = _build_app(
        monkeypatch, existing_run=run, existing_snapshot=SimpleNamespace(analysis=parked)
    )
    run.tenant_id = real_tid
    run.user_id = real_uid

    r = client.get(f"/runs/{run_id}/approval")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "status": "awaiting_approval",
        "phase": "analyze",
        "payload": parked,
        "summary": "Rotate the cert",
    }


def test_approval_endpoint_defaults_summary_to_the_first_string_and_202s_while_working(monkeypatch):
    registry.register(_ToyAgent())  # no manifest → the output's first string
    run_id = uuid.uuid4()
    run = _make_run_orm(run_id, uuid.uuid4(), uuid.uuid4(), status="awaiting_approval", agent_id="toy-v1")
    client, calls, real_tid, real_uid = _build_app(
        monkeypatch,
        existing_run=run,
        existing_snapshot=SimpleNamespace(analysis={"n": 1, "text": "first words", "more": "later"}),
    )
    run.tenant_id = real_tid
    run.user_id = real_uid

    r = client.get(f"/runs/{run_id}/approval")
    assert r.status_code == 200, r.text
    assert r.json()["summary"] == "first words"

    run.status = "refining"
    r = client.get(f"/runs/{run_id}/approval")
    assert r.status_code == 202
    assert r.json()["status"] == "refining" and r.json()["payload"] is None


def test_approval_phase_is_the_producer_of_the_payload_not_the_cursor(monkeypatch):
    """Codex P2 on PR #50: once approved, the runner moves ``current_phase``
    to the resumed phase while ``snapshot.analysis`` still holds the gated
    phase's output — ``phase`` must name the producer of ``payload``."""
    from app.agents.manifest import AgentManifest

    manifest = AgentManifest.model_validate({
        "id": "toy-v1", "name": "toy", "runtime": "python-package",
        "phases": [{"name": "analyze"}, {"name": "investigate", "approval": True}],
        "output": {"mode": "structured"},
    })
    registry.register(_ToyAgent(), manifest)
    run_id = uuid.uuid4()
    run = _make_run_orm(run_id, uuid.uuid4(), uuid.uuid4(), status="investigating", agent_id="toy-v1")
    run.current_phase = "investigate"
    client, calls, real_tid, real_uid = _build_app(
        monkeypatch, existing_run=run, existing_snapshot=SimpleNamespace(analysis={"draft": "d"})
    )
    run.tenant_id = real_tid
    run.user_id = real_uid

    for status_ in ("investigating", "complete"):
        run.status = status_
        r = client.get(f"/runs/{run_id}/approval")
        assert r.status_code == 200, r.text
        assert r.json()["phase"] == "analyze", status_

    # Parked at the gate the cursor IS the producer; a pre-009 row without
    # a cursor names the first phase.
    run.status = "awaiting_approval"
    assert client.get(f"/runs/{run_id}/approval").json()["phase"] == "investigate"
    run.current_phase = None
    assert client.get(f"/runs/{run_id}/approval").json()["phase"] == "analyze"

    # While working the answer is status only, as the 202 declares.
    run.status = "refining"
    r = client.get(f"/runs/{run_id}/approval")
    assert r.status_code == 202 and r.json() == {
        "status": "refining", "phase": None, "payload": None, "summary": None,
    }


def test_approval_serves_an_empty_parked_payload(monkeypatch):
    """Codex P2 on PR #50: a successful non-final phase may return
    ``structured={}``; the runner parks the run with that empty dict and
    the view must still be able to approve or edit it — 404 is only for
    a missing snapshot or a non-dict."""
    registry.register(_ToyAgent())
    run_id = uuid.uuid4()
    run = _make_run_orm(run_id, uuid.uuid4(), uuid.uuid4(), status="awaiting_approval", agent_id="toy-v1")
    run.current_phase = "analyze"
    snapshot = SimpleNamespace(analysis={})
    client, calls, real_tid, real_uid = _build_app(
        monkeypatch, existing_run=run, existing_snapshot=snapshot
    )
    run.tenant_id = real_tid
    run.user_id = real_uid

    r = client.get(f"/runs/{run_id}/approval")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "awaiting_approval", "phase": "analyze", "payload": {}, "summary": None}

    snapshot.analysis = None
    assert client.get(f"/runs/{run_id}/approval").status_code == 404


def test_search_and_list_keep_pre_s2_rows_findable_and_titled(monkeypatch):
    """Codex P2 on PR #50: a run persisted before S2 computed titles at
    intake has a NULL title. Search falls back to the text of its stored
    inputs for exactly those rows, and the list derives its title from
    the inputs with the installed agent's rule (or the first string when
    the agent is gone) — never written back."""
    from sqlalchemy.dialects import postgresql

    from app.agents.manifest import AgentManifest
    from app.services.run_service import display_title

    sql = str(
        runs_router._search_clause("cert").compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    # (literal_binds renders the driver's escaped form: %% for a literal %)
    assert "runs.problem_statement ILIKE '%%cert%%'" in sql
    assert "runs.run_number ILIKE '%%cert%%'" in sql
    assert "runs.problem_statement IS NULL AND CAST(runs.user_inputs AS TEXT) ILIKE '%%cert%%'" in sql

    run = _make_run_orm(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), agent_id="toy-v1")
    run.title = None
    run.problem_statement = None
    run.user_inputs = {"urgency": 2, "description": "Rotate the cert before Friday"}
    # Agent gone: the first string in the payload.
    assert display_title(run) == "Rotate the cert before Friday"
    # Agent installed with a title path: its rule, and nothing persisted.
    manifest = AgentManifest.model_validate({
        "id": "toy-v1", "name": "toy", "runtime": "python-package",
        "phases": [{"name": "analyze"}, {"name": "investigate", "approval": True}],
        "output": {"mode": "structured"},
        "ui": {"list": {"title_path": "description"}},
    })
    registry.register(_ToyAgent(), manifest)
    assert display_title(run) == "Rotate the cert before Friday"
    assert run.title is None
    assert runs_router._to_summary(run).title == "Rotate the cert before Friday"
    # The detail carries the derived title in every field that duplicates
    # it (the deprecated problem_statement is identical by contract).
    from app.services.run_service import build_run_detail_fields

    fields = build_run_detail_fields(run, None)
    assert fields["title"] == fields["problem_statement"] == "Rotate the cert before Friday"
    assert fields["problem_summary"] == "Rotate the cert before Friday"
    # A titled row is untouched; a NULL title with no inputs stays untitled.
    run.title = "stored"
    assert display_title(run) == "stored"
    run.title = None
    run.user_inputs = None
    assert display_title(run) is None


def test_refined_statement_alias_is_deprecated_but_still_served(monkeypatch):
    """The pre-S2 path keeps answering for one release, marked deprecated
    in the schema; it goes at v1.1."""
    registry.register(_StubAgent())
    run_id = uuid.uuid4()
    run = _make_run_orm(run_id, uuid.uuid4(), uuid.uuid4(), status="awaiting_approval")
    client, calls, real_tid, real_uid = _build_app(
        monkeypatch,
        existing_run=run,
        existing_snapshot=SimpleNamespace(
            analysis={"refined_problem": {"refined_problem_statement": "rp"}}
        ),
    )
    run.tenant_id = real_tid
    run.user_id = real_uid
    r = client.get(f"/runs/{run_id}/refined-statement")
    assert r.status_code == 200 and r.json()["refined_problem_statement"] == "rp"
    spec = client.app.openapi()
    assert spec["paths"]["/runs/{run_id}/refined-statement"]["get"]["deprecated"] is True
    assert "deprecated" not in spec["paths"]["/runs/{run_id}/approval"]["get"]


# --------------------------------------------------------------------------
# S4: the submission opens the run's root span and persists its W3C pair
# --------------------------------------------------------------------------

UPSTREAM_TRACE = "4bf92f3577b34da6a2ce929d0e0e4736"
UPSTREAM_SPAN = "00f067aa0ba902b7"
FIXTURE_EMAIL = "pii.fixture@example.com"


@pytest.fixture
def run_tracer(monkeypatch):
    """A real provider with the run-plane sampler behind ``run_trace``."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from app.observability import run_trace

    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=run_trace.RunRootSampler())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(run_trace, "_tracer", provider.get_tracer("test"))
    yield exporter
    provider.shutdown()


def _capture_runs(monkeypatch) -> list:
    added: list = []

    def _make(**kw):
        row = _fake_run(**kw)
        added.append(row)
        return row

    monkeypatch.setattr(runs_router, "Run", _make)
    return added


def _root_span(exporter):
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "run"]
    return span


def test_create_run_opens_the_root_span_and_persists_its_pair(monkeypatch, run_tracer):
    registry.register(_StubAgent())
    client, calls, tid, _uid = _build_app(monkeypatch)
    added = _capture_runs(monkeypatch)

    r = client.post("/runs", json=_post_run_payload())

    assert r.status_code == 202, r.text
    root = _root_span(run_tracer)
    assert root.parent is None
    assert root.attributes["run.id"] == str(added[0].id)
    assert root.attributes["agent.id"] == "vita-v1"
    assert root.attributes["tenant.id"] == str(tid)
    trace_hex = format(root.context.trace_id, "032x")
    assert added[0].trace_id == trace_hex
    assert added[0].root_traceparent == (
        f"00-{trace_hex}-{format(root.context.span_id, '016x')}-03"
    )
    assert added[0].root_tracestate is None
    # The row's id is the one the root was stamped with — chosen before
    # the flush so the span and the row agree.
    assert calls["phase1"][0][0] == added[0].id
    # Ended before the response: the phases parent on an ended span.
    assert root.end_time is not None


def test_create_run_adopts_a_valid_upstream_traceparent_even_unsampled(
    monkeypatch, run_tracer
):
    """The Accept cases: an upstream ``traceparent`` ending in ``00`` still
    yields a sampled root whose pair ends in ``01``, under the caller's
    trace, with the vendor ``tracestate`` persisted."""
    registry.register(_StubAgent())
    client, _calls, _tid, _uid = _build_app(monkeypatch)
    added = _capture_runs(monkeypatch)

    r = client.post(
        "/runs",
        json=_post_run_payload(),
        headers={
            "traceparent": f"00-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-00",
            "tracestate": "vendor=abc, rojo=00f067aa0ba902b7",
        },
    )

    assert r.status_code == 202, r.text
    root = _root_span(run_tracer)
    assert format(root.context.trace_id, "032x") == UPSTREAM_TRACE
    assert format(root.parent.span_id, "016x") == UPSTREAM_SPAN
    assert root.context.trace_flags.sampled
    assert added[0].trace_id == UPSTREAM_TRACE
    assert added[0].root_traceparent == (
        f"00-{UPSTREAM_TRACE}-{format(root.context.span_id, '016x')}-01"
    )
    assert added[0].root_tracestate == "vendor=abc,rojo=00f067aa0ba902b7"


def test_create_run_drops_a_tracestate_carrying_pii_and_names_only_the_header(
    monkeypatch, run_tracer, caplog
):
    import logging

    registry.register(_StubAgent())
    client, _calls, _tid, _uid = _build_app(monkeypatch)
    added = _capture_runs(monkeypatch)

    with caplog.at_level(logging.WARNING):
        r = client.post(
            "/runs",
            json=_post_run_payload(),
            headers={
                "traceparent": f"00-{UPSTREAM_TRACE}-{UPSTREAM_SPAN}-01",
                "tracestate": f"vendor={FIXTURE_EMAIL}",
            },
        )

    assert r.status_code == 202, r.text
    assert added[0].root_tracestate is None
    assert added[0].trace_id == UPSTREAM_TRACE  # the parent itself survives
    root = _root_span(run_tracer)
    assert root.context.trace_state.to_header() == ""
    assert "tracestate_dropped" in caplog.text
    assert FIXTURE_EMAIL not in caplog.text
    assert not any(
        FIXTURE_EMAIL in str(v) for v in root.attributes.values()
    )


def test_create_run_ignores_a_malformed_traceparent(monkeypatch, run_tracer, caplog):
    import logging

    registry.register(_StubAgent())
    client, _calls, _tid, _uid = _build_app(monkeypatch)
    added = _capture_runs(monkeypatch)

    with caplog.at_level(logging.WARNING):
        r = client.post(
            "/runs",
            json=_post_run_payload(),
            headers={"traceparent": "00-nothex-00f067aa0ba902b7-01", "tracestate": "vendor=abc"},
        )

    assert r.status_code == 202, r.text
    root = _root_span(run_tracer)
    assert root.parent is None
    assert added[0].trace_id != UPSTREAM_TRACE
    assert added[0].root_tracestate is None
    assert "traceparent_ignored" in caplog.text
