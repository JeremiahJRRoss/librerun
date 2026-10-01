#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Migration parity (blueprint S1): the schema file and the migration chain
# must produce the SAME database, both ways.
#
#   fresh   : backend/db/schema.sql, then `alembic upgrade head`,
#             which must change nothing (every migration no-ops on the
#             head snapshot) and must land on head;
#   old     : the pre-S1 baseline (backend/tests/fixtures/schema_pre_s1.sql)
#             seeded with a tenant's cases and their child rows, then
#             `alembic upgrade head` — data intact, labels unchanged
#             (G0: the tenant keeps VITA-, a new tenant gets RUN-);
#   pre012  : the pre-S1 baseline upgraded to 0011, the state a downgrade
#             of 012 must restore exactly.
#
# The proofs are pg_dump diffs: old == fresh after the upgrade, old == pre012
# after `alembic downgrade 0011`, old == fresh again after re-upgrading.
# Any object a migration forgets to rename shows up as a diff line.
#
# Usage: scripts/migration_parity.sh
#   PGURL   base URL of a role that may CREATE DATABASE
#           (default postgresql://librerun:librerun_dev_pw@localhost:5432)
#   PYTHON  interpreter with the backend requirements (default python3)
#   KEEP=1  leave the three databases behind for inspection
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PGURL="${PGURL:-postgresql://librerun:librerun_dev_pw@localhost:5432}"
PYTHON="${PYTHON:-python3}"
SCHEMA="$ROOT/backend/db/schema.sql"
BASELINE="$ROOT/backend/tests/fixtures/schema_pre_s1.sql"
WORK="$(mktemp -d)"
DBS=(lr_parity_fresh lr_parity_old lr_parity_pre012)
DEV_TENANT='a0000000-0000-0000-0000-000000000001'
SEED_USER='b1000000-0000-0000-0000-000000000001'

cleanup() {
    if [ "${KEEP:-0}" != "1" ]; then
        for db in "${DBS[@]}"; do
            psql "$PGURL/postgres" -q -c "DROP DATABASE IF EXISTS $db" >/dev/null 2>&1 || true
        done
        rm -rf "$WORK"
    else
        echo "KEEP=1: databases ${DBS[*]} and dumps in $WORK left in place"
    fi
}
trap cleanup EXIT

url()   { echo "$PGURL/$1"; }
aurl()  { echo "${PGURL/postgresql:/postgresql+asyncpg:}/$1"; }
sql()   { psql "$(url "$1")" -v ON_ERROR_STOP=1 -qtA -c "$2"; }
alembic() {
    # Alembic's exit status is the verdict; the INFO chatter is filtered
    # for display only, after the status has been checked, so a failed
    # migration can never be masked by the filter.
    local db="$1"; shift
    local log="$WORK/alembic-$db.log"
    if ! (cd "$ROOT/backend" && DATABASE_URL="$(aurl "$db")" "$PYTHON" -m alembic "$@" > "$log" 2>&1); then
        echo "ALEMBIC FAILED on $db: alembic $*"
        cat "$log"
        exit 1
    fi
    grep -v '^INFO  \[alembic' "$log" || true
}
dump() {
    # Schema only, owners and grants stripped, comments and SET noise
    # removed; alembic_version is excluded so "fresh before and after
    # alembic" can be compared (the stamp is the only allowed change).
    pg_dump "$(url "$1")" --schema-only --no-owner --no-privileges \
        --exclude-table=alembic_version \
        | grep -vE '^(--|SET |SELECT pg_catalog\.set_config|\\restrict|\\unrestrict|$)' > "$WORK/$2.sql"
}
same() {
    if ! diff -u "$WORK/$1.sql" "$WORK/$2.sql" > "$WORK/$1-vs-$2.diff"; then
        echo "PARITY FAILED: $1 differs from $2:"
        cat "$WORK/$1-vs-$2.diff"
        exit 1
    fi
    echo "  ok: $1 == $2 ($(wc -l < "$WORK/$1.sql") dump lines)"
}
expect() {
    # expect <db> <sql> <value>
    local got; got="$(sql "$1" "$2")"
    if [ "$got" != "$3" ]; then
        echo "ASSERTION FAILED on $1: $2"
        echo "  expected: $3"
        echo "  got:      $got"
        exit 1
    fi
    echo "  ok: $2 -> $got"
}
head_rev() {
    local heads
    heads="$(cd "$ROOT/backend" && "$PYTHON" -m alembic heads)" || { echo "alembic heads failed"; exit 1; }
    echo "$heads" | awk 'NR==1 {print $1}'
}
PRE012="0011_manifest_feedback_sections"

HEAD="$(head_rev)"
echo "alembic head: $HEAD"
for db in "${DBS[@]}"; do
    psql "$(url postgres)" -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS $db" -c "CREATE DATABASE $db"
done

echo "== fresh: head schema file, then alembic upgrade head must be a no-op"
psql "$(url lr_parity_fresh)" -v ON_ERROR_STOP=1 -q -f "$SCHEMA"
dump lr_parity_fresh fresh-before
alembic lr_parity_fresh upgrade head
dump lr_parity_fresh fresh
same fresh-before fresh
expect lr_parity_fresh "SELECT version_num FROM alembic_version" "$HEAD"
expect lr_parity_fresh "SELECT run_prefix FROM tenants WHERE slug = 'dev'" "RUN"
expect lr_parity_fresh "SELECT allocate_run_number('$DEV_TENANT')" "RUN-1000"
expect lr_parity_fresh "SELECT allocate_run_number('$DEV_TENANT')" "RUN-1001"

