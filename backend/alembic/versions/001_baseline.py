"""baseline

Revision ID: 0001_baseline
Revises:
Create Date: 2025-01-01 00:00:00.000000

This is an EMPTY baseline migration. The initial schema is loaded directly
from ``backend/db/schema.sql`` during setup (see docs/platform/Install.md). This
revision exists so that subsequent ``alembic revision --autogenerate``
invocations diff against the current SQLAlchemy model state rather than
against an empty database (which would regenerate ``CREATE TABLE`` for every
table and fail on a populated DB).

After loading the SQL schema, operators must stamp this revision:

    alembic stamp 0001_baseline
"""
from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Intentional no-op — schema is bootstrapped from backend/db/schema.sql.
    pass


def downgrade() -> None:
    # Cannot downgrade from baseline.
    pass
