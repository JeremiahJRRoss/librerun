"""The encrypted secrets store (K6; L30, L31, D32).

A secret set in the admin UI is a row here: MultiFernet ciphertext under
the store key of the process that owns the row, a keyed id of that key,
and a keyed fingerprint of the value — never the value.
``app.services.secrets_service`` writes and reads every value of the
backend's own scopes, and never selects ``ciphertext`` for a list;
``app.scripts.rewrap_secrets`` re-seals rows in place,
``tool_secrets_service`` touches ``last_used_at`` alone (K8a), and
``provider_keys_service`` stores, lists and deletes the ``gateway`` rows,
whose blobs only the gateway process opens (K7).
"""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# The owner a scope names, and the columns it uses (D32). ``gateway`` rows
# are the gateway's (K7) and ``agent`` and ``tenant`` rows are an agent's
# tool secrets (K8a); the backend's own settings are ``platform``.
SCOPES = ("platform", "gateway", "agent", "tenant")


class Secret(Base):
    """One secret, keyed by its owners and its name.

    ``updated_at`` has a server default and deliberately no ``onupdate``:
    the service writes this table with a Core upsert that sets it, so an
    ORM ``onupdate`` would be dead code — and, as SQL the database
    evaluates, one that K4b's rule (``tests/test_orm_server_defaults.py``)
    would require this mapper to fetch eagerly.
    """

    __tablename__ = "secrets"
    __table_args__ = (
        CheckConstraint(
            "(scope IN ('platform', 'gateway') AND tenant_id IS NULL AND agent_id IS NULL)"
            " OR (scope = 'agent' AND tenant_id IS NULL AND agent_id IS NOT NULL)"
            " OR (scope = 'tenant' AND tenant_id IS NOT NULL AND agent_id IS NOT NULL)",
            name="ck_secrets_scope_owner",
        ),
        UniqueConstraint(
            "scope",
            "tenant_id",
            "agent_id",
            "name",
            name="uq_secrets_owner_name",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    tenant_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
    )
    agent_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(String(16), nullable=False)
    fingerprint: Mapped[str] = mapped_column(CHAR(12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_by: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Stamped by K8a's reader, at most hourly per row per process; the
    # column is here because K8a writes no migration (D32).
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
