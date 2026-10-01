"""add phase2_span_id to cases

Revision ID: 0005_case_phase2_span_id
Revises: 0004_case_trace_id
Create Date: 2026-04-21 23:30:00.000000

Blueprint B.1 Turn 3 posted user feedback as vendor annotations (path
retired at LibreRun B5). That
annotation API is indexed by OTEL span_id (not trace_id), so we persist
the Phase 2 root span_id alongside the trace_id captured in Turn 2.
Feedback targets this root span so reviewers see the annotation on the
trace summary.

``ADD COLUMN IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` keep this
idempotent in line with migrations 003 and 004.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0005_case_phase2_span_id"
down_revision = "0004_case_trace_id"
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
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS phase2_span_id VARCHAR(16)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_cases_phase2_span_id "
        "ON cases (phase2_span_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_cases_phase2_span_id")
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS phase2_span_id")
