"""Blueprint B9: generic output + manifest-driven feedback.

Covers the three chassis pieces that stopped being VITA-shaped:

1. Report generation — the shell renders downloads through the agent's
   ``render_report_document`` hook, with generic fallbacks; it imports no
   agent modules and knows no result vocabulary.
2. Run detail — ``output_mode`` comes from the manifest and
   ``structured_output`` is exposed only for structured-mode agents.
3. Feedback — ``section_type`` is validated against the case agent's
   manifest ``feedback_sections``.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from starlette.testclient import TestClient

from app.agents import registry
from app.agents.manifest import AgentManifest
from app.agents.protocol import AgentProtocol
from app.database import get_db
from app.middleware import get_current_user
from app.routers import feedback as feedback_router
from app.services.run_service import build_run_detail_fields
from app.services.report_service import _generic_document


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


def _manifest(agent_id: str, *, mode: str = "html_report", feedback=None) -> AgentManifest:
    data = {
        "id": agent_id,
        "name": "stub",
        "runtime": "python-package",
        "phases": [{"name": "analyze"}],
        "output": {"mode": mode},
    }
    if feedback is not None:
        data["feedback_sections"] = feedback
    return AgentManifest.model_validate(data)


class _Agent(AgentProtocol):
    agent_id = "stub-v1"
    display_name = "stub"
    description = "d"


# --------------------------- report generation -------------------------------


def test_generic_document_prefers_report_html():
    run = SimpleNamespace(run_number="VITA-9")
    doc = _generic_document(run, {"a": 1}, "<h2>agent html</h2>")
    assert "<h2>agent html</h2>" in doc
    assert "VITA-9" in doc


def test_generic_document_renders_structured_sections():
    run = SimpleNamespace(run_number="VITA-9")
    doc = _generic_document(run, {"root_cause": {"summary": "x"}}, None)
    assert "Root Cause" in doc
    assert "&quot;summary&quot;" in doc


def test_generic_document_escapes_content():
    run = SimpleNamespace(run_number="<script>alert(1)</script>")
    doc = _generic_document(run, {"k": "<img onerror=x>"}, None)
    assert "<script>" not in doc
    assert "<img" not in doc


def test_generic_document_empty_structured_result_renders():
    """A completed no-findings run persists {} — that is exportable
    content, not an error (Codex P2 on PR #27)."""
    run = SimpleNamespace(run_number="VITA-9")
    doc = _generic_document(run, {}, None)
    assert "No result content available" in doc


def test_vita_render_report_document_runs_agent_template():
    import agents.vita_v1.agent as vita_module

    run = SimpleNamespace(
        run_number="VITA-7",
        vendor_a_name="A",
        vendor_b_name="B",
        logs_a="",
        logs_b="",
        use_case="u",
        problem_statement="p",
        impact_statement=None,
        severity=None,
        created_at=None,
    )
    structured = {
        "refined_problem": {"refined_problem_statement": "rp"},
        "works_cited_a": [{"id": 1, "title": "Doc", "url": "https://d.example"}],
        "works_cited_b": [],
        "skills_cited": [],
        "resolution_plan": {"mitigation": {"text": "Do the thing [1]"}},
        "followup_questions": [],
    }
    html = vita_module.VitaAgent().render_report_document(run, structured)
    assert html is not None
    assert "rp" in html
    # The linkifier now lives in the agent — [1] became an anchor.
    assert 'href="https://d.example"' in html


def test_protocol_default_render_report_document_is_none():
    assert _Agent().render_report_document(SimpleNamespace(), {"x": 1}) is None


# --------------------------- run detail output fields -----------------------


def _run(agent_id="stub-v1"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        run_number="C-1",
        vendor_a_name=None,
        vendor_a_product=None,
        vendor_a_feature=None,
        vendor_a_observation=None,
        vendor_b_name=None,
        vendor_b_product=None,
        vendor_b_feature=None,
        vendor_b_observation=None,
        title=None,
        problem_statement=None,
        severity=None,
        status="complete",
        created_at=None,
        updated_at=None,
        use_case=None,
        impact_statement=None,
        agent_id=agent_id,
        user_inputs={},
        error_code=None,
        error_detail=None,
    )


def _snapshot(structured):
    return SimpleNamespace(analysis=None, structured_data=structured)


def test_structured_mode_exposes_structured_output():
    registry.register(_Agent(), _manifest("stub-v1", mode="structured"))
    fields = build_run_detail_fields(_run(), _snapshot({"answer": 42}))
    assert fields["output_mode"] == "structured"
    assert fields["structured_output"] == {"answer": 42}


def test_detail_carries_run_scoped_feedback_sections():
    """The UI renders exactly what POST /feedback accepts — resolved
    server-side per run, empty when the agent is unresolvable (Codex P2
    on PR #27: no guessed fallback control that can only 422)."""
    registry.register(
        _Agent(),
        _manifest("stub-v1", feedback=[{"id": "accuracy", "label": "Accuracy"}]),
    )
    fields = build_run_detail_fields(_run(), _snapshot(None))
    assert fields["feedback_sections"] == [{"id": "accuracy", "label": "Accuracy"}]
    orphan = build_run_detail_fields(_run("gone-v1"), _snapshot(None))
    assert orphan["feedback_sections"] == []


def test_html_report_mode_keeps_structured_output_private():
    registry.register(_Agent(), _manifest("stub-v1", mode="html_report"))
    fields = build_run_detail_fields(_run(), _snapshot({"answer": 42}))
    assert fields["output_mode"] == "html_report"
    assert fields["structured_output"] is None


def test_unknown_agent_defaults_html_report():
    fields = build_run_detail_fields(_run("gone-v1"), _snapshot({"answer": 42}))
    assert fields["output_mode"] == "html_report"
    assert fields["structured_output"] is None


def test_detail_carries_the_error_code_and_the_chassis_sentence_never_the_detail():
    """Blueprint S7: the customer detail says WHY in the chassis's own
    words — a code and its sentence. The operator text on the row is not
    in these shared fields at all; the admin handler layers it on."""
    registry.register(_Agent(), _manifest("stub-v1", mode="structured"))
    run = _run()
    run.status = "error"
    run.error_code = "backend_restarted"
    run.error_detail = "backend restarted during phase 'analyze'"
    fields = build_run_detail_fields(run, _snapshot(None))
    assert fields["error_code"] == "backend_restarted"
    assert "restarted" in fields["error_message"] and "Run it again" in fields["error_message"]
    assert "error_detail" not in fields
    ok = build_run_detail_fields(_run(), _snapshot(None))
    assert ok["error_code"] is None and ok["error_message"] is None


# --------------------------- feedback validation -----------------------------


def _feedback_app(run) -> tuple[TestClient, SimpleNamespace]:
    tenant_id = run.tenant_id
    user = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=tenant_id, email="u@example.com", role="customer"
    )
    db = SimpleNamespace(added=[])

    async def _get(model, pk):
        return run if pk == run.id else None

    db.get = _get

    def _add(obj):
        obj.id = uuid.uuid4()
        db.added.append(obj)

    db.add = _add

    async def _flush():
        return None

    db.flush = _flush

    async def _db_dep():
        yield db

    async def _user_dep(request: Request):
        request.state.tenant_id = tenant_id
        return user

    app = FastAPI()
    app.include_router(feedback_router.router)
    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep
    return TestClient(app), db


