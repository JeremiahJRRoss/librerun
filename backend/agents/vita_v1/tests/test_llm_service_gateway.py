"""What this agent sends to the gateway, and what it does with the reply.

Until blueprint S4a this file's predecessor tested three provider SDK
integrations — OpenAI's ``response_format``, Anthropic's tool-use,
Gemini's system instruction, reasoning-model token parameters, lazy
client construction. None of that is the agent's business any more: the
gateway owns the provider boundary, the admin owns the model, and this
agent owns its prompts, its schemas and its parsing.

So what is left to prove is the seam. The service names a **step**, asks
for its schema, hands over its own keyless fixture, and parses what
comes back — and it never names a model, because naming one would be
this agent overruling the admin.
"""
from __future__ import annotations

import json
import logging

import pytest

from agents.vita_v1.llm_service import STEP_SCHEMAS, LLMService


class _Recorder:
    """Stands in for the granted ``llm`` capability, recording the call."""

    def __init__(self, content: str = '{"ok": true}', model: str = "gpt-4o"):
        self.calls: list[tuple[str, list[dict], dict]] = []
        self._content = content
        self._model = model

    def stub_mode(self) -> bool:
        return False

    async def complete(self, step: str, messages: list[dict], **kwargs) -> dict:
        self.calls.append((step, messages, kwargs))
        return {
            "id": "chatcmpl-1",
            "model": self._model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self._content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }


def _service(recorder: _Recorder) -> LLMService:
    service = LLMService.__new__(LLMService)
    service.llm_capability = recorder
    service._used = {}
    return service


_MESSAGES = [{"role": "system", "content": "do the thing"}]


@pytest.mark.asyncio
async def test_the_call_names_a_step_and_never_a_model():
    """L25: the agent declares steps, the admin picks the model. A call
    that named a model would route around every choice the admin made."""
    recorder = _Recorder()

    await _service(recorder).call("refine_problem_statement", _MESSAGES)

    step, messages, kwargs = recorder.calls[0]
    assert step == "refine_problem_statement"
    assert messages == _MESSAGES
    assert "model" not in kwargs
    assert "provider" not in kwargs
    assert "temperature" not in kwargs
    assert "max_tokens" not in kwargs


@pytest.mark.asyncio
async def test_a_schematized_step_asks_for_its_schema():
    recorder = _Recorder(content=json.dumps({"refined_problem_statement": "x"}))

    await _service(recorder).call("refine_problem_statement", _MESSAGES)

    response_format = recorder.calls[0][2]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "refine_problem_statement"
    assert (
        response_format["json_schema"]["schema"]
        is STEP_SCHEMAS["refine_problem_statement"]
    )


@pytest.mark.asyncio
async def test_a_step_with_no_schema_asks_for_none():
    recorder = _Recorder()

    await _service(recorder).call("validate_and_classify_inputs", _MESSAGES)

    assert "response_format" not in recorder.calls[0][2]


@pytest.mark.asyncio
async def test_the_agents_keyless_fixture_travels_with_the_call():
    """Keyless mode is the gateway's business now, but the FIXTURE is
    this agent's: the platform carries no content for somebody else's
    agent (L13). It rides as librerun.stub_reply, which the gateway
    honours only when the resolved provider is the stub."""
    recorder = _Recorder()

    await _service(recorder).call("refine_problem_statement", _MESSAGES)

    reply = recorder.calls[0][2]["librerun"]["stub_reply"]
    assert "refined_problem_statement" in reply
    assert "stub-llm fixture" in json.dumps(reply)


@pytest.mark.asyncio
async def test_a_step_with_no_fixture_sends_no_stub_reply():
    recorder = _Recorder()

    await _service(recorder).call("no_such_step_has_a_fixture", _MESSAGES)

    assert "librerun" not in recorder.calls[0][2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        'Here is the analysis:\n```\n{"a": 1}\n```\nhope that helps',
        '{"a": 1,}',
    ],
    ids=["raw", "fenced", "preamble and commentary", "trailing comma"],
)
async def test_the_reply_is_parsed_out_of_whatever_the_model_wrapped_it_in(content):
    parsed = await _service(_Recorder(content=content)).call("step", _MESSAGES)

    assert parsed == {"a": 1}


@pytest.mark.asyncio
async def test_a_reply_with_no_content_is_an_error_not_a_silent_none():
    class _ToolCallOnly(_Recorder):
        async def complete(self, step, messages, **kwargs):
            return {
                "model": "gpt-4o",
                "choices": [
                    {"message": {"role": "assistant", "content": None}},
                ],
            }

    with pytest.raises(ValueError, match="no content"):
        await _service(_ToolCallOnly()).call("step", _MESSAGES)


