"""add trace_id column + index to cases

Revision ID: 0004_case_trace_id
Revises: 0003_drift_action_type
Create Date: 2026-04-21 23:00:00.000000

Blueprint B.1 Turn 2 persists the OTEL trace_id at phase start so the
admin UI can deep-link into the trace viewer. The SQLAlchemy model was updated to
include ``Case.trace_id`` previously but no Alembic migration carried
the column into the DB schema.

``ADD COLUMN IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` keep this
idempotent: dev DBs created purely from SQLAlchemy metadata already have
the column and the migration no-ops; older DBs bootstrapped from the
shipped SQL baseline gain the column here.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0004_case_trace_id"
down_revision = "0003_drift_action_type"
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
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS trace_id VARCHAR(64)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_cases_trace_id ON cases (trace_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_cases_trace_id")
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS trace_id")
