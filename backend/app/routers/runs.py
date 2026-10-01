import json
from uuid import UUID, uuid4

import structlog

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import Text, and_, cast, or_, select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import get_agent, get_manifest, list_agents
from app.database import get_db
from app.middleware import get_current_user
from app.models import Run, RunSnapshot, User
from app.logging_context import log_context
from app.observability import run_trace
from app.observability.trace_viewer import effective_trace_url
from app.redis import get_redis
from app.schemas.run import (
    ApprovalResponse,
    RunCreatedResponse,
    RunDetail,
    RunListResponse,
    RunSummary,
    EditStatementRequest,
    ProgressResponse,
    RefinedStatement,
    StepProgress,
)
from app.services import agent_runner
from app.services.audit_service import log_audit
from app.services.run_service import (
    allocate_run_number,
    build_run_detail_fields,
    display_title,
    parked_output_phase,
    summary_text,
)
from app.services.intake import (
    approval_summary,
    extract_legacy_columns,
    redact_pii_fields,
    run_title,
    validate_user_inputs,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["runs"])


def _default_agent_id() -> str | None:
    """The agent a bare ``POST /runs`` (no ``agent_id``) targets.

    Chassis-generic (blueprint B11 — no agent id is hardcoded here): when
    exactly one agent is registered it is the unambiguous default, which
    keeps single-agent deployments' pre-B7 clients working. With zero or
    several agents there is no honest default — callers must say which
    agent they mean.
    """
    agents = list_agents()
    return agents[0].agent_id if len(agents) == 1 else None


def _search_clause(term: str):
    """What a run-list search matches (blueprint S2): the title and the run
    number — and, for a row persisted before S2 computed titles at intake
    (``title`` NULL), the text of its stored inputs, so an older run by a
    generic agent stays findable by its content (Codex P2 on PR #50)."""
    like = f"%{term}%"
    return or_(
        Run.title.ilike(like),
        Run.run_number.ilike(like),
        and_(Run.title.is_(None), cast(Run.user_inputs, Text).ilike(like)),
    )


def _to_summary(run: Run) -> RunSummary:
    title = display_title(run)
    return RunSummary(
        id=run.id,
        run_number=run.run_number,
        title=summary_text(title) or None,
        vendor_a_name=run.vendor_a_name,
        vendor_b_name=run.vendor_b_name,
        problem_summary=summary_text(title),
        severity=run.severity,
        status=run.status,
        created_at=run.created_at,
        updated_at=run.updated_at,
        agent_id=run.agent_id,
    )


