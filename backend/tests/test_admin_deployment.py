"""The deployment view (K blueprint K9-04, K9-03; D16, D43, L31).

``GET /api/v1/admin/deployment`` shows a platform operator the posture the
backend runs with, each value with its source. What makes it safe to serve
is that it is an allowlist read name by name: these tests hold the list to
``.env.example``'s classes and to compose's backend environment, plant a
canary everywhere a secret or a stray variable could come from, and take
``strip_url`` through the shapes a URL's credential hides in.
"""
from __future__ import annotations

import json
import os
import pathlib
import uuid
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI, Request
from pydantic import SecretStr
from starlette.testclient import TestClient

from app import config as _config
from app.config import FILE_BACKED_SECRETS, Settings
from app.database import get_db
from app.main import source_url
from app.middleware import get_current_user, require_admin, require_platform_admin
from app.routers import admin as admin_router
from app.services import deployment_view

from tests import test_env_example_classes as env_classes

REPO = pathlib.Path(__file__).resolve().parents[2]

CANARY = f"k9canary{uuid.uuid4().hex[:12]}"
ALLOWED = [name for name, _klass, _hint in deployment_view.ALLOWLIST]


def _client(*, base_url: str = "http://testserver") -> TestClient:
    user = SimpleNamespace(
        id=uuid.uuid4(), email="operator@example.com", role="admin", tenant_id=uuid.uuid4()
    )
    app = FastAPI()
    app.include_router(admin_router.router)

    async def _db_dep():
        yield None

    async def _user_dep(request: Request):
        request.state.tenant_id = user.tenant_id
        return user

    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_current_user] = _user_dep
    app.dependency_overrides[require_admin] = _user_dep
    app.dependency_overrides[require_platform_admin] = _user_dep
    return TestClient(app, base_url=base_url)


def _gateway(monkeypatch, *, health=None, providers=None) -> None:
    """The gateway's two answers, without a gateway or a database."""

    async def _health(timeout: float = 2.0):
        return health

    async def _providers(_db):
        return providers or {
            "reported": False,
            "stub": None,
            "gateway_version": None,
            "updated_at": None,
            "public_key_pem": None,
            "providers": [],
        }

    monkeypatch.setattr("app.services.gateway_client.health", _health)
    monkeypatch.setattr("app.services.provider_keys_service.providers", _providers)


def _fresh_settings(monkeypatch, env: dict[str, str] | None = None) -> Settings:
    """A settings object of this test's own: the session's has fields set by
    every earlier assignment, so its ``model_fields_set`` says nothing about
    where a value came from."""
    for name in ALLOWED:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    fresh = Settings(_env_file=None)
    monkeypatch.setattr(_config, "settings", fresh)
    return fresh


def _valid(name: str) -> str:
    annotation = Settings.model_fields[name].annotation
    if annotation is bool:
        return "true"
    if annotation is int:
        return "7"
    if annotation is float:
        return "0.5"
    if name in deployment_view.URL_NAMES:
        return "http://example.invalid:4317/p"
    return "set-here"


