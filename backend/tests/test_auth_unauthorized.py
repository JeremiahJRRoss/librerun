"""Tests for the 401 path in ``app.middleware`` and the session lifetime.

Written after a burst of 401s on run polling could not be explained from
telemetry. Two gaps sat behind it:

- Every rejection raised a bare ``HTTPException(401, reason)``. The reason
  reached only the response body, which nothing logs or stamps, so an expired
  token, a session revoked by a logout in another tab, an admin revoke and a
  deactivated user all produced the same blank signal.
- The identity was unavailable by construction: ``bind_request_context`` runs
  only after a *successful* auth, and the span enricher copies context at
  ``on_start`` anyway, so the request span never carried who was asking.

The claimed identity is stamped under ``librerun.auth.claimed_*`` — never the
verified ``tenant.id`` / ``user.id`` names, which must keep meaning that the
request actually proved who it was.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import config as config_module
from app import middleware as mw
from app.services import auth_service

# NOTE on patching settings. `tests/conftest.py` rebinds `app.config.settings`
# to a fresh Settings instance at session start, while every module that did
# `from app.config import settings` at import time still holds the original.
# Patching `config_module.settings` therefore does NOT reach the code under
# test here — and because the values involved match the real defaults, such a
# patch fails silently, leaving a test that asserts the default while proving
# nothing. These patch `auth_service.settings`, the object the function
# actually reads.

CLAIMS = {
    "sub": "11111111-1111-1111-1111-111111111111",
    "tenant_id": "22222222-2222-2222-2222-222222222222",
    "session_id": "33333333-3333-3333-3333-333333333333",
    "role": "user",
}


def _span_for(reason: str, claims: dict | None):
    """Run ``_unauthorized`` inside a recording span; return (exc, attributes).

    A local provider is used rather than the global one: ``_unauthorized``
    reads the *current* span from context, so nothing global needs replacing.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)

    with tracer.start_as_current_span("request"):
        exc = mw._unauthorized(reason, claims)

    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    return exc, dict(finished[0].attributes or {})


@pytest.mark.parametrize(
    "reason",
    [
        "Missing bearer token",
        "Invalid token",
        "Token expired",
        "Invalid token claims",
        "Session not found",
        "Session revoked",
        "Session expired",
        "User inactive",
    ],
)
def test_every_rejection_reason_reaches_the_span(reason):
    """The reason is what separates the four very different causes."""
    exc, attributes = _span_for(reason, CLAIMS)

    assert exc.status_code == 401
    assert exc.detail == reason
    assert attributes["librerun.auth.failure_reason"] == reason


def test_claimed_identity_is_stamped_when_the_token_carried_it():
    _, attributes = _span_for("Session expired", CLAIMS)

    assert attributes["librerun.auth.claimed_user_id"] == CLAIMS["sub"]
    assert attributes["librerun.auth.claimed_tenant_id"] == CLAIMS["tenant_id"]
    assert attributes["librerun.auth.claimed_session_id"] == CLAIMS["session_id"]


def test_claimed_identity_never_lands_on_the_verified_attribute_names():
    """The negative guard, and the reason the names differ at all.

    ``tenant.id`` and ``user.id`` are written by the span enricher only after
    auth succeeds. If a rejected request could write them too, every query
    that trusts those attributes to mean "verified" would quietly start
    counting unverified claims — so assert the failing path cannot.
    """
    _, attributes = _span_for("Session revoked", CLAIMS)

    assert "tenant.id" not in attributes
    assert "user.id" not in attributes
    assert "user.email" not in attributes


def test_no_claims_still_records_the_reason():
    """A request with no parsable token has nothing to attribute, but the
    reason is still the thing that makes the burst readable."""
    _, attributes = _span_for("Missing bearer token", None)

    assert attributes["librerun.auth.failure_reason"] == "Missing bearer token"
    assert not any(k.startswith("librerun.auth.claimed_") for k in attributes)


def test_unauthorized_works_without_a_recording_span():
    """Tracing is optional; auth is not. With no provider configured the
    current span is non-recording, and the rejection must still be built."""
    exc = mw._unauthorized("Session expired", CLAIMS)

    assert exc.status_code == 401
    assert exc.detail == "Session expired"


# ---------------------------------------------------------------------------
# Session lifetime — ``session_timeout_minutes`` used to be dead config.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_lifetime_honours_the_admin_setting(monkeypatch):
    """Admin → Settings now actually changes how long a session lasts."""
    import app.services.app_settings_service as settings_service

    async def fake_get_setting(db, key):
        assert key == "session_timeout_minutes"
        return 30

    monkeypatch.setattr(settings_service, "get_setting", fake_get_setting)

    assert await auth_service.session_lifetime(object()) == timedelta(minutes=30)


@pytest.mark.asyncio
async def test_session_lifetime_falls_back_when_the_store_is_unreachable(monkeypatch):
    """A broken settings store must not lock everyone out of the product."""
    import app.services.app_settings_service as settings_service

    async def boom(db, key):
        raise RuntimeError("redis unreachable")

    monkeypatch.setattr(settings_service, "get_setting", boom)
    monkeypatch.setattr(auth_service.settings, "JWT_EXPIRY_HOURS", 24)

    assert await auth_service.session_lifetime(object()) == timedelta(hours=24)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -5])
