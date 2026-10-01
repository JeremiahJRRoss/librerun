"""the chassis speaks `run`: cases -> runs across tables, columns, names

Revision ID: 0012_run_vocabulary
Revises: 0011_manifest_feedback_sections
Create Date: 2026-09-10 00:00:00.000000

Blueprint S1 (locked decision L18): the platform noun is ``run``. This
migration renames the database vocabulary to match — tables, columns,
constraints, indexes and triggers, so that a database upgraded through
the chain and one created fresh from the schema file (``backend/db/schema.sql``)
are identical object for object (the database-parity workflow diffs the
two dumps and must find nothing):

- tables ``cases`` → ``runs``, ``case_files`` → ``run_files``,
  ``case_snapshots`` → ``run_snapshots``, ``case_reports`` →
  ``run_reports``, ``case_feedback`` → ``run_feedback``;
- columns ``case_id`` → ``run_id`` on every child table,
  ``cases.case_number`` → ``runs.run_number``,
  ``tenants.next_case_number`` → ``tenants.next_run_number``;
- every constraint, index and trigger renamed to the name a fresh
  ``CREATE TABLE`` of the new tables would generate;
- ``allocate_case_number(uuid)`` replaced by ``allocate_run_number(uuid)``,
  which reads the new ``tenants.run_prefix`` column (L19: default ``RUN``).
  Tenants that exist at upgrade time are backfilled ``VITA``, so their
  labels keep counting from where they were and nothing renumbers;
- the ``'vita-v1'`` column default on the agent id (migration 006) is
  dropped — the chassis names no agent (blueprint B11);
- the audit ``action_type`` CHECK gains ``run_create`` / ``run_update`` /
  ``run_delete``. Existing rows keep their ``case_*`` values: audit rows
  are history, and history is not rewritten.

Guarded and idempotent, in the pattern of 002–011: a fresh container
loads the head schema file, which already carries every name below, and
then runs ``alembic upgrade head`` from an empty ``alembic_version`` —
every statement here checks the catalog first and no-ops on a database
that is already in the new vocabulary. (The entrypoint only warns when
migrations fail, so a non-idempotent 012 would leave every fresh install
silently stuck behind head.)
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_run_vocabulary"
down_revision = "0011_manifest_feedback_sections"
branch_labels = None
depends_on = None


# (old, new) — order matters only for readability; every rename is guarded.
_TABLES = (
    ("cases", "runs"),
    ("case_files", "run_files"),
    ("case_snapshots", "run_snapshots"),
    ("case_reports", "run_reports"),
    ("case_feedback", "run_feedback"),
)

# (table_after_rename, old_column, new_column)
_COLUMNS = (
    ("runs", "case_number", "run_number"),
    ("run_files", "case_id", "run_id"),
    ("run_snapshots", "case_id", "run_id"),
    ("run_reports", "case_id", "run_id"),
    ("run_feedback", "case_id", "run_id"),
    ("tenants", "next_case_number", "next_run_number"),
)

# (table_after_rename, old_constraint, new_constraint): what PostgreSQL
# auto-named on the baseline CREATE TABLE, and what it would auto-name on
# the new one. Renaming a table renames none of these on its own.
_CONSTRAINTS = (
    ("runs", "cases_pkey", "runs_pkey"),
    ("runs", "cases_tenant_id_fkey", "runs_tenant_id_fkey"),
    ("runs", "cases_user_id_fkey", "runs_user_id_fkey"),
    ("runs", "cases_tenant_id_case_number_key", "runs_tenant_id_run_number_key"),
    ("runs", "cases_severity_check", "runs_severity_check"),
    ("runs", "cases_status_check", "runs_status_check"),
    ("run_files", "case_files_pkey", "run_files_pkey"),
    ("run_files", "case_files_case_id_fkey", "run_files_run_id_fkey"),
    ("run_files", "case_files_tenant_id_fkey", "run_files_tenant_id_fkey"),
    ("run_files", "case_files_vendor_side_check", "run_files_vendor_side_check"),
    ("run_files", "case_files_file_type_check", "run_files_file_type_check"),
    ("run_snapshots", "case_snapshots_pkey", "run_snapshots_pkey"),
    ("run_snapshots", "case_snapshots_case_id_fkey", "run_snapshots_run_id_fkey"),
    ("run_snapshots", "case_snapshots_case_id_key", "run_snapshots_run_id_key"),
    ("run_snapshots", "case_snapshots_tenant_id_fkey", "run_snapshots_tenant_id_fkey"),
    ("run_reports", "case_reports_pkey", "run_reports_pkey"),
    ("run_reports", "case_reports_case_id_fkey", "run_reports_run_id_fkey"),
    ("run_reports", "case_reports_tenant_id_fkey", "run_reports_tenant_id_fkey"),
    ("run_reports", "case_reports_format_check", "run_reports_format_check"),
    ("run_feedback", "case_feedback_pkey", "run_feedback_pkey"),
    ("run_feedback", "case_feedback_case_id_fkey", "run_feedback_run_id_fkey"),
    ("run_feedback", "case_feedback_user_id_fkey", "run_feedback_user_id_fkey"),
    ("run_feedback", "case_feedback_tenant_id_fkey", "run_feedback_tenant_id_fkey"),
    ("run_feedback", "case_feedback_rating_check", "run_feedback_rating_check"),
)

# (old_index, new_index) — the baseline's idx_* and the migrations' ix_*.
_INDEXES = (
    ("idx_cases_tenant", "idx_runs_tenant"),
    ("idx_cases_tenant_user", "idx_runs_tenant_user"),
    ("idx_cases_tenant_status", "idx_runs_tenant_status"),
    ("idx_cases_number", "idx_runs_number"),
    ("idx_case_files_case", "idx_run_files_run"),
    ("idx_case_snapshots_case", "idx_run_snapshots_run"),
    ("idx_case_reports_case", "idx_run_reports_run"),
    ("idx_feedback_case", "idx_feedback_run"),
    ("ix_cases_trace_id", "ix_runs_trace_id"),
    ("ix_cases_phase2_span_id", "ix_runs_phase2_span_id"),
    ("ix_cases_agent_id", "ix_runs_agent_id"),
    ("ix_cases_phase1_span_id", "ix_runs_phase1_span_id"),
)

# (table_after_rename, old_trigger, new_trigger)
_TRIGGERS = (("runs", "tr_cases_updated", "tr_runs_updated"),)

# The audit action_type vocabulary after this migration: 003's list plus
# the run_* spellings. Order is the CHECK's textual order — the schema
# file lists the same values in the same order so the two dumps match.
_ACTION_TYPES_012 = (
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
)
_ACTION_TYPES_011 = tuple(a for a in _ACTION_TYPES_012 if not a.startswith("run_"))

_ALLOCATE_RUN_NUMBER = """
CREATE OR REPLACE FUNCTION allocate_run_number(p_tenant_id UUID)
RETURNS VARCHAR(20) AS $$
DECLARE
    v_num INTEGER;
    v_prefix VARCHAR(12);
