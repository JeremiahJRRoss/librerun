from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Text, func, DateTime
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RunSnapshot(Base):
    __tablename__ = "run_snapshots"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Generic agent outputs (migration 006). Agents write their full
    # structured result here. The seven demo-agent-shaped mirror columns that
    # predated Phase 6 still exist in the database but are unmapped since
    # blueprint B9 — nothing wrote or read them after Phase 6, and the
    # chassis no longer knows their vocabulary (see the 001 baseline SQL
    # for their names). Dropping the physical columns is recorded
    # B10-era debt.
    analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    structured_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    report_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
