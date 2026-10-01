"""relax NOT NULL on VITA-shaped case columns for generic intake

Revision ID: 0010_generic_intake_nullable
Revises: 0009_case_current_phase
Create Date: 2026-08-16 00:00:00.000000

Blueprint B8: ``POST /cases`` accepts a schema-validated payload per
agent, so a case row is no longer guaranteed to carry vendor names, a use
case, or a problem statement — those are VITA vocabulary, lifted from the
payload into the legacy columns only when present (see
``app/services/intake.py``). The columns themselves stay (renaming the
case vocabulary is deferred debt, blueprint B10); they just stop being
mandatory.

Idempotent (safe to re-run) in line with migrations 003-009 — DROP NOT
NULL on an already-nullable column is a no-op.
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_generic_intake_nullable"
down_revision = "0009_case_current_phase"
branch_labels = None
depends_on = None

_COLUMNS = ("vendor_a_name", "vendor_b_name", "use_case", "problem_statement")


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
    for col in _COLUMNS:
        op.execute(f"ALTER TABLE cases ALTER COLUMN {col} DROP NOT NULL")


def downgrade() -> None:
    # Backfill placeholders so re-adding NOT NULL cannot fail on rows
    # created by generic agents.
    for col in _COLUMNS:
        op.execute(f"UPDATE cases SET {col} = '' WHERE {col} IS NULL")
        op.execute(f"ALTER TABLE cases ALTER COLUMN {col} SET NOT NULL")