BEGIN
    UPDATE tenants
       SET next_run_number = next_run_number + 1,
           updated_at = NOW()
     WHERE id = p_tenant_id
    RETURNING next_run_number - 1, run_prefix INTO v_num, v_prefix;
    RETURN v_prefix || '-' || v_num;
END;
$$ LANGUAGE plpgsql;
"""

# The pre-012 function, verbatim from the baseline schema, for downgrade.
_ALLOCATE_CASE_NUMBER = """
CREATE OR REPLACE FUNCTION allocate_case_number(p_tenant_id UUID)
RETURNS VARCHAR(20) AS $$
DECLARE
    v_num INTEGER;
BEGIN
    UPDATE tenants
       SET next_case_number = next_case_number + 1,
           updated_at = NOW()
     WHERE id = p_tenant_id
    RETURNING next_case_number - 1 INTO v_num;
    RETURN 'VITA-' || v_num;
END;
$$ LANGUAGE plpgsql;
"""


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _rename_table(old: str, new: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('public.{old}') IS NOT NULL
               AND to_regclass('public.{new}') IS NULL THEN
                ALTER TABLE {old} RENAME TO {new};
            END IF;
        END $$;
        """
    )


def _rename_column(table: str, old: str, new: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = '{table}'
                  AND column_name = '{old}'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = '{table}'
                  AND column_name = '{new}'
            ) THEN
                ALTER TABLE {table} RENAME COLUMN {old} TO {new};
            END IF;
        END $$;
        """
    )


def _rename_constraint(table: str, old: str, new: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = '{table}' AND c.conname = '{old}'
            ) AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = '{table}' AND c.conname = '{new}'
            ) THEN
                ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new};
            END IF;
        END $$;
        """
    )


