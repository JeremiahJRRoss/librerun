"""The knowledge base's vector-store key moves onto K6's store (K8a, D36).

``kb.pinecone_api_key`` is a platform secret setting beside
``kb.embed_model``: a platform admin seals it in Admin -> Settings, and the
environment's ``PINECONE_API_KEY`` (or its ``_FILE``) serves while it is
not set. ``KbCapability.available()`` is synchronous, so it reads what
``get_secret_setting`` last resolved in this process
(``last_secret_setting``); ``search()`` resolves the key afresh and uses
it, and the runner resolves it once before a phase granted ``kb`` — so a
key set in the UI counts from the next phase, with nothing restarted.
"""
from __future__ import annotations

import sys
import types
import uuid

import pytest

import app.config as cfg
from app import capabilities as caps_mod
from app.agents import registry
from app.agents.protocol import AgentProtocol, InvestigationResult
from app.services import agent_runner, app_settings_service, secrets_service
from tests.test_agent_runner import (  # noqa: E402
    _FakeRun,
    _FakeSession,
    _manifest,
    patch_runner,  # noqa: F401  (fixture)
)
from tests.test_secrets_service import db, fernet_key, store  # noqa: F401  (fixtures)

KEY = "kb.pinecone_api_key"
ROW_VALUE = "pcsk-the-row-value-4b1d"
ENV_VALUE = "pcsk-the-environment-value-9e07"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    app_settings_service._LAST_RESOLVED.clear()
    monkeypatch.setattr(cfg.settings, "PINECONE_API_KEY", "")
    monkeypatch.setattr(cfg.settings, "PINECONE_ENVIRONMENT", "us-east-1")
    registry._clear_registry_for_tests()
    yield
    app_settings_service._LAST_RESOLVED.clear()
    registry._clear_registry_for_tests()


async def _seal(db, value: str) -> None:  # noqa: F811
    await secrets_service.set_secret(db, secrets_service.PLATFORM, KEY, value, user_id=None)
    await secrets_service.notify_change(secrets_service.PLATFORM, KEY)


def test_the_key_is_a_platform_secret_setting():
    spec = app_settings_service.get_spec(KEY)
    assert (spec.value_type, spec.env_var) == ("secret", "PINECONE_API_KEY")
    assert "PINECONE_API_KEY" in cfg.FILE_BACKED_SECRETS  # K2's _FILE kept
    # A secret never takes the plain path, whose cache is Redis.
    with pytest.raises(TypeError):
        import asyncio

        asyncio.run(app_settings_service.get_setting(None, KEY))


@pytest.mark.asyncio
async def test_last_secret_setting_is_what_was_last_resolved_else_the_environment(db, store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cfg.settings, "PINECONE_API_KEY", ENV_VALUE)
    assert app_settings_service.last_secret_setting(KEY) == ENV_VALUE  # nothing resolved yet

    await _seal(db, ROW_VALUE)
    assert app_settings_service.last_secret_setting(KEY) == ENV_VALUE  # not re-resolved yet
    assert await app_settings_service.get_secret_setting(db, KEY) == ROW_VALUE
    assert app_settings_service.last_secret_setting(KEY) == ROW_VALUE

    await secrets_service.unset_secret(db, secrets_service.PLATFORM, KEY)
    await secrets_service.notify_change(secrets_service.PLATFORM, KEY)
    assert await app_settings_service.get_secret_setting(db, KEY) == ENV_VALUE
    assert app_settings_service.last_secret_setting(KEY) == ENV_VALUE


@pytest.mark.asyncio
async def test_available_reads_it_and_search_resolves_and_uses_it(db, store, monkeypatch):  # noqa: F811
    """With no environment value, the row a platform admin sealed is what
    the vector store is opened with — read afresh by ``search``, which also
    brings ``available`` up to date."""
    import contextlib

    import app.database
    from app.services import gateway_client

    @contextlib.asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(app.database, "async_session", _session)
    opened_with: list[str] = []

    class _Index:
        def query(self, **kwargs):
            return {"matches": [{"score": 0.9, "metadata": {"title": "t", "url": "u", "text": "x"}}]}

    class _Pinecone:
        def __init__(self, api_key):
            opened_with.append(api_key)

        def Index(self, name):  # noqa: N802 — the client's own spelling
            return _Index()

    monkeypatch.setitem(sys.modules, "pinecone", types.SimpleNamespace(Pinecone=_Pinecone))

    async def _embed(**kwargs):
        return [[0.1, 0.2]]

    monkeypatch.setattr(gateway_client, "embed", _embed)
    kb = caps_mod.KbCapability(uuid.uuid4())

    assert kb.available() is False
    assert await kb.search(["q"]) == []  # nothing set anywhere: no store opened
    assert opened_with == []

    await _seal(db, ROW_VALUE)
    results = await kb.search(["q"])
    assert results and opened_with == [ROW_VALUE]
    assert kb.available() is True


class _KbAgent(AgentProtocol):
    agent_id = "kb-v1"
    display_name = "kb"
    description = "d"

    def __init__(self):
        self.saw_available: list[bool] = []

    async def analyze(self, inp, on_progress):  # noqa: ARG002
        self.saw_available.append(inp.capabilities.kb.available())
        return InvestigationResult(status="complete", structured={})


@pytest.mark.asyncio
async def test_the_runner_resolves_it_before_a_phase_granted_kb(patch_runner, monkeypatch):  # noqa: F811
    """A key set in the UI counts from the next phase: the runner resolves
    it once before a phase granted ``kb``, so the synchronous
    ``available()`` sees it — and not for a phase whose agent has no
    ``kb`` grant."""
    resolved: list[str] = []

    async def _get_secret_setting(db, key):
        resolved.append(key)
        app_settings_service._LAST_RESOLVED[key] = ROW_VALUE
        return ROW_VALUE

    monkeypatch.setattr(app_settings_service, "get_secret_setting", _get_secret_setting)
    agent = _KbAgent()
    manifest = _manifest("kb-v1", [{"name": "analyze"}], mode="structured")
    manifest.capabilities.append("kb")
    registry.register(agent, manifest)
    run = _FakeRun(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    run.agent_id = "kb-v1"
    patch_runner(_FakeSession(run))
    await agent_runner.start_run(run.id, run.tenant_id, "kb-v1")

    assert run.status == "complete", run.error_detail
    assert resolved == [KEY]
    assert agent.saw_available == [True]

    # Without the grant, no read.
    resolved.clear()
    registry._clear_registry_for_tests()
    registry.register(_KbAgent(), _manifest("kb-v1", [{"name": "analyze"}], mode="structured"))
    other = _FakeRun(run_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    other.agent_id = "kb-v1"
    patch_runner(_FakeSession(other))
    await agent_runner.start_run(other.id, other.tenant_id, "kb-v1")
    assert resolved == []
