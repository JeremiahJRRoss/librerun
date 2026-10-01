"""one trace per run: the root span's W3C pair replaces the phase-1 pointers

Revision ID: 0013_run_root_trace
Revises: 0012_run_vocabulary
Create Date: 2026-09-11 00:00:00.000000

Blueprint S4 (promise 3, "one tree from intake"): the submission request
opens the run's root span and persists its context on the row as the
serialized W3C pair, ``root_traceparent`` (55 chars: version, trace id,
root span id, flags) and ``root_tracestate`` (up to 512 bytes of vendor
state an upstream caller sent). Every phase span restores the pair as
its remote parent, so a gated run is one trace before and after the
approval and nothing links across traces any more.

That retires the ``phase1_trace_id`` / ``phase1_span_id`` pointer pair
(migration 007; the B10 rename debt), which the next phase's OTel Link
used to read. The two columns are **renamed and retyped in place**
rather than dropped and added: a rename keeps the column's position, so
a database upgraded through the chain and one created fresh from
``backend/db/schema.sql`` dump identically (the parity workflow diffs the
two) — both ways, since the downgrade renames back. Their values are
discarded in the retype (``USING NULL``): a bare hex trace id is not a
``traceparent`` and a span id is not a ``tracestate``, and the only
reader of the old values, the link, is gone. The ``phase1_span_id``
index goes with the column (a trace state is never looked up).

Guarded and idempotent, in the pattern of 002–012: a fresh install loads
the head schema file, which already carries the new columns, and every
statement here no-ops on it.
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_run_root_trace"
down_revision = "0012_run_vocabulary"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    return (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t "
                "AND column_name = :c)"
            ),
            {"t": table, "c": column},
        )
        .scalar()
        is True
    )


def _retype_in_place(table: str, old: str, new: str, new_type: str) -> None:
    """Rename ``old`` to ``new`` and give it ``new_type``, discarding the
    values; a no-op when ``new`` already exists (fresh install, re-run)."""
    if _column_exists(table, new):
        return
    op.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
    op.execute(
        f"ALTER TABLE {table} ALTER COLUMN {new} TYPE {new_type} "
        f"USING NULL::{new_type}"
    )


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_runs_phase1_span_id")
    _retype_in_place("runs", "phase1_trace_id", "root_traceparent", "VARCHAR(55)")
    _retype_in_place("runs", "phase1_span_id", "root_tracestate", "VARCHAR(512)")


def downgrade() -> None:
    _retype_in_place("runs", "root_traceparent", "phase1_trace_id", "VARCHAR(64)")
    _retype_in_place("runs", "root_tracestate", "phase1_span_id", "VARCHAR(16)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_runs_phase1_span_id ON runs (phase1_span_id)"
    )
