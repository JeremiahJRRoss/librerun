from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActivityAuditLog


class _DriftReport(Protocol):
    """Duck-typed shape for any agent's schema-drift report.

    Each agent owns its own ``SchemaDriftReport`` dataclass; this audit
    service only needs ``to_dict()``, so it accepts anything that
    satisfies the protocol. Keeps the shell decoupled from any specific
    agent package.
    """

    def to_dict(self) -> dict: ...


SCHEMA_DRIFT_ACTION_TYPE = "llm_schema_drift"


async def log_audit(
    db: AsyncSession,
    tenant_id: UUID,
    user_id: UUID | None,
    email: str | None,
    action_type: str,
    detail: dict | None = None,
    ip: str | None = None,
) -> None:
    entry = ActivityAuditLog(
        tenant_id=tenant_id,
        user_id=user_id,
        user_email=email,
        action_type=action_type,
        detail=detail or {},
        ip_address=ip,
    )
    db.add(entry)
    await db.flush()


async def log_schema_drift(
    db: AsyncSession,
    tenant_id: UUID,
    run_id: UUID,
    reports: list[_DriftReport],
) -> None:
    """Persist a single audit row describing LLM output drift for a run.

    Multiple drift reports from the same step-batch land in one row so that
    the admin dashboard can show one "event" per pipeline stage rather than
    one row per coerced field.
    """
    if not reports:
        return
    entry = ActivityAuditLog(
        tenant_id=tenant_id,
        user_id=None,
        user_email=None,
        action_type=SCHEMA_DRIFT_ACTION_TYPE,
        detail={
            "run_id": str(run_id),
            "reports": [r.to_dict() for r in reports],
        },
        ip_address=None,
    )
    db.add(entry)
    await db.flush()
