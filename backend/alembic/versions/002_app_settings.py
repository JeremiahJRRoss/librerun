"""add app_settings table

Revision ID: 0002_app_settings
Revises: 0001_baseline
Create Date: 2026-04-13 00:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "0002_app_settings"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Raw DDL rather than op.create_table so it is idempotent: the head
    # schema file (blueprint S1) already carries this table, and a fresh
    # container runs every migration against that file's result. The
    # definition matches the original create_table exactly (PostgreSQL
    # auto-names the constraints app_settings_pkey /
    # app_settings_updated_by_fkey either way).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key         VARCHAR(100) NOT NULL,
            value       JSONB NOT NULL,
            updated_by  UUID REFERENCES users(id),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (key)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app_settings")
