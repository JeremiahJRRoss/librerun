"""What the gateway reads: manifest snapshots, step overrides, agent keys.

The LLM gateway is a separate service (blueprint S4a, L23). The agent
registry it would otherwise ask lives in the backend process and dies
with it, so the three things the gateway needs at request time —
whether an agent may call a model, which model that step resolves to,
and whether the credential in front of it belongs to that agent — are
rows, written by the backend and read by the gateway. Beside them sit a
tenant's values for an agent's declared settings (K5a), which only the
backend reads.
"""
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CHAR,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentManifestSnapshot(Base):
    """One agent's validated manifest, as discovery last saw it.

    Platform-scoped: an agent is installed into the deployment, not into
    a tenant. Rows are **never deleted** — an agent that disappears from
    discovery is stamped ``absent_at`` and the stamp is cleared when it
    comes back, so uninstalling an agent does not take its keys and its
    tenants' step overrides with it.
    """

    __tablename__ = "agent_manifests"

    agent_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    absent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AgentStepConfig(Base):
    """A tenant's override of one step's model configuration.

    The manifest's ``llm.steps`` defaults are the fallback: a row here
    never *declares* a step, it only replaces values for one the
    manifest still declares, so a row left behind by a step that was
    removed or renamed is inert.
    """

    __tablename__ = "agent_step_configs"
    # H17's rule (``tests/test_orm_server_defaults.py``): a server-side
    # ``onupdate`` is fetched on UPDATE, or reading it after a flush is a
    # lazy load async code cannot make. The service writes this table with
    # a Core upsert that sets ``updated_at`` itself, so here it prevents the
    # defect rather than fixing one.
    __mapper_args__ = {"eager_defaults": True}

    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    agent_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    step_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AgentSetting(Base):
    """A tenant's value for one setting an agent's manifest declares
    (``settings[]``, K5a, L32).

    The manifest's ``default`` is the fallback: a row exists only while
    the tenant's value DIFFERS from it (D17), and a row whose key the
    manifest no longer declares is inert, as a step override is. The
    backend alone reads and writes this table; the gateway never needs it.
    """

    __tablename__ = "agent_settings"
    # H17's rule (``tests/test_orm_server_defaults.py``), as on
    # ``AgentStepConfig``: the service writes this table with a Core
    # upsert that sets ``updated_at`` itself, so here it prevents the
    # defect rather than fixing one.
    __mapper_args__ = {"eager_defaults": True}

    tenant_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    agent_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
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


class AgentKey(Base):
    """A per-agent gateway credential (D10).

    The value is never stored: only its sha256 and the eight characters
    after ``lr_agent_``, which is what the admin page shows so an
    operator can tell which key they are holding. ``key_hash`` is unique
    across the whole table — a presented key maps to exactly one agent,
    so two agents can never share a value — and the two partial unique
    indexes give an agent at most one ``current`` and one ``previous``
    key.
    """

    __tablename__ = "agent_keys"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    agent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    key_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    issued_by: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    previous_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    previous_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
