from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.middleware import get_current_user, is_platform_admin
from app.models import Session, Tenant, User
from app.schemas.auth import (
    GoogleLoginRequest,
    LoginRequest,
    MicrosoftLoginRequest,
    TokenResponse,
    UserProfile,
)
from app.services import app_settings_service
from app.services.audit_service import log_audit
from app.services.auth_service import (
    create_session,
    get_user_by_email,
    verify_password,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


async def _default_tenant(db: AsyncSession) -> Tenant:
    """The platform tenant — same slug bootstrap provisions into and
    require_platform_admin gates on, so one knob (PLATFORM_TENANT_SLUG)
    keeps login, bootstrap and the settings gate coherent."""
    result = await db.execute(
        select(Tenant).where(Tenant.slug == settings.PLATFORM_TENANT_SLUG)
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "No default tenant configured")
    return tenant


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    tenant = await _default_tenant(db)
    ip = request.client.host if request.client else None
    user = await get_user_by_email(db, tenant.id, payload.email)
    if user is None or user.password_hash is None or not verify_password(payload.password, user.password_hash):
        logger.warning(
            "auth_sign_in_failed",
            provider="credentials",
            email=payload.email,
            ip=ip,
            reason="invalid_credentials",
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    if not user.is_active:
        logger.warning(
            "auth_sign_in_failed",
            provider="credentials",
            email=payload.email,
            ip=ip,
            reason="user_inactive",
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User inactive")

    ua = request.headers.get("user-agent")
    _, token = await create_session(db, user, ip=ip, user_agent=ua)
    await log_audit(db, tenant.id, user.id, user.email, "sign_in", {"provider": "credentials"}, ip)
    logger.info(
        "auth_sign_in",
        provider="credentials",
        email=user.email,
        user_id=str(user.id),
        tenant_id=str(tenant.id),
        ip=ip,
    )
    return TokenResponse(access_token=token)


@router.post("/google", response_model=TokenResponse)
async def login_google(payload: GoogleLoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    # Verify Google ID token
    try:
        from google.oauth2 import id_token  # type: ignore
        from google.auth.transport import requests as google_requests  # type: ignore
    except ImportError:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Google auth library not available")

    # The client id is a runtime setting (K6): read per request, so an
    # edit in Admin -> Settings applies to the next sign-in. It is all
    # Google needs — the ID token is verified against it alone.
    client_id = await app_settings_service.get_setting(db, "auth.google_client_id")
    if not client_id:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Google SSO not configured")

    ip = request.client.host if request.client else None
    try:
        info = id_token.verify_oauth2_token(
            payload.credential, google_requests.Request(), client_id
        )
    except ValueError as e:
        logger.warning(
            "auth_sign_in_failed",
            provider="google",
            ip=ip,
            reason="invalid_token",
            error=str(e),
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid Google token: {e}")

    email = info.get("email")
    if not email:
        logger.warning(
            "auth_sign_in_failed",
            provider="google",
            ip=ip,
            reason="missing_email",
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No email in Google token")

    tenant = await _default_tenant(db)
    auth_cfg = tenant.auth_config or {}
    if not auth_cfg.get("google_enabled", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Google SSO disabled")
    allowed_domains = auth_cfg.get("google_allowed_domains", []) or []
    allowed_emails = auth_cfg.get("google_allowed_emails", []) or []
    if allowed_domains or allowed_emails:
        domain = email.split("@")[-1]
        if domain not in allowed_domains and email not in allowed_emails:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Email not allowed")

    user = await get_user_by_email(db, tenant.id, email)
    if user is None:
        user = User(
            tenant_id=tenant.id,
            email=email,
            auth_provider="google",
            display_name=info.get("name") or email,
            role="customer",
        )
        db.add(user)
        await db.flush()

    ua = request.headers.get("user-agent")
    _, token = await create_session(db, user, ip=ip, user_agent=ua)
    await log_audit(db, tenant.id, user.id, user.email, "sign_in", {"provider": "google"}, ip)
    logger.info(
        "auth_sign_in",
        provider="google",
        email=user.email,
        user_id=str(user.id),
        tenant_id=str(tenant.id),
        ip=ip,
    )
    return TokenResponse(access_token=token)


@router.post("/microsoft", response_model=TokenResponse)
async def login_microsoft(payload: MicrosoftLoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    # Every value is read per request (K6): the two ids from the settings
    # registry, the secret from the encrypted store with the environment
    # as its fallback. A platform admin who replaces the secret in Admin ->
    # Settings changes the next sign-in with nothing restarted.
    client_id = await app_settings_service.get_setting(db, "auth.azure_client_id")
    azure_tenant = await app_settings_service.get_setting(db, "auth.azure_tenant_id")
    azure_client_secret = await app_settings_service.get_secret_setting(
        db, "auth.azure_client_secret"
    )
    if not client_id or not azure_client_secret:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Microsoft SSO not configured")

    try:
        import msal  # type: ignore
    except ImportError:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "MSAL library not available")

    authority = f"https://login.microsoftonline.com/{azure_tenant or 'common'}"
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=authority,
        client_credential=azure_client_secret,
    )
    result = app.acquire_token_by_authorization_code(
        payload.code,
        scopes=["User.Read"],
        redirect_uri=payload.redirect_uri or "http://localhost:3000/login",
    )
    ip = request.client.host if request.client else None
    if "error" in result:
        logger.warning(
            "auth_sign_in_failed",
            provider="microsoft",
            ip=ip,
            reason="msal_error",
            error=result.get("error_description", ""),
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, result.get("error_description", "Microsoft auth failed"))
    claims = result.get("id_token_claims", {})
    email = claims.get("email") or claims.get("preferred_username")
    if not email:
        logger.warning(
            "auth_sign_in_failed",
            provider="microsoft",
            ip=ip,
            reason="missing_email",
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No email in Microsoft token")

    tenant = await _default_tenant(db)
    auth_cfg = tenant.auth_config or {}
    if not auth_cfg.get("microsoft_enabled", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Microsoft SSO disabled")

    user = await get_user_by_email(db, tenant.id, email)
    if user is None:
        user = User(
            tenant_id=tenant.id,
            email=email,
            auth_provider="microsoft",
            display_name=claims.get("name") or email,
            role="customer",
        )
        db.add(user)
        await db.flush()

    ua = request.headers.get("user-agent")
    _, token = await create_session(db, user, ip=ip, user_agent=ua)
    await log_audit(db, tenant.id, user.id, user.email, "sign_in", {"provider": "microsoft"}, ip)
    logger.info(
        "auth_sign_in",
        provider="microsoft",
        email=user.email,
        user_id=str(user.id),
        tenant_id=str(tenant.id),
        ip=ip,
    )
    return TokenResponse(access_token=token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    session_id = request.state.session_id
    session = await db.get(Session, session_id)
    if session is not None and session.revoked_at is None:
        session.revoked_at = datetime.now(timezone.utc)
        await db.flush()
    ip = request.client.host if request.client else None
    await log_audit(db, user.tenant_id, user.id, user.email, "sign_out", {}, ip)
    logger.info(
        "auth_sign_out",
        email=user.email,
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
        ip=ip,
    )
    return None


@router.get("/me", response_model=UserProfile)
async def me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return UserProfile(
        id=user.id,
        email=user.email,
        role=user.role,
        tenant_id=user.tenant_id,
        display_name=user.display_name,
        # The platform gate's own predicate, so the page and the route
        # can never disagree about who the operator is.
        is_platform_admin=await is_platform_admin(user, db),
    )
