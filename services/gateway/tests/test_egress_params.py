"""What reaches LiteLLM is an allowlist (blueprint S4a; Codex round 3).

``litellm.acompletion(**kwargs)`` accepts far more than a model request.
Forwarding "everything the caller sent except a few keys" put the
transport and the credential arguments in the caller's hands, and this
is the last line before the gateway attaches its real provider key — so
an agent could name its own endpoint and have the gateway post the
tenant's content and that key to it.

The first two cases are that disclosure, written as the attack. The rest
pin the boundary in both directions: nothing routing gets through, and
nothing that is ordinary model input gets refused.
"""
from __future__ import annotations

import pytest

from gateway import egress, errors
from gateway.steps import ResolvedStep

REAL_KEY = "sk-the-gateways-own-provider-key"
FOREIGN = "https://an-agent-controlled-host.example/v1"


@pytest.fixture(autouse=True)
def provider_key(monkeypatch):
    monkeypatch.setattr(egress.settings, "OPENAI_API_KEY", REAL_KEY)
    monkeypatch.setattr(egress.settings, "OPENAI_BASE_URL", "")


def step(**overrides) -> ResolvedStep:
    values = {
        "step_id": "think",
        "provider": "openai",
        "model": "gpt-4o",
        "max_tokens": 100,
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return ResolvedStep(**values)


def chat(**extra) -> dict:
    return {
        "model": "librerun/think",
        "messages": [{"role": "user", "content": "hello"}],
        **extra,
    }


def refusal(body: dict, step_=None) -> errors.GatewayError:
    with pytest.raises(errors.GatewayError) as exc:
        egress._call_kwargs(body, step_ or step())
    return exc.value


# --------------------------------------------------------------------------
# The disclosure
# --------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["api_base", "base_url"])
def test_a_caller_cannot_choose_the_endpoint(field):
    """The whole reason the gateway is a separate process is that it
    holds the provider credential. A caller naming the endpoint sends
    that credential — and the tenant's content — wherever it likes."""
    error = refusal(chat(**{field: FOREIGN}))

    assert error.status_code == 400
    assert error.code == "parameter_not_allowed"
    assert error.param == field
    # The refusal names the parameter and not its value.
    assert FOREIGN not in error.message


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_key", "sk-somebody-elses"),
        ("api_version", "2024-02-01"),
        ("custom_llm_provider", "openai"),
        ("extra_headers", {"x-forward-to": FOREIGN}),
        ("vertex_project", "a-project"),
        ("vertex_location", "us-central1"),
        ("aws_access_key_id", "AKIA0000000000000000"),
        ("mock_response", "not a model"),
        ("proxy", FOREIGN),
    ],
)
def test_no_transport_or_credential_argument_is_forwarded(field, value):
    """Each of these is a real LiteLLM argument. None is model input."""
    error = refusal(chat(**{field: value}))

    assert error.code == "parameter_not_allowed"
    assert error.param == field


def test_the_credential_is_still_attached_for_an_ordinary_call():
    """The boundary must not have been closed by breaking the call."""
    kwargs = egress._call_kwargs(chat(), step())

    assert kwargs["api_key"] == REAL_KEY
    assert kwargs["model"] == step().target
    assert "api_base" not in kwargs


def test_the_gateways_own_base_url_is_still_set_from_its_settings(monkeypatch):
    """The deployment may point the gateway at a compatible endpoint.
    That is the operator's setting, not the caller's parameter."""
    monkeypatch.setattr(egress.settings, "OPENAI_BASE_URL", "https://ours.internal/v1")

    kwargs = egress._call_kwargs(chat(), step())

    assert kwargs["api_base"] == "https://ours.internal/v1"


def test_an_embedding_call_goes_to_the_same_endpoint(monkeypatch):
    """Chat applied the operator's endpoint and embeddings did not, so
    pointing the gateway at a compatible service routed the chat there
    and sent the knowledge-search query text to OpenAI's own — the
    opposite of what setting the variable asks for (Codex P1)."""
    monkeypatch.setattr(egress.settings, "OPENAI_BASE_URL", "https://ours.internal/v1")
    kwargs = egress.forwarded({"input": ["hi"]}, egress.EMBEDDING_PARAMS)

    egress.attach_route(kwargs, step(step_id="kb_embed", model="text-embedding-3-small"))

    assert kwargs["api_base"] == "https://ours.internal/v1"
    assert kwargs["api_key"] == REAL_KEY


def test_the_two_call_paths_route_identically(monkeypatch):
    """One function decides where a call goes, for both. Two call sites
    doing the same thing is how they came apart."""
    monkeypatch.setattr(egress.settings, "OPENAI_BASE_URL", "https://ours.internal/v1")
    chat_kwargs = egress._call_kwargs(chat(), step())
    embed_kwargs = egress.attach_route(
        egress.forwarded({"input": ["hi"]}, egress.EMBEDDING_PARAMS), step()
    )

    for field in ("api_key", "api_base"):
        assert chat_kwargs[field] == embed_kwargs[field], field


