"""What LiteLLM is asked for, and what it is not allowed to do."""
from __future__ import annotations

import pytest

from gateway import egress
from gateway.steps import ResolvedStep


def _step(**overrides) -> ResolvedStep:
    base = dict(
        step_id="think",
        provider="openai",
        model="gpt-4o",
        temperature=0.0,
        max_tokens=1000,
        timeout_seconds=30,
    )
    base.update(overrides)
    return ResolvedStep(**base)


def test_the_steps_resolved_values_win_over_the_callers():
    """L25: the admin picks the model and the limits. A caller that sets
    its own would otherwise route around every choice the admin made —
    which is the whole thing the gateway exists to prevent."""
    body = {
        "model": "some-expensive-model",
        "temperature": 1.9,
        "max_tokens": 999999,
        "timeout": 3600,
        "messages": [{"role": "user", "content": "hi"}],
    }

    kwargs = egress._call_kwargs(body, _step())

    assert kwargs["model"] == "openai/gpt-4o"
    assert kwargs["temperature"] == 0.0
    assert kwargs["max_tokens"] == 1000
    assert kwargs["timeout"] == 30
    assert kwargs["messages"] == body["messages"]


def test_an_unset_step_value_leaves_the_field_off_rather_than_guessing():
    kwargs = egress._call_kwargs(
        {"messages": []}, _step(temperature=None, max_tokens=None, timeout_seconds=None)
    )

    assert "temperature" not in kwargs
    assert "max_tokens" not in kwargs
    assert "timeout" not in kwargs


def test_the_provider_credential_comes_from_this_process_alone(monkeypatch):
    monkeypatch.setattr(egress.settings, "OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setattr(egress.settings, "ANTHROPIC_API_KEY", "")

    assert egress.credential_for("openai") == "sk-test-openai"
    assert egress.credential_for("anthropic") is None
    # An unknown provider gets no credential rather than a default one.
    assert egress.credential_for("some-new-provider") is None
    assert egress.credential_for(None) is None

    kwargs = egress._call_kwargs({"messages": []}, _step())
    assert kwargs["api_key"] == "sk-test-openai"


def test_the_stub_provider_never_reaches_the_egress_library():
    assert egress.is_stub(_step(provider="stub")) is True
    assert egress.is_stub(_step(provider="openai")) is False


def test_litellm_is_never_allowed_to_record_message_content():
    """The gateway writes the one LLM span, with content that has been
    through the walker. A library recording the raw reply beside it would
    undo that silently, and nothing in the exported span set would show
    it — so the settings are asserted rather than assumed."""
    litellm = egress._litellm()

    assert litellm.turn_off_message_logging is True
    assert litellm.success_callback == []
    assert litellm.failure_callback == []
    assert litellm.callbacks == []


def test_a_stub_call_is_costed_against_the_model_it_stands_in_for():
    """Keyless mode is meant to exercise the real telemetry. A cost
    attribute that only ever appeared with a provider account would be
    one nobody could test."""
    response = {"usage": {"prompt_tokens": 1000, "completion_tokens": 500}}

    cost = egress.cost_usd(response, _step(provider="stub"))

    assert cost is not None and cost > 0


def test_an_unpriced_model_is_not_an_error():
    response = {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    assert egress.cost_usd(response, _step(model="not-a-real-model-xyz")) is None
    # No usage at all means no cost, not a zero that looks like one.
    assert egress.cost_usd({"usage": {}}, _step()) is None


# ------------------------- the content-capture switch ------------------------
#
# The variable moved here from the backend with the provider keys: this is
# the process that makes the model call, so it is the process the switch
# governs. Its resolution moved intact, including the two parts that look
# like fussiness and are not.


import pytest as _pytest  # noqa: E402  (grouped with the tests it serves)

from gateway import telemetry  # noqa: E402


@_pytest.fixture
def content(monkeypatch):
    monkeypatch.delenv(telemetry._CONTENT_VAR, raising=False)
    monkeypatch.setattr(
        telemetry.settings, "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", ""
    )
    return telemetry


def test_content_capture_defaults_to_full(content):
    assert content.content_capture_value() == "SPAN_AND_EVENT"
    assert content.capture_content() is True


@_pytest.mark.parametrize(
    "value,expected",
    [
        ("NO_CONTENT", "NO_CONTENT"),
        ("SPAN_ONLY", "SPAN_ONLY"),
        ("event_only", "EVENT_ONLY"),
        ("true", "SPAN_AND_EVENT"),
        ("False", "NO_CONTENT"),
    ],
    ids=["enum", "span only", "lowercase enum", "legacy true", "legacy false"],
)
def test_content_capture_normalises_what_deployments_actually_write(
    content, monkeypatch, value, expected
):
    monkeypatch.setenv(content._CONTENT_VAR, value)

    assert content.content_capture_value() == expected


@_pytest.mark.parametrize("value", ["NO_CONTNET", "nope", "SPAN AND EVENT"])
def test_an_unrecognised_value_fails_closed(content, monkeypatch, value):
    """The only person who writes this variable is one trying to suppress
    content, so a typo must suppress it rather than export everything."""
    monkeypatch.setenv(content._CONTENT_VAR, value)

    assert content.content_capture_value() == "NO_CONTENT"
    assert content.capture_content() is False


def test_the_environment_outranks_the_settings_file(content, monkeypatch):
    monkeypatch.setattr(
        content.settings,
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
        "NO_CONTENT",
    )
    assert content.content_capture_value() == "NO_CONTENT"

    monkeypatch.setenv(content._CONTENT_VAR, "SPAN_AND_EVENT")
    assert content.content_capture_value() == "SPAN_AND_EVENT"

    # A BLANK environment value falls through rather than winning: that is
    # what compose's ${VAR:-} passthrough of an unset variable looks like,
    # and treating it as a value would turn it into an accidental opt-out.
    monkeypatch.setenv(content._CONTENT_VAR, "")
    assert content.content_capture_value() == "NO_CONTENT"