def test_the_view_is_the_allowlist_and_nothing_else(monkeypatch):
    # Class [2]: every posture name of .env.example the settings model has,
    # but keyless mode, which is the gateway's to report (D16).
    declarations, _families, _headers = env_classes.read_all()
    posture = {
        d.name
        for d in declarations
        if d.file == ".env.example" and d.klass == 2 and d.name in Settings.model_fields
    } - {"LIBRERUN_STUB_LLM"}
    listed = {name: klass for name, klass, _hint in deployment_view.ALLOWLIST}
    assert {n for n, k in listed.items() if k == 2} == posture
    assert len(posture) == 26
    assert {n for n, k in listed.items() if k == 1} == {
        "LIBRERUN_AGENTS_PATH",
        "LIBRERUN_STATE_DIR",
        "LIBRERUN_PUBLIC_URL",
        "LIBRERUN_SOURCE_URL",
    }
    bootstrap = {d.name for d in declarations if d.file == ".env.example" and d.klass == 1}
    assert {n for n, k in listed.items() if k == 1} <= bootstrap
    assert len(ALLOWED) == len(set(ALLOWED)) == 30

    # No secret is on it, by type or by name.
    for name in ALLOWED:
        assert name in Settings.model_fields, name
        assert name not in FILE_BACKED_SECRETS, name
        assert Settings.model_fields[name].annotation is not SecretStr, name

    # Each hint says what compose's backend environment does with the name.
    compose = yaml.safe_load((REPO / "compose.yaml").read_text())
    environment = compose["services"]["backend"]["environment"]
    for name, _klass, hint in deployment_view.ALLOWLIST:
        raw = environment.get(name)
        if name == "LIBRERUN_PUBLIC_URL":
            assert str(raw).startswith("${LIBRERUN_PUBLIC_URL"), raw
            assert hint == deployment_view.LEAVE_COMMENTED
        elif raw is None:
            assert hint == deployment_view.NOT_PASSED, name
        elif str(raw).startswith("${" + name):
            assert hint == deployment_view.FROM_ENV, name
        else:
            assert hint == deployment_view.PINNED_STATE_DIR, name
            assert str(raw) in hint, (name, raw)

    # Each source: the one name set in the environment says env, the rest
    # default.
    _gateway(monkeypatch)
    _fresh_settings(monkeypatch)
    body = _client().get("/admin/deployment").json()
    assert [row["name"] for row in body["settings"]] == ALLOWED
    assert {row["source"] for row in body["settings"]} == {"default"}
    for name in ALLOWED:
        _fresh_settings(monkeypatch, {name: _valid(name)})
        rows = _client().get("/admin/deployment").json()["settings"]
        assert [r["name"] for r in rows if r["source"] == "env"] == [name]

    # One provider, as the gateway reports it: its name, source and
    # fingerprint, and none of the row's other fields.
    _gateway(
        monkeypatch,
        health={"stub": True},
        providers={
            "reported": True,
            "stub": True,
            "gateway_version": "9.9.9",
            "updated_at": "2026-09-30T12:00:00+00:00",
            "public_key_pem": f"-----BEGIN PUBLIC KEY-----{CANARY}",
            "providers": [
                {
                    "name": "openai",
                    "aliases": ["openai"],
                    "source": "runtime",
                    "fingerprint": "0123456789ab",
                    "set_by": str(uuid.uuid4()),
                    "set_at": "2026-09-30T11:00:00+00:00",
                    "row": "runtime",
                    "reason": None,
                }
            ],
        },
    )
    body = _client().get("/admin/deployment").json()
    assert body["stub"] is True
    assert body["gateway"]["reachable"] is True
    assert body["gateway"]["version"] == "9.9.9"
    assert body["gateway"]["providers"] == [
        {"name": "openai", "source": "runtime", "fingerprint": "0123456789ab"}
    ]
    assert CANARY not in json.dumps(body)

    # A gateway that does not answer: stub unknown, not "off".
    _gateway(monkeypatch, health=None)
    body = _client().get("/admin/deployment").json()
    assert body["stub"] is None
    assert body["gateway"]["reachable"] is False