@pytest.mark.parametrize("provider", ["anthropic", "gemini", "vertex_ai"])
def test_a_non_openai_step_gets_no_openai_endpoint(monkeypatch, provider):
    """The variable names OpenAI's protocol, not every provider's."""
    monkeypatch.setattr(egress.settings, "OPENAI_BASE_URL", "https://ours.internal/v1")

    kwargs = egress.attach_route({}, step(provider=provider))

    assert "api_base" not in kwargs


# --------------------------------------------------------------------------
# The output budget, under every spelling
# --------------------------------------------------------------------------


def test_the_callers_max_completion_tokens_does_not_survive():
    """The reasoning families take the output budget under this name.
    Stripping only ``max_tokens`` left the caller's value here while
    ``drop_params`` discarded the admin's — the limit inverted."""
    kwargs = egress._call_kwargs(chat(max_completion_tokens=999_999), step())

    assert "max_completion_tokens" not in kwargs
    assert kwargs["max_tokens"] == 100


def test_the_callers_max_tokens_does_not_survive_either():
    kwargs = egress._call_kwargs(chat(max_tokens=999_999), step())

    assert kwargs["max_tokens"] == 100


def test_both_spellings_at_once_still_leave_only_the_steps_limit():
    kwargs = egress._call_kwargs(
        chat(max_tokens=999_999, max_completion_tokens=999_999), step()
    )

    assert kwargs["max_tokens"] == 100
    assert "max_completion_tokens" not in kwargs


def test_a_step_with_no_limit_sends_none():
    """An agent that declares no budget does not acquire the caller's."""
    kwargs = egress._call_kwargs(
        chat(max_completion_tokens=999_999), step(max_tokens=None)
    )

    assert "max_tokens" not in kwargs
    assert "max_completion_tokens" not in kwargs


@pytest.mark.parametrize("field", ["model", "temperature", "timeout"])
def test_the_steps_own_fields_are_replaced_rather_than_refused(field):
    """A framework fills these in because that is what its API takes.
    Refusing them would refuse every ordinary client."""
    kwargs = egress._call_kwargs(chat(**{field: "whatever"}), step(temperature=0.25))

    assert kwargs["model"] == step().target
    assert kwargs["temperature"] == 0.25
    assert kwargs["timeout"] == 30


def test_the_platforms_own_extension_never_reaches_a_provider():
    kwargs = egress._call_kwargs(chat(librerun={"stub_reply": "x"}), step())

    assert "librerun" not in kwargs


# --------------------------------------------------------------------------
# …and the model input that must still go through
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("tools", [{"type": "function", "function": {"name": "f"}}]),
        ("functions", [{"name": "f"}]),
        ("tool_choice", "auto"),
        ("response_format", {"type": "json_object"}),
        ("stop", ["\n\n"]),
        ("top_p", 0.9),
        ("seed", 7),
        ("logprobs", True),
        ("n", 2),
        ("user", "opaque-handle"),
        ("reasoning_effort", "low"),
        ("metadata", {"note": "ours"}),
    ],
)
def test_model_input_is_forwarded(field, value):
    kwargs = egress._call_kwargs(chat(**{field: value}), step())

    assert kwargs[field] == value


def test_every_allowed_chat_parameter_is_forwarded_by_name():
    """The allowlist and the forwarding must not drift: whatever the set
    says is allowed, the function forwards."""
    body = chat(**{name: "value" for name in egress.CHAT_PARAMS})

    kwargs = egress._call_kwargs(body, step())

    for name in egress.CHAT_PARAMS - egress.STEP_OWNED_PARAMS:
        assert name in kwargs, name


def test_the_allowlist_names_no_transport_argument():
    """A guard on the set itself, so a future addition has to be a
    deliberate one rather than a plausible-looking name."""
    forbidden = {
        "api_key",
        "api_base",
        "base_url",
        "api_version",
        "custom_llm_provider",
        "extra_headers",
        "headers",
        "proxy",
        "client",
        "mock_response",
        "vertex_project",
        "vertex_location",
        "aws_access_key_id",
        "aws_secret_access_key",
    }

    assert not (egress.CHAT_PARAMS & forbidden)
    assert not (egress.EMBEDDING_PARAMS & forbidden)


# --------------------------------------------------------------------------
# Embeddings take the same door
# --------------------------------------------------------------------------


def test_an_embedding_request_is_held_to_the_same_allowlist():
    with pytest.raises(errors.GatewayError) as exc:
        egress.forwarded({"input": ["hi"], "api_base": FOREIGN}, egress.EMBEDDING_PARAMS)

    assert exc.value.code == "parameter_not_allowed"
    assert exc.value.param == "api_base"


