"""The platform-admin flag on ``GET /auth/me`` (K4b, D30).

The UI needs to know which pages are the deployment operator's — the
settings page, and from K6 on the secrets and keys — before it offers
them, rather than letting a tenant admin click into a 403. So the
current-user response carries ``is_platform_admin``, computed by the
same predicate ``require_platform_admin`` gates on: an admin of the
tenant whose slug is ``PLATFORM_TENANT_SLUG``.

It is a display hint. Routes keep their gate; nothing is authorized on
the flag, and the last test here holds the flag and the gate to one
answer so they can never drift apart.

On the fakes of ``test_platform_admin_gate.py``, with the slug patched
the same way and to a non-default value, so a reader of some other
settings object could not pass by coincidence. Each case reads the flag
through the ``/auth/me`` handler itself, not only through the predicate,
so a flag forced in either place is caught.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.middleware import is_platform_admin, require_admin, require_platform_admin
from app.models import Tenant, User
from app.routers.auth import me

PLATFORM_SLUG = "platform-hq"


class FakeDB:
    def __init__(self, tenant: Tenant | None) -> None:
        self._tenant = tenant

    async def get(self, model, key):
        assert model is Tenant
        return self._tenant


def _user(tenant_id, role: str = "admin") -> User:
    return User(id=uuid4(), tenant_id=tenant_id, role=role, email="a@x.example")


def _patch_live_slug(monkeypatch, value: str) -> None:
    import app.config as cfg

    monkeypatch.setattr(cfg.settings, "PLATFORM_TENANT_SLUG", value)


async def _flag(user: User, db: FakeDB) -> bool:
    """The flag as ``/auth/me`` serves it, checked against the predicate."""
    profile = await me(user=user, db=db)
    assert profile.is_platform_admin is await is_platform_admin(user, db)
    return profile.is_platform_admin


@pytest.mark.asyncio
async def test_a_platform_admin_reads_true(monkeypatch):
    _patch_live_slug(monkeypatch, PLATFORM_SLUG)
    tid = uuid4()
    user = _user(tid)
    db = FakeDB(Tenant(id=tid, name="HQ", slug=PLATFORM_SLUG))
    assert await _flag(user, db) is True
    profile = await me(user=user, db=db)
    assert profile.model_dump()["is_platform_admin"] is True
    assert profile.email == user.email and profile.role == "admin"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tenant", [Tenant(id=uuid4(), name="Acme", slug="acme"), None], ids=["acme", "no-tenant"]
)
async def test_a_tenant_admin_of_another_tenant_reads_false(monkeypatch, tenant):
    _patch_live_slug(monkeypatch, PLATFORM_SLUG)
    user = _user(tenant.id if tenant else uuid4())
    assert await _flag(user, FakeDB(tenant)) is False


async def _gate_passes(user: User, db: FakeDB) -> bool:
    """The route's answer, through the chain FastAPI runs: ``require_admin``
    first (a customer never reaches the platform gate), then the gate."""
    try:
        await require_admin(user=user)
        await require_platform_admin(user=user, db=db)
    except HTTPException as exc:
        assert exc.status_code == 403
        return False
    return True


@pytest.mark.asyncio
async def test_the_flag_and_the_gate_agree(monkeypatch):
    _patch_live_slug(monkeypatch, PLATFORM_SLUG)
    platform = Tenant(id=uuid4(), name="HQ", slug=PLATFORM_SLUG)
    acme = Tenant(id=uuid4(), name="Acme", slug="acme")
    cases = [
        ("platform admin", _user(platform.id), FakeDB(platform), True),
        ("another tenant's admin", _user(acme.id), FakeDB(acme), False),
        ("an admin whose tenant is gone", _user(uuid4()), FakeDB(None), False),
        ("a customer of the platform tenant", _user(platform.id, "customer"), FakeDB(platform), False),
    ]
    for label, user, db, expected in cases:
        flag = await _flag(user, db)
        gate = await _gate_passes(user, db)
        assert (flag, gate) == (expected, expected), label