def test_the_view_serves_no_secret_and_no_environment_dump(monkeypatch):
    env = {name: f"{CANARY}-{name}" for name in FILE_BACKED_SECRETS}
    env["DATABASE_URL"] = f"postgresql+asyncpg://u:{CANARY}@db:5432/x"
    env["REDIS_URL"] = f"redis://:{CANARY}@redis:6379/0"
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://u:{CANARY}@collector:4317/v1?k={CANARY}#{CANARY}"
    env["LIBRERUN_PUBLIC_URL"] = f"https://{CANARY}@public.example:8443/api?token={CANARY}"
    env["LIBRERUN_SOURCE_URL"] = f"https://git.example/src?access={CANARY}"
    for name in deployment_view.OTLP_HEADER_VARIABLES:
        env[name] = f"authorization=Bearer {CANARY}"
    env["K9_UNLISTED"] = CANARY
    env["OPENAI_API_KEY"] = f"sk-{CANARY}"
    settings = _fresh_settings(monkeypatch, env)
    # The canaries did reach the settings object: a view that read the
    # environment, or a secret field, would have them to give away.
    assert settings.APP_SECRET_KEY.get_secret_value().startswith(CANARY)
    _gateway(monkeypatch, health={"stub": False})

    response = _client().get("/admin/deployment")
    assert response.status_code == 200, response.text
    assert CANARY not in response.text
    body = response.json()
    assert {h["name"]: h["set"] for h in body["otlp_headers"]} == {
        name: True for name in deployment_view.OTLP_HEADER_VARIABLES
    }
    rows = {row["name"]: row["value"] for row in body["settings"]}
    assert rows["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector:4317/v1"
    assert rows["LIBRERUN_PUBLIC_URL"] == "https://public.example:8443/api"
    assert rows["LIBRERUN_SOURCE_URL"] == "https://git.example/src"
    assert body["source_url"] == "https://git.example/src"
    assert set(rows) == set(ALLOWED)


def test_a_url_loses_its_userinfo_query_and_fragment():
    strip = deployment_view.strip_url
    assert strip("http://user:pw@collector:4317/v1/traces?key=s#frag") == "http://collector:4317/v1/traces"
    assert strip("https://token@host.example/path") == "https://host.example/path"
    # A bare authority stays bare, as compose and the OTel SDK both allow it.
    assert strip("vector:4317") == "vector:4317"
    assert strip("user:pw@vector:4317") == "vector:4317"
    assert strip("http://[::1]:4317/x?y=z") == "http://[::1]:4317/x"
    assert strip("http://h:notaport/") == deployment_view.UNPARSED
    assert strip("http://h/a@b") == deployment_view.UNPARSED
    assert strip("http:///no-host?s=1") == deployment_view.UNPARSED
    assert strip("") == ""
    assert strip(None) is None
    # /meta keeps A1's value; the view's is stripped the same way.
    assert strip(source_url("")) == source_url("")


def test_a_dotenv_state_dir_reaches_the_process_environment(monkeypatch, tmp_path):
    from app.main import _export_state_dir

    # Recorded first, so the undo restores what was there, absence too.
    monkeypatch.setenv("LIBRERUN_STATE_DIR", "placeholder")
    monkeypatch.delenv("LIBRERUN_STATE_DIR")
    dotenv = tmp_path / ".env"
    dotenv.write_text("LIBRERUN_STATE_DIR=/srv/librerun-state\n")
    monkeypatch.setattr(_config, "settings", Settings(_env_file=dotenv))
    assert "LIBRERUN_STATE_DIR" not in os.environ

    _export_state_dir()
    assert os.environ["LIBRERUN_STATE_DIR"] == "/srv/librerun-state"

    # A value the environment already carries wins: compose pins one.
    monkeypatch.setenv("LIBRERUN_STATE_DIR", "/app/data/state")
    _export_state_dir()
    assert os.environ["LIBRERUN_STATE_DIR"] == "/app/data/state"

    # Blank in .env exports nothing.
    monkeypatch.delenv("LIBRERUN_STATE_DIR")
    dotenv.write_text("LIBRERUN_STATE_DIR=\n")
    monkeypatch.setattr(_config, "settings", Settings(_env_file=dotenv))
    _export_state_dir()
    assert "LIBRERUN_STATE_DIR" not in os.environ


@pytest.mark.parametrize("scheme", ["https", "http"])
def test_the_transport_is_the_requests_own(monkeypatch, scheme):
    _fresh_settings(monkeypatch)
    _gateway(monkeypatch)
    body = _client(base_url=f"{scheme}://testserver").get("/admin/deployment").json()
    assert body["transport"] == {"scheme": scheme, "host": "testserver"}
