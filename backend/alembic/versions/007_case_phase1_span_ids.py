"""add phase1_trace_id + phase1_span_id to cases

Revision ID: 0007_case_phase1_span_ids
Revises: 0006_genericize_case_model
Create Date: 2026-05-06 00:00:00.000000

Phase 2 carries an OTEL ``Link`` back to phase 1's root span so a viewer
operator can navigate between the two traces in one click. Building the
Link needs phase 1's trace_id + span_id at the moment phase 2's span is
created — but ``run_phase2`` overwrites ``Case.trace_id`` with the
phase-2 trace as soon as its own span starts (so feedback annotations
target the phase-2 root), so we keep a separate ``phase1_trace_id``
column rather than re-deriving it.

``ADD COLUMN IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` keep this
idempotent in line with migrations 003/004/005.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0007_case_phase1_span_ids"
down_revision = "0006_genericize_case_model"
branch_labels = None
depends_on = None


def _already_renamed(old: str, new: str) -> bool:
    """Migration 012 (blueprint S1) renamed ``old`` to ``new``. A database
    created from the head schema file never had ``old`` and already
    carries this migration's result under ``new`` — skip. A database with
    NEITHER is broken, and the statements below fail loudly on it, as
    they always did."""
    return bool(
        op.get_bind()
        .execute(
            sa.text("SELECT to_regclass(:old) IS NULL AND to_regclass(:new) IS NOT NULL"),
            {"old": f"public.{old}", "new": f"public.{new}"},
        )
        .scalar()
    )


def upgrade() -> None:
    if _already_renamed("cases", "runs"):
        return
    op.execute(
        "ALTER TABLE cases ADD COLUMN IF NOT EXISTS phase1_trace_id VARCHAR(64)"
    )
    op.execute(
        "ALTER TABLE cases ADD COLUMN IF NOT EXISTS phase1_span_id VARCHAR(16)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_cases_phase1_span_id "
        "ON cases (phase1_span_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_cases_phase1_span_id")
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS phase1_span_id")
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS phase1_trace_id")
