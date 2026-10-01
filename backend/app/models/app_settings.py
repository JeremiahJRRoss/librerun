from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import String, ForeignKey, func, DateTime
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AppSetting(Base):
    """Runtime-tunable application settings.

    Rows in this table override the `.env` / ``config.py`` defaults. The
    settings service merges the two so reads always return an effective value.
    Writes are audited via ``updated_by`` / ``updated_at``.
    """

    __tablename__ = "app_settings"
    # H17: ``updated_at``'s ``onupdate`` is SQL the database evaluates, and
    # the default ("auto") fetches server values on INSERT only — so every
    # write after a key's first expired ``updated_at``, and the PUT handler's
    # read of it became a lazy load, which async code cannot do
    # (``MissingGreenlet``, a 500). True fetches it by RETURNING on UPDATE
    # too. ``tests/test_orm_server_defaults.py`` holds every mapper to this.
    __mapper_args__ = {"eager_defaults": True}

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
