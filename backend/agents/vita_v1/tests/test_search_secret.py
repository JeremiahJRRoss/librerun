"""The demo agent's web-search key is its declared tool secret (K8a, K8-16).

``search_tavily`` asks the run's façade for ``tavily_api_key`` — this
tenant's value, else every tenant's default, else the backend's
``TAVILY_API_KEY`` — and reads ``SecretNotSet`` as it always read an unset
key: no web results, and no request made. It no longer reads the process
environment itself.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agents.vita_v1 import search_service
from agents.vita_v1.search_service import SearchService
from app.agents.manifest import load_manifest
from app.capabilities import SecretNotSet

KEY = "tvly-the-tenants-search-key-60aa"


class _Secrets:
    def __init__(self, value: str | None):
        self.value = value
        self.asked: list[str] = []

    async def get(self, name: str) -> str:
        self.asked.append(name)
        if self.value is None:
            raise SecretNotSet(name)
        return self.value


def _service(value: str | None):
    secrets = _Secrets(value)
    return SearchService(SimpleNamespace(secrets=secrets)), secrets


class _Response:
    is_success = True

    def json(self):
        return {"results": [{"url": "https://example.com/a", "title": "A", "content": "c"}]}


@pytest.fixture
def requests(monkeypatch):
    sent: list[dict] = []

    async def _post(self, url, json=None, **kwargs):  # noqa: A002 — httpx's own name
        sent.append({"url": url, "json": json})
        return _Response()

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    return sent


def test_the_manifest_declares_the_key():
    manifest = load_manifest(Path(search_service.__file__).resolve().parent)
    assert manifest.secrets == ["tavily_api_key"]


@pytest.mark.asyncio
async def test_an_unset_key_is_no_web_results_and_no_request(requests, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", KEY)  # the façade decides, not the agent
    service, secrets = _service(None)
    assert await service.search_tavily(["q"]) == []
    assert secrets.asked == ["tavily_api_key"]
    assert requests == []


@pytest.mark.asyncio
async def test_the_delivered_key_is_the_one_sent(requests):
    service, _ = _service(KEY)
    results = await service.search_tavily(["q"])
    assert [r.url for r in results] == ["https://example.com/a"]
    (request,) = requests
    assert request["json"]["api_key"] == KEY


def test_the_agent_reads_no_environment_variable_itself():
    source = inspect.getsource(search_service)
    assert "os.environ" not in source and "import os" not in source
