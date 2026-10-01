"""The platform-operator gate on application-global runtime settings.

``require_admin`` is tenant-scoped — every tenant has admins, and they
administer their tenant. The ``app_settings`` rows are application-global
(single ``key`` PK, no ``tenant_id``), read by every tenant's requests, so
mutating them must require the PLATFORM operator: an admin of the tenant
whose slug is ``PLATFORM_TENANT_SLUG`` (default ``dev`` — the seeded tenant
bootstrap credentials land in). Without the gate, tenant A's admin could
set an attacker-controlled trace-viewer URL template rendered as "View
trace" for tenant B's users, disclosing their trace ids on click.

Both directions, per the house rule: the platform admin passes, any other
tenant's admin is refused — and the wiring test proves the three settings
routes actually declare the gate while ordinary admin routes do not.
"""
from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.middleware import require_platform_admin
from app.models import Tenant, User


class FakeDB:
    def __init__(self, tenant: Tenant | None) -> None:
        self._tenant = tenant

    async def get(self, model, key):
        assert model is Tenant
        return self._tenant


def _admin(tenant_id) -> User:
    return User(tenant_id=tenant_id, role="admin", email="a@x.example")


def _patch_live_slug(monkeypatch, value: str) -> None:
    """Patch the settings object the code under test actually reads.

    The tests' conftest REPLACES ``app.config.settings`` with a fresh
    instance at session start, so a module's ``settings`` binding depends
    on when it was imported relative to that swap. Patching a test-module
    alias can therefore miss the live object entirely — and a test that
    uses the DEFAULT slug would still pass by coincidence, hiding exactly
    that. Hence: patch through ``app.config`` (which the gate re-resolves
    per call) and use a NON-default slug in the positive run.
    """
    import app.config as cfg

    monkeypatch.setattr(cfg.settings, "PLATFORM_TENANT_SLUG", value)


@pytest.mark.asyncio
async def test_platform_tenant_admin_passes(monkeypatch):
    _patch_live_slug(monkeypatch, "platform-hq")
    tid = uuid4()
    user = _admin(tid)
    result = await require_platform_admin(
        user=user, db=FakeDB(Tenant(id=tid, name="HQ", slug="platform-hq"))
    )
    assert result is user


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant", [Tenant(id=uuid4(), name="Acme", slug="acme"), None])
async def test_other_tenant_admin_is_refused(monkeypatch, tenant):
    _patch_live_slug(monkeypatch, "platform-hq")
    with pytest.raises(HTTPException) as exc:
        await require_platform_admin(user=_admin(uuid4()), db=FakeDB(tenant))
    assert exc.value.status_code == 403


def _route_dependency_calls(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(
            route, "methods", set()
        ):
            return [d.call for d in route.dependant.dependencies]
    raise AssertionError(f"route not found: {method} {path}")


def test_settings_routes_declare_the_platform_gate():
    """All three /admin/settings endpoints depend on require_platform_admin;
    an ordinary admin route (user list) does not — the gate is scoped to
    application-global state, not admin surface generally.

    Inspected on the admin router itself: this FastAPI version keeps
    included routers lazy on ``app.routes``, so the flattened APIRoutes
    live on the router object.
    """
    from app.routers.admin import router

    for path, method in [
        ("/admin/settings", "GET"),
        ("/admin/settings/{key}", "PUT"),
        ("/admin/settings/reset/{key}", "POST"),
    ]:
        calls = _route_dependency_calls(router, path, method)
        assert require_platform_admin in calls, (path, method)

    users_calls = _route_dependency_calls(router, "/admin/users", "GET")
    assert require_platform_admin not in users_calls


@pytest.mark.asyncio
async def test_login_tenant_lookup_uses_the_platform_slug(monkeypatch):
    """One knob keeps login, bootstrap and the settings gate coherent: the
    credentials login's tenant lookup must query PLATFORM_TENANT_SLUG, not
    a hardcoded 'dev' — otherwise a deployment that changes the slug
    bootstraps an operator the login endpoint cannot authenticate."""
    import app.routers.auth as auth_mod
    from app.routers.auth import _default_tenant

    # auth.py binds ``settings`` at module import; patch THAT object (see
    # _patch_live_slug's note on the conftest settings swap).
    monkeypatch.setattr(auth_mod.settings, "PLATFORM_TENANT_SLUG", "opsco")
    captured: dict = {}

    class CapturingDB:
        async def execute(self, stmt):
            captured.update(stmt.compile().params)

            class _R:
                def scalar_one_or_none(self):
                    return Tenant(id=uuid4(), name="Ops", slug="opsco")

            return _R()

    tenant = await _default_tenant(CapturingDB())
    assert tenant.slug == "opsco"
    assert "opsco" in captured.values()
