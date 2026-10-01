"""add cases.current_phase for the manifest-driven phase runner

Revision ID: 0009_case_current_phase
Revises: 0008_drop_seed_admin
Create Date: 2026-08-16 00:00:00.000000

Blueprint B7 makes the phase list data (``agent.yaml`` manifests), so the
chassis needs a durable cursor: which manifest phase most recently ran on
this case. The approve endpoint resumes from the phase *after* it; the
edit endpoint re-runs it. Two-phase agents could derive this from
``status`` alone, but a manifest may declare any number of phases with
gates anywhere, and Redis progress state is not durable.

NULL on rows that predate this migration — the runner treats NULL as
"start of the phase list", which reproduces the pre-B7 behaviour for
in-flight legacy cases.

Idempotent (safe to re-run) in line with migrations 003-008.
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_case_current_phase"
down_revision = "0008_drop_seed_admin"
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
        """
        ALTER TABLE cases
        ADD COLUMN IF NOT EXISTS current_phase VARCHAR(64)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS current_phase")
