"""genericize case model for pluggable agents

Revision ID: 0006_genericize_case_model
Revises: 0005_case_phase2_span_id
Create Date: 2026-04-22 17:00:00.000000

Phase 3 of the shell + pluggable-agent refactor. Adds generic columns
alongside the existing VITA-specific ones so the shell can start
persisting agent-neutral data while the legacy columns keep powering
the old code paths. Nothing is removed here; Phase 6 drops the
VITA-specific columns after the cutover.

- ``cases.agent_id``: which agent owns the case (``vita-v1`` by default
  so legacy rows keep flowing through VITA after the migration).
- ``cases.user_inputs``: JSON form payload captured from the agent's
  input schema; replaces the per-wizard scalar columns for new agents.
- ``case_snapshots.analysis``: agent-neutral Phase 1 output (what
  ``AgentProtocol.analyze`` returns as ``display``).
- ``case_snapshots.structured_data``: agent-neutral Phase 2 structured
  result.
- ``case_snapshots.report_html``: pre-rendered report HTML, so the
  shell's report endpoint can serve it without re-running a Jinja
  template.

Idempotent via ``IF NOT EXISTS`` in line with migrations 003–005.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0006_genericize_case_model"
down_revision = "0005_case_phase2_span_id"
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
        "ALTER TABLE cases "
        "ADD COLUMN IF NOT EXISTS agent_id VARCHAR(50) DEFAULT 'vita-v1'"
    )
    op.execute(
        "ALTER TABLE cases ADD COLUMN IF NOT EXISTS user_inputs JSONB"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_cases_agent_id ON cases (agent_id)"
    )

    op.execute(
        "ALTER TABLE case_snapshots ADD COLUMN IF NOT EXISTS analysis JSONB"
    )
    op.execute(
        "ALTER TABLE case_snapshots ADD COLUMN IF NOT EXISTS structured_data JSONB"
    )
    op.execute(
        "ALTER TABLE case_snapshots ADD COLUMN IF NOT EXISTS report_html TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE case_snapshots DROP COLUMN IF EXISTS report_html")
    op.execute("ALTER TABLE case_snapshots DROP COLUMN IF EXISTS structured_data")
    op.execute("ALTER TABLE case_snapshots DROP COLUMN IF EXISTS analysis")

    op.execute("DROP INDEX IF EXISTS ix_cases_agent_id")
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS user_inputs")
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS agent_id")
