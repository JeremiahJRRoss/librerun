"""drop the VITA-shaped CHECK on case_feedback.section_type

Revision ID: 0011_manifest_feedback_sections
Revises: 0010_generic_intake_nullable
Create Date: 2026-08-16 00:00:00.000000

Blueprint B9: feedback sections come from each agent's manifest, so the
baseline CHECK constraint enumerating VITA's section vocabulary
(refined_problem / works_cited_* / skills_cited / mitigation /
resolution / avoidance / followup_questions) can no longer be a database
invariant. Validation moves to the API layer, which checks submissions
against the case agent's declared ``feedback_sections``.

Existing rows are untouched. Idempotent in line with migrations 003-010.
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_manifest_feedback_sections"
down_revision = "0010_generic_intake_nullable"
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
    if _already_renamed("case_feedback", "run_feedback"):
        return
    op.execute(
        "ALTER TABLE case_feedback DROP CONSTRAINT IF EXISTS "
        "case_feedback_section_type_check"
    )


def downgrade() -> None:
    # Recreate the baseline vocabulary CHECK. Rows written by other
    # agents' manifests since the upgrade would violate it, so purge-free
    # downgrade is only safe on VITA-only databases.
    op.execute(
        """
        ALTER TABLE case_feedback ADD CONSTRAINT case_feedback_section_type_check
        CHECK (section_type IN (
            'refined_problem', 'works_cited_a', 'works_cited_b', 'skills_cited',
            'mitigation', 'resolution', 'avoidance', 'followup_questions'
        ))
        """
    )
