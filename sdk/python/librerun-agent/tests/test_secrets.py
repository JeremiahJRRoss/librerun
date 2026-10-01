"""``ctx.secrets.get`` — the tool secrets a manifest declares, this tenant's
values, over the MCP ``secret_get`` tool (K8a's server half, K8b's client).

The chassis answers ``{value}``, or ``-32005 secret_not_declared`` for a
name the manifest does not declare and ``-32006 secret_not_set`` for a
declared name nobody set. Both refusals are typed, so an agent can read
"no key" as a state and a typo as a bug, and both are ``CapabilityError``s,
so the template's ``except CapabilityError`` still covers every refusal.
"""
from __future__ import annotations

import pytest
from _fakes import FakeMCP, start

from librerun_agent import CapabilityError, RunContext, SecretNotDeclared, SecretNotSet
from librerun_agent._context import Invocation

VALUE = "a-tool-secret-the-fake-hands-back"


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


def _ctx(url: str) -> RunContext:
    return RunContext(
        Invocation(
            id="inv-secrets",
            token="tok-secrets",
            run_id="run-secrets",
            tenant_id=None,
            phase="echo",
            input={},
            prior_output=None,
            user_edits=None,
            rerun=False,
            deadline_seconds=None,
            mcp_url=url,
            traceparent=None,
            tracestate=None,
            trace_id=None,
        )
    )


async def test_get_returns_the_value(mcp_url, scripted):
    scripted.responses = {"secret_get": {"value": VALUE}}
    assert await _ctx(mcp_url).secrets.get("search_key") == VALUE
    assert [(c["name"], c["arguments"], c["authorization"]) for c in scripted.calls] == [
        ("secret_get", {"name": "search_key"}, "Bearer tok-secrets")
    ]


async def test_minus_32006_is_secret_not_set(mcp_url, scripted):
    """A declared name nobody set: the agent's "no key" branch, typed."""
    scripted.responses = {"secret_get": ("error", -32006, "secret_not_set: search_key")}
    with pytest.raises(SecretNotSet) as caught:
        await _ctx(mcp_url).secrets.get("search_key")
    assert caught.value.code == -32006
    assert "search_key" in caught.value.message


async def test_both_are_capability_errors(mcp_url, scripted):
    """-32005 is its own type, and neither refusal escapes an agent that
    catches ``CapabilityError`` — nor is either read as a value."""
    scripted.responses = {"secret_get": ("error", -32005, "secret_not_declared: other")}
    with pytest.raises(SecretNotDeclared) as caught:
        await _ctx(mcp_url).secrets.get("other")
    assert caught.value.code == -32005
    assert issubclass(SecretNotDeclared, CapabilityError)
    assert issubclass(SecretNotSet, CapabilityError)
    assert not issubclass(SecretNotSet, SecretNotDeclared)
    for answer in (
        ("error", -32005, "secret_not_declared: other"),
        ("error", -32006, "secret_not_set: other"),
    ):
        scripted.responses = {"secret_get": answer}
        with pytest.raises(CapabilityError):
            await _ctx(mcp_url).secrets.get("other")