async def test_session_lifetime_rejects_a_nonpositive_window(monkeypatch, bad):
    """Zero or negative minutes would expire every session at creation."""
    import app.services.app_settings_service as settings_service

    async def fake_get_setting(db, key):
        return bad

    monkeypatch.setattr(settings_service, "get_setting", fake_get_setting)
    monkeypatch.setattr(auth_service.settings, "JWT_EXPIRY_HOURS", 24)

    assert await auth_service.session_lifetime(object()) == timedelta(hours=24)


# ---------------------------------------------------------------------------
# An expired token is the case this whole path exists to tell apart.
#
# Codex P2 on PR #52: jose raises ExpiredSignatureError as a JWTError
# subclass, so catching JWTError alone filed every ordinary expiry under
# "Invalid token" beside malformed and badly signed tokens — and discarded
# its claims. The commonest cause of a 401 burst was the one the change
# could not diagnose.
# ---------------------------------------------------------------------------


async def _span_for_request(token: str):
    """Drive the REAL get_current_user decode path inside a recording span.

    Both branches under test reject before the database is touched, so a
    placeholder db is enough; what matters is that this exercises the
    shipped function rather than a copy of its logic.
    """
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    raised = None
    with tracer.start_as_current_span("request"):
        try:
            await mw.get_current_user(request=None, creds=creds, db=None)
        except HTTPException as exc:
            raised = exc

    assert raised is not None, "expected the request to be rejected"
    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    return raised, dict(finished[0].attributes or {})


def _token(secret: str, *, expired: bool, **claims) -> str:
    from datetime import datetime, timedelta, timezone

    from jose import jwt

    delta = timedelta(hours=-1) if expired else timedelta(hours=1)
    body = {**CLAIMS, **claims, "exp": int((datetime.now(timezone.utc) + delta).timestamp())}
    return jwt.encode(body, secret, algorithm="HS256")


@pytest.mark.asyncio
async def test_an_expired_token_is_reported_as_expired_with_its_claims(monkeypatch):
    """The regression guard: expiry must not read as "Invalid token"."""
    monkeypatch.setattr(auth_service.settings, "APP_SECRET_KEY", "unit-test-secret")

    exc, attributes = await _span_for_request(_token("unit-test-secret", expired=True))

    assert exc.status_code == 401
    assert exc.detail == "Token expired"
    assert attributes["librerun.auth.failure_reason"] == "Token expired"
    # And the identity survives, which is what makes a burst attributable.
    assert attributes["librerun.auth.claimed_user_id"] == CLAIMS["sub"]
    assert attributes["librerun.auth.claimed_tenant_id"] == CLAIMS["tenant_id"]


@pytest.mark.asyncio
async def test_a_badly_signed_token_stays_invalid_and_yields_no_claims(monkeypatch):
    """The other half: recovering claims must not extend to unsigned tokens.

    Claims are re-read only after the signature has already been verified.
    A token signed with the wrong key must produce no identity at all, or
    anyone could put whatever they liked on the span.
    """
    monkeypatch.setattr(auth_service.settings, "APP_SECRET_KEY", "unit-test-secret")

    exc, attributes = await _span_for_request(_token("the-wrong-secret", expired=True))

    assert exc.detail == "Invalid token"
    assert not any(k.startswith("librerun.auth.claimed_") for k in attributes)


def test_claims_helper_refuses_a_token_signed_with_another_key(monkeypatch):
    monkeypatch.setattr(auth_service.settings, "APP_SECRET_KEY", "unit-test-secret")

    assert auth_service.claims_without_expiry_check(_token("nope", expired=True)) is None
    recovered = auth_service.claims_without_expiry_check(
        _token("unit-test-secret", expired=True)
    )
    assert recovered is not None and recovered["sub"] == CLAIMS["sub"]


# ---------------------------------------------------------------------------
# Codex P2 on PR #52: an absurd session_timeout_minutes overflowed timedelta,
# turning every sign-in into a 500 until the row was repaired by hand.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("absurd", [10**13, 10**18, 999_999_999])
async def test_an_absurd_session_timeout_falls_back_instead_of_overflowing(
    monkeypatch, absurd
):
    import app.services.app_settings_service as settings_service

    async def fake_get_setting(db, key):
        return absurd

    monkeypatch.setattr(settings_service, "get_setting", fake_get_setting)
    monkeypatch.setattr(auth_service.settings, "JWT_EXPIRY_HOURS", 24)

    assert await auth_service.session_lifetime(object()) == timedelta(hours=24)


@pytest.mark.asyncio
async def test_an_absurd_env_fallback_is_clamped_too(monkeypatch):
    """The fallback is not automatically sane either — JWT_EXPIRY_HOURS is
    just as settable, and clamping only the DB value would still overflow."""
    import app.services.app_settings_service as settings_service

    async def boom(db, key):
        raise RuntimeError("settings store down")

    monkeypatch.setattr(settings_service, "get_setting", boom)
    monkeypatch.setattr(auth_service.settings, "JWT_EXPIRY_HOURS", 10**12)

    lifetime = await auth_service.session_lifetime(object())
    assert lifetime == timedelta(minutes=auth_service._MAX_SESSION_MINUTES)
