"""Blueprint S1 (decision L18): the chassis speaks ``run``.

* No route says ``/cases`` any more. The alias was never public, and the
  strangler rule retires the old path in the same PR that lands the new
  noun — so ``/cases`` answers 404 on the real app while ``/runs`` serves
  everything it used to and is what the OpenAPI schema documents.
* Responses carry ``run_id`` / ``run_number`` as authoritative and the
  pre-1.0 ``case_id`` / ``case_number`` as identical duplicates for one
  release, marked deprecated in the schema and removed at v1.1. The
  models fill the duplicates themselves, so a handler cannot forget.
* ``case_id`` is still accepted where a run id is posted.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from starlette.testclient import TestClient

from app.main import app
from app.routers import admin as admin_router
from app.routers import feedback as feedback_router
from app.routers import files as files_router
from app.routers import reports as reports_router
from app.routers import runs as runs_router
from app.schemas.admin import AdminRunDetail
from app.schemas.feedback import FeedbackSubmit
from app.schemas.run import RunCreatedResponse, RunDetail, RunListResponse, RunSummary

_ROUTERS = [
    runs_router.router,
    files_router.router,
    reports_router.router,
    admin_router.router,
    feedback_router.router,
]


def _api_routes():
    for router in _ROUTERS:
        yield from (r for r in router.routes if hasattr(r, "path"))


def test_no_route_speaks_case():
    paths = [r.path for r in _api_routes()]
    assert paths, "expected API routes"
    assert not [p for p in paths if "case" in p], "old vocabulary survived in a path"
    assert any(p == "/runs" for p in paths)
    assert any(p == "/runs/{run_id}" for p in paths)


def test_every_runs_route_is_documented_and_no_cases_path_exists():
    spec_paths = set(app.openapi()["paths"].keys())
    assert not any("/cases" in p for p in spec_paths)
    for wanted in (
        "/api/v1/runs",
        "/api/v1/runs/{run_id}",
        "/api/v1/runs/{run_id}/progress",
        "/api/v1/runs/{run_id}/files",
        "/api/v1/runs/{run_id}/report",
        "/api/v1/admin/runs/{run_id}",
    ):
        assert wanted in spec_paths, f"{wanted} missing from the OpenAPI paths"
    hidden = [r.path for r in _api_routes() if r.include_in_schema is False]
    assert hidden == [], f"schema-hidden routes are the retired alias pattern: {hidden}"


def test_cases_returns_404_on_the_real_app():
    client = TestClient(app)  # no lifespan needed: routing decides before auth
    rid = uuid.uuid4()
    for method, path in (
        ("GET", "/api/v1/cases"),
        ("POST", "/api/v1/cases"),
        ("GET", f"/api/v1/cases/{rid}"),
        ("GET", f"/api/v1/cases/{rid}/progress"),
        ("POST", f"/api/v1/cases/{rid}/approve"),
        ("GET", f"/api/v1/cases/{rid}/files"),
        ("GET", f"/api/v1/cases/{rid}/report/embedded"),
        ("GET", f"/api/v1/admin/cases/{rid}"),
    ):
        r = client.request(method, path, headers={"Authorization": "Bearer nope"})
        assert r.status_code == 404, f"{method} {path} -> {r.status_code}"


def test_responses_emit_run_fields_with_identical_deprecated_duplicates():
    rid = uuid.uuid4()
    now = datetime.now(timezone.utc)

    created = RunCreatedResponse(run_id=rid, run_number="RUN-1000", status="submitted").model_dump()
    assert created["run_id"] == created["case_id"] == rid
    assert created["run_number"] == created["case_number"] == "RUN-1000"

    common = dict(
        run_number="VITA-1042", problem_summary="", severity=None, status="complete",
        created_at=now, updated_at=now,
    )
    summary = RunSummary(id=rid, **common).model_dump()
    assert summary["run_number"] == summary["case_number"] == "VITA-1042"
    detail = RunDetail(id=rid, **common).model_dump()
    assert detail["run_number"] == detail["case_number"] == "VITA-1042"
    admin = AdminRunDetail(id=rid, **common).model_dump()
    assert admin["run_number"] == admin["case_number"] == "VITA-1042"

    listing = RunListResponse(runs=[RunSummary(id=rid, **common)], total=1, page=1, per_page=20).model_dump()
    assert "runs" in listing and "cases" not in listing


def test_deprecated_duplicates_are_marked_in_the_openapi_schema():
    schemas = app.openapi()["components"]["schemas"]
    for model, fields in (
        ("RunCreatedResponse", ("case_id", "case_number")),
        ("RunSummary", ("case_number",)),
        ("RunDetail", ("case_number",)),
        ("AdminRunDetail", ("case_number",)),
    ):
        for name in fields:
            prop = schemas[model]["properties"][name]
            assert prop.get("deprecated") is True, f"{model}.{name} not marked deprecated"
            assert "v1.1" in prop.get("description", ""), f"{model}.{name} says nothing about removal"
        for name in ("run_number",):
            assert "deprecated" not in schemas[model]["properties"][name]


def test_case_id_is_still_accepted_on_input_for_one_release():
    rid = uuid.uuid4()
    base = {"section_type": "overall", "rating": "positive"}
    assert FeedbackSubmit.model_validate({"run_id": str(rid), **base}).run_id == rid
    assert FeedbackSubmit.model_validate({"case_id": str(rid), **base}).run_id == rid
    schema = FeedbackSubmit.model_json_schema()
    assert "run_id" in schema["properties"]
    assert "case_id" in schema["properties"]["run_id"]["description"]


def test_agent_input_keeps_a_read_alias_for_the_old_spelling():
    """The Python agent contract: ``AgentInput.run_id`` is the field;
    ``inp.case_id`` still reads for one release (removed at v1.1)."""
    from app.agents.protocol import AgentInput

    rid = uuid.uuid4()
    inp = AgentInput(run_id=rid, tenant_id=uuid.uuid4(), user_inputs={})
    assert inp.case_id == rid
