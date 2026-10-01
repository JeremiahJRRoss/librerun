import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import structlog
from jose import JWTError, jwt
from passlib.hash import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Session, User

logger = structlog.get_logger(__name__)

# One year. Longer than any real session policy, and far below the point
# where timedelta overflows.
_MAX_SESSION_MINUTES = 365 * 24 * 60


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.verify(plain, hashed)
    except Exception:
        return False


def hash_password(plain: str) -> str:
    return bcrypt.using(rounds=settings.BCRYPT_ROUNDS).hash(plain)


def create_jwt(user: User, session_id: UUID, expires_at: datetime | None = None) -> str:
    """Mint the bearer token for a session.

    ``create_session`` passes ``expires_at`` so the token's ``exp`` claim and
    the session row's ``expires_at`` column are the same instant, computed
    once, and both honour the admin-configured lifetime.
    """
    if expires_at is None:
        expires_at = datetime.now(timezone.utc) + timedelta(
            hours=settings.JWT_EXPIRY_HOURS
        )
    payload = {
        "sub": str(user.id),
        "tenant_id": str(user.tenant_id),
        "role": user.role,
        "session_id": str(session_id),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(
        payload, settings.APP_SECRET_KEY.get_secret_value(), algorithm="HS256"
    )


async def session_lifetime(db: AsyncSession) -> timedelta:
    """How long a newly minted session and its token stay valid.

    Reads the admin-visible ``session_timeout_minutes`` app setting, whose
    default is derived from ``JWT_EXPIRY_HOURS``. That setting was previously
    written, validated, audited and rendered in Admin → Settings, but never
    read by anything: every deployment ran the 24-hour env default and an
    admin who shortened the window saw no effect at all.

    A settings-store failure falls back to the env value rather than blocking
    sign-in — an unreachable Redis or settings table should not lock everyone
    out of the product.
    """
    fallback = settings.JWT_EXPIRY_HOURS * 60
    try:
        from app.services.app_settings_service import get_setting

        minutes = int(await get_setting(db, "session_timeout_minutes"))
    except Exception as exc:
        logger.warning(
            "session_timeout_setting_unreadable",
            error=str(exc),
            error_type=type(exc).__name__,
            fallback_minutes=fallback,
        )
        minutes = fallback
    # The setting spec coerces to int and nothing else, so a platform admin
    # can persist any integer at all. Past roughly 10**13 minutes
    # ``timedelta`` itself raises OverflowError, which — now that this
    # formerly dead setting controls authentication — would turn every
    # sign-in into a 500 until someone repaired the row by hand. The bound
    # is applied to the env fallback too, since JWT_EXPIRY_HOURS can be set
    # just as carelessly.
    if not 0 < minutes <= _MAX_SESSION_MINUTES:
        logger.warning(
            "session_timeout_setting_invalid",
            minutes=minutes,
            fallback_minutes=fallback,
            max_minutes=_MAX_SESSION_MINUTES,
        )
        minutes = min(max(fallback, 1), _MAX_SESSION_MINUTES)
    return timedelta(minutes=minutes)


def claims_without_expiry_check(token: str) -> dict | None:
    """Claims from a token whose signature is good but whose ``exp`` passed.

    Used only to attribute an expiry rejection to the identity that asked.
    The signature is still verified — only the expiry check is disabled —
    so an unsigned or tampered token yields nothing. Returns ``None``
    rather than raising: this feeds a log line and a span attribute, and a
    diagnostic path must never turn a 401 into a 500.
    """
    try:
        return jwt.decode(
            token,
            settings.APP_SECRET_KEY.get_secret_value(),
            algorithms=["HS256"],
            options={"verify_exp": False},
        )
    except JWTError:
        return None


def decode_jwt(token: str) -> dict:
    return jwt.decode(
        token, settings.APP_SECRET_KEY.get_secret_value(), algorithms=["HS256"]
    )


async def get_user_by_email(db: AsyncSession, tenant_id: UUID, email: str) -> User | None:
    result = await db.execute(
        select(User).where(User.tenant_id == tenant_id, User.email == email)
    )
    return result.scalar_one_or_none()


async def create_session(
    db: AsyncSession, user: User, ip: str | None = None, user_agent: str | None = None
) -> tuple[Session, str]:
    """Create a session row and return (session, raw_jwt)."""
    session_id = uuid4()
    # One instant for both the token's exp claim and the row's expires_at.
    # They were computed from two separate clock reads before, so the token
    # and the row it is checked against disagreed by a hair.
    expires_at = datetime.now(timezone.utc) + await session_lifetime(db)
    # Generate token first so we can hash it
    token = create_jwt(user, session_id, expires_at)
    session = Session(
        id=session_id,
        user_id=user.id,
        tenant_id=user.tenant_id,
        token_hash=hash_token(token),
        ip_address=ip,
        user_agent=user_agent,
        expires_at=expires_at,
    )
    db.add(session)
    user.last_sign_in = datetime.now(timezone.utc)
    await db.flush()
    return session, token


async def get_session(db: AsyncSession, session_id: UUID) -> Session | None:
    result = await db.execute(select(Session).where(Session.id == session_id))
    return result.scalar_one_or_none()
