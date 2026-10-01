"""extend activity_audit_log action_type CHECK constraint

Revision ID: 0003_drift_action_type
Revises: 0002_app_settings
Create Date: 2026-04-19 05:00:00.000000

The baseline schema shipped with a CHECK constraint
``activity_audit_log_action_type_check`` enumerating the initial set of
action_type values. Package D.1 added ``llm_schema_drift`` as a new
value but this migration was missed (the SQLAlchemy model doesn't
declare the constraint, so it wasn't visible on a plain file read).
Running the pipeline on any case with schema drift therefore raised
``asyncpg.exceptions.CheckViolationError`` and poisoned Phase 2's
transaction.

This migration drops and re-creates the constraint with the full
current list of action types actually used by the codebase (see grep
for ``log_audit(...,"...",...)`` and direct ``action_type=`` usages).

``DROP CONSTRAINT IF EXISTS`` is used so the migration is idempotent
on DBs that never had the constraint (e.g. a fresh dev DB created
purely from SQLAlchemy metadata).
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "0003_drift_action_type"
down_revision = "0002_app_settings"
branch_labels = None
depends_on = None


_ACTION_TYPES_FULL = (
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
)

_ACTION_TYPES_PRE = tuple(a for a in _ACTION_TYPES_FULL if a != "llm_schema_drift")


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE activity_audit_log "
        "DROP CONSTRAINT IF EXISTS activity_audit_log_action_type_check"
    )
    op.execute(
        "ALTER TABLE activity_audit_log "
        "ADD CONSTRAINT activity_audit_log_action_type_check "
        f"CHECK (action_type IN ({_in_list(_ACTION_TYPES_FULL)}))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE activity_audit_log "
        "DROP CONSTRAINT IF EXISTS activity_audit_log_action_type_check"
    )
    op.execute(
        "ALTER TABLE activity_audit_log "
        "ADD CONSTRAINT activity_audit_log_action_type_check "
        f"CHECK (action_type IN ({_in_list(_ACTION_TYPES_PRE)}))"
    )
