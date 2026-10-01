import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import get_manifest
from app.database import get_db
from app.middleware import get_current_user
from app.models import Run, RunFeedback, User
from app.schemas.feedback import FeedbackSubmit
from app.services.audit_service import log_audit

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["feedback"])


@router.post("/feedback", status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    payload: FeedbackSubmit,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    run = await db.get(Run, payload.run_id)
    if run is None or run.tenant_id != tenant_id or run.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")

    # The feedback vocabulary belongs to the agent's manifest (blueprint
    # B9) — a section the agent never declared is a client bug, not data.
    manifest = get_manifest(run.agent_id) if run.agent_id else None
    allowed = (
        {s.id for s in manifest.feedback_sections} if manifest is not None else set()
    )
    if payload.section_type not in allowed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown feedback section {payload.section_type!r} for agent "
            f"{run.agent_id!r}; declared sections: {sorted(allowed)}",
        )

    fb = RunFeedback(
        run_id=run.id,
        user_id=user.id,
        tenant_id=tenant_id,
        trace_id=run.trace_id,
        section_type=payload.section_type,
        citation_id=payload.citation_id,
        rating=payload.rating,
        comment=payload.comment,
    )
    db.add(fb)
    await db.flush()
    ip = request.client.host if request.client else None
    await log_audit(
        db, tenant_id, user.id, user.email, "run_update",
        {"run_id": str(run.id), "action": "feedback", "section": payload.section_type, "rating": payload.rating},
        ip,
    )

    # Blueprint B5: the vendor annotation path is retired. Feedback is
    # DB-persisted above; observability gets a structured ``run_feedback``
    # log event that flows through the normal telemetry pipeline (JSON log
    # file -> Vector -> configured sinks). The RUN's ids ride in
    # ``run_trace_id`` / ``run_span_id`` — NOT ``trace_id``/``span_id``,
    # which the ``add_otel_ids`` log processor overwrites with the ids of
    # the span active at log time (this feedback POST's request span), so
    # those names could never carry the run's identity. The comment text
    # itself stays out of the event — it is user content.
    logger.info(
        "run_feedback",
        run_id=str(run.id),
        run_number=run.run_number,
        section_type=payload.section_type,
        rating=payload.rating,
        citation_id=payload.citation_id,
        has_comment=bool(payload.comment),
        run_trace_id=fb.trace_id,
        run_span_id=run.phase2_span_id,
        agent_id=run.agent_id,
    )

    return {"id": str(fb.id)}
