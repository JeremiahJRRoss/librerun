"""agent settings as tenant data: one row per divergence from a manifest default

Revision ID: 0018_agent_settings
Revises: 0017_run_error_reason
Create Date: 2026-09-27 00:00:00.000000

K5a (L32, D17): an agent declares the settings it reads in its manifest
(``settings[]``, each with a type and a default), and a tenant's admin
edits the effective values on the agent page's Settings tab. The values
live here, keyed by ``(tenant_id, agent_id, key)`` — the defect S4a fixed
for steps, which a settings store keyed by agent alone would bring back:
one tenant's edit becoming every tenant's value.

A row records a DIVERGENCE only: a value equal to the manifest's default
is not stored (D17, the rule ``agent_step_configs`` already follows), so
an agent's next release can move a default and reach every tenant that
never chose one. ``value`` is JSONB because a setting's type is the
manifest's to declare — a string, a number, a boolean or a list of
strings — and the backend holds each value to it before it is written.

No data is carried over (D18): nothing a run reads lives anywhere else
to carry. Idempotent on the head snapshot in the pattern of 002–017:
``CREATE TABLE IF NOT EXISTS`` no-ops where ``backend/db/schema.sql``
already carries the table, and the downgrade drops it only if present.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "0018_agent_settings"
down_revision = "0017_run_error_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_settings (
            tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            agent_id        VARCHAR(100) NOT NULL,
            key             VARCHAR(64) NOT NULL,
            value           JSONB NOT NULL,
            updated_by      UUID REFERENCES users(id),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (tenant_id, agent_id, key)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_settings")