def _feedback_run():
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        deleted_at=None,
        trace_id=None,
        run_number="C-1",
        phase2_span_id=None,
        agent_id="stub-v1",
    )


def test_feedback_accepts_manifest_declared_section(monkeypatch):
    registry.register(
        _Agent(),
        _manifest(
            "stub-v1",
            feedback=[{"id": "mitigation", "label": "Immediate mitigation"}],
        ),
    )
    run = _feedback_run()
    client, db = _feedback_app(run)
    monkeypatch.setattr(feedback_router, "log_audit", _noop_audit)
    r = client.post(
        "/feedback",
        json={
            "run_id": str(run.id),
            "section_type": "mitigation",
            "rating": "positive",
        },
    )
    assert r.status_code == 201, r.text
    assert db.added[0].section_type == "mitigation"


def test_feedback_rejects_undeclared_section(monkeypatch):
    registry.register(
        _Agent(), _manifest("stub-v1", feedback=[{"id": "overall", "label": "Overall"}])
    )
    run = _feedback_run()
    client, db = _feedback_app(run)
    monkeypatch.setattr(feedback_router, "log_audit", _noop_audit)
    r = client.post(
        "/feedback",
        json={
            "run_id": str(run.id),
            "section_type": "works_cited_a",
            "rating": "positive",
        },
    )
    assert r.status_code == 422
    assert "works_cited_a" in r.text
    assert db.added == []


async def _noop_audit(*a, **k):
    return None
