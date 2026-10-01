"""drop the dead per-tenant session_timeout_hours knob

Revision ID: 0014_drop_dead_session_timeout
Revises: 0013_run_root_trace
Create Date: 2026-09-12 00:00:00.000000

``tenants.auth_config`` carried a ``session_timeout_hours`` key that nothing
ever read. It was seeded by the schema, accepted and echoed by
``PUT /api/v1/admin/auth-config``, written to the audit log, and rendered as
an editable number field on ``/admin/auth-config`` — and session lifetime
came from ``JWT_EXPIRY_HOURS`` regardless. An admin who shortened it saw no
effect anywhere.

Session lifetime is now a live platform setting (``session_timeout_minutes``,
read by ``auth_service.session_lifetime``). Two knobs for one behaviour is
what produced the original confusion, so this removes the one that never
worked rather than wiring a second.

Wiring it instead was considered and rejected under L14 (1.0 is the three
promises in blueprint §1.1 and nothing else): per-tenant session policy is in
none of them, and doing it correctly needs a value that can mean "inherit the
platform default". The seeded 24 cannot — every tenant carries it, so a
tenant-wins rule would shadow the platform setting for all of them, which is
the same lie inverted. Removing it before 1.0 ships costs nothing; after 1.0
it is a breaking API change.

Numbered 0014, not 0013: S4 landed ``0013_run_root_trace`` on main while
this branch was open, and both chained from 0012. Two heads off one
parent is not a merge conflict git can see — the files do not overlap —
but ``alembic upgrade head`` fails on the ambiguity. Renumbered onto the
end of the chain rather than merged as a branch, since these two
migrations have nothing to do with each other.

Idempotent, in line with migrations 003-013: the UPDATE is guarded by a key
test so it no-ops on the head snapshot (``backend/db/schema.sql`` no longer
seeds the key), and SET DEFAULT is naturally repeatable.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "0015_drop_dead_session_timeout"
down_revision = "0014_agent_gateway_tables"
branch_labels = None
depends_on = None

# Kept byte-identical to the DEFAULT in backend/db/schema.sql. Postgres
# normalises a jsonb constant, so the two render the same in pg_dump and
# scripts/migration_parity.sh compares them.
_DEFAULT_WITHOUT = """'{
    "google_enabled": true,
    "google_allowed_domains": [],
    "google_allowed_emails": [],
    "microsoft_enabled": true,
    "microsoft_allowed_tenants": [],
    "microsoft_allowed_emails": [],
    "credentials_enabled": false
}'"""

_DEFAULT_WITH = """'{
    "google_enabled": true,
    "google_allowed_domains": [],
    "google_allowed_emails": [],
    "microsoft_enabled": true,
    "microsoft_allowed_tenants": [],
    "microsoft_allowed_emails": [],
    "credentials_enabled": false,
    "session_timeout_hours": 24
}'"""


def upgrade() -> None:
    # Existing rows: drop the key. Guarded so a database created from the
    # head snapshot, which never had it, is left untouched.
    op.execute(
        """
        UPDATE tenants
        SET auth_config = auth_config - 'session_timeout_hours'
        WHERE auth_config ? 'session_timeout_hours';
        """
    )
    # New rows: stop handing it back out.
    op.execute(
        f"ALTER TABLE tenants ALTER COLUMN auth_config SET DEFAULT {_DEFAULT_WITHOUT}::jsonb;"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE tenants ALTER COLUMN auth_config SET DEFAULT {_DEFAULT_WITH}::jsonb;"
    )
    # Restore the key at its documented default on rows that lack it, so a
    # downgraded database matches what 0012 produced.
    op.execute(
        """
        UPDATE tenants
        SET auth_config = auth_config || '{"session_timeout_hours": 24}'::jsonb
        WHERE NOT (auth_config ? 'session_timeout_hours');
        """
    )
