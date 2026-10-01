"""The browser telemetry relay — LibreRun RUM v1 ingestion.

``POST /api/v1/_o/e`` (the path is deliberately neutral: common filter
lists carry generic first-party rules for ``/collect``/``/rum``-style
paths, so the name is an availability optimization — never a security
control). The browser POSTs the closed envelope from
``observability/rum_envelope.py``; this handler is the trust boundary:

1. kill switch — ``UX_TELEMETRY_ENABLED=false`` answers 410 and the
   client backs off for the rest of its session (runtime off switch, no
   frontend rebuild);
2. authentication — the ordinary session JWT, checked BEFORE the body is
   read; anonymous telemetry does not exist;
3. bounded read — Content-Length precheck plus a streamed hard cap, so a
   lying client cannot make us buffer more than ``MAX_BODY_BYTES``; a
   request that trips either cap is charged quota at the cap cost before
   the 413 leaves (a reject is never free);
4. structural validation — strict JSON (NaN/Infinity rejected), strict
   envelope (unknown fields rejected: this is our schema, not OTLP);
5. quotas — cost-weighted (records and kibibytes, whichever is larger)
   fixed-window buckets per user, tenant, and process-global, keyed on
   the VERIFIED principal, never on payload fields;
6. per-record validation — schema, clock-skew window, and surface
   authorization (an ``agent_id`` must exist in the registry; a
   ``run_id`` must be the tenant's own live run of that agent). Bad
   records drop individually; the batch survives;
7. stamping — tenant id and an HMAC user pseudonym derive from the
   verified session; the payload cannot claim identity at all (the
   schema has no such fields), and everything emitted is marked
   ``librerun.telemetry.source=browser_untrusted``.

Accepted records are handed to ``observability/web_telemetry.py`` — the
only place OTel objects are built for browser data. The response is a
tiny ``202 {"accepted": n, "dropped": m, "reasons": {...}}``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import json
import time
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import config as _config
from app.agents.registry import get_manifest
from app.database import get_db
from app.middleware import get_current_user
from app.models import Run, User
from app.observability.rum_envelope import (
    MAX_BODY_BYTES,
    MAX_FUTURE_SKEW_MS,
    MAX_PAST_SKEW_MS,
    RumEnvelope,
    record_adapter,
)
from app.observability.web_telemetry import (
    RelayContext,
    get_web_telemetry,
    now_ms,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/_o", tags=["observability"])

_WINDOW_SECONDS = 60
# Hard deadline on the quota round-trip. The shared redis client sets no
# socket timeout, so a Redis that accepts connections but stops
# responding would otherwise hang this await forever — and an eternal
# await raises nothing, so the fail-open except below would never run
# while relay requests pile up holding worker and DB-session resources.
# asyncio.wait_for turns that hang into a TimeoutError → fail open.
_QUOTA_TIMEOUT_SECONDS = 2.0
# What an attempt that trips the body cap costs: priced at the cap it
# tried to breach. On the streamed path (no/lying Content-Length) the
# server has already read and buffered the full cap before the 413
# exists, and a free 413 would let an authenticated caller loop
# over-limit bodies past the relay's only rate-based control — the same
# rule that makes malformed JSON cost its bytes. The declared
# Content-Length reject is priced identically so honesty about the size
# is never the cheaper way to spam the endpoint.
_OVERLIMIT_BODY_COST = MAX_BODY_BYTES // 1024


async def require_ux_telemetry_enabled() -> None:
    """410 Gone when the operator switched UX telemetry off. Declared as
    the route's first dependency so the answer costs nothing — no auth,
    no body read — and 410 (vs 404) tells the client to stop for good."""
    if not _config.settings.UX_TELEMETRY_ENABLED:
        raise HTTPException(status.HTTP_410_GONE, "UX telemetry is disabled")


def user_pseudonym(tenant_id: UUID, user_id: UUID) -> str:
    """Tenant-scoped keyed pseudonym for the telemetry stream.

    HMAC, not a bare hash: user ids are enumerable, so an unkeyed digest
    would be a dictionary-reversible rename rather than a pseudonym. The
    key never leaves the server and differs per tenant, so the same user
    id maps to unrelated pseudonyms under different tenants.
    """
    key = hashlib.sha256(
        f"{_config.settings.APP_SECRET_KEY.get_secret_value()}"
        f":ux-pseudonym:{tenant_id}".encode()
    ).digest()
    return hmac_mod.new(key, str(user_id).encode(), hashlib.sha256).hexdigest()[:32]


async def _read_bounded_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                raise HTTPException(
                    status.HTTP_413_CONTENT_TOO_LARGE, "Body too large"
                )
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bad Content-Length")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE, "Body too large"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_strict_json(raw: bytes) -> dict:
    def _reject_constant(_name: str) -> None:
        raise ValueError("non-finite JSON numbers are not accepted")

    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Envelope must be an object")
    return payload


async def _enforce_quota(user: User, cost: int) -> None:
    """Cost-weighted fixed-window quotas on the verified principal.

    Fails OPEN on Redis errors: quota is an abuse bound, not a
    correctness gate, and the endpoint keeps its hard body/record caps
    and authentication either way — a Redis outage should degrade abuse
    protection, not turn telemetry into a 500 source.
    """
    settings = _config.settings
    window = int(time.time()) // _WINDOW_SECONDS
    scopes = (
        (f"rum:q:u:{user.id}:{window}", settings.UX_TELEMETRY_USER_UNITS_PER_MINUTE),
        (
            f"rum:q:t:{user.tenant_id}:{window}",
            settings.UX_TELEMETRY_TENANT_UNITS_PER_MINUTE,
        ),
        (f"rum:q:g:{window}", settings.UX_TELEMETRY_GLOBAL_UNITS_PER_MINUTE),
    )
    async def _run_pipeline() -> list:
        from app.redis import get_redis

        redis = await get_redis()
        pipe = redis.pipeline()
        for key, _limit in scopes:
            pipe.incrby(key, cost)
            pipe.expire(key, _WINDOW_SECONDS * 2)
        return await pipe.execute()

    try:
        results = await asyncio.wait_for(
            _run_pipeline(), timeout=_QUOTA_TIMEOUT_SECONDS
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — fail open, see docstring
        logger.warning(
            "ux_telemetry_quota_unavailable",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return
    counters = results[::2]  # incrby/expire interleaved
    for (key, limit), counter in zip(scopes, counters):
        if counter > limit:
            retry_after = _WINDOW_SECONDS - (int(time.time()) % _WINDOW_SECONDS)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "UX telemetry quota exceeded",
                headers={"Retry-After": str(retry_after)},
            )


@router.post("/e", status_code=status.HTTP_202_ACCEPTED)
async def ingest_ux_telemetry(
    request: Request,
    _enabled: None = Depends(require_ux_telemetry_enabled),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        raw = await _read_bounded_body(request)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_413_CONTENT_TOO_LARGE:
            # Charge before propagating (see _OVERLIMIT_BODY_COST). An
            # exhausted bucket raises 429 here, superseding the 413 —
            # once quota is spent, the quota answer wins, exactly as it
            # does for malformed JSON below.
            await _enforce_quota(user, _OVERLIMIT_BODY_COST)
        raise

    # Quota BEFORE parsing: a 400 must still cost quota, or an
    # authenticated caller could loop malformed 128 KiB bodies forever —
    # each forcing a full read + JSON parse + envelope validation —
    # without ever touching the relay's only rate-based abuse control.
    # Bytes are charged here; the record-count component (the other half
    # of the max(records, KiB) cost model) tops up after validation.
    byte_cost = max(1, (len(raw) + 1023) // 1024)
    await _enforce_quota(user, byte_cost)

    payload = _parse_strict_json(raw)

    try:
        envelope = RumEnvelope.model_validate(payload)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Envelope failed validation")

    record_cost = len(envelope.records) - byte_cost
    if record_cost > 0:
        await _enforce_quota(user, record_cost)

    reasons = {"schema": 0, "skew": 0, "surface": 0}
    now = now_ms()
    candidates = []
    for raw_record in envelope.records:
        try:
            rec = record_adapter.validate_python(raw_record)
        except Exception:
            reasons["schema"] += 1
            continue
        if not (now - MAX_PAST_SKEW_MS <= rec.occurred_at_ms <= now + MAX_FUTURE_SKEW_MS):
            reasons["skew"] += 1
            continue
        candidates.append(rec)

    # Surface authorization: agent claims must name a registered agent;
    # run claims must be the tenant's own live run OF that agent. One
    # batched query covers every claimed run id.
    claimed_run_ids = {rec.run_id for rec in candidates if rec.run_id is not None}
    runs_by_id: dict[UUID, str | None] = {}
    if claimed_run_ids:
        rows = await db.execute(
            select(Run.id, Run.agent_id).where(
                Run.id.in_(claimed_run_ids),
                Run.tenant_id == user.tenant_id,
                Run.deleted_at.is_(None),
            )
        )
        runs_by_id = {row.id: row.agent_id for row in rows}

    accepted = []
    for rec in candidates:
        if rec.ui_owner == "agent":
            if get_manifest(rec.agent_id) is None:
                reasons["surface"] += 1
                continue
            if rec.run_id is not None:
                owner_agent = runs_by_id.get(rec.run_id)
                if owner_agent is None or owner_agent != rec.agent_id:
                    reasons["surface"] += 1
                    continue
        accepted.append(rec)

    ctx = RelayContext(
        tenant_id=str(user.tenant_id),
        user_pseudonym=user_pseudonym(user.tenant_id, user.id),
        session_id=str(envelope.session_id),
        app_version=envelope.app_version,
        received_at_ns=time.time_ns(),
    )
    get_web_telemetry().emit(ctx, accepted)

    dropped = sum(reasons.values())
    if dropped:
        logger.info(
            "ux_telemetry_records_dropped",
            accepted=len(accepted),
            **{f"dropped_{k}": v for k, v in reasons.items() if v},
        )
    return {
        "accepted": len(accepted),
        "dropped": dropped,
        "reasons": {k: v for k, v in reasons.items() if v},
    }
