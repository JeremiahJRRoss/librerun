"""what the gateway holds: one row, written by the gateway

Revision ID: 0020_gateway_status
Revises: 0019_secrets
Create Date: 2026-09-29 00:00:00.000000

K7 (L33, D16 as K7 refines it): the channel through which the backend
learns what the gateway holds. The gateway writes this row at boot and on
every change to its own ``gateway``-scope secrets — the reverse of the
direction the S4a tables use — and the platform-admin endpoints read it:
the provider names with their source and fingerprint, the public key the
browser seals a provider key to, the gateway's version and whether it is
keyless. No new credential between the two processes.

One row, held there by ``ck_gateway_status_singleton``. ``providers`` is a
JSON array, one entry per name — ``{name, aliases, source, fingerprint,
set_by, set_at, row, reason}`` — and never a value. ``public_key_pem`` is
NULL while the gateway's store key is blank: no key, nothing to seal to.
The sealing key's fingerprint (``SHA256:`` over the SPKI DER) has no
column: each reader computes it from the PEM (D34). No ORM model: the
backend reads the row with one statement and the gateway writes it with
one upsert.

The wave's only migration, after K6's ``0019_secrets``, the head wave 1
left (§5.4 of the K blueprint). Idempotent on the head snapshot in the
pattern of 002–019: ``CREATE TABLE IF NOT EXISTS`` no-ops where
``backend/db/schema.sql`` already carries the table, and the downgrade
drops it only if present.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "0020_gateway_status"
down_revision = "0019_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_status (
            id              INTEGER PRIMARY KEY
                            CONSTRAINT ck_gateway_status_singleton CHECK (id = 1),
            version         VARCHAR(32) NOT NULL,
            stub            BOOLEAN NOT NULL,
            providers       JSONB NOT NULL DEFAULT '[]',
            public_key_pem  TEXT,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS gateway_status")
