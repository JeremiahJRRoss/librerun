"""``ctx.config.settings()`` — the agent's settings as its tenant set them
(K5a, L32).

A container declares ``settings[]`` in its manifest and reads the values
the chassis holds for this run's tenant through the ``config_get`` MCP
tool, which answers ``settings: [{key, value}]``. The SDK hands the
handler a mapping, because a handler asks for a setting by its key.
"""
from __future__ import annotations

import pytest
from _fakes import FakeMCP, start

from librerun_agent._llm import Config
from librerun_agent._mcp import CapabilityError, CapabilityNotGranted, MCPClient


@pytest.fixture(scope="module")
def mcp_url():
    server, url = start(FakeMCP)
    yield url
    server.shutdown()


@pytest.fixture
def scripted():
    """The fake's script, restored afterwards: other modules script it too."""
    saved = FakeMCP.responses
    FakeMCP.calls.clear()
    yield FakeMCP
    FakeMCP.responses = saved
    FakeMCP.calls.clear()


def _config(url: str) -> Config:
    return Config(MCPClient(url, "tok-cfg"))


async def test_settings_is_a_mapping(mcp_url, scripted):
    scripted.responses = {
        "config_get": {
            "steps": [{"step_id": "echo", "model": "m"}],
            "settings": [
                {"key": "note", "value": "hello"},
                {"key": "limit", "value": 3},
                {"key": "strict", "value": False},
                {"key": "tags", "value": []},
            ],
        }
    }
    settings = await _config(mcp_url).settings()
    # Every key the chassis sent, its value as sent: a false and an empty
    # list are values, not absences.
    assert settings == {"note": "hello", "limit": 3, "strict": False, "tags": []}
    assert [(c["name"], c["arguments"], c["authorization"]) for c in scripted.calls] == [
        ("config_get", {}, "Bearer tok-cfg")
    ]


async def test_an_agent_that_declares_none_reads_an_empty_mapping(mcp_url, scripted):
    """An empty list, and a chassis older than ``settings[]`` whose answer
    has no ``settings`` at all, both read as no settings."""
    for answer in ({"steps": [], "settings": []}, {"steps": []}):
        scripted.responses = {"config_get": answer}
        assert await _config(mcp_url).settings() == {}


async def test_steps_still_read_the_same_answer(mcp_url, scripted):
    scripted.responses = {
        "config_get": {
            "steps": [{"step_id": "echo", "model": "m"}],
            "settings": [{"key": "note", "value": "hello"}],
        }
    }
    config = _config(mcp_url)
    assert await config.steps() == [{"step_id": "echo", "model": "m"}]
    assert await config.step("echo") == {"step_id": "echo", "model": "m"}


async def test_a_refusal_is_raised_not_read_as_no_settings(mcp_url, scripted):
    """A chassis that refuses the call is not an agent with no settings:
    an empty mapping here would run the handler on defaults it never
    read, silently."""
    scripted.responses = {"config_get": ("error", -32002, "capability 'llm' is not granted")}
    with pytest.raises(CapabilityNotGranted):
        await _config(mcp_url).settings()


async def test_without_an_mcp_url_it_says_so():
    with pytest.raises(CapabilityError, match="run.mcp.url"):
        await Config(None).settings()