echo "== old: pre-S1 baseline, seeded, then alembic upgrade head"
psql "$(url lr_parity_old)" -v ON_ERROR_STOP=1 -q -f "$BASELINE"
psql "$(url lr_parity_old)" -v ON_ERROR_STOP=1 -q <<SQL
INSERT INTO users (id, tenant_id, email, auth_provider, role)
VALUES ('$SEED_USER', '$DEV_TENANT', 'parity@example.com', 'credentials', 'admin');
INSERT INTO cases (tenant_id, user_id, case_number, vendor_a_name, vendor_b_name,
                   use_case, problem_statement, status)
SELECT '$DEV_TENANT', '$SEED_USER', allocate_case_number('$DEV_TENANT'),
       'Vendor A', 'Vendor B', 'use', 'problem', 'complete'
FROM generate_series(1, 3);
INSERT INTO case_snapshots (case_id, tenant_id, refined_problem)
SELECT id, tenant_id, '{"statement": "s"}'::jsonb FROM cases WHERE case_number = 'VITA-1000';
INSERT INTO case_files (case_id, tenant_id, vendor_side, file_type, original_name, storage_path, file_size_bytes)
SELECT id, tenant_id, 'a', 'log', 'a.log', '/x/a.log', 12 FROM cases WHERE case_number = 'VITA-1000';
INSERT INTO case_reports (case_id, tenant_id, format, storage_path)
SELECT id, tenant_id, 'html', '/x/r.html' FROM cases WHERE case_number = 'VITA-1000';
INSERT INTO case_feedback (case_id, user_id, tenant_id, section_type, rating)
SELECT id, '$SEED_USER', tenant_id, 'refined_problem', 'positive' FROM cases WHERE case_number = 'VITA-1000';
INSERT INTO activity_audit_log (tenant_id, user_id, action_type, detail)
VALUES ('$DEV_TENANT', '$SEED_USER', 'case_create', '{}');
SQL
alembic lr_parity_old upgrade head
dump lr_parity_old old
same old fresh
expect lr_parity_old "SELECT version_num FROM alembic_version" "$HEAD"
expect lr_parity_old "SELECT string_agg(run_number, ',' ORDER BY run_number) FROM runs" "VITA-1000,VITA-1001,VITA-1002"
expect lr_parity_old "SELECT run_prefix FROM tenants WHERE slug = 'dev'" "VITA"
expect lr_parity_old "SELECT allocate_run_number('$DEV_TENANT')" "VITA-1003"
expect lr_parity_old "INSERT INTO tenants (name, slug) VALUES ('Fresh', 'fresh') RETURNING run_prefix" "RUN"
expect lr_parity_old "SELECT allocate_run_number((SELECT id FROM tenants WHERE slug = 'fresh'))" "RUN-1000"
expect lr_parity_old "SELECT count(*) FROM run_snapshots s JOIN runs r ON r.id = s.run_id WHERE r.run_number = 'VITA-1000'" "1"
expect lr_parity_old "SELECT count(*) FROM run_files f JOIN runs r ON r.id = f.run_id" "1"
expect lr_parity_old "SELECT count(*) FROM run_reports p JOIN runs r ON r.id = p.run_id" "1"
expect lr_parity_old "SELECT count(*) FROM run_feedback fb JOIN runs r ON r.id = fb.run_id" "1"
expect lr_parity_old "SELECT column_default IS NULL FROM information_schema.columns WHERE table_name = 'runs' AND column_name = 'agent_id'" "t"
expect lr_parity_old "SELECT action_type FROM activity_audit_log" "case_create"
expect lr_parity_old "INSERT INTO activity_audit_log (tenant_id, action_type) VALUES ('$DEV_TENANT', 'run_create') RETURNING action_type" "run_create"
expect lr_parity_old "SELECT to_regclass('public.cases') IS NULL AND to_regproc('allocate_case_number') IS NULL" "t"
# Cascade still wired through the renamed FKs: deleting the run removes its children.
expect lr_parity_old "DELETE FROM runs WHERE run_number = 'VITA-1000' RETURNING run_number" "VITA-1000"
expect lr_parity_old "SELECT count(*) FROM run_snapshots" "0"

echo "== downgrade: old back to 0011 must equal the baseline upgraded to 0011"
psql "$(url lr_parity_pre012)" -v ON_ERROR_STOP=1 -q -f "$BASELINE"
alembic lr_parity_pre012 upgrade "$PRE012"
expect lr_parity_pre012 "SELECT version_num FROM alembic_version" "$PRE012"
dump lr_parity_pre012 pre012
alembic lr_parity_old downgrade "$PRE012"
expect lr_parity_old "SELECT version_num FROM alembic_version" "$PRE012"
dump lr_parity_old old-downgraded
same old-downgraded pre012
expect lr_parity_old "SELECT string_agg(case_number, ',' ORDER BY case_number) FROM cases" "VITA-1001,VITA-1002"
expect lr_parity_old "SELECT string_agg(action_type, ',' ORDER BY action_type) FROM activity_audit_log" "case_create,case_create"
expect lr_parity_old "SELECT allocate_case_number('$DEV_TENANT')" "VITA-1004"

echo "== re-upgrade: old must equal fresh again"
alembic lr_parity_old upgrade head
expect lr_parity_old "SELECT version_num FROM alembic_version" "$HEAD"
dump lr_parity_old old-again
same old-again fresh
expect lr_parity_old "SELECT run_prefix FROM tenants WHERE slug = 'dev'" "VITA"

echo "MIGRATION PARITY OK (head $HEAD)"
