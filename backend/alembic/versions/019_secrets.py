"""the encrypted secrets store: one row per secret, ciphertext only

Revision ID: 0019_secrets
Revises: 0018_agent_settings
Create Date: 2026-09-28 00:00:00.000000

K6 (L30, L31; D14 as D32 and D33 refine it): a secret set in the admin UI
lives here as MultiFernet ciphertext under the backend's store key
(``LIBRERUN_BACKEND_SECRETS_KEY``) — never as plaintext, and never in
``app_settings``, whose values the API returns. ``key_id`` is a keyed
digest of the store key that sealed the row, so a row no configured key
opens is reported as such without decrypting anything; ``fingerprint`` is
a keyed, truncated digest of the value, which is all the API ever shows of
it.

Keyed by its owners (D32): ``scope`` beside a nullable ``tenant_id`` and
``agent_id``, with a CHECK tying each scope to the columns it uses —
``platform`` and ``gateway`` neither, ``agent`` the agent, ``tenant`` both
— and one name per owner, NULLs compared as equal (PostgreSQL 15+). K6
writes ``platform`` rows; the ``gateway`` rows are K7's and the ``agent``
and ``tenant`` rows K8a's, and neither adds a migration.

``updated_at`` has no trigger and no ORM ``onupdate``: the service's
upsert sets it itself.

Second of wave 1's two migrations: K5a's 0018 merged first (#163), so this
revision follows it (§5.4 of the K blueprint). Idempotent on the head
snapshot in the pattern of 002–018: ``CREATE TABLE IF NOT EXISTS`` no-ops
where ``backend/db/schema.sql`` already carries the table, and the
downgrade drops it only if present.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "0019_secrets"
down_revision = "0018_agent_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS secrets (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            scope           VARCHAR(16) NOT NULL,
            tenant_id       UUID REFERENCES tenants(id) ON DELETE CASCADE,
            agent_id        VARCHAR(100),
            name            VARCHAR(100) NOT NULL,
            ciphertext      BYTEA NOT NULL,
            key_id          VARCHAR(16) NOT NULL,
            fingerprint     CHAR(12) NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by      UUID REFERENCES users(id) ON DELETE SET NULL,
            last_used_at    TIMESTAMPTZ,
            CONSTRAINT ck_secrets_scope_owner CHECK (
                   (scope IN ('platform', 'gateway') AND tenant_id IS NULL AND agent_id IS NULL)
                OR (scope = 'agent'  AND tenant_id IS NULL     AND agent_id IS NOT NULL)
                OR (scope = 'tenant' AND tenant_id IS NOT NULL AND agent_id IS NOT NULL)),
            CONSTRAINT uq_secrets_owner_name
                UNIQUE NULLS NOT DISTINCT (scope, tenant_id, agent_id, name)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS secrets")
