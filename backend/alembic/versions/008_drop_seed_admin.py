"""drop the legacy seeded admin user

Revision ID: 0008_drop_seed_admin
Revises: 0007_case_phase1_span_ids
Create Date: 2026-08-16 00:00:00.000000

The initial schema used to seed ``admin@company.com`` (fixed UUID
``b0000000-0000-0000-0000-000000000001``) with no password; operators then
set a publicly documented password via ``scripts/seed-admin-password.sh``.
Both are deleted — accounts now come exclusively from the
``INITIAL_ADMIN_*`` / ``INITIAL_USER_*`` startup bootstrap
(``app/scripts/bootstrap_admin.py``). This revision removes the seeded row
from databases initialized before the seed was dropped from
the schema file (now ``backend/db/schema.sql``).

Only the exact seeded identity (id AND email both matching) is touched; a
row whose email an operator changed is considered claimed and left alone.
``cases``, ``case_feedback``, ``activity_audit_log``, and ``pdf_exports``
reference ``users`` without ON DELETE CASCADE, so if the seeded row owns
data the DELETE is answered with a foreign-key violation — in that case the
row is deactivated instead: ``is_active = FALSE`` blocks login (enforced in
``routers/auth.py`` and ``middleware.py``), ``password_hash = NULL`` kills
the known password, and its sessions are revoked. An operator who uses
``admin@company.com`` as ``INITIAL_ADMIN_EMAIL`` gets the account
re-created (or re-activated with the configured password) by the bootstrap,
which the entrypoint runs right after migrations.

Idempotent (safe to re-run) in line with migrations 003–007.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "0008_drop_seed_admin"
down_revision = "0007_case_phase1_span_ids"
branch_labels = None
depends_on = None

_SEED_ID = "b0000000-0000-0000-0000-000000000001"
_SEED_EMAIL = "admin@company.com"


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM users
                WHERE id = '{_SEED_ID}' AND email = '{_SEED_EMAIL}'
            ) THEN
                BEGIN
                    DELETE FROM users WHERE id = '{_SEED_ID}';
                EXCEPTION WHEN foreign_key_violation THEN
                    UPDATE users
                    SET is_active = FALSE, password_hash = NULL
                    WHERE id = '{_SEED_ID}';
                    DELETE FROM sessions WHERE user_id = '{_SEED_ID}';
                END;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # The seeded credential-less admin is intentionally unrestorable.
    pass