@pytest.mark.asyncio
async def test_what_answered_becomes_the_step_config_the_steps_read():
    """The normalizers attribute a drift report to a provider and a model.
    With an admin editing models at request time, "what answered" is a
    better answer than "what we asked for"."""
    service = _service(_Recorder(model="gpt-4o-mini"))
    # Before any call, the manifest's declared default stands.
    assert service.get_step_config("refine_problem_statement")["model"] == (
        "claude-sonnet-4-6"
    )

    await service.call("refine_problem_statement", _MESSAGES)

    assert service.get_step_config("refine_problem_statement")["model"] == "gpt-4o-mini"


def test_a_step_that_calls_no_model_raises_key_error():
    """The orchestrator tells an LLM step from a search step by exactly
    this, and the two search steps left the manifest's llm.steps when the
    pipeline block moved there."""
    service = _service(_Recorder())

    with pytest.raises(KeyError):
        service.get_step_config("search_internal_kb")
    with pytest.raises(KeyError):
        service.get_step_config("search_public_resources")


def test_the_manifest_declares_every_step_the_service_can_call():
    """A schema for a step the manifest does not declare would be a step
    the gateway refuses — caught here rather than at the first run."""
    declared = LLMService.declared_steps()

    assert set(STEP_SCHEMAS) <= set(declared)
    for step in declared.values():
        assert step.get("provider") and step.get("model")


@pytest.mark.asyncio
async def test_a_gateway_refusal_is_logged_and_re_raised(caplog):
    class _Refusing(_Recorder):
        async def complete(self, step, messages, **kwargs):
            from app.services.gateway_client import GatewayError

            raise GatewayError(400, "unknown_step", "no such step")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(Exception) as exc:
            await _service(_Refusing()).call("step", _MESSAGES)

    assert "unknown_step" in str(exc.value)
    assert "llm_call_failed" in caplog.text


# ---------------------------------------------------------------------------
# One service per invocation (blueprint S4a, Codex P1)
# ---------------------------------------------------------------------------


class _TokenCapability:
    """A granted ``llm`` capability carrying one run's token, as the real
    one does from S4a on."""

    def __init__(self, token: str):
        self.token = token
        self.seen: list[str] = []

    def stub_mode(self) -> bool:
        return False

    async def complete(self, step: str, messages: list[dict], **kwargs) -> dict:
        self.seen.append(step)
        return {
            "id": "chatcmpl-1",
            "model": f"model-for-{self.token}",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": '{"ok": true}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


@pytest.mark.asyncio
async def test_two_overlapping_runs_keep_their_own_capability():
    """The service was process-wide with its capability rebound per run,
    on the premise that the capability was stateless. From S4a it
    carries the invocation's RUN TOKEN — the run's identity at the
    gateway — so the second binding stole the first run's remaining
    calls: spend and telemetry on the wrong run and the wrong tenant,
    and that tenant's model overrides applied (Codex P1).

    Interleaved on purpose: both services exist before either calls.
    """
    from agents.vita_v1 import llm_service

    first_cap = _TokenCapability("run-A")
    second_cap = _TokenCapability("run-B")

    first = llm_service.for_run(first_cap)
    second = llm_service.for_run(second_cap)

    assert first is not second

    await second.call("validate_and_classify_inputs", [{"role": "user", "content": "b"}])
    await first.call("validate_and_classify_inputs", [{"role": "user", "content": "a"}])

    assert first.llm_capability is first_cap
    assert second.llm_capability is second_cap
    assert first_cap.seen == ["validate_and_classify_inputs"]
    assert second_cap.seen == ["validate_and_classify_inputs"]


@pytest.mark.asyncio
async def test_the_model_that_answered_is_not_shared_between_runs():
    """``_used`` was on the shared instance too, so one run's step list
    could report the model another run's call returned."""
    from agents.vita_v1 import llm_service

    first = llm_service.for_run(_TokenCapability("run-A"))
    second = llm_service.for_run(_TokenCapability("run-B"))

    await first.call("validate_and_classify_inputs", [{"role": "user", "content": "a"}])
    await second.call("validate_and_classify_inputs", [{"role": "user", "content": "b"}])

    assert first.get_step_config("validate_and_classify_inputs")["model"] == (
        "model-for-run-A"
    )
    assert second.get_step_config("validate_and_classify_inputs")["model"] == (
        "model-for-run-B"
    )


def test_the_declared_steps_stay_process_wide():
    """What is genuinely shared stays shared: the manifest's declaration
    is read once, on a class-level cache nothing run-scoped reaches."""
    from agents.vita_v1 import llm_service

    first = llm_service.for_run(_TokenCapability("run-A"))
    second = llm_service.for_run(_TokenCapability("run-B"))

    assert first.declared_steps() is second.declared_steps()
    assert "validate_and_classify_inputs" in first.declared_steps()
