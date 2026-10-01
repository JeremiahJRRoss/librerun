from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config as _config
from app.database import get_db
from app.middleware import require_admin, require_platform_admin
from app.agents.registry import get_agent
from app.services import agent_key_service, deployment_view, edge_tls, provider_keys_service
from app.models import (
    ActivityAuditLog,
    Run,
    RunFeedback,
    RunSnapshot,
    Session,
    Tenant,
    User,
)
from app.schemas.admin import (
    AdminRunDetail,
    AgentKeyRow,
    AuditLogEntry,
    AuthConfig,
    DeploymentView,
    FeedbackAggregate,
    FeedbackAggregateSection,
    IssuedAgentKey,
    ProviderKeyAccepted,
    ProviderKeyUpdate,
    ProvidersStatus,
    RotatedAgentKey,
    SecretSettingState,
    SettingResponse,
    SettingUpdate,
    TlsStatus,
    UserEntry,
    UserInvite,
    UserUpdate,
)
from app.services import app_settings_service
from app.services.audit_service import SCHEMA_DRIFT_ACTION_TYPE, log_audit
from app.services.auth_service import hash_password
from app.observability import obs_vendors
from app.observability.trace_viewer import effective_trace_url
from app.services.run_service import build_run_detail_fields


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# ---- Users ----
@router.get("/users", response_model=list[UserEntry])
async def list_users(db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    rows = (
        await db.execute(select(User).where(User.tenant_id == user.tenant_id))
    ).scalars().all()
    return [UserEntry.model_validate(r) for r in rows]


@router.post("/users", response_model=UserEntry, status_code=status.HTTP_201_CREATED)
async def invite_user(
    payload: UserInvite,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    # Generate a temp password
    import secrets as _secrets

    temp_pw = _secrets.token_urlsafe(12)
    new = User(
        tenant_id=user.tenant_id,
        email=payload.email,
        role=payload.role,
        auth_provider="credentials",
        password_hash=hash_password(temp_pw),
    )
    db.add(new)
    await db.flush()
    ip = request.client.host if request.client else None
    await log_audit(db, user.tenant_id, user.id, user.email, "role_change", {"action": "invite", "email": payload.email, "role": payload.role}, ip)
    return UserEntry.model_validate(new)


@router.put("/users/{user_id}", response_model=UserEntry)
async def update_user(
    user_id: UUID,
    payload: UserUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    u = await db.get(User, user_id)
    if u is None or u.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if payload.role is not None:
        u.role = payload.role
    if payload.is_active is not None:
        u.is_active = payload.is_active
    await db.flush()
    ip = request.client.host if request.client else None
    await log_audit(db, user.tenant_id, user.id, user.email, "role_change", {"user_id": str(user_id), "role": u.role}, ip)
    return UserEntry.model_validate(u)


@router.post("/users/{user_id}/revoke")
async def revoke_sessions(
    user_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    u = await db.get(User, user_id)
    if u is None or u.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    sessions = (
        await db.execute(select(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None)))
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for s in sessions:
        s.revoked_at = now
    await db.flush()
    ip = request.client.host if request.client else None
    await log_audit(db, user.tenant_id, user.id, user.email, "session_revoke", {"user_id": str(user_id), "count": len(sessions)}, ip)
    return {"revoked": len(sessions)}


# ---- Auth config ----
@router.get("/auth-config", response_model=AuthConfig)
async def get_auth_config(db: AsyncSession = Depends(get_db), user: User = Depends(require_admin)):
    tenant = await db.get(Tenant, user.tenant_id)
    cfg = tenant.auth_config if tenant else {}
    return AuthConfig(**cfg)


@router.put("/auth-config", response_model=AuthConfig)
async def put_auth_config(
    payload: AuthConfig,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    tenant = await db.get(Tenant, user.tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    tenant.auth_config = payload.model_dump()
    await db.flush()
    ip = request.client.host if request.client else None
    await log_audit(db, user.tenant_id, user.id, user.email, "auth_config_change", payload.model_dump(), ip)
    return payload


# ---- Run observability ----
@router.get("/runs/{run_id}", response_model=AdminRunDetail)
async def get_admin_run_detail(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> AdminRunDetail:
    """Admin-only run detail including raw OTEL trace_id and Phase 2
    span_id. The trace deep-link (``trace_url``) also appears on the
    customer-facing ``GET /runs/{run_id}`` since blueprint B5 — the
    raw ids stay admin-only."""
    run = await db.get(Run, run_id)
    if (
        run is None
        or run.tenant_id != user.tenant_id
        or run.deleted_at is not None
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")

    snapshot = (
        await db.execute(select(RunSnapshot).where(RunSnapshot.run_id == run.id))
    ).scalar_one_or_none()
    base_fields = build_run_detail_fields(run, snapshot)
    return AdminRunDetail(
        **base_fields,
        trace_id=run.trace_id,
        phase2_span_id=run.phase2_span_id,
        trace_url=await effective_trace_url(db, run.trace_id),
        # Operator-facing by the Run Contract, so it is served here and
        # nowhere else (blueprint S7).
        error_detail=run.error_detail,
    )


# ---- Feedback aggregates ----
@router.get("/feedback", response_model=FeedbackAggregate)
async def feedback_aggregate(
    days: int = Query(90, ge=1, le=3650),
    section_type: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    conds = [RunFeedback.tenant_id == user.tenant_id, RunFeedback.created_at >= since]
    if section_type:
        conds.append(RunFeedback.section_type == section_type)

    rows = (
        await db.execute(
            select(RunFeedback.section_type, RunFeedback.rating, func.count()).where(and_(*conds)).group_by(
                RunFeedback.section_type, RunFeedback.rating
            )
        )
    ).all()
    total = 0
    pos = 0
    neg = 0
    per: dict[str, dict[str, int]] = {}
    for section, rating, count in rows:
        per.setdefault(section, {"positive": 0, "negative": 0})[rating] = count
        total += count
        if rating == "positive":
            pos += count
        else:
            neg += count

    per_section = []
    for section, d in per.items():
        tot = d["positive"] + d["negative"]
        per_section.append(
            FeedbackAggregateSection(
                section_type=section,
                positive=d["positive"],
                negative=d["negative"],
                positive_rate=(d["positive"] / tot) if tot else 0.0,
            )
        )

    recent = (
        await db.execute(
            select(RunFeedback).where(and_(*conds, RunFeedback.rating == "negative")).order_by(desc(RunFeedback.created_at)).limit(20)
        )
    ).scalars().all()

    return FeedbackAggregate(
        total=total,
        positive=pos,
        negative=neg,
        positive_rate=(pos / total) if total else 0.0,
        per_section=per_section,
        recent_negatives=[
            {
                "id": str(r.id),
                "run_id": str(r.run_id),
                # Deprecated duplicate, one release (blueprint S1), gone at v1.1.
                "case_id": str(r.run_id),
                "section_type": r.section_type,
                "comment": r.comment,
                "trace_id": r.trace_id,
                "created_at": r.created_at.isoformat(),
            }
            for r in recent
        ],
    )


# ---- OTEL diagnostics ----
@router.get("/otel-status", tags=["admin-observability"])
async def otel_status(_: User = Depends(require_platform_admin)) -> dict:
    """Introspect the live OpenTelemetry TracerProvider and the vendor overlay.

    Shows the provider class, every attached span processor + exporter
    (with endpoint), the OTLP export configuration, and runs a
    ``force_flush(5000ms)`` as a liveness check. ``force_flush_5s=True``
    means the BatchSpanProcessor queue drained inside 5 seconds — spans
    reached the OTLP endpoint. ``False`` means the exporter is stuck;
    pair with ``OTEL_DEBUG=true`` (exporter DEBUG logs) to find the cause.

    ``overlay`` answers the question an operator asks when telemetry
    leaves the box (blueprint S7a): which vendor overlay Vector and the
    otel-bridge were started with, the config files that selection
    loads, and the sinks those files declare. An unrecognised
    ``LIBRERUN_OBS_VENDOR`` is reported ``supported=false`` — Vector and
    the bridge refuse to start on one, so nothing is being forwarded.
    The backend holds none of the vendor's credentials; it has the name
    alone. ``vector`` is the router's reachability FROM THE BACKEND (a
    TCP connect to the OTLP endpoint it exports to), because Vector's
    own API is loopback-only inside its container by design.

    Platform-admin only (K9-05): all it shows is the deployment's, none
    of it a tenant's. Every endpoint passes ``strip_url`` — no URL's
    userinfo or query leaves — and a failed flush names its exception's
    class alone, as ``/health`` does.
    """
    from opentelemetry import trace as _trace

    settings = _config.settings
    provider = _trace.get_tracer_provider()

    result: dict = {
        "provider_class": type(provider).__name__,
        "otel_endpoint": deployment_view.strip_url(settings.OTEL_EXPORTER_OTLP_ENDPOINT) or None,
        "otel_protocol": settings.OTEL_EXPORTER_OTLP_PROTOCOL,
        "otel_service_name": settings.OTEL_SERVICE_NAME,
        "otel_debug": settings.OTEL_DEBUG,
        "trace_viewer": settings.TRACE_VIEWER,
        # Where telemetry goes once it has left this process (S7a).
        "overlay": obs_vendors.overlay_status(settings.LIBRERUN_OBS_VENDOR),
        "vector": await obs_vendors.vector_health(
            settings.OTEL_EXPORTER_OTLP_ENDPOINT
        ),
        "span_processors": [],
    }
    if result["vector"].get("endpoint"):
        result["vector"]["endpoint"] = deployment_view.strip_url(result["vector"]["endpoint"])

    active = getattr(provider, "_active_span_processor", None)
    children = getattr(active, "_span_processors", [active] if active is not None else [])
    for sp in children:
        if sp is None:
            continue
        entry: dict = {"processor_class": type(sp).__name__}
        exp = getattr(sp, "span_exporter", None) or getattr(sp, "_exporter", None)
        if exp is not None:
            entry["exporter_class"] = type(exp).__name__
            endpoint = getattr(exp, "_endpoint", None) or getattr(exp, "endpoint", None)
            if endpoint:
                entry["endpoint"] = deployment_view.strip_url(str(endpoint))
        result["span_processors"].append(entry)

    if hasattr(provider, "force_flush"):
        try:
            result["force_flush_5s"] = bool(provider.force_flush(timeout_millis=5000))
        except Exception as e:
            result["force_flush_5s"] = False
            result["force_flush_error"] = type(e).__name__
    else:
        result["force_flush_5s"] = None

    return result


# ---- App settings ----
@router.get(
    "/settings",
    response_model=list[SettingResponse],
    tags=["admin-settings"],
    summary="List runtime-tunable application settings",
)
async def list_app_settings(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_platform_admin),
):
    """Return every registered app setting with its effective value.

    Values marked ``is_default=true`` are using the ``.env`` / ``config.py``
    fallback; values marked ``false`` have a DB override. A ``secret``
    setting answers ``value`` and ``default_value`` null and says where its
    value comes from in ``secret`` — never the value (L31).
    """
    rows = await app_settings_service.get_all_settings(db)
    return [_setting_response(r) for r in rows]


def _secret_block(state) -> SecretSettingState:
    return SecretSettingState(
        set=state.set,
        source=state.source,
        fingerprint=state.fingerprint,
        updated_at=state.updated_at,
        updated_by=state.updated_by,
    )


def _setting_response(r) -> SettingResponse:
    """One registry row as the API shows it. The secret block rides on a
    secret alone, whose value and default are null by construction."""
    return SettingResponse(
        key=r.key,
        value=r.value,
        default_value=r.default_value,
        value_type=r.value_type,
        description=r.description,
        is_default=r.is_default,
        updated_at=r.updated_at,
        updated_by=r.updated_by,
        secret=_secret_block(r.secret) if r.value_type == "secret" else None,
    )


def _secret_setting_response(spec, state) -> SettingResponse:
    return SettingResponse(
        key=spec.key,
        value=None,
        default_value=None,
        value_type=spec.value_type,
        description=spec.description,
        is_default=not state.set,
        updated_at=state.updated_at,
        updated_by=state.updated_by,
        secret=_secret_block(state),
    )


@router.put(
    "/settings/{key}",
    response_model=SettingResponse,
    tags=["admin-settings"],
    summary="Update a single app setting",
)
async def update_app_setting(
    key: str,
    payload: SettingUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_platform_admin),
):
    """Persist a new value for ``key``. The request value is coerced to the
    setting's declared type before being stored. Editing ``cors_origins`` sets
    a restart-required flag (see ``/health``).

    Platform-operator only: these rows are application-global — every
    tenant's requests read them — so a tenant admin must not be able to
    configure what other tenants see.

    A ``secret`` setting is sealed into the encrypted secrets store (K6):
    the response and the audit row carry no value — the audit detail is
    ``{setting, action: set | replace}`` — and with no store key the write
    answers ``503 secrets_store_unconfigured``."""
    try:
        spec = app_settings_service.get_spec(key)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown setting: {key}")
    if spec.value_type == "secret":
        return await _update_secret_setting(spec, payload, request, db, user)
    try:
        row = await app_settings_service.set_setting(db, key, payload.value, user)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown setting: {key}")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    ip = request.client.host if request.client else None
    await log_audit(
        db,
        user.tenant_id,
        user.id,
        user.email,
        "config_change",
        {"setting": key, "value": row.value},
        ip,
    )
    spec = app_settings_service.get_spec(key)
    return SettingResponse(
        key=key,
        value=row.value,
        default_value=spec.default,
        value_type=spec.value_type,
        description=spec.description,
        is_default=False,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


async def _update_secret_setting(spec, payload, request, db, user) -> SettingResponse:
    # A refusal names the setting and never the value; SecretsStoreUnconfigured
    # is not caught here — the app answers it 503 for every writer alike.
    try:
        write = await app_settings_service.set_secret_setting(db, spec.key, payload.value, user)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    ip = request.client.host if request.client else None
    await log_audit(
        db,
        user.tenant_id,
        user.id,
        user.email,
        "config_change",
        {"setting": spec.key, "action": write.action},
        ip,
    )
    # The row and its audit commit together, and only then is every process
    # told: told first, a reader could cache the old row under the new version.
    await db.commit()
    await app_settings_service.notify_secret_setting(spec.key)
    return _secret_setting_response(
        spec,
        app_settings_service.SecretState(
            set=True,
            source="runtime",
            fingerprint=write.fingerprint,
            updated_at=write.updated_at,
            updated_by=write.updated_by,
        ),
    )


@router.post(
    "/settings/reset/{key}",
    response_model=SettingResponse,
    tags=["admin-settings"],
    summary="Reset a setting to its .env / config default",
)
async def reset_app_setting(
    key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_platform_admin),
):
    """Delete the DB override for ``key``, reverting to the ``.env`` default.

    For a ``secret`` setting this is Clear: the store's row is removed, the
    environment's value applies again, and the audit detail is ``{setting,
    action: clear}``."""
    try:
        spec = app_settings_service.get_spec(key)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown setting: {key}")
    if spec.value_type == "secret":
        await app_settings_service.clear_secret_setting(db, key)
        ip = request.client.host if request.client else None
        await log_audit(
            db,
            user.tenant_id,
            user.id,
            user.email,
            "config_change",
            {"setting": key, "action": "clear"},
            ip,
        )
        # The answer is read before the commit (Codex on #172): read after
        # it, a failed read would answer 500 for a clear already durable.
        # Inside the transaction the row is already gone, so the state is
        # the one the commit makes true.
        response = _secret_setting_response(
            spec, await app_settings_service.get_secret_state(db, key)
        )
        await db.commit()
        await app_settings_service.notify_secret_setting(key)
        return response
    try:
        await app_settings_service.reset_setting(db, key)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown setting: {key}")

    ip = request.client.host if request.client else None
    await log_audit(
        db,
        user.tenant_id,
        user.id,
        user.email,
        "config_change",
        {"setting": key, "action": "reset"},
        ip,
    )
    spec = app_settings_service.get_spec(key)
    return SettingResponse(
        key=key,
        value=spec.default,
        default_value=spec.default,
        value_type=spec.value_type,
        description=spec.description,
        is_default=True,
        updated_at=None,
        updated_by=None,
    )


# ---- Audit log ----
@router.get("/audit-log")
async def list_audit_log(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    action_type: str | None = None,
    user_email: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    conds = [ActivityAuditLog.tenant_id == user.tenant_id]
    if action_type:
        conds.append(ActivityAuditLog.action_type == action_type)
    if user_email:
        conds.append(ActivityAuditLog.user_email == user_email)

    total = (
        await db.execute(select(func.count()).where(and_(*conds)))
    ).scalar_one()
    rows = (
        await db.execute(
            select(ActivityAuditLog)
            .where(and_(*conds))
            .order_by(desc(ActivityAuditLog.created_at))
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).scalars().all()
    return {
        "entries": [AuditLogEntry.from_row(r).model_dump(mode="json") for r in rows],
        "total": total,
    }


# ---- Schema-drift summary ----
@router.get("/drift-summary")
async def drift_summary(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Counts and recent samples of LLM schema-drift audit events.

    Tenant-scoped via the admin's own tenant_id; require_admin enforces role.
    """
    now = datetime.now(timezone.utc)
    twenty_four_hours_ago = now - timedelta(hours=24)
    seven_days_ago = now - timedelta(days=7)

    tenant_scope = and_(
        ActivityAuditLog.tenant_id == user.tenant_id,
        ActivityAuditLog.action_type == SCHEMA_DRIFT_ACTION_TYPE,
    )

    last_24h = (
        await db.execute(
            select(func.count())
            .select_from(ActivityAuditLog)
            .where(and_(tenant_scope, ActivityAuditLog.created_at >= twenty_four_hours_ago))
        )
    ).scalar_one()

    last_7d = (
        await db.execute(
            select(func.count())
            .select_from(ActivityAuditLog)
            .where(and_(tenant_scope, ActivityAuditLog.created_at >= seven_days_ago))
        )
    ).scalar_one()

    recent_rows = (
        await db.execute(
            select(ActivityAuditLog)
            .where(tenant_scope)
            .order_by(desc(ActivityAuditLog.created_at))
            .limit(20)
        )
    ).scalars().all()

    by_step: dict[str, int] = {}
    by_model: dict[str, int] = {}
    recent: list[dict] = []
    for row in recent_rows:
        detail = row.detail or {}
        reports = detail.get("reports") or []
        for r in reports:
            step_id = r.get("step_id") or "unknown"
            model = r.get("model") or "unknown"
            by_step[step_id] = by_step.get(step_id, 0) + 1
            by_model[model] = by_model.get(model, 0) + 1
        primary = reports[0] if reports else {}
        # Audit rows are history: those written before S1 carry the run id
        # under ``case_id`` in their detail JSON and are never rewritten.
        drift_run_id = detail.get("run_id") or detail.get("case_id")
        recent.append({
            "id": str(row.id),
            "run_id": drift_run_id,
            # Deprecated duplicate, one release (blueprint S1), gone at v1.1.
            "case_id": drift_run_id,
            "step_id": primary.get("step_id"),
            "drift_type": primary.get("drift_type"),
            "model": primary.get("model"),
            "report_count": len(reports),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })

    return {
        "last_24h": last_24h,
        "last_7d": last_7d,
        "by_step": by_step,
        "by_model": by_model,
        "recent": recent,
    }


# ---------------------------------------------------------------------------
# Per-agent gateway keys (blueprint S4a, D10)
#
# Platform-scoped and behind the PLATFORM-admin gate, deliberately: a key
# names an agent, not a tenant, so a tenant admin issuing one would be
# minting a credential that answers for every tenant's runs. The value is
# shown once, at issuance, and never stored — an operator who loses it
# rotates rather than recovers.
#
# An agent id takes the manifest's charset and length (K9-08), so a value
# no agent could have is a 422 before any query: the agent page and the
# hub's agent-id field send ids a manifest could declare, nothing else.
# ---------------------------------------------------------------------------

AGENT_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
AGENT_ID_MAX_LENGTH = 50


def _agent_id_path():
    return Path(pattern=AGENT_ID_PATTERN, max_length=AGENT_ID_MAX_LENGTH)


@router.get("/agent-keys", response_model=list[AgentKeyRow])
async def list_agent_keys(
    agent_id: str | None = Query(
        default=None, pattern=AGENT_ID_PATTERN, max_length=AGENT_ID_MAX_LENGTH
    ),
    _: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    keys = await agent_key_service.list_keys(db, agent_id)
    return [
        AgentKeyRow(
            **row,
            # What the admin page needs to know without offering a button
            # that cannot work: an env key is rotated in .env, not here.
            rotatable=row["source"] == "admin",
            # A key for an id no agent here answers to: installed ahead of
            # its agent, or left behind by one uninstalled (K9-02).
            registered=get_agent(row["agent_id"]) is not None,
        )
        for row in keys
    ]


@router.post(
    "/agent-keys/{agent_id}",
    status_code=status.HTTP_201_CREATED,
    response_model=IssuedAgentKey,
)
async def issue_agent_key(
    request: Request,
    agent_id: str = _agent_id_path(),
    user: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """A first key for an agent that has none. The value is in this
    response and nowhere else, ever again."""
    try:
        issued = await agent_key_service.issue(db, agent_id, user.id)
    except agent_key_service.KeyError_ as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    await log_audit(
        db,
        user.tenant_id,
        user.id,
        user.email,
        "config_change",
        {"surface": "agent_keys", "agent_id": agent_id, "action": "issue",
         "key_prefix": issued.prefix},
        request.client.host if request.client else None,
    )
    return IssuedAgentKey(agent_id=agent_id, key=issued.value, key_prefix=issued.prefix)


@router.post("/agent-keys/{agent_id}/rotate", response_model=RotatedAgentKey)
async def rotate_agent_key(
    request: Request,
    agent_id: str = _agent_id_path(),
    grace_hours: int = Query(
        default=agent_key_service.DEFAULT_GRACE_HOURS, ge=0, le=720
    ),
    user: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """A new current key; the old one keeps working for ``grace_hours``
    so a container still holding it has time to be recreated."""
    try:
        issued = await agent_key_service.rotate(db, agent_id, user.id, grace_hours)
    except agent_key_service.KeyError_ as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    await log_audit(
        db,
        user.tenant_id,
        user.id,
        user.email,
        "config_change",
        {"surface": "agent_keys", "agent_id": agent_id, "action": "rotate",
         "key_prefix": issued.prefix, "grace_hours": grace_hours},
        request.client.host if request.client else None,
    )
    return RotatedAgentKey(
        agent_id=agent_id,
        key=issued.value,
        key_prefix=issued.prefix,
        grace_hours=grace_hours,
    )


@router.delete("/agent-keys/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_agent_keys(
    request: Request,
    agent_id: str = _agent_id_path(),
    user: User = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
):
    """Drop every admin-issued key for one agent. An environment key is
    left alone — the environment is its source of truth and the gateway
    would reinstall it at the next boot, so deleting one here would look
    like a revocation that silently undid itself."""
    removed = await agent_key_service.revoke(db, agent_id)
    await log_audit(
        db,
        user.tenant_id,
        user.id,
        user.email,
        "config_change",
        {"surface": "agent_keys", "agent_id": agent_id, "action": "revoke",
         "removed": removed},
        request.client.host if request.client else None,
    )


# ---------------------------------------------------------------------------
# Model providers (K7; L33, D16, D19, D34)
#
# A provider key pasted in Application Settings arrives SEALED to the
# gateway's public key, which the browser read from the GET below; the
# backend stores and relays ciphertext it cannot open, and the gateway
# adopts it and serves it to the next model call. Platform-admin only, as
# the provider keys are the deployment's. No key passes through here, so
# none can be returned, logged or audited: the audit detail is
# {surface, provider, action}.
# ---------------------------------------------------------------------------


def _provider_refusal(status_code: int, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": str(exc), "code": exc.code})


@router.get(
    "/providers",
    response_model=ProvidersStatus,
    tags=["admin-settings"],
    summary="What the gateway holds for each model provider, and the key to seal to",
)
async def list_providers(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_platform_admin),
):
    """The gateway's own report (``gateway_status``): each provider-key name
    with its aliases, where its key comes from, a stored key's fingerprint
    and state — ``pending`` while a key set here waits for the gateway —
    and the public key the browser seals a new one to. Never a key."""
    return await provider_keys_service.providers(db)


@router.post(
    "/providers/{name}/key",
    response_model=ProviderKeyAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["admin-settings"],
    summary="Store a provider key sealed to the gateway, for the gateway to adopt",
    responses={
        400: {"description": "plaintext_refused: the body is not one sealed 3072-bit block"},
        404: {"description": "unknown_provider"},
        503: {"description": "secrets_store_unconfigured: the gateway published no public key"},
    },
)
async def set_provider_key(
    name: str,
    payload: ProviderKeyUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_platform_admin),
):
    """``202 {name, row: pending}``: the blob is stored as the ``gateway``
    row ``provider.<name>``; the gateway adopts it within seconds, and
    the list then shows its fingerprint. A raw key is refused ``400
    plaintext_refused`` and nothing is written."""
    try:
        action = await provider_keys_service.set_key(db, name, payload.sealed, user_id=user.id)
    except provider_keys_service.UnknownProvider as exc:
        return _provider_refusal(status.HTTP_404_NOT_FOUND, exc)
    except provider_keys_service.PlaintextRefused as exc:
        return _provider_refusal(status.HTTP_400_BAD_REQUEST, exc)
    await log_audit(
        db,
        user.tenant_id,
        user.id,
        user.email,
        "config_change",
        {"surface": provider_keys_service.SURFACE, "provider": name, "action": action},
        request.client.host if request.client else None,
    )
    # The row and its audit commit together, and only then is the gateway
    # told: told first, it could read the new version and the old row.
    await db.commit()
    await provider_keys_service.notify_gateway()
    return ProviderKeyAccepted(name=name)


@router.delete(
    "/providers/{name}/key",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["admin-settings"],
    summary="Remove a stored provider key; the gateway's environment serves that provider again",
    responses={404: {"description": "unknown_provider"}},
)
async def clear_provider_key(
    name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_platform_admin),
):
    """The stored row goes, whatever its state, and the gateway drops the
    key at its next reload. Audited ``{surface, provider, action: clear}``."""
    try:
        await provider_keys_service.clear_key(db, name)
    except provider_keys_service.UnknownProvider as exc:
        return _provider_refusal(status.HTTP_404_NOT_FOUND, exc)
    await log_audit(
        db,
        user.tenant_id,
        user.id,
        user.email,
        "config_change",
        {"surface": provider_keys_service.SURFACE, "provider": name, "action": "clear"},
        request.client.host if request.client else None,
    )
    await db.commit()
    await provider_keys_service.notify_gateway()


# ---------------------------------------------------------------------------
# The deployment (K9-04; D16, L29, D43)
#
# The posture, read-only, each value with its source, for Application
# Settings' Deployment panel and doctor. An allowlist of names read one by
# one (services/deployment_view.py): never a secret, the environment, a
# header's value or a URL's userinfo or query. Platform-admin only, as
# every value is the deployment's.
# ---------------------------------------------------------------------------


@router.get(
    "/deployment",
    response_model=DeploymentView,
    tags=["admin-settings"],
    summary="The deployment as the backend reads it: posture read-only, each value with its source",
)
async def get_deployment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_platform_admin),
):
    """The version, licence and source; the gateway's version, report and
    keyless mode; each allowlisted setting with its value, ``env`` or
    ``default``, and where to change it; the OTLP header variables, set or
    not; and ``transport``, this request's own scheme and host (D43):
    ``https`` through the edge, whose forwarding headers the backend
    believes from the edge's address alone, or plain ``http``."""
    return await deployment_view.build(
        db, transport={"scheme": request.url.scheme, "host": request.url.hostname}
    )


# ---------------------------------------------------------------------------
# Certificates at the edge (T2; L42, L43, D44 refined)
#
# What the HTTPS edge serves and what it needs next, read from
# edge-control's public status. The four changes — a CA, a certificate and
# key, ACME, back to the environment — are edge-control's, reached through
# the edge alone: the edge asks /tls/authorize below first, with the
# request's headers and never its body, so a key never enters this process,
# where an in-process agent runs. Platform-admin only: the edge's
# certificate is the deployment's.
# ---------------------------------------------------------------------------


@router.get(
    "/tls",
    response_model=TlsStatus,
    tags=["admin-settings"],
    summary="What the HTTPS edge serves, the source in effect and why, and what it needs next",
)
async def get_tls_status(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_platform_admin),
):
    """The site's names, the issuer, the root and the leaf the edge serves —
    public material alone — the source in effect (the environment, or a
    choice made here) and why, and ``needs``, each with its action. With no
    answer from edge-control, ``200`` and ``needs: [edge_off]``: the ``tls``
    profile is off, and there is nothing to change."""
    return await edge_tls.status(db, user)


@router.get(
    "/tls/root.pem",
    tags=["admin-settings"],
    summary="The root the edge issues from, for the browsers to trust",
    response_class=Response,
    responses={
        200: {"content": {"application/x-pem-file": {}}, "description": "The root certificate, PEM"},
        404: {"description": "no_root: the edge issues from no CA of its own, or is off"},
    },
)
async def get_tls_root(_: User = Depends(require_platform_admin)):
    """Public: the certificate every browsing machine trusts
    (``docs/platform/Install.md``, "Trust the local CA"). Never a key."""
    pem = await edge_tls.root_pem()
    if pem is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": "The edge issues from no CA of its own, or is off.", "code": "no_root"},
        )
    return Response(
        content=pem,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": 'attachment; filename="librerun-edge-root.crt"'},
    )


@router.post(
    "/tls/acknowledge",
    response_model=TlsStatus,
    tags=["admin-settings"],
    summary="Record the root in use as the one the browsers were told to trust",
    responses={409: {"description": "no_root: the edge issues from no CA of its own, or is off"}},
)
async def acknowledge_tls_root(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_platform_admin),
):
    """Clears ``trust_root`` and ``root_changed`` until the root changes.
    Audited ``config_change`` ``{surface: tls, action: acknowledge_root,
    root}``, the root's SHA-256."""
    sha = await edge_tls.acknowledge_root(db, user)
    if sha is None:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "The edge issues from no CA of its own, or is off.", "code": "no_root"},
        )
    await log_audit(
        db,
        user.tenant_id,
        user.id,
        user.email,
        "config_change",
        {"surface": edge_tls.SURFACE, "action": "acknowledge_root", "root": sha},
        request.client.host if request.client else None,
    )
    await db.commit()
    return await edge_tls.status(db, user)


@router.get(
    "/tls/authorize",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["admin-settings"],
    summary="The edge's forward_auth for a certificate change: reads no body",
    responses={404: {"description": "not_a_change: the forwarded method and path are not one of the four"}},
)
async def authorize_tls_change(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_platform_admin),
):
    """Asked by the edge before it sends a certificate change to
    edge-control, with the request's headers — its ``X-Forwarded-Method``
    and ``X-Forwarded-Uri`` — and never its body, which this route does not
    read. A platform admin gets ``204`` and ``X-Librerun-Chosen-By``, their
    id, which the edge passes on in place of any a client sent; anyone else
    gets the gate's refusal, which the edge returns as it is. The change it
    lets through is audited ``config_change`` ``{surface: tls, route,
    change}``, as K7's are."""
    change = edge_tls.authorized_change(
        request.headers.get("x-forwarded-method"), request.headers.get("x-forwarded-uri")
    )
    if change is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": "Not a certificate change.", "code": "not_a_change"},
        )
    route, name = change
    await log_audit(
        db,
        user.tenant_id,
        user.id,
        user.email,
        "config_change",
        {"surface": edge_tls.SURFACE, "route": route, "change": name},
        request.client.host if request.client else None,
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"X-Librerun-Chosen-By": str(user.id)})
