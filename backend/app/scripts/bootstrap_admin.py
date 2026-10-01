"""Idempotently upsert the configured admin + customer users from settings.

Wired into ``backend/entrypoint.sh`` to run after Alembic migrations and
before uvicorn starts. Reads two pairs of env vars
(``INITIAL_ADMIN_EMAIL`` / ``INITIAL_ADMIN_PASSWORD`` and the matching
``INITIAL_USER_*`` pair); each pair is independent — leave either blank
to skip that role.

Always-overwrites: if a user with the configured email exists, the
password is rehashed from the env value, ``role`` is forced to the
configured role, ``is_active`` is set True, and ``auth_provider`` is
forced to ``credentials``. ``.env`` is the source of truth, so rotating
a password is a ``.env`` edit plus a container restart.

Logs structured events (``bootstrap_user_created``,
``bootstrap_user_updated``, ``bootstrap_user_skipped``,
``bootstrap_failed``). The password is never logged. Any error returns a
non-zero exit code so the entrypoint can decide whether to continue or
abort.
"""
from __future__ import annotations

import asyncio
import sys

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.logging_config import configure_logging
from app.models import Tenant, User
from app.services.auth_service import hash_password


log = structlog.get_logger(__name__)


async def _upsert(
    db: AsyncSession,
    tenant_id,
    email: str,
    password: str,
    role: str,
) -> str:
    """Upsert one user. Returns ``"created"`` or ``"updated"``."""
    result = await db.execute(
        select(User).where(User.tenant_id == tenant_id, User.email == email)
    )
    existing = result.scalar_one_or_none()
    pw_hash = hash_password(password)
    if existing is None:
        db.add(
            User(
                tenant_id=tenant_id,
                email=email,
                password_hash=pw_hash,
                role=role,
                auth_provider="credentials",
                display_name=email.split("@")[0],
                is_active=True,
            )
        )
        return "created"
    existing.password_hash = pw_hash
    existing.role = role
    existing.is_active = True
    existing.auth_provider = "credentials"
    return "updated"


async def _bootstrap() -> None:
    pairs = [
        (
            "admin",
            settings.INITIAL_ADMIN_EMAIL,
            settings.INITIAL_ADMIN_PASSWORD.get_secret_value(),
        ),
        (
            "customer",
            settings.INITIAL_USER_EMAIL,
            settings.INITIAL_USER_PASSWORD.get_secret_value(),
        ),
    ]

    if not any(email and password for _, email, password in pairs):
        log.info(
            "bootstrap_users_skipped",
            reason="no INITIAL_ADMIN_* / INITIAL_USER_* env vars configured",
        )
        return

    async with async_session() as db:
        # The PLATFORM tenant — the same one require_platform_admin gates
        # on, so the operator this bootstraps can actually edit the
        # application-global settings the docs promise them.
        platform_slug = settings.PLATFORM_TENANT_SLUG
        tenant_result = await db.execute(
            select(Tenant).where(Tenant.slug == platform_slug)
        )
        tenant = tenant_result.scalar_one_or_none()
        if tenant is None:
            log.error(
                "bootstrap_failed",
                reason=(
                    f"platform tenant {platform_slug!r} not found "
                    "(PLATFORM_TENANT_SLUG) — the schema seeds 'dev'; a "
                    "custom slug needs its tenant row created first, and "
                    "Alembic migrations must run before this script"
                ),
            )
            raise SystemExit(2)

        for role, email, password in pairs:
            if not email or not password:
                log.info(
                    "bootstrap_user_skipped",
                    role=role,
                    reason="email or password env var blank",
                )
                continue
            action = await _upsert(db, tenant.id, email, password, role)
            log.info(
                f"bootstrap_user_{action}",
                role=role,
                email=email,
                tenant_id=str(tenant.id),
            )

        await db.commit()


def main() -> int:
    configure_logging()
    try:
        asyncio.run(_bootstrap())
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1
    except Exception as e:
        log.error(
            "bootstrap_failed",
            error=str(e),
            error_type=type(e).__name__,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
