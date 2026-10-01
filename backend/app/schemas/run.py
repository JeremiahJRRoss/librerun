from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Response-side shape of the well-known vendor keys. The REQUEST body for
# POST /runs stopped being chassis-defined in blueprint B8 — each agent's
# ``input_schema()`` is the request contract (the chassis-defined create
# body is gone), so vendor fields only appear when the payload carried them.
class VendorInput(BaseModel):
    name: str
    product: str | None = None
    feature: str | None = None
    observation: str | None = None


# Blueprint S1 (decision L18): the platform noun is ``run``. ``run_id`` and
# ``run_number`` are authoritative; the pre-1.0 ``case_id`` / ``case_number``
# ride along as identical duplicates for ONE release so nothing breaks the
# day the noun changes, and are removed at v1.1. They are marked deprecated
# in the OpenAPI schema, never read by the chassis, and filled by the
# models themselves so a handler cannot forget them.
_DEPRECATED_NOTE = (
    "Deprecated duplicate of `{new}` (LibreRun speaks `run`, blueprint S1 / "
    "decision L18): always identical to `{new}`, emitted for one release and "
    "removed at v1.1. Read `{new}` instead."
)


def _deprecated_duplicate(new: str) -> Any:
    return Field(
        default=None,
        description=_DEPRECATED_NOTE.format(new=new),
        json_schema_extra={"deprecated": True},
    )


class RunCreatedResponse(BaseModel):
    run_id: UUID
    run_number: str
    status: str
    case_id: UUID | None = _deprecated_duplicate("run_id")
    case_number: str | None = _deprecated_duplicate("run_number")

    @model_validator(mode="after")
    def _fill_deprecated_duplicates(self):
        if self.case_id is None:
            self.case_id = self.run_id
        if self.case_number is None:
            self.case_number = self.run_number
        return self


_DEPRECATED_FIELD_NOTE = (
    "Deprecated (LibreRun blueprint S2): `{new}` replaces it; kept identical "
    "for one release and removed at v1.1. Read `{new}` instead."
)


def _deprecated_field(new: str, **kwargs: Any) -> Any:
    return Field(
        description=_DEPRECATED_FIELD_NOTE.format(new=new),
        json_schema_extra={"deprecated": True},
        **kwargs,
    )


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_number: str
    # The run's title (blueprint S2): the manifest's ``ui.list.title_path``
    # resolved at intake, shortened for the list. What the dashboard shows
    # and search matches, for every agent.
    title: str | None = None
    # Nullable since migration 010: generic agents may not use vendor
    # vocabulary at all.
    vendor_a_name: str | None = None
    vendor_b_name: str | None = None
    problem_summary: str = _deprecated_field("title", default="")
    severity: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    agent_id: str | None = None
    case_number: str | None = _deprecated_duplicate("run_number")

    @model_validator(mode="after")
    def _fill_deprecated_duplicates(self):
        if self.case_number is None:
            self.case_number = self.run_number
        return self


class RefinedStatement(BaseModel):
    refined_problem_statement: str
    key_signals: list[str]
    suspected_root_causes: list[str]
    research_focus_areas: list[str]


class RunDetail(BaseModel):
    id: UUID
    run_number: str
    # The run's title, in full (blueprint S2) — see RunSummary.title.
    title: str | None = None
    # The string the approval view shows for the parked phase output —
    # the manifest's ``ui.approval.summary_path`` resolved, or the first
    # string in the output. None until a phase has parked.
    approval_summary: str | None = None
    # All legacy demo-agent-shaped fields are optional (migration 010) —
    # present only when the agent's payload carried the well-known keys.
    # The full payload is always in ``user_inputs``.
    vendor_a: VendorInput | None = None
    vendor_b: VendorInput | None = None
    vendor_a_name: str | None = None
    vendor_b_name: str | None = None
    problem_summary: str = _deprecated_field("title", default="")
    severity: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    use_case: str | None = None
    problem_statement: str | None = _deprecated_field("title", default=None)
    impact_statement: str | None = None
    refined_problem_statement: str | None = _deprecated_field("approval_summary", default=None)
    agent_id: str | None = None
    user_inputs: dict | None = None
    # How the agent's final result renders (from its manifest, blueprint
    # B9): ``html_report`` ships rendered HTML via the report endpoints;
    # ``structured`` exposes the payload here for generic rendering.
    output_mode: str = "html_report"
    structured_output: dict | None = None
    # The feedback sections POST /feedback accepts for this run (from the
    # agent's manifest) — the results view renders exactly these, and
    # hides the panel when empty.
    feedback_sections: list[dict] = Field(default_factory=list)
    # Deep link into the configured trace viewer (blueprint B5, L8);
    # None when tracing is off, the viewer is off, or no trace exists.
    trace_url: str | None = None
    # Why the run ended ``error`` (blueprint S7, migration 017): a value of
    # the chassis's closed vocabulary (``app.services.run_errors``) and the
    # sentence the chassis wrote for it. Never the agent's own failure
    # text — that is operator-facing by the Run Contract and lives on
    # ``AdminRunDetail.error_detail`` only. Both None unless the run errored.
    error_code: str | None = None
    error_message: str | None = None
    case_number: str | None = _deprecated_duplicate("run_number")

    @model_validator(mode="after")
    def _fill_deprecated_duplicates(self):
        if self.case_number is None:
            self.case_number = self.run_number
        return self


class RunListResponse(BaseModel):
    runs: list[RunSummary]
    total: int
    page: int
    per_page: int


class ApprovalResponse(BaseModel):
    """What the approval view renders for a run (blueprint S2, generic
    across agents): the parked phase, its full output, and the summary
    string the manifest's ``ui.approval.summary_path`` names. While the
    run is still working the endpoint answers 202 with ``status`` only."""

    status: str
    phase: str | None = None
    payload: dict | None = None
    summary: str | None = None


class EditStatementRequest(BaseModel):
    edited_statement: str


class StepProgress(BaseModel):
    step_id: str
    status: Literal["pending", "running", "complete", "skipped", "error"]
    duration_ms: int | None = None
    detail: str | None = None
    # Blueprint S4a (D13): the model that actually answered this step,
    # written by the gateway. The point of showing it is that an admin
    # who changes a step's model in the UI can see the change take
    # effect on the next run without anything being restarted. It comes
    # from the platform's configuration or the provider's reply, never
    # from agent text.
    model: str | None = None


class ProgressResponse(BaseModel):
    phase: int
    steps: list[StepProgress]
    # Manifest phase currently (or most recently) running — e.g. the demo agent's
    # ``analyze`` / ``investigate``. None on legacy rows from before the
    # manifest-driven runner (blueprint B7).
    phase_name: str | None = None
