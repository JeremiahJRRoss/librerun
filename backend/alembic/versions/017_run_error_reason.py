"""a run says why it ended in error

Revision ID: 0017_run_error_reason
Revises: 0016_pii_detector_degraded
Create Date: 2026-09-22 00:00:00.000000

Blueprint S7 (gap H16, and the run page's sanitized error). A run that
ended ``error`` said nothing else: ``_mark_error`` wrote the status and
the reason lived in a log line, so the page could only print "Run
ended in error" and a run the backend was restarted under said nothing
at all — it was never marked. The blueprint's S7 text asked for the
reason to land "in the field the row already stores its error text in
— verify"; verified, and there was none. Two columns, because the two
readers are different people:

* ``error_code`` — a closed vocabulary the chassis owns
  (``app.services.run_errors``): what KIND of failure. The customer run
  page maps it to a sentence the chassis wrote. Never agent text.
* ``error_detail`` — the operator-facing text: the agent's own
  ``failed.error``, the exception, the deadline, the restart's phase.
  The Run Contract makes ``failed.error`` operator-facing ("surfaced to
  operators (not end users)"), so it is served on the admin run view
  only, and it is redacted before it is stored like every other string
  an agent can influence (CLAUDE.md: unredacted content never touches
  the database).

Both nullable, appended after ``current_phase`` — the position
``backend/db/schema.sql`` gives them, so a database upgraded through the
chain and one created fresh dump identically. Idempotent on the head
snapshot in the pattern of 002–016: ``ADD COLUMN IF NOT EXISTS`` no-ops
where the snapshot already carries the columns.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "0017_run_error_reason"
down_revision = "0016_pii_detector_degraded"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS error_code VARCHAR(40)")
    op.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS error_detail TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS error_detail")
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS error_code")
