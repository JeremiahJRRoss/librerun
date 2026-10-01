"""Which admin routes are the platform operator's, and which a tenant admin's
(K blueprint K9; L31, D29).

Every ``/admin`` route is in exactly one of two lists, and the list is held
to the route's own dependencies: ``PLATFORM_ONLY`` routes declare
``require_platform_admin``, ``TENANT_ADMIN`` routes do not. A route added
to neither list turns ``test_every_admin_route_is_classified`` red, which
is the point: the next platform-wide read cannot land ungated by
accident. The refusal is then driven through the real dependencies, as a
tenant admin of another tenant, one route at a time.
"""
from __future__ import annotations

import importlib
import pkgutil
import re
import uuid

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.database import get_db
from app.middleware import get_current_user, require_platform_admin
from app.models import Tenant, User

# The deployment's: application-global state, the gateway's keys, the
# edge's certificate, the deployment view and the trace pipeline.
PLATFORM_ONLY = {
    ("GET", "/admin/otel-status"),
    ("GET", "/admin/settings"),
    ("PUT", "/admin/settings/{key}"),
    ("POST", "/admin/settings/reset/{key}"),
    ("GET", "/admin/agent-keys"),
    ("POST", "/admin/agent-keys/{agent_id}"),
    ("POST", "/admin/agent-keys/{agent_id}/rotate"),
    ("DELETE", "/admin/agent-keys/{agent_id}"),
    # K7's three.
    ("GET", "/admin/providers"),
    ("POST", "/admin/providers/{name}/key"),
    ("DELETE", "/admin/providers/{name}/key"),
    ("GET", "/admin/deployment"),
    # T2's four backend routes. Its four changes (PUT /admin/tls/ca,
    # /files and /acme, DELETE /admin/tls/choice) are edge-control's,
    # reached through the edge alone (config/Caddyfile), and no backend
    # route to classify.
    ("GET", "/admin/tls"),
    ("GET", "/admin/tls/root.pem"),
    ("POST", "/admin/tls/acknowledge"),
    ("GET", "/admin/tls/authorize"),
}

# A tenant's own: its users, its sign-in, its runs, feedback and audit.
TENANT_ADMIN = {
    ("GET", "/admin/users"),
    ("POST", "/admin/users"),
    ("PUT", "/admin/users/{user_id}"),
    ("POST", "/admin/users/{user_id}/revoke"),
    ("GET", "/admin/auth-config"),
    ("PUT", "/admin/auth-config"),
    ("GET", "/admin/runs/{run_id}"),
    ("GET", "/admin/feedback"),
    ("GET", "/admin/audit-log"),
    ("GET", "/admin/drift-summary"),
}


def _admin_routes():
    """Every route under ``/admin`` in every router module, not the admin
    router's alone: a platform read added to another router must be
    classified too. Read on the routers themselves, since the app keeps
    included routers lazy on ``app.routes``."""
    import app.routers as routers_pkg

    found = {}
    for module_info in pkgutil.iter_modules(routers_pkg.__path__):
        module = importlib.import_module(f"app.routers.{module_info.name}")
        router = getattr(module, "router", None)
        for route in getattr(router, "routes", []):
            path = getattr(route, "path", "")
            if not path.startswith("/admin"):
                continue
            for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}:
                found[(method, path)] = route
    return found


def _dependency_calls(route) -> list:
    return [d.call for d in route.dependant.dependencies]


def test_every_admin_route_is_classified():
    routes = _admin_routes()
    assert PLATFORM_ONLY.isdisjoint(TENANT_ADMIN)
    unclassified = sorted(set(routes) - PLATFORM_ONLY - TENANT_ADMIN)
    assert unclassified == [], f"admin routes in neither list: {unclassified}"
    stale = sorted((PLATFORM_ONLY | TENANT_ADMIN) - set(routes))
    assert stale == [], f"listed routes that no longer exist: {stale}"
    for key in sorted(PLATFORM_ONLY):
        assert require_platform_admin in _dependency_calls(routes[key]), (
            f"{key[0]} {key[1]} is platform-only but does not declare require_platform_admin"
        )
    for key in sorted(TENANT_ADMIN):
        assert require_platform_admin not in _dependency_calls(routes[key]), (
            f"{key[0]} {key[1]} is a tenant admin's but declares require_platform_admin"
        )


class FakeDB:
    """The platform gate's one question, answered with another tenant.

    Nothing else: a route whose gate was dropped reaches its handler, finds
    no ``execute`` here and answers 500, so the test fails naming it."""

    def __init__(self, tenant: Tenant) -> None:
        self._tenant = tenant

    async def get(self, model, key):
        assert model is Tenant
        return self._tenant


_PARAMS = {
    "key": "cors_origins",
    "agent_id": "agent-x",
    "name": "openai",
}


def _fill(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: _PARAMS[m.group(1)], path)


def test_a_tenant_admin_is_refused_every_platform_route():
    from app.routers import admin as admin_router

    tenant_id = uuid.uuid4()
    admin = User(id=uuid.uuid4(), tenant_id=tenant_id, role="admin", email="a@acme.example")
    app = FastAPI()
    app.include_router(admin_router.router)

    async def _db():
        yield FakeDB(Tenant(id=tenant_id, name="Acme", slug="acme"))

    async def _user():
        return admin

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    client = TestClient(app, raise_server_exceptions=False)
    answers = {}
    for method, path in sorted(PLATFORM_ONLY):
        response = client.request(method, _fill(path), json={"value": 1, "sealed": "x"})
        answers[f"{method} {path}"] = response.status_code
    refused = {route: code for route, code in answers.items() if code != 403}
    assert refused == {}, "; ".join(f"{route} answered {code}" for route, code in refused.items())


def test_otel_status_strips_its_urls(monkeypatch):
    from app import config as _config
    from app.routers import admin as admin_router

    from tests.test_handler_elements_evaluated import _client

    secret = "pw" + uuid.uuid4().hex[:8]
    monkeypatch.setattr(
        _config.settings, "OTEL_EXPORTER_OTLP_ENDPOINT", f"http://u:{secret}@collector:4317/?t={secret}"
    )

    async def _vector_health(endpoint, **_kw):
        return {"endpoint": endpoint, "reachable": True, "checked": "tcp connect", "detail": "ok"}

    monkeypatch.setattr(admin_router.obs_vendors, "vector_health", _vector_health)

    class _Exporter:
        _endpoint = f"https://u:{secret}@collector.example/v1/traces?token={secret}"

    class _Processor:
        span_exporter = _Exporter()

    class _Provider:
        _active_span_processor = type("Multi", (), {"_span_processors": [_Processor()]})()

        def force_flush(self, timeout_millis=0):
            raise RuntimeError(f"export to https://u:{secret}@collector.example failed")

    monkeypatch.setattr("opentelemetry.trace.get_tracer_provider", lambda: _Provider())

    response = _client(admin_router).get("/admin/otel-status")
    assert response.status_code == 200, response.text
    assert secret not in response.text
    body = response.json()
    assert body["otel_endpoint"] == "http://collector:4317/"
    assert body["vector"]["endpoint"] == "http://collector:4317/"
    assert [p["endpoint"] for p in body["span_processors"]] == ["https://collector.example/v1/traces"]
    assert body["force_flush_5s"] is False
    assert body["force_flush_error"] == "RuntimeError"