def _rename_index(old: str, new: str) -> None:
    # No-op when `old` is gone; a fresh database has only `new`.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF to_regclass('public.{old}') IS NOT NULL
               AND to_regclass('public.{new}') IS NULL THEN
                ALTER INDEX {old} RENAME TO {new};
            END IF;
        END $$;
        """
    )


def _rename_trigger(table: str, old: str, new: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_trigger g JOIN pg_class t ON t.oid = g.tgrelid
                WHERE t.relname = '{table}' AND g.tgname = '{old}'
            ) AND NOT EXISTS (
                SELECT 1 FROM pg_trigger g JOIN pg_class t ON t.oid = g.tgrelid
                WHERE t.relname = '{table}' AND g.tgname = '{new}'
            ) THEN
                ALTER TRIGGER {old} ON {table} RENAME TO {new};
            END IF;
        END $$;
        """
    )


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


def _table_exists(name: str) -> bool:
    return (
        op.get_bind()
        .execute(sa.text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f"public.{name}"})
        .scalar()
        is True
    )


def upgrade() -> None:
    # Decided up front, before anything is renamed: only a database that
    # still carries the old tables is an UPGRADE, whose tenants predate
    # run prefixes and keep their VITA labels. A database created from the
    # head schema file is fresh; its tenants get the RUN default.
    upgrading_old_database = _table_exists("cases")

    for old, new in _TABLES:
        _rename_table(old, new)
    for table, old, new in _COLUMNS:
        _rename_column(table, old, new)
    for table, old, new in _CONSTRAINTS:
        _rename_constraint(table, old, new)
    for old, new in _INDEXES:
        _rename_index(old, new)
    for table, old, new in _TRIGGERS:
        _rename_trigger(table, old, new)

    op.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS run_prefix VARCHAR(12) "
        "NOT NULL DEFAULT 'RUN' CHECK (run_prefix ~ '^[A-Z][A-Z0-9]{0,11}$')"
    )
    if upgrading_old_database:
        op.execute("UPDATE tenants SET run_prefix = 'VITA'")

    op.execute("DROP FUNCTION IF EXISTS allocate_case_number(UUID)")
    op.execute(_ALLOCATE_RUN_NUMBER)

    op.execute("ALTER TABLE runs ALTER COLUMN agent_id DROP DEFAULT")

    _replace_action_type_check(_ACTION_TYPES_012)


def downgrade() -> None:
    # Audit rows written since the upgrade carry run_* action types the
    # 011 CHECK does not admit; they are mapped back to the case_* spelling
    # (same meaning) so the constraint can be re-added.
    op.execute(
        "UPDATE activity_audit_log SET action_type = 'case_' || substr(action_type, 5) "
        "WHERE action_type IN ('run_create', 'run_update', 'run_delete')"
    )
    _replace_action_type_check(_ACTION_TYPES_011)

    op.execute("ALTER TABLE runs ALTER COLUMN agent_id SET DEFAULT 'vita-v1'")

    op.execute("DROP FUNCTION IF EXISTS allocate_run_number(UUID)")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS run_prefix")

    for table, old, new in _TRIGGERS:
        _rename_trigger(table, new, old)
    for old, new in _INDEXES:
        _rename_index(new, old)
    for table, old, new in _CONSTRAINTS:
        _rename_constraint(table, new, old)
    for table, old, new in _COLUMNS:
        _rename_column(table, new, old)
    for old, new in _TABLES:
        _rename_table(new, old)

    op.execute(_ALLOCATE_CASE_NUMBER)
