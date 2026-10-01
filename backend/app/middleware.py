from datetime import datetime, timezone
from uuid import UUID

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from jose.exceptions import ExpiredSignatureError
from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.logging_context import bind_request_context
from app.models import Tenant, User
from app.services.auth_service import (
    claims_without_expiry_check,
    decode_jwt,
    get_session,
)

logger = structlog.get_logger(__name__)

bearer = HTTPBearer(auto_error=False)

# Identity taken from an unverified JWT, kept under names of its own.
#
# The verified attributes (``tenant.id`` / ``user.id``, written by the span
# enricher from the request context) must keep meaning "this request proved
# who it was". A rejected request has not, so its claims get separate names —
# enough to attribute a burst of 401s to one user without ever implying the
# claim was checked.
_AUTH_FAILURE_ATTR = "librerun.auth.failure_reason"
_CLAIMED_ATTRS = (
    ("librerun.auth.claimed_tenant_id", "tenant_id"),
    ("librerun.auth.claimed_user_id", "sub"),
    ("librerun.auth.claimed_session_id", "session_id"),
)


def _unauthorized(reason: str, claims: dict | None = None) -> HTTPException:
    """Build the 401, recording *why* on the live span and in the log.

    The reason used to exist only in the response body, which nothing logs or
    stamps. A burst of 401s was therefore indistinguishable between a token
    that expired, a session revoked by a logout in another tab, an admin
    revoke, and a deactivated user — the four have very different causes and
    the same blank signature.

    The span is stamped directly rather than through the request context,
    because the enricher copies context onto a span at ``on_start`` and the
    request span has long since started by the time a dependency runs.
    """
    attributes = {_AUTH_FAILURE_ATTR: reason}
    for attribute, claim in _CLAIMED_ATTRS:
        value = (claims or {}).get(claim)
        if value:
            attributes[attribute] = str(value)

    span = trace.get_current_span()
    if span is not None and span.is_recording():
        span.set_attributes(attributes)

    logger.warning(
        "auth_unauthorized",
        reason=reason,
        **{
            name.removeprefix("librerun.auth."): value
            for name, value in attributes.items()
            if name != _AUTH_FAILURE_ATTR
        },
    )
    return HTTPException(status.HTTP_401_UNAUTHORIZED, reason)


async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise _unauthorized("Missing bearer token")
    try:
        payload = decode_jwt(creds.credentials)
    except ExpiredSignatureError:
        # A token that simply aged out is the commonest cause of a 401 burst
        # and the whole reason this path records a reason at all. jose raises
        # this as a JWTError subclass, so catching JWTError alone filed every
        # ordinary expiry under "Invalid token" beside malformed and badly
        # signed ones — leaving the single case worth telling apart
        # indistinguishable from the rest, and discarding its claims with it.
        #
        # Note this branch, not the later "Session expired" one, is where a
        # normal expiry lands: the JWT's own exp is checked before the
        # session row is ever read.
        #
        # The signature was already verified to get here; only exp failed, so
        # the claims can be re-read to say who asked. They stay claimed, never
        # verified.
        raise _unauthorized("Token expired", claims_without_expiry_check(creds.credentials))
    except JWTError:
        raise _unauthorized("Invalid token")

    # A correctly signed token can still carry an unusable session_id. Left
    # unguarded this raised KeyError/ValueError and surfaced as a 500, which
    # reads as a server fault rather than the auth rejection it is.
    try:
        session_id = UUID(payload["session_id"])
    except (KeyError, ValueError, TypeError):
        raise _unauthorized("Invalid token claims", payload)

    session = await get_session(db, session_id)
    if session is None:
        raise _unauthorized("Session not found", payload)
    if session.revoked_at is not None:
        raise _unauthorized("Session revoked", payload)
    now = datetime.now(timezone.utc)
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise _unauthorized("Session expired", payload)

    user = await db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise _unauthorized("User inactive", payload)

    request.state.current_user = user
    request.state.tenant_id = user.tenant_id
    request.state.session_id = session.id
    # Opaque ids only (blueprint S4): the email left the log context.
    bind_request_context(
        tenant_id=str(user.tenant_id),
        user_id=str(user.id),
    )
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user


async def is_platform_admin(user: User, db: AsyncSession) -> bool:
    """Whether ``user`` is an admin of the PLATFORM tenant.

    The one predicate: ``require_platform_admin`` gates on it, and
    ``GET /auth/me`` reports it so the UI can say which pages are the
    operator's. The report is a display hint and never a permission — every
    route keeps its own gate, so a client that forged the flag gains a
    page it cannot load, not a setting it can write.
    """
    from app.config import settings as _settings

    if user.role != "admin":
        return False
    tenant = await db.get(Tenant, user.tenant_id)
    return tenant is not None and tenant.slug == _settings.PLATFORM_TENANT_SLUG


async def require_platform_admin(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Admin of the PLATFORM tenant — the deployment operator, not a tenant.

    ``require_admin`` is tenant-scoped: every tenant has its own admins, and
    they administer *their tenant*. Application-global state — the
    ``app_settings`` rows, which every tenant's requests read — must not be
    writable by one tenant's admin, or tenant A configures what tenant B
    sees (e.g. an attacker-controlled trace-viewer URL template rendered as
    "View trace" for every tenant, disclosing trace ids on click).

    The platform tenant is the one the operator's own bootstrap credentials
    land in: ``bootstrap_admin.py`` provisions into the tenant whose slug is
    ``PLATFORM_TENANT_SLUG`` (default ``dev``, the row seeded by the schema).
    Single-tenant deployments are unaffected — the only admin there IS the
    platform admin.
    """
    if not await is_platform_admin(user, db):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Platform operator only — this setting is application-global",
        )
    return user
