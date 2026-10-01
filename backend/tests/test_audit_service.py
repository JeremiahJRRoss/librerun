"""Unit tests for audit_service. Focuses on log_schema_drift shape; broader
integration tests against a live DB live in the E2E test suite."""
from __future__ import annotations

from uuid import uuid4

import pytest

from agents.vita_v1.normalizers import SchemaDriftReport
from app.services.audit_service import SCHEMA_DRIFT_ACTION_TYPE, log_schema_drift


class _FakeSession:
    def __init__(self) -> None:
        self.added: list = []
        self.flushed = 0

    def add(self, entry) -> None:
        self.added.append(entry)

    async def flush(self) -> None:
        self.flushed += 1


@pytest.mark.asyncio
async def test_log_schema_drift_noop_on_empty_reports():
    session = _FakeSession()
    await log_schema_drift(session, uuid4(), uuid4(), reports=[])
    assert session.added == []
    assert session.flushed == 0


@pytest.mark.asyncio
async def test_log_schema_drift_writes_single_row_with_run_id_and_reports():
    session = _FakeSession()
    tenant_id = uuid4()
    run_id = uuid4()
    reports = [
        SchemaDriftReport(
            step_id="refine_problem_statement",
            provider="openai",
            model="gpt-4o",
            drift_type="object_to_string_flattening",
            details={"field": "problem_statement"},
        ),
        SchemaDriftReport(
            step_id="refine_problem_statement",
            provider="openai",
            model="gpt-4o",
            drift_type="dict_to_list_flattening",
            details={"field": "research_focus_areas"},
        ),
    ]

    await log_schema_drift(session, tenant_id, run_id, reports)

    assert len(session.added) == 1
    entry = session.added[0]
    assert entry.action_type == SCHEMA_DRIFT_ACTION_TYPE
    assert entry.tenant_id == tenant_id
    assert entry.detail["run_id"] == str(run_id)
    assert len(entry.detail["reports"]) == 2
    assert entry.detail["reports"][0]["drift_type"] == "object_to_string_flattening"
    assert entry.detail["reports"][1]["details"]["field"] == "research_focus_areas"
    assert session.flushed == 1
