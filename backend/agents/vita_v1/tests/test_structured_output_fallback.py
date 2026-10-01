"""Two things the gateway migration lost, and one it can now give back
(blueprint S4a; Codex round 9, two P2s).

Before S4a this service held three provider SDK integrations. Each of
them, when a model rejected structured-output enforcement, retried
WITHOUT it and let the parser and the normalizers do the work. Routing
every call through one OpenAI-shaped door dropped that, so a tenant
choosing a model — or an OpenAI-compatible endpoint — that does not
support `response_format: json_schema` turned a run that used to degrade
into a run that fails.

And `_provider_of` answered from the manifest's packaged declaration,
because an OpenAI-shaped reply names no provider. That made every
schema-drift audit row for an overridden step name the wrong provider,
in the batch whose whole point is that a tenant may override it. The
gateway resolves the step, so the gateway is the one that knows; it says
so in the `librerun` envelope now.
"""
from __future__ import annotations

import pytest

from app.capabilities import LlmError
from agents.vita_v1 import llm_service as mod


# ---------------------------------------------------------------- provider


def test_the_resolved_provider_wins_over_the_declaration():
    """A tenant override is exactly the case the declaration gets wrong."""
    response = {"librerun": {"provider": "anthropic", "model": "claude-x"}}
    declared = {"provider": "openai", "model": "gpt-4o"}

    assert mod._provider_of(response, declared) == "anthropic"


def test_the_declaration_is_only_the_fallback():
    """A reply from something older than the envelope still attributes
    to something rather than to nothing."""
    assert mod._provider_of({}, {"provider": "openai"}) == "openai"
    assert mod._provider_of({"librerun": {}}, {"provider": "openai"}) == "openai"
    assert mod._provider_of({}, None) == ""


# ------------------------------------------------- structured-output retry


def _rejection(message: str, *, status: int = 400, code: str = "provider_refused"):
    return LlmError(status, code, message)


@pytest.mark.parametrize(
    "message",
    [
        "the provider refused this call (BadRequestError): response_format is not supported",
        "Invalid parameter: 'json_schema' is unsupported for this model",
        "structured output is not available on this deployment",
    ],
)
def test_a_refusal_naming_the_feature_is_recognised(message):
    assert mod._rejected_response_format(
        _rejection(message), {"response_format": {"type": "json_schema"}}
    )


def test_a_call_that_never_sent_the_parameter_is_not_a_candidate():
    """Retrying here would be retrying a different failure."""
    assert not mod._rejected_response_format(
        _rejection("response_format is not supported"), {}
    )


@pytest.mark.parametrize(
    "exc",
    [
        # A refusal about something else entirely: retrying without the
        # schema would mask it and pay for the masking.
        _rejection("context_length_exceeded"),
        _rejection("invalid api key", status=401),
        # A 5xx is the retryable class and `call_with_retry` owns it;
        # stripping the schema would silently degrade a transient blip.
        _rejection("upstream unavailable", status=502, code="provider_unavailable"),
        # Our own refusal, not the provider's.
        _rejection("response_format", status=400, code="pii_in_identifier"),
    ],
)
def test_every_other_failure_is_left_alone(exc):
    assert not mod._rejected_response_format(
        exc, {"response_format": {"type": "json_schema"}}
    )


def test_the_narrowness_is_the_point():
    """All four conditions must hold. Matching on a provider's prose is
    inexact; the containment is what makes it safe, so each condition is
    pinned rather than left to the implementation's shape."""
    kwargs = {"response_format": {"type": "json_schema"}}
    assert mod._rejected_response_format(_rejection("bad response_format"), kwargs)
    # ...drop any one of them and it no longer fires.
    assert not mod._rejected_response_format(_rejection("bad response_format"), {})
    assert not mod._rejected_response_format(
        _rejection("bad response_format", status=500), kwargs
    )
    assert not mod._rejected_response_format(
        _rejection("bad response_format", code="gateway_timeout"), kwargs
    )
    assert not mod._rejected_response_format(_rejection("something else"), kwargs)


# ------------------------------------------------------- the retry itself


class _RejectsSchemaOnce:
    """A provider that refuses structured output, then answers."""

    def __init__(self):
        self.calls: list[dict] = []

    def stub_mode(self) -> bool:
        return False

    async def complete(self, step: str, messages: list[dict], **kwargs) -> dict:
        self.calls.append(dict(kwargs))
        if "response_format" in kwargs:
            raise LlmError(
                400,
                "provider_refused",
                "the provider refused this call (BadRequestError): "
                "response_format of type json_schema is not supported",
            )
        return {
            "id": "chatcmpl-1",
            "model": "some-model",
            "librerun": {"provider": "anthropic", "model": "some-model"},
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": '{"ok": true}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 15},
        }


def _service(capability):
    service = mod.LLMService.__new__(mod.LLMService)
    service.llm_capability = capability
    service._used = {}
    return service


@pytest.mark.asyncio
async def test_the_step_degrades_instead_of_failing_the_run():
    """End to end: the predicate is not the fix, the retry is. A guard
    that stopped at `_rejected_response_format` would pass while the run
    still died."""
    capability = _RejectsSchemaOnce()

    reply = await _service(capability).call(
        "refine_problem_statement", [{"role": "system", "content": "go"}]
    )

    assert len(capability.calls) == 2, "the call was not retried"
    assert "response_format" in capability.calls[0], "the first try sent no schema"
    assert "response_format" not in capability.calls[1], (
        "the retry sent the schema again — the same refusal, twice the spend"
    )
    assert reply == {"ok": True}, "the unstructured reply was not parsed"


@pytest.mark.asyncio
async def test_a_refusal_about_anything_else_is_not_retried():
    """Retrying a real error masks it and pays for the masking."""

    class _AlwaysRefuses:
        def __init__(self):
            self.calls = 0

        def stub_mode(self) -> bool:
            return False

        async def complete(self, step, messages, **kwargs):
            self.calls += 1
            raise LlmError(400, "provider_refused", "context_length_exceeded")

    capability = _AlwaysRefuses()
    with pytest.raises(LlmError):
        await _service(capability).call(
            "refine_problem_statement", [{"role": "system", "content": "go"}]
        )
    assert capability.calls == 1, "a non-schema refusal was retried"


@pytest.mark.asyncio
async def test_the_degraded_call_still_attributes_to_the_resolved_provider():
    """The two fixes meet here: the retry succeeds and the drift row it
    may produce still names the provider that actually answered."""
    capability = _RejectsSchemaOnce()
    service = _service(capability)

    await service.call("refine_problem_statement", [{"role": "system", "content": "go"}])

    assert service._used["refine_problem_statement"]["provider"] == "anthropic"
