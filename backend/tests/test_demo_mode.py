"""Blueprint S3 (decision L22): demo mode and the public ``/meta`` facts.

The shipped default ``APP_SECRET_KEY`` is refused at startup unless
``LIBRERUN_DEMO=true``; demo mode announces itself with a banner; and
``GET /api/v1/meta`` tells the login page and the demo banner what they
need before anyone signs in.
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import app.config
from app import demo
from app.agents import registry
from app.agents.manifest import AgentManifest
from app.agents.protocol import AgentProtocol
from app.config import DEFAULT_APP_SECRET_KEY
from app.version import __version__


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


def test_default_secret_is_refused_outside_demo_mode(monkeypatch, caplog):
    monkeypatch.setattr(app.config.settings, "APP_SECRET_KEY", DEFAULT_APP_SECRET_KEY)
    monkeypatch.setattr(app.config.settings, "LIBRERUN_DEMO", False)
    with pytest.raises(demo.DefaultSecretRefused):
        demo.refuse_default_secret_unless_demo()
    # A blank secret is the same refusal.
    monkeypatch.setattr(app.config.settings, "APP_SECRET_KEY", "   ")
    with pytest.raises(demo.DefaultSecretRefused):
        demo.refuse_default_secret_unless_demo()


def test_real_secret_starts_without_a_banner(monkeypatch, capsys):
    monkeypatch.setattr(app.config.settings, "APP_SECRET_KEY", "x" * 64)
    monkeypatch.setattr(app.config.settings, "LIBRERUN_DEMO", False)
    assert demo.refuse_default_secret_unless_demo() is False
    assert "DEMO MODE" not in capsys.readouterr().err


def test_demo_mode_accepts_the_default_secret_and_announces_itself(monkeypatch, capsys, caplog):
    monkeypatch.setattr(app.config.settings, "APP_SECRET_KEY", DEFAULT_APP_SECRET_KEY)
    monkeypatch.setattr(app.config.settings, "LIBRERUN_DEMO", True)
    monkeypatch.setattr(app.config.settings, "LIBRERUN_STUB_LLM", True)
    assert demo.refuse_default_secret_unless_demo() is True
    err = capsys.readouterr().err
    assert "LibreRun DEMO MODE" in err
    assert "default APP_SECRET_KEY" in err and "stub" in err
    assert any(r.msg.get("event") == "librerun_demo_mode" if isinstance(r.msg, dict) else "librerun_demo_mode" in str(r.msg) for r in caplog.records)


def test_the_app_lifespan_refuses_the_default_secret(monkeypatch):
    """The guard is wired into the real lifespan: entering it with the
    shipped secret and demo mode off raises, so uvicorn exits."""
    import app.main as main

    monkeypatch.setattr(app.config.settings, "APP_SECRET_KEY", DEFAULT_APP_SECRET_KEY)
    monkeypatch.setattr(app.config.settings, "LIBRERUN_DEMO", False)
    with pytest.raises(demo.DefaultSecretRefused):
        with TestClient(main.app):
            pass


class _Toy(AgentProtocol):
    agent_id = "toy-v1"
    display_name = "Toy"
    description = "d"

    def input_schema(self):
        return {"type": "object", "properties": {}}


def _meta_client(monkeypatch) -> TestClient:
    """The real ``/meta`` route on a bare app with the DB dependency stubbed
    — the handler must answer from settings when no database is there."""
    import app.main as main
    from app.database import get_db

    # The gateway's /healthz is cached for a few seconds so /meta can be
    # polled per page load; clear it so each test resolves its own answer
    # rather than the previous test's.
    main._gateway_health_cache = None

    bare = FastAPI()
    bare.title, bare.version = main.app.title, main.app.version
    for route in main.app.routes:
        if getattr(route, "path", "") == "/api/v1/meta":
            bare.routes.append(route)

    async def _no_db():
        yield None

    # On the app the route was CREATED on, not on ``bare``: a route keeps
    # its app as the provider of overrides, so an override set on ``bare``
    # was never read and ``/meta`` reached the real database — and cached
    # what it found in Redis for a minute, for whichever test came next
    # (K blueprint A1's canary test tripped over it).
    monkeypatch.setitem(main.app.dependency_overrides, get_db, _no_db)
    return TestClient(bare)


def test_meta_is_public_and_carries_the_demo_facts(monkeypatch):
    monkeypatch.setattr(app.config.settings, "LIBRERUN_DEMO", True)
    monkeypatch.setattr(app.config.settings, "APP_SECRET_KEY", DEFAULT_APP_SECRET_KEY)
    monkeypatch.setattr(app.config.settings, "TRACE_VIEWER", "jaeger")
    monkeypatch.setattr(app.config.settings, "TRACE_VIEWER_BASE_URL", "http://localhost:16686")
    # Keyless mode is the GATEWAY's fact (blueprint S4a): the backend
    # holds no such switch any more and asks /healthz for it. Stub the
    # answer here rather than a setting, which is also how it arrives in
    # production.
    from app.services import gateway_client

    async def _health(timeout: float = 2.0):
        return {"status": "ok", "stub": True}

    monkeypatch.setattr(gateway_client, "health", _health)
    manifest = AgentManifest.model_validate({
        "id": "toy-v1", "name": "Toy Agent", "runtime": "python-package",
        "phases": [{"name": "analyze"}], "output": {"mode": "structured"},
    })
    registry.register(_Toy(), manifest)

    monkeypatch.setattr(app.config.settings, "LIBRERUN_SOURCE_URL", "")

    r = _meta_client(monkeypatch).get("/api/v1/meta")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "name": "LibreRun",
        "version": __version__,
        # K blueprint A1 (R17): the licence, and where the source of the
        # running version is — its tag, since nothing overrides it here.
        "license": "AGPL-3.0-only",
        "source_url": f"https://github.com/JeremiahJRRoss/librerun/tree/v{__version__}",
        "demo": True,
        "stub_llm": True,
        "gateway": "ok",
        "default_secret": True,
        "trace_viewer_configured": True,
        "trace_viewer": "jaeger",
        "trace_viewer_source": "env",
        "agents": [{"id": "toy-v1", "name": "Toy Agent"}],
    }


def test_meta_names_the_viewer_a_runtime_override_selected(monkeypatch):
    """An admin override can switch the viewer behind the environment's
    back; ``/meta`` says which preset renders the links and that the
    environment is not the source, so a script that knows only ``.env``
    claims no destination (Codex on PR #51). The URL itself stays off
    the public surface."""
    from app.services import app_settings_service as svc

    monkeypatch.setattr(app.config.settings, "LIBRERUN_DEMO", False)
    monkeypatch.setattr(app.config.settings, "APP_SECRET_KEY", "r" * 64)
    monkeypatch.setattr(app.config.settings, "TRACE_VIEWER", "jaeger")
    monkeypatch.setattr(app.config.settings, "TRACE_VIEWER_BASE_URL", "http://localhost:16686")
    monkeypatch.setattr(app.config.settings, "TRACE_VIEWER_URL_TEMPLATE", "")
    overrides = {
        "trace_viewer": "custom",
        "trace_viewer_base_url": "",
        "trace_viewer_url_template": "https://viewer.corp.example/t/{trace_id}",
    }

    async def _get_setting(db, key):
        return overrides[key]

    monkeypatch.setattr(svc, "get_setting", _get_setting)
    body = _meta_client(monkeypatch).get("/api/v1/meta").json()
    assert body["trace_viewer_configured"] is True
    assert body["trace_viewer"] == "custom"
    assert body["trace_viewer_source"] == "runtime"
    assert "viewer.corp.example" not in str(body)

    # The same values as the environment, set at runtime, are still the
    # environment's configuration: the script's destination holds.
    overrides.update(
        {"trace_viewer": "Jaeger", "trace_viewer_base_url": "http://localhost:16686/",
         "trace_viewer_url_template": ""}
    )
    overrides["trace_viewer_base_url"] = "http://localhost:16686"
    body = _meta_client(monkeypatch).get("/api/v1/meta").json()
    assert body["trace_viewer"] == "jaeger" and body["trace_viewer_source"] == "env"


def test_meta_says_the_secret_is_real_when_the_demo_generated_one(monkeypatch):
    """scripts/demo.sh generates a secret: the banner must not claim the
    shipped default is in use (Codex on PR #51)."""
    monkeypatch.setattr(app.config.settings, "LIBRERUN_DEMO", True)
    monkeypatch.setattr(app.config.settings, "APP_SECRET_KEY", "g" * 64)
    body = _meta_client(monkeypatch).get("/api/v1/meta").json()
    assert body["demo"] is True and body["default_secret"] is False


def test_meta_reports_no_viewer_when_trace_links_are_off(monkeypatch):
    monkeypatch.setattr(app.config.settings, "LIBRERUN_DEMO", False)
    monkeypatch.setattr(app.config.settings, "LIBRERUN_STUB_LLM", False)
    monkeypatch.setattr(app.config.settings, "APP_SECRET_KEY", "r" * 64)
    monkeypatch.setattr(app.config.settings, "TRACE_VIEWER", "off")
    # Stubbed, not left to whatever happens to be listening: the
    # documented local workflow is `scripts/demo.sh` and then work, so a
    # developer running the suite on that machine HAS a gateway on the
    # default URL, and this test would fail on a tree with nothing wrong
    # with it.
    from app.services import gateway_client

    async def _unreachable(timeout: float = 2.0):
        return None

    monkeypatch.setattr(gateway_client, "health", _unreachable)
    body = _meta_client(monkeypatch).get("/api/v1/meta").json()
    # stub_llm is the GATEWAY's fact from blueprint S4a: with no gateway
    # answering it is null, and ``gateway`` says why — "not stubbed"
    # would be a guess the banner acts on.
    assert body["demo"] is False
    assert body["stub_llm"] is None
    assert body["gateway"] == "unreachable"
    assert body["default_secret"] is False
    assert body["trace_viewer_configured"] is False
    assert body["trace_viewer"] == "off" and body["trace_viewer_source"] == "env"
    assert body["agents"] == []


def test_the_shipped_default_renders_no_trace_link(monkeypatch):
    """TRACE_VIEWER defaults to off (Codex on PR #51): a fresh install has
    no viewer running, so no link is built until one is configured."""
    from app.config import Settings
    from app.observability.trace_viewer import viewer_configured

    fresh = Settings(_env_file=None)
    assert fresh.TRACE_VIEWER == "off"
    assert viewer_configured(fresh.TRACE_VIEWER, fresh.TRACE_VIEWER_BASE_URL, fresh.TRACE_VIEWER_URL_TEMPLATE) is False
    assert viewer_configured("jaeger", "http://localhost:16686", "") is True
    # A preset whose template needs {base} is not configured without a base
    # URL: a relative /trace/<id> would open the LibreRun UI, not a viewer.
    assert viewer_configured("jaeger", "", "") is False
    assert viewer_configured("phoenix", "   ", "") is False
    # A custom template that carries its own host needs no base URL.
    assert viewer_configured("custom", "", "https://viewer.example/t/{trace_id}") is True


# ---------------------------------------------------------------------------
# The banner reaches the operator, and reaches them NOW (blueprint S4a)
# ---------------------------------------------------------------------------

_BANNER_PROBE = """
import os, sys
os.environ["LOG_QUEUE_ONLY"] = "true"
os.environ["LIBRERUN_DEMO"] = "true"
from app.logging_config import configure_logging, install_process_capture
configure_logging()
from app import logging_queue
if {fd_belt}:
    install_process_capture()
else:
    # sys.stderr replaced, descriptors 1 and 2 left alone.
    logging_queue.install_thread_context()
    logging_queue.install_stdio_capture(fd_belt=False)
if not logging_queue.installed():
    print("PROBE-ABORT: the queue pipeline is not installed", file=sys.__stderr__)
    os._exit(3)
if sys.stderr.__class__ is not logging_queue.CapturingWriter:
    print("PROBE-ABORT: sys.stderr was not captured", file=sys.__stderr__)
    os._exit(4)
from app.demo import log_demo_banner
log_demo_banner()
if {drain}:
    # Let the listener catch up, so a SECOND copy of the banner — one
    # that also went the ordinary, queued way — becomes visible.
    logging_queue.drain(timeout=120.0)
# Without the drain there is no orderly exit either: whatever is on the
# console got there WITHOUT the listener thread having run. That is the
# assertion — a banner that needs the queue drained is a banner an
# operator may never see, because the first walk loads spaCy.
sys.stdout.flush()
os._exit(0)
"""


def _run_banner_probe(tmp_path, *, fd_belt: bool = True, drain: bool = False):
    import subprocess
    import sys as _sys
    from pathlib import Path

    backend_root = Path(__file__).resolve().parents[1]
    script = tmp_path / f"banner_probe_{fd_belt}_{drain}.py"
    script.write_text(_BANNER_PROBE.format(fd_belt=fd_belt, drain=drain))
    env = dict(os.environ)
    env["PYTHONPATH"] = str(backend_root)
    env.pop("LOG_FORMAT", None)
    # The suite's conftest turns the stderr sink off and redirects the
    # log file; a probe that inherited those would be watching a console
    # nothing writes to, and would pass whatever the banner did. This
    # runs with the deployment's own settings — the operator's
    # `compose logs` view.
    env["LOG_STDERR_ENABLED"] = "true"
    env.pop("LOG_FILE_PATH", None)
    return subprocess.run(
        [_sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


@pytest.mark.parametrize("fd_belt", [True, False])
def test_the_banner_reaches_the_console_without_the_queue_draining(tmp_path, fd_belt):
    """Blueprint S4 replaced ``sys.stderr`` with a writer that turns each
    line into a walked, queued log record. That is right for anything
    that might carry a value and wrong for this: the first walk loads
    spaCy, so the banner surfaced seconds after the API was already
    serving — and an operator who looked in between saw a demo-mode
    deployment with no notice that it was one. The CI gate read the log
    in that window and went red, which is how this was found.

    The probe exits with ``os._exit`` so the listener thread never runs:
    anything on stderr got there immediately, past the capture.

    Both belts, because the two have different consoles: with the fd
    belt, descriptor 2 is a pipe and only the backup reaches the
    terminal; without it, the replaced ``sys.stderr`` object still does.
    """
    result = _run_banner_probe(tmp_path, fd_belt=fd_belt)

    assert result.returncode == 0, result.stderr
    assert "PROBE-ABORT" not in result.stderr
    assert "LibreRun DEMO MODE" in result.stderr, (
        "the banner did not reach the console before the queue drained:\n"
        + result.stderr[-2000:]
    )
    # Verbatim, too: the walk reads names out of prose, and a banner that
    # says [REDACTED_PERSON_1] is not a notice.
    assert demo.BANNER_FOOTER in result.stderr


def test_the_banner_is_not_also_queued(tmp_path):
    """Exactly once, which only a drained queue can show.

    The console write and the ordinary ``sys.stderr`` write are an
    either/or, and doing both would print the banner twice — once on the
    terminal now and once through the walk later, where the recognizers
    read "LibreRun DEMO MODE" out of prose as a person. The no-drain
    probe above cannot see the second copy: it is still in the queue,
    which is exactly why it needs its own test.
    """
    result = _run_banner_probe(tmp_path, fd_belt=True, drain=True)

    assert result.returncode == 0, result.stderr
    assert "PROBE-ABORT" not in result.stderr
    count = result.stderr.count("LibreRun DEMO MODE")
    assert count == 1, (
        f"the banner reached the operator {count} times, not once:\n"
        + result.stderr[-3000:]
    )


def test_the_banner_is_built_only_from_literals(monkeypatch):
    """The banner skips the walk, so nothing variable may reach it.

    Composed from :data:`demo.BANNER_LINES` and asserted against it: an
    f-string added to :func:`demo.banner_lines` fails here rather than
    quietly putting a value on an unwalked stream.
    """
    for default_secret in (True, False):
        monkeypatch.setattr(
            app.config.settings,
            "APP_SECRET_KEY",
            DEFAULT_APP_SECRET_KEY if default_secret else "x" * 64,
        )
        monkeypatch.setattr(app.config.settings, "LIBRERUN_DEMO", True)
        lines = demo.banner_lines()
        assert lines, "the banner must say something"
        unknown = [line for line in lines if line not in demo.BANNER_LINES]
        assert not unknown, f"banner lines not drawn from BANNER_LINES: {unknown}"
        assert (demo.BANNER_DEFAULT_SECRET in lines) is default_secret
