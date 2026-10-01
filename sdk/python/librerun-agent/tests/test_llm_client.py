"""``ctx.llm.client()`` and ``ctx.llm.step()`` — the framework door
(blueprint S5/S5-R).

``ctx.llm.complete()`` is for a handler that makes its own model call.
A framework makes the call itself, and all it will accept is a base
URL, a key, some default headers and a model name. Handing those four
out is the whole of this surface — and the reason it exists at all is
D10: the headers carry THIS invocation's run token, so a client built
inside the handler is attributable to this run and this tenant, and a
client built once at import time is not.

Every test here is about a value a framework will carry verbatim into
an HTTP request, so they assert the exact strings rather than that
something truthy came back.
"""
from __future__ import annotations

import pathlib
import time

import pytest

from librerun_agent import GatewayClient, LlmError, _llm


class _Inv:
    """The parts of an Invocation this client reads."""

    def __init__(self, token="tok-1", traceparent=None, tracestate=None):
        self.deadline_seconds = 300
        self.started_at = time.monotonic()
        self.token = token
        self.traceparent = traceparent
        self.tracestate = tracestate

    def seconds_left(self):
        return max(0.0, self.started_at + self.deadline_seconds - time.monotonic())


@pytest.fixture
def gateway_env(monkeypatch):
    """A container's environment as ``agents.compose.yaml`` sets it."""
    monkeypatch.delenv("LIBRERUN_AGENT_KEY", raising=False)
    monkeypatch.setenv("LIBRERUN_GATEWAY_URL", "http://gateway:8090")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gateway:8090/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "lr_agent_abc")
    return monkeypatch


# ------------------------------- step() -------------------------------


def test_step_names_a_step_in_the_one_form_the_gateway_parses():
    llm = _llm.Llm(_Inv())
    assert llm.step("extract") == "librerun/extract"


def test_step_refuses_an_empty_id():
    """A framework handed "librerun/" would be refused `step_required`
    by the gateway after the call had already left — and the author
    would be reading a gateway refusal for a bug in their own code."""
    llm = _llm.Llm(_Inv())
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            llm.step(bad)


def test_the_step_form_is_the_one_the_gateway_actually_parses():
    """Pinned against the gateway's own source rather than restated.

    The prefix is a contract between two processes, and an SDK test
    asserting the SDK's own spelling would go on passing after the
    gateway's half changed. The gateway is a separate service with its
    own dependencies, so this reads its source rather than importing it
    — and it fails, rather than skipping, when it cannot find what it
    came to read: a pin that quietly stops pinning is the shape
    CLAUDE.md forbids.

    In a checkout that is only the SDK (the package is installable on
    its own) there is no gateway to pin against and the test says so.
    """
    import re

    root = pathlib.Path(__file__).resolve().parents[4]
    source = root / "services" / "gateway" / "gateway" / "steps.py"
    if not (root / "services" / "gateway").is_dir():
        pytest.skip("no gateway service in this checkout (the SDK ships alone too)")

    assert source.is_file(), f"the gateway's step resolver is missing: {source}"
    body = source.read_text()
    match = re.search(r"def requested_step_id\(.*?\n(?=\S)", body, re.S)
    assert match, (
        "requested_step_id is gone from the gateway — the model-string "
        "fallback this SDK depends on may have gone with it"
    )
    literals = set(re.findall(r'"(librerun/[^"]*)"', match.group(0)))
    assert literals == {"librerun/"}, (
        f"the gateway parses {sorted(literals)}; the SDK emits "
        f"{_llm.STEP_MODEL_PREFIX!r}"
    )
    assert _llm.Llm(_Inv()).step("draft") == "librerun/draft"


# ------------------------------ client() ------------------------------


def test_the_client_carries_the_gateway_the_key_and_this_runs_token(gateway_env):
    client = _llm.Llm(_Inv(token="tok-42")).client()

    assert isinstance(client, GatewayClient)
    assert client.base_url == "http://gateway:8090/v1"
    assert client.api_key == "lr_agent_abc"
    assert client.default_headers == {"X-LibreRun-Run-Token": "tok-42"}


def test_the_trace_context_travels_so_the_llm_span_joins_this_runs_tree(gateway_env):
    inv = _Inv(traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01", tracestate="x=1")

    headers = _llm.Llm(inv).client().default_headers

    assert headers["traceparent"] == inv.traceparent
    assert headers["tracestate"] == "x=1"


def test_two_invocations_get_two_clients_with_two_tokens(gateway_env):
    """D10, stated as a test: the token is per invocation, so the client
    is. This is the failure the whole surface exists to prevent — a
    client built once and reused carries the first run's token into
    every later run, and on a multi-tenant deployment the gateway then
    resolves the wrong tenant's step configuration."""
    first = _llm.Llm(_Inv(token="tok-a")).client()
    second = _llm.Llm(_Inv(token="tok-b")).client()

    assert first.default_headers["X-LibreRun-Run-Token"] == "tok-a"
    assert second.default_headers["X-LibreRun-Run-Token"] == "tok-b"


def test_the_client_is_frozen_so_a_framework_cannot_retarget_it(gateway_env):
    client = _llm.Llm(_Inv()).client()
    with pytest.raises(Exception):
        client.base_url = "https://api.openai.com/v1"


def test_openai_base_url_alone_is_enough(gateway_env):
    """A framework-shaped container may set only OPENAI_BASE_URL. It
    already ends in /v1, and the client must not append a second one."""
    gateway_env.delenv("LIBRERUN_GATEWAY_URL")

    assert _llm.Llm(_Inv()).client().base_url == "http://gateway:8090/v1"


def test_the_explicit_key_variable_wins_over_the_openai_one(gateway_env):
    gateway_env.setenv("LIBRERUN_AGENT_KEY", "lr_agent_explicit")

    assert _llm.Llm(_Inv()).client().api_key == "lr_agent_explicit"


def test_no_gateway_is_named_rather_than_half_built(gateway_env):
    gateway_env.delenv("LIBRERUN_GATEWAY_URL")
    gateway_env.delenv("OPENAI_BASE_URL")

    with pytest.raises(LlmError) as exc:
        _llm.Llm(_Inv()).client()
    assert exc.value.code == "gateway_not_configured"


def test_no_agent_key_is_named_rather_than_half_built(gateway_env):
    """An empty key is a 401 from the gateway several layers later, by
    which point the author is debugging a model outage."""
    gateway_env.setenv("OPENAI_API_KEY", "   ")

    with pytest.raises(LlmError) as exc:
        _llm.Llm(_Inv()).client()
    assert exc.value.code == "agent_key_not_configured"
