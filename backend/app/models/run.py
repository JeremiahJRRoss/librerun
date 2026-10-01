from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import String, Text, ForeignKey, UniqueConstraint, func, DateTime
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, synonym

from app.database import Base


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (UniqueConstraint("tenant_id", "run_number"),)

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    run_number: Mapped[str] = mapped_column(String(20), nullable=False)
    # Legacy demo-agent-shaped columns (nullable since migration 010): populated
    # only when the agent's payload carries the well-known keys — see
    # ``app/services/intake.py::extract_legacy_columns``. The full payload
    # always lives in ``user_inputs``.
    vendor_a_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor_a_product: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor_a_feature: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor_a_observation: Mapped[str | None] = mapped_column(Text, nullable=True)
    vendor_b_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor_b_product: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor_b_feature: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor_b_observation: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs_a: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs_b: Mapped[str | None] = mapped_column(Text, nullable=True)
    use_case: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The run's title (blueprint S2): resolved at intake from the agent
    # manifest's ``ui.list.title_path`` (or the first non-PII string
    # input) for EVERY agent, and what the dashboard lists and searches.
    # Stored in the pre-S2 ``problem_statement`` column, which held the
    # demo agent's problem statement — its manifest points the title at
    # exactly that field, so nothing moved for it. ``problem_statement``
    # stays readable as a synonym for one release (the demo agent's own
    # code and template read it); removed at v1.1.
    title: Mapped[str | None] = mapped_column("problem_statement", Text, nullable=True)
    problem_statement = synonym("title")
    impact_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="submitted")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The run's trace id — the root ``run`` span's, set at submission and
    # never changed by a phase (blueprint S4: one trace per run).
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The root span's W3C context (migration 013): ``traceparent`` (trace
    # id, root span id, flags — always sampled) and the vendor
    # ``tracestate`` an upstream caller sent, if any. Every phase span
    # restores the pair as its remote parent, so the approval gate sits
    # inside one tree. ``app.observability.run_trace`` owns the format.
    root_traceparent: Mapped[str | None] = mapped_column(String(55), nullable=True)
    root_tracestate: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # The final phase's span id — feedback annotations deep-link to it.
    phase2_span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Generic agent columns (migration 006). The chassis always sets
    # agent_id explicitly at create time (blueprint B11 — no agent id is
    # hardcoded here); the column-level SQL DEFAULT that backfilled legacy
    # rows still exists in the database and is recorded rename/cleanup
    # debt with the rest of the case vocabulary (B10 §12 entry).
    agent_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    user_inputs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Name of the manifest phase that most recently ran (migration 009).
    # The generic approval gate reads it to know which phase to resume
    # with once phase lists are data (blueprint B7). NULL on legacy rows —
    # the runner falls back to manifest position for those.
    current_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Why the run ended ``error`` (migration 017, blueprint S7). ``error_code``
    # is a value of the chassis's closed vocabulary
    # (``app.services.run_errors``) that the customer page maps to a
    # sentence; ``error_detail`` is the operator-facing text — the agent's
    # own failure message, the exception, the phase a restart cut short —
    # redacted before it is stored and served on the admin run view only.
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