@router.get("/runs", response_model=RunListResponse)
async def list_runs(
    request: Request,
    status_filter: str | None = Query(None, alias="status"),
    severity: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    stmt = select(Run).where(Run.tenant_id == tenant_id, Run.deleted_at.is_(None))
    if user.role != "admin":
        stmt = stmt.where(Run.user_id == user.id)
    if status_filter:
        stmt = stmt.where(Run.status == status_filter)
    if severity:
        stmt = stmt.where(Run.severity == severity)
    if search:
        # Title and run number are the two things every agent's runs have
        # (blueprint S2); the demo agent's vendor columns are not.
        stmt = stmt.where(_search_clause(search))

    total_stmt = select(sqlfunc.count()).select_from(stmt.subquery())
    total = (await db.execute(total_stmt)).scalar_one()

    stmt = stmt.order_by(Run.created_at.desc()).offset((page - 1) * per_page).limit(per_page)
    runs = (await db.execute(stmt)).scalars().all()
    return RunListResponse(
        runs=[_to_summary(r) for r in runs], total=total, page=page, per_page=per_page
    )


@router.post("/runs", response_model=RunCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: dict = Body(...),
    agent_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a run from a schema-validated payload (blueprint B8).

    The body shape belongs to the selected agent: it is validated against
    ``agent.input_schema()`` server-side (the wizard enforces the same
    schema client-side), ``x-pii`` string fields are redacted before
    anything is persisted, and well-known keys are lifted into the legacy
    list/search columns.
    """
    tenant_id = request.state.tenant_id
    agent_id = agent_id or _default_agent_id()
    if agent_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "agent_id is required — there is no unambiguous default agent "
            "(zero or several agents are registered)",
        )
    agent = get_agent(agent_id)
    if agent is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown agent: {agent_id}"
        )

    try:
        schema = agent.input_schema()
    except Exception:
        logger.exception("input_schema_unavailable", agent_id=agent_id)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Agent {agent_id} does not expose an input schema",
        )

    schema_errors = validate_user_inputs(schema, payload)
    if schema_errors:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=schema_errors
        )

    # PII redaction before persist — unredacted content never touches the
    # database (CLAUDE.md invariant), driven by the schema's x-pii marks.
    user_inputs, redactions = redact_pii_fields(schema, payload)

    # The run's title (blueprint S2): the manifest names the input, or the
    # first non-PII string input stands in. Computed from the REDACTED
    # payload, so a title can never carry what the column must not.
    manifest = get_manifest(agent_id)
    title_path = manifest.ui.list.title_path if manifest is not None else None
    title = run_title(title_path, schema, user_inputs)

    # One trace per run (blueprint S4): the caller's W3C headers, if any,
    # validated — ``traceparent`` in its fixed grammar, ``tracestate``
    # within the W3C limits and checked as an identifier — become the
    # parent of the run's root span; the request's own span never does.
    upstream = run_trace.upstream_context(request.headers)

    run_number = await allocate_run_number(db, tenant_id)
    run_id = uuid4()
    ip = request.client.host if request.client else None
    # The root ``run`` span: the row is created inside it and it ends
    # here — an ended parent is a valid parent for the phase spans that
    # follow, and its persisted context is what they restore. The run
    # identity is bound around it so the enricher stamps the run plane
    # and the agent on the root the way it does on the phases.
    with log_context(
        agent_id=agent_id,
        run_id=str(run_id),
        tenant_id=str(tenant_id),
        run_number=run_number,
        user_id=str(user.id),
        session_id=str(run_id),
    ), run_trace.root_span(
        upstream=upstream,
        run_id=run_id,
        run_number=run_number,
        agent_id=agent_id,
        tenant_id=tenant_id,
    ) as root:
        run = Run(
            id=run_id,
            tenant_id=tenant_id,
            user_id=user.id,
            run_number=run_number,
            status="refining",
            agent_id=agent_id,
            user_inputs=user_inputs,
            title=title,
            **extract_legacy_columns(user_inputs),
        )
        run_trace.persist_root(run, root)
        db.add(run)
        await db.flush()

        await log_audit(
            db, tenant_id, user.id, user.email, "run_create",
            {"run_id": str(run.id), "run_number": run.run_number, "agent_id": agent_id}, ip,
        )
        await db.commit()

    logger.info(
        "run_created",
        run_id=str(run.id),
        run_number=run.run_number,
        severity=run.severity,
        agent_id=agent_id,
        trace_id=run.trace_id,
        pii_redactions=redactions,
    )

    # Which phase actually runs first is the agent manifest's business
    # (blueprint B7); the runner parents its span on the persisted root.
    background_tasks.add_task(agent_runner.start_run, run.id, tenant_id, agent_id)

    return RunCreatedResponse(run_id=run.id, run_number=run.run_number, status=run.status)


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(
    run_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    run = await db.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id or run.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if user.role != "admin" and run.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your run")

    snapshot = (
        await db.execute(select(RunSnapshot).where(RunSnapshot.run_id == run.id))
    ).scalar_one_or_none()
    return RunDetail(
        **build_run_detail_fields(run, snapshot),
        trace_url=await effective_trace_url(db, run.trace_id),
    )


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(
    run_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    run = await db.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id or run.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if user.role != "admin" and run.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your run")

    run.deleted_at = sqlfunc.now()
    await db.flush()
    ip = request.client.host if request.client else None
    await log_audit(
        db, tenant_id, user.id, user.email, "run_delete", {"run_id": str(run_id)}, ip
    )
    logger.info("run_deleted", run_id=str(run_id))
    return None


@router.get(
    "/runs/{run_id}/approval",
    response_model=ApprovalResponse,
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": ApprovalResponse,
            "description": (
                "The run is still working (`submitted` / `refining`): `status` "
                "only — `phase`, `payload` and `summary` are null. Poll again."
            ),
        }
    },
)
async def get_approval(
    run_id: UUID,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """What the approval view renders, for every agent (blueprint S2).

    ``phase`` is the manifest phase whose output is parked, ``payload``
    that output in full, and ``summary`` the string the manifest's
    ``ui.approval.summary_path`` names inside it (or the first string in
    it) — what the view shows and offers for editing. While the run is
    still working the answer is ``202`` with ``status`` alone.
    """
    tenant_id = request.state.tenant_id
    run = await db.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id or run.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if user.role != "admin" and run.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your run")

    if run.status in ("submitted", "refining"):
        response.status_code = status.HTTP_202_ACCEPTED
        return ApprovalResponse(status=run.status)
    if run.status not in ("awaiting_approval", "investigating", "complete"):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"No parked output in status={run.status}"
        )

    snap = (
        await db.execute(select(RunSnapshot).where(RunSnapshot.run_id == run.id))
    ).scalar_one_or_none()
    # An empty dict is a parked output too (a phase may legitimately return
    # ``structured={}``); only a missing snapshot or a non-dict is "not yet".
    if snap is None or not isinstance(snap.analysis, dict):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No parked output yet")
    manifest = get_manifest(run.agent_id) if run.agent_id else None
    summary_path = manifest.ui.approval.summary_path if manifest is not None else None
    return ApprovalResponse(
        status=run.status,
        # The producer of ``payload`` — after approval the cursor has moved on.
        phase=parked_output_phase(manifest, run),
        payload=snap.analysis,
        summary=approval_summary(summary_path, snap.analysis),
    )


@router.get(
    "/runs/{run_id}/refined-statement",
    deprecated=True,
    responses={
        status.HTTP_202_ACCEPTED: {
            "description": (
                "The run is still working (`submitted` / `refining`): "
                "`{\"status\": ...}` only. Poll again."
            ),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                        "required": ["status"],
                    }
                }
            },
        }
    },
    description=(
        "Deprecated (blueprint S2): the demo agent's own view of the parked "
        "output. `GET /runs/{run_id}/approval` serves every agent's — read "
        "`summary` and `payload` there. Kept for one release, removed at v1.1."
    ),
)
async def get_refined_statement(
    run_id: UUID,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    run = await db.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id or run.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if user.role != "admin" and run.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your run")

    if run.status in ("submitted", "refining"):
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": run.status}
    if run.status != "awaiting_approval" and run.status != "investigating" and run.status != "complete":
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Refined statement not available in status={run.status}")

    snap = (
        await db.execute(select(RunSnapshot).where(RunSnapshot.run_id == run.id))
    ).scalar_one_or_none()
    if snap is None or not isinstance(snap.analysis, dict):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No refined statement yet")
    refined = snap.analysis.get("refined_problem")
    if not refined:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No refined statement yet")
    return refined


@router.post("/runs/{run_id}/approve", status_code=status.HTTP_202_ACCEPTED)
async def approve_run(
    run_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    run = await db.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id or run.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if user.role != "admin" and run.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your run")
    if run.status != "awaiting_approval":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run status is {run.status}, cannot approve")

    # The manifest decides what approval resumes into: ``investigating``
    # when the next phase is the last, ``refining`` when more phases (and
    # possibly more gates) follow (blueprint B7). The case row's agent_id
    # is authoritative (backfilled since migration 006); an unresolvable
    # agent surfaces via the runner's run_unknown_agent error path.
    agent_id = run.agent_id or ""
    next_status = agent_runner.resume_status_for(agent_id, run.current_phase)
    run.status = next_status
    await db.flush()
    ip = request.client.host if request.client else None
    await log_audit(db, tenant_id, user.id, user.email, "run_update", {"run_id": str(run_id), "action": "approve"}, ip)
    await db.commit()

    logger.info("run_approved", run_id=str(run_id))
    background_tasks.add_task(agent_runner.resume_run, run.id, tenant_id, agent_id)
    return {"status": next_status}


@router.post("/runs/{run_id}/edit-statement", status_code=status.HTTP_202_ACCEPTED)
async def edit_statement(
    run_id: UUID,
    payload: EditStatementRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    run = await db.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id or run.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if user.role != "admin" and run.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your run")
    if run.status not in ("awaiting_approval", "refining"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot edit in status {run.status}")

    run.status = "refining"
    await db.flush()
    ip = request.client.host if request.client else None
    await log_audit(db, tenant_id, user.id, user.email, "run_update", {"run_id": str(run_id), "action": "edit_statement"}, ip)
    await db.commit()

    logger.info("run_edit_statement", run_id=str(run_id))
    agent_id = run.agent_id or ""
    background_tasks.add_task(
        agent_runner.rerun_current_phase,
        run.id,
        payload.edited_statement,
        agent_id,
    )
    return {"status": "refining"}


@router.get("/runs/{run_id}/progress", response_model=ProgressResponse)
async def get_progress(
    run_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = request.state.tenant_id
    run = await db.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id or run.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    if user.role != "admin" and run.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your run")

    redis = await get_redis()
    from app.services.run_boundary import progress_key, step_models_key

    raw = await redis.hgetall(progress_key(run_id))
    # The model that answered each step is written by the gateway, in a
    # key of its own (D13): the progress entry is a closed shape with one
    # writer, and the terminal status always lands after the call, so a
    # model merged into that entry would be overwritten every time. Join
    # on read instead.
    models = await redis.hgetall(step_models_key(run_id))

    # NO SUFFIX JOIN HERE, deliberately, and the absence is the decision.
    #
    # The gateway records a model under the LLM STEP id; an adapter may
    # namespace its progress rows (`phase:node`), so for such an agent no
    # row ever shows a model — D13's visible half is missing while the
    # span, the cost and the trace are all correct. A tempting fix is to
    # also match the id's last segment when it names a step the agent's
    # manifest declares. It was written, tested, and reverted.
    #
    # It infers CAUSALITY FROM A NAME. `StepProgress.step_id` is free
    # text the agent chooses — through `caps.progress.update` in process,
    # through the `progress` event over the Run Contract — so a row
    # called `phase:classify` need not be the work that called the
    # `classify` step, and may be a row that called no model at all.
    # Manifest membership proves the step exists, never that THIS row
    # made the call (Codex P2). Attributing a model to the wrong row is
    # worse than attributing it to none: a blank cell is honest, and a
    # confident wrong one is what an admin would act on.
    #
    # The real fix is an explicit association the agent states, because
    # the node that calls `complete("classify", …)` is the only thing
    # that knows. Recorded as gap E5; an agent displays a model today
    # only when its progress ids happen to equal its step ids, which is a
    # coincidence and not a contract.

    def _step(step_id: str, stored: str) -> StepProgress:
        entry = json.loads(stored)
        # An entry written by an earlier build may carry ``model``
        # inside it. Dropping it rather than letting it through keeps
        # this from being a duplicate keyword argument — a 500 on the
        # progress endpoint for the life of that run.
        entry.pop("model", None)
        return StepProgress(step_id=step_id, model=models.get(step_id), **entry)

    steps = [_step(k, v) for k, v in raw.items()]
    # Legacy numeric phase kept for API compat; ``phase_name`` carries the
    # manifest phase actually running (blueprint B7).
    phase = 1 if run.status in ("submitted", "refining", "awaiting_approval") else 2
    return ProgressResponse(phase=phase, steps=steps, phase_name=run.current_phase)
