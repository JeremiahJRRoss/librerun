"""the pii_detector_degraded audit action type

Revision ID: 0016_pii_detector_degraded
Revises: 0015_drop_dead_session_timeout
Create Date: 2026-09-21 00:00:00.000000

Blueprint S4c (gap H15). When an operator sets
``LIBRERUN_PII_ALLOW_DEGRADED=true`` the chassis keeps redacting with the
regex stages alone, and every tenant it served that way gets an audit row
saying so — ``action_type="pii_detector_degraded"``. Without this the
insert raises ``CheckViolationError`` and the opt-out's one durable
trace is the thing that fails.

S4c was admitted under L14 as "no schema" and this is the exception,
recorded in the blueprint's §12: the batch's Accept list requires the
audit row, ``activity_audit_log.action_type`` is a CHECK-constrained
enumeration, and widening that enumeration by one value is the smallest
change that makes the row insertable. No table, no column, no index, no
data rewrite — the same shape migration 0003 had when ``llm_schema_drift``
was added and its migration was missed.

Idempotent on the head snapshot, like 0003-0015: ``DROP CONSTRAINT IF
EXISTS`` then ``ADD CONSTRAINT`` with the full list, which
``backend/db/schema.sql`` already spells the same way and in the same
textual order, so ``scripts/migration_parity.sh`` sees no diff.

The downgrade narrows the list back, and — as with 0003 — it FAILS if
rows carrying the value are present, because ``ADD CONSTRAINT ... CHECK``
validates existing rows. That is the correct loud behaviour: an operator
going back past this revision must first decide what happens to the
record that their deployment ran degraded. ``DELETE FROM
activity_audit_log WHERE action_type = 'pii_detector_degraded'`` is the
decision, not something a migration should take on their behalf.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "0016_pii_detector_degraded"
down_revision = "0015_drop_dead_session_timeout"
branch_labels = None
depends_on = None


# The vocabulary after this migration: 0012's list plus the one value.
# Order is the CHECK's textual order — backend/db/schema.sql lists the
# same values in the same order so the two dumps match.
_ACTION_TYPES_016 = (
    "sign_in",
    "sign_out",
    "case_create",
    "case_update",
    "case_delete",
    "blocked_request",
    "config_change",
    "vendor_registry_edit",
    "role_change",
    "session_revoke",
    "auth_config_change",
    "llm_schema_drift",
    "run_create",
    "run_update",
    "run_delete",
    "pii_detector_degraded",
)

_ACTION_TYPES_015 = tuple(
    a for a in _ACTION_TYPES_016 if a != "pii_detector_degraded"
)


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _replace_action_type_check(values: tuple[str, ...]) -> None:
    op.execute(
        "ALTER TABLE activity_audit_log "
        "DROP CONSTRAINT IF EXISTS activity_audit_log_action_type_check"
    )
    op.execute(
        "ALTER TABLE activity_audit_log "
        "ADD CONSTRAINT activity_audit_log_action_type_check "
        f"CHECK (action_type IN ({_in_list(values)}))"
    )


def upgrade() -> None:
    _replace_action_type_check(_ACTION_TYPES_016)


def downgrade() -> None:
    _replace_action_type_check(_ACTION_TYPES_015)
