"""Phase 4 regression guard: Phase 4 removes ``/admin/llm-config`` and
``/admin/vendors`` — the generic ``/agents`` router supersedes them."""
from __future__ import annotations

from app.routers import admin as admin_router


def _paths() -> set[str]:
    return {route.path for route in admin_router.router.routes}


def test_llm_config_routes_removed():
    paths = _paths()
    assert "/admin/llm-config" not in paths
    assert not any(p.startswith("/admin/llm-config") for p in paths)


def test_vendor_registry_routes_removed():
    paths = _paths()
    assert "/admin/vendors" not in paths
    assert not any(p.startswith("/admin/vendors") for p in paths)


def test_admin_retains_kept_routes():
    """Paranoia check: make sure Phase 4 didn't accidentally delete the
    other admin surfaces that should keep working."""
    paths = _paths()
    # Sampling what must remain post-refactor.
    assert "/admin/users" in paths
    assert "/admin/auth-config" in paths
    assert "/admin/settings" in paths
    assert "/admin/audit-log" in paths
    assert "/admin/drift-summary" in paths