def test_an_embedding_request_forwards_its_own_fields():
    kwargs = egress.forwarded(
        {"input": ["hi"], "dimensions": 256, "encoding_format": "float"},
        egress.EMBEDDING_PARAMS,
    )

    assert kwargs == {"input": ["hi"], "dimensions": 256, "encoding_format": "float"}


# --------------------------------------------------------------------------
# A provider's refusal comes back as a refusal
# --------------------------------------------------------------------------


class _ProviderError(Exception):
    """What LiteLLM raises: an exception carrying the provider's status."""

    def __init__(self, message: str, status_code: int | None):
        super().__init__(message)
        self.status_code = status_code


def _raised(exc: Exception) -> errors.GatewayError:
    with pytest.raises(errors.GatewayError) as caught:
        with egress._translating(step()):
            raise exc
    return caught.value


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422, 429])
def test_a_status_the_provider_chose_is_passed_through(status):
    """Untranslated, these arrived as a generic 500 — which the chassis
    client reads as retryable, so a call that will never work was
    retried with backoff and then reported as a gateway outage."""
    error = _raised(_ProviderError("model does not support that", status))

    assert error.status_code == status
    assert error.code == "provider_refused"
    assert "model does not support that" in error.message


@pytest.mark.parametrize("status", [500, 502, 503, None])
def test_a_failure_with_no_actionable_status_is_a_gateway_502(status):
    error = _raised(_ProviderError("upstream fell over", status))

    assert error.status_code == 502
    assert error.code == "provider_unavailable"


def test_the_providers_message_cannot_carry_this_processs_key_back():
    """LiteLLM's error text has echoed the arguments it was called with.
    The one secret this process holds is known by value, so it is removed
    by value rather than hoped about."""
    error = _raised(_ProviderError(f"bad request: api_key={REAL_KEY}", 400))

    assert REAL_KEY not in error.message
    assert "[REDACTED_PROVIDER_KEY]" in error.message


def test_a_gateway_refusal_raised_inside_is_not_reclassified():
    """A refusal of ours passing through the translation stays itself —
    otherwise every 400 the gateway raises would become a 502."""
    original = errors.bad_request("kb_embed_bounds", "too many")

    with pytest.raises(errors.GatewayError) as caught:
        with egress._translating(step()):
            raise original

    assert caught.value is original


# --------------------------------------------------------------------------
# Keyless mode validates too (Codex round 16, P2)
# --------------------------------------------------------------------------
#
# `forwarded` lived only inside `_call_kwargs`, and every stub branch
# returns before reaching it. So `LIBRERUN_STUB_LLM=true` accepted a
# request that a credentialled gateway refuses — an agent built against
# the demo broke on its first real call, and CI could not have caught it
# because CI runs keyless. Keyless mode exists to prove the wiring end to
# end; a check it skips is wiring it does not prove.


def stub_step(**overrides) -> ResolvedStep:
    values = {"step_id": "think", "provider": "stub", "model": "stub"}
    values.update(overrides)
    return ResolvedStep(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["api_base", "api_key", "proxy", "mock_response"])
async def test_the_stub_refuses_what_a_provider_would_refuse(field):
    for call in (
        lambda b: egress.complete(b, stub_step(), scenario=None),
        lambda b: egress.open_stream(b, stub_step(), scenario=None),
    ):
        with pytest.raises(errors.GatewayError) as caught:
            await call(chat(**{field: "anything"}))
        assert caught.value.code == "parameter_not_allowed"
        assert caught.value.param == field


@pytest.mark.asyncio
async def test_the_stub_embeddings_path_refuses_them_too():
    with pytest.raises(errors.GatewayError) as caught:
        await egress.embed({"input": "hello", "api_base": FOREIGN}, stub_step())
    assert caught.value.code == "parameter_not_allowed"


@pytest.mark.asyncio
async def test_an_ordinary_keyless_request_still_works():
    """The refusal must not cost keyless mode its job."""
    answer = await egress.complete(chat(), stub_step(), scenario=None)
    assert answer["choices"]


def test_every_stub_branch_is_behind_the_allowlist():
    """The structural half, because the defect was an ORDERING one and
    any single-path test leaves the other two free to regress.

    Every `if is_stub(step):` in this module must have a `forwarded(`
    call above it inside the same function — that is what the three
    behavioural cases above assert one path at a time, stated once for
    all of them."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(egress))
    checked = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        stub_lines = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "is_stub"
        ]
        if not stub_lines:
            continue
        guard_lines = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "forwarded"
        ]
        for stub_at in stub_lines:
            assert any(g < stub_at for g in guard_lines), (
                f"{fn.name} chooses the stub at line {stub_at} with no "
                f"forwarded() call before it, so a keyless request skips "
                f"the allowlist a credentialled one must pass"
            )
            checked += 1
    assert checked >= 3, f"expected the three egress paths, found {checked}"
