"""The next sign-in uses the new secret, with nothing restarted (K6's goal).

A platform admin sets the Microsoft sign-in secret in Admin -> Settings;
``POST /auth/microsoft`` reads it — and both ids — per request, so the
very next sign-in uses it. Replace it, and the one after uses the
replacement; clear it, and the environment's ``AZURE_CLIENT_SECRET``
applies again (L29); with neither, Microsoft sign-in is "not configured".

The sign-in buttons are disabled (``login/page.tsx``) and no batch wires
them, so this is proved at the API: ``msal`` is replaced in
``sys.modules`` by a stand-in that records what it was constructed with
and answers a token for one address. The Google id is read the same way,
and Google's verifier is stood in to record the audience it was asked for.
"""
from __future__ import annotations

import sys
import types
import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.database import get_db
from app.middleware import get_current_user
from app.models import User
from app.routers import admin as admin_router
from app.routers import auth as auth_router
from tests.test_secrets_service import db, store  # noqa: F401  (fixtures)

SECRET = "the-first-client-secret-17c4"
REPLACED = "the-replacing-client-secret-e20b"
FROM_ENV = "the-environment-client-secret-9a3d"


class _FakeMsal:
    """``msal.ConfidentialClientApplication``, recording its arguments."""

    built: list[dict] = []

    def __init__(self, client_id, authority=None, client_credential=None):
        type(self).built.append(
            {"client_id": client_id, "authority": authority, "secret": client_credential}
        )

    def acquire_token_by_authorization_code(self, code, scopes, redirect_uri):
        return {"id_token_claims": {"email": "sso-user@example.com", "name": "SSO User"}}


@pytest.fixture
def fake_msal(monkeypatch):
    module = types.ModuleType("msal")
    module.ConfidentialClientApplication = _FakeMsal
    _FakeMsal.built = []
    monkeypatch.setitem(sys.modules, "msal", module)
    return _FakeMsal


@pytest_asyncio.fixture
async def platform_admin(db, monkeypatch) -> User:  # noqa: F811
    import app.config as cfg

    tenant_id = uuid.uuid4()
    slug = f"platform-{tenant_id.hex[:12]}"
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, :n, :s)"),
        {"id": tenant_id, "n": "Platform", "s": slug},
    )
    user_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, auth_provider, role)"
            " VALUES (:id, :t, :e, 'credentials', 'admin')"
        ),
        {"id": user_id, "t": tenant_id, "e": f"{user_id.hex[:10]}@example.com"},
    )
    monkeypatch.setattr(cfg.settings, "PLATFORM_TENANT_SLUG", slug)
    # The environment's defaults, all blank: every value below is one a
    # platform admin set, or the fallback a test sets on purpose.
    for name in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET", "GOOGLE_CLIENT_ID"):
        monkeypatch.setattr(cfg.settings, name, "")
    return await db.get(User, user_id)


def _client(db, user: User) -> AsyncClient:  # noqa: F811
    """The admin router and the auth router in one app, as the backend
    serves them: a setting written through one is read by the other."""
    app = FastAPI()
    app.include_router(admin_router.router)
    app.include_router(auth_router.router)

    async def _db_dep():
        yield db

    async def _user_dep():
        return user

    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def _sign_in(client) -> int:
    answer = await client.post(
        "/auth/microsoft", json={"code": "auth-code", "redirect_uri": "http://localhost:3000/login"}
    )
    return answer.status_code


@pytest.mark.asyncio
async def test_next_sign_in_uses_new_secret(db, store, platform_admin, fake_msal, monkeypatch):  # noqa: F811
    import app.config as cfg

    async with _client(db, platform_admin) as client:
        # Nothing configured: Microsoft sign-in says so, and builds nothing.
        assert await _sign_in(client) == 501
        assert fake_msal.built == []

        for key, value in (
            ("auth.azure_client_id", "11111111-2222-3333-4444-555555555555"),
            ("auth.azure_tenant_id", "contoso-tenant"),
        ):
            put = await client.put(f"/admin/settings/{key}", json={"value": value})
            assert put.status_code == 200, put.text
        put = await client.put("/admin/settings/auth.azure_client_secret", json={"value": SECRET})
        assert put.status_code == 200, put.text

        # The next sign-in uses it — with nothing restarted.
        assert await _sign_in(client) == 200
        assert fake_msal.built[-1] == {
            "client_id": "11111111-2222-3333-4444-555555555555",
            "authority": "https://login.microsoftonline.com/contoso-tenant",
            "secret": SECRET,
        }

        # Replace it: the sign-in after uses the replacement.
        put = await client.put("/admin/settings/auth.azure_client_secret", json={"value": REPLACED})
        assert put.status_code == 200
        assert await _sign_in(client) == 200
        assert fake_msal.built[-1]["secret"] == REPLACED

        # Clear it: the environment's value applies again (L29) ...
        monkeypatch.setattr(cfg.settings, "AZURE_CLIENT_SECRET", FROM_ENV)
        assert (await client.post("/admin/settings/reset/auth.azure_client_secret")).status_code == 200
        assert await _sign_in(client) == 200
        assert fake_msal.built[-1]["secret"] == FROM_ENV

        # ... and with neither, sign-in is not configured.
        monkeypatch.setattr(cfg.settings, "AZURE_CLIENT_SECRET", "")
        assert await _sign_in(client) == 501
    assert len(fake_msal.built) == 3


@pytest.mark.asyncio
async def test_google_sign_in_reads_the_runtime_client_id(db, store, platform_admin, monkeypatch):  # noqa: F811
    """Google's half needs no secret: the ID token is verified against the
    client id alone, which is now a runtime setting too."""
    from google.oauth2 import id_token

    audiences = []

    def verify(credential, request, audience):
        audiences.append(audience)
        return {"email": "google-user@example.com", "name": "Google User"}

    monkeypatch.setattr(id_token, "verify_oauth2_token", verify)
    async with _client(db, platform_admin) as client:
        assert (await client.post("/auth/google", json={"credential": "jwt"})).status_code == 501

        put = await client.put(
            "/admin/settings/auth.google_client_id", json={"value": "client-id.apps.example"}
        )
        assert put.status_code == 200, put.text
        answer = await client.post("/auth/google", json={"credential": "jwt"})
        assert answer.status_code == 200, answer.text
    assert audiences == ["client-id.apps.example"]
