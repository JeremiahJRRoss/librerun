"""Calling the provider, through LiteLLM (D9, L24).

LiteLLM is used as a **library**, not as its proxy server: one code path
covers every provider the frameworks L11 expects, and its cost table is
where ``librerun.cost_usd`` comes from. What LiteLLM is emphatically NOT
allowed to do here is observe: its OpenTelemetry callback is never
registered and ``turn_off_message_logging`` is set at import, so no
second span and no log line of its own carries a completion. The gateway
writes the one LLM span itself, with content that has been through the
walker (``gateway/telemetry.py``); a library recording the raw reply
beside it would undo that silently.

Provider credentials are read from this process's environment — or, since
K7, from a key pasted in the admin UI that ``gateway/provider_store.py``
holds — and passed explicitly per call rather than left for LiteLLM to
discover, so the set of credentials in play is the set this module names.

**What reaches LiteLLM is an allowlist, not a denylist.** This is the
last line before the gateway attaches a provider credential, and
``acompletion(**kwargs)`` accepts far more than a model request:
``api_base``, ``base_url``, ``api_key``, ``api_version``,
``custom_llm_provider``, ``extra_headers``, vertex and bedrock routing,
and more. Forwarding "everything the caller sent except a few keys" put
those in the caller's hands, so an agent could name its own endpoint and
the gateway would post the tenant's content AND its real provider key to
it — the one disclosure the whole split exists to prevent (Codex P1,
confirmed against the running code before it was fixed). The redaction
walk is not a second line here: it looks for personal data, not for
routing, and it refused such a URL only when the hostname happened to
trip a recognizer — and not at all with ``llm.redact_outbound: false``.

So: a parameter is model input, or it is the step's, or the request is
refused naming it. Nothing is forwarded unexamined, which is the rule
the redaction module already follows for the same reason.
"""
from __future__ import annotations

import asyncio
import time as _time
from contextlib import asynccontextmanager, contextmanager

import structlog

from app import secret_files
from gateway import errors, provider_store, stub_provider
from gateway.config import settings
from gateway.steps import STUB_PROVIDER, ResolvedStep

logger = structlog.get_logger(__name__)

# The OpenAI chat-completions fields the gateway forwards: model input,
# sampling, and the shape of the reply. Every one of these changes what
# the model is asked or how it answers, and none of them changes WHERE
# the request goes or WHAT credential goes with it.
#
# Deliberately absent, beyond the transport and credential arguments:
# ``store`` and ``service_tier``, which are the platform's relationship
# with the provider rather than the agent's — an agent turning on
# provider-side retention of a completion is the sort of decision the
# admin page exists for, and until there is one the answer is no.
CHAT_PARAMS = frozenset(
    {
        "messages",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        # The legacy spellings, supported for the same reason the
        # redaction walk handles them: clients still send them.
        "functions",
        "function_call",
        "response_format",
        "n",
        "stop",
        "stream",
        "stream_options",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "seed",
        "modalities",
        "audio",
        "prediction",
        "reasoning_effort",
        "metadata",
        "user",
    }
)

EMBEDDING_PARAMS = frozenset({"input", "encoding_format", "dimensions", "user"})

# The step's, under every spelling, so a caller cannot route around the
# admin's choice (L25). ``max_completion_tokens`` is the reason this is a
# SET rather than a list of five names: it is the same budget as
# ``max_tokens`` for the reasoning families, and stripping only one of
# them let a caller send an arbitrary limit under the other while
# ``drop_params`` discarded the configured one (Codex P1). LiteLLM
# translates the step's ``max_tokens`` to whichever spelling the target
# model takes, so setting one is setting both.
STEP_OWNED_PARAMS = frozenset(
    {
        "model",
        "temperature",
        "max_tokens",
        "max_completion_tokens",
        "timeout",
    }
)

# The platform's own request extension (today: an agent's keyless
# fixture). Stripped so no provider ever sees it, whatever it carries.
PLATFORM_PARAMS = frozenset({"librerun"})

# provider -> the setting that holds its key.
_PROVIDER_KEYS = {
    "openai": "OPENAI_API_KEY",
    "azure": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GOOGLE_AI_API_KEY",
    "google": "GOOGLE_AI_API_KEY",
    "vertex_ai": "GOOGLE_AI_API_KEY",
}

_configured = False


def _litellm():
    """Import and quieten LiteLLM, once.

    Deliberately lazy: the stub path must work in a process that never
    imports it, so keyless mode has no provider library in its way and a
    test of the redaction rules does not pay for a heavyweight import.
    """
    global _configured
    import litellm

    if not _configured:
        # Never let the library record message content. The gateway's own
        # span carries content, walked; anything LiteLLM wrote beside it
        # would be the raw reply, outside the walk.
        litellm.turn_off_message_logging = True
        litellm.success_callback = []
        litellm.failure_callback = []
        litellm.callbacks = []
        litellm.drop_params = True
        litellm.suppress_debug_info = True
        _configured = True
    return litellm


def credential_for(provider: str | None) -> str | None:
    name = _PROVIDER_KEYS.get((provider or "").lower())
    if name is None:
        return None
    # K7 (L33): a key pasted in the admin UI, adopted by the refresher,
    # serves before the environment's — runtime first, as a settings row
    # outranks its .env default everywhere else (L29). Synchronous: the
    # refresher keeps it in memory, so the call path never waits on a
    # table.
    stored = provider_store.runtime_key(provider)
    if stored:
        return stored
    # ``SecretStr`` since K2: revealed HERE, at the one point the value
    # is about to be attached to an outbound call, and never bound to a
    # plain attribute on the settings model where a dump could print it.
    return secret_files.reveal(getattr(settings, name, "")) or None


def attach_route(kwargs: dict, step: ResolvedStep) -> dict:
    """Where the call goes and what it goes with — one function.

    The credential and the endpoint are the deployment's, never the
    caller's (``forwarded`` refuses both from a request), and they are
    attached in exactly one place because chat and embeddings had
    diverged: the chat path applied ``OPENAI_BASE_URL`` and the
    embeddings path attached only the key, so an operator pointing the
    gateway at a compatible endpoint had their chat calls routed there
    and their ``kb_embed`` query text sent to OpenAI's default endpoint
    instead — the opposite of what setting that variable asks for
    (Codex P1). Two call sites doing "the same thing" is how that
    happens; now there is one.
    """
    key = credential_for(step.provider)
    if key:
        kwargs["api_key"] = key
    if (step.provider or "").lower() == "openai" and settings.OPENAI_BASE_URL:
        kwargs["api_base"] = settings.OPENAI_BASE_URL
    return kwargs


def is_stub(step: ResolvedStep) -> bool:
    return (step.provider or "").lower() == STUB_PROVIDER


# Statuses a provider returns that mean "this request, as sent, will
# never work": retrying them burns the deadline and reports an outage
# for what is a configuration or a request problem.
_PASSED_THROUGH_STATUSES = frozenset({400, 401, 403, 404, 408, 409, 413, 422, 429})


def _scrubbed(text: str) -> str:
    """The provider's message with this process's own secrets removed.

    The message is worth forwarding — it is how an agent author learns
    that a parameter is unsupported for the model the admin chose — but
    it is written by the provider and the library, and LiteLLM's error
    text has been known to echo the arguments it was called with. The
    one secret this process holds that the caller must never see is the
    provider key, and we know exactly what it is, so it is removed by
    value rather than hoped about.
    """
    out = text or ""
    secrets = [
        secret_files.reveal(getattr(settings, name, "")).strip()
        for name in set(_PROVIDER_KEYS.values())
    ]
    # And every key pasted in the admin UI (K7): the one attached to this
    # call may be a stored one, which the settings model never saw.
    secrets += [value.strip() for value in provider_store.runtime_values()]
    for secret in secrets:
        if secret and secret in out:
            out = out.replace(secret, "[REDACTED_PROVIDER_KEY]")
    return out


@contextmanager
def _translating(step: ResolvedStep):
    """Give a provider's refusal back as a refusal, not as a 500.

    Nothing translated LiteLLM's exceptions, so a provider 400 for an
    unsupported parameter or a 401 for a bad deployment credential came
    out of FastAPI as a generic 500 — which the chassis's client reads as
    retryable, so a deterministic failure was retried with backoff and
    then reported as a gateway outage, with the provider's actual
    complaint nowhere (Codex P2). A status the provider chose is a fact;
    passing it through is what lets the retry rule tell "try again" from
    "this will never work".
    """
    try:
        yield
    except errors.GatewayError:
        raise
    except Exception as exc:  # noqa: BLE001 — every provider failure, one door
        status = getattr(exc, "status_code", None)
        kind = type(exc).__name__
        message = _scrubbed(str(exc))
        logger.warning(
            "llm_provider_error",
            provider=step.provider,
            step_id=step.step_id,
            status=status,
            error_type=kind,
            error=message,
        )
        if isinstance(status, int) and status in _PASSED_THROUGH_STATUSES:
            raise errors.GatewayError(
                status,
                "provider_refused",
                f"the provider refused this call ({kind}): {message}",
                error_type="invalid_request_error",
            ) from None
        # Anything else — a connection that never landed, a 5xx, a
        # library error with no status — is the retryable class, and
        # says so rather than arriving as an unexplained 500.
        raise errors.GatewayError(
            502,
            "provider_unavailable",
            f"the provider call failed ({kind}): {message}",
            error_type="api_error",
        ) from None


def forwarded(body: dict, allowed: frozenset) -> dict:
    """The caller's parameters that are model input, and only those.

    A key the step owns or the platform added is dropped silently: the
    step's value is about to replace it, and the caller sending one is
    ordinary — a framework fills in ``model`` and ``temperature`` because
    that is what its API takes. A key that is NEITHER is refused, naming
    itself, rather than passed to a function that would happily accept it
    as routing or as a credential.

    Refusing rather than dropping is the same choice the redaction walk
    makes at the same boundary. Dropping would mean an agent's parameter
    silently stopping at the gateway with the call still succeeding —
    which reads as the parameter having no effect on the model, and is
    the failure mode that is hardest to find.
    """
    out = {}
    for key, value in body.items():
        if key in allowed:
            out[key] = value
        elif key in STEP_OWNED_PARAMS or key in PLATFORM_PARAMS:
            continue
        else:
            raise errors.bad_request(
                "parameter_not_allowed",
                f"{key!r} is not a parameter the gateway forwards. It "
                f"accepts the model-input fields of the OpenAI request; "
                f"the model, the limits and the timeout are the step's "
                f"(set them on the agent's configuration page), and the "
                f"endpoint and the credential are the platform's and "
                f"cannot be set per request.",
                param=key,
            )
    return out


@asynccontextmanager
async def _within(seconds_left):
    """A wall-clock scope the GATEWAY owns, around the whole operation.

    ``bounded_timeout`` hands LiteLLM a number, and LiteLLM applies it to
    the request it makes — not to iterating a stream, and not to anything
    the library does around the call. So a stream could be iterated long
    after the invocation ended, with nothing in this process stopping it
    (Codex P1). This is the scope that does.

    A refusal inside it surfaces as the provider-unavailable class from
    ``_translating``, which is the honest reading: the call did not
    finish, and this process is why.
    """
    if seconds_left is None:
        yield
        return
    remaining = float(seconds_left)
    if remaining <= 0:
        # Refuse, do not floor.
        #
        # This used to be ``max(1.0, ...)``, on the reasoning that zero is
        # a ``ValueError`` at the client and a traceback about arguments
        # is a poor refusal. True, and the wrong remedy: it granted a
        # spent invocation another full second, in which a fast call
        # could be launched, billed and COMPLETE — after the very
        # deadline this scope exists to enforce (Codex P1). The answer to
        # "zero is not a valid timeout" is not to invent a timeout; it is
        # not to make the call.
        raise errors.GatewayError(
            504,
            "invocation_deadline",
            "the invocation's deadline has already passed; the gateway "
            "refused to start this call rather than spend past it",
            error_type="api_error",
        )
    try:
        async with asyncio.timeout(remaining):
            yield
    except TimeoutError as exc:
        raise errors.GatewayError(
            504,
            "invocation_deadline",
            "the invocation's deadline passed while this call was in "
            "flight; the gateway stopped it rather than keep spending",
            error_type="api_error",
        ) from exc


def bounded_timeout(step_timeout, seconds_left) -> float | None:
    """The provider timeout: the step's, but never outliving the run.

    The step timeout is the admin's policy for ONE call. The run token's
    remaining lifetime is what the invocation has left overall. A call
    starting with four seconds of invocation left and a 180-second step
    timeout — or a step declaring none at all — would otherwise run for
    the full 180 seconds, or until the provider decided to stop.

    Nothing upstream can prevent that. The backend's transport ceiling
    closes a *client socket*; it does not cancel this FastAPI task, and
    the runner's ``asyncio.timeout`` is in another process entirely. This
    is the last place that can bound the spend, so it does (Codex P1).

    It is decision 62 one layer out: there the MCP handler was a separate
    task the runner could not cancel; here the gateway is a separate
    PROCESS, and the same reasoning applies with more force.
    """
    if seconds_left is None:
        return step_timeout
    # No floor here either. An exhausted budget is refused by ``_within``
    # before any provider call is made; a budget that is merely SMALL is
    # passed through honestly, because a call with a tenth of a second
    # left should fail in a tenth of a second.
    remaining = max(0.0, float(seconds_left))
    if step_timeout is None:
        return remaining
    return min(float(step_timeout), remaining)


def _call_kwargs(body: dict, step: ResolvedStep, seconds_left=None) -> dict:
    """The request LiteLLM is given.

    ``forwarded`` runs here AND at the top of each entry point, and that
    is deliberate rather than redundant. This is the last line before
    ``attach_route`` puts the gateway's real provider credential in the
    kwargs, so the allowlist belongs here; the entry points validate too
    because the stub branch returns before reaching this function — see
    the note there.

    The step's resolved values win over whatever the caller sent: the
    model, the limits and the timeout are the admin's choice (L25), and
    a caller that set its own would otherwise route around them. The
    endpoint and the credential are set here and nowhere else, which is
    what makes them not the caller's — see the module docstring.
    """
    kwargs = forwarded(body, CHAT_PARAMS)
    kwargs["model"] = step.target
    if step.temperature is not None:
        kwargs["temperature"] = step.temperature
    if step.max_tokens is not None:
        # One spelling, set last. LiteLLM translates it to whatever the
        # target model calls its output budget, and the caller's own
        # spellings are gone by here.
        kwargs["max_tokens"] = step.max_tokens
    timeout = bounded_timeout(step.timeout_seconds, seconds_left)
    if timeout is not None:
        kwargs["timeout"] = timeout
    attach_route(kwargs, step)
    return kwargs


async def _bounded_sync(seconds_left, produce):
    """The stub's work, under the invocation's deadline — for real.

    Round 19 put `async with _within(seconds_left)` around these calls
    and that was half a fix. `asyncio.timeout` can only deliver
    cancellation at an `await`, and `stub_provider.complete` is
    synchronous from end to end: a 0.05s scope around a 0.4s block
    returns after 0.4s, measured. So a budget already spent was refused
    (the scope's own entry check) and a budget that ran out DURING
    synthesis was not, while the credentialled path is interrupted
    mid-call (Codex P2).

    Nothing here can interrupt synchronous work — offloading it to a
    thread would only move the same problem — so the deadline is
    enforced where it can be: the elapsed time is measured across the
    call and an overrun is refused afterwards. The answer is discarded,
    which is the point: a credentialled call cancelled at its deadline
    also produces no answer, and parity between the two modes is the
    whole reason this exists.
    """
    started = _time.monotonic()
    async with _within(seconds_left):
        produced = produce()
    if seconds_left is not None and _time.monotonic() - started > float(seconds_left):
        raise errors.GatewayError(
            504,
            "invocation_deadline",
            "the invocation's deadline passed while this call was being "
            "answered; the answer is discarded rather than returned late",
            error_type="api_error",
        )
    return produced


async def complete(
    body: dict, step: ResolvedStep, *, scenario: str | None, seconds_left=None
) -> dict:
    """One non-streaming chat completion, as an OpenAI-shaped dict."""
    # Validated BEFORE the provider is chosen, stub included.
    # ``forwarded`` is what refuses a routing or credential field —
    # ``api_base``, ``api_key`` — and it lived only inside
    # ``_call_kwargs``, which the stub branch below returns before
    # reaching. So keyless mode accepted a request a real provider would
    # refuse with ``parameter_not_allowed``, and an agent developed
    # against the demo broke on its first credentialled call (Codex P2).
    # Keyless mode exists to prove the wiring end to end; a check it
    # skips is wiring it does not prove.
    #
    # It runs again inside ``_call_kwargs``. Not redundant: that is the
    # last line before the gateway's own credential is attached, and a
    # boundary that decides where tenant content and a provider key are
    # sent is worth holding in both places.
    forwarded(body, CHAT_PARAMS)
    # The deadline applies in keyless mode too. I exempted the stub in
    # §12 decision 68 on the grounds that it neither spends nor
    # overruns — true, and not the whole reason the scope exists. A
    # spent invocation is REFUSED against a real provider and ANSWERED
    # by the stub, so a deadline-sensitive flow passes the demo and CI
    # and fails the moment credentials are enabled (Codex P2).
    #
    # That is decision 72 applied to decision 68, by the reviewer, one
    # round later: an exemption is a claim about one thing the branch
    # skips, not about the branch.
    if is_stub(step):
        return await _bounded_sync(
            seconds_left,
            lambda: stub_provider.complete(
                body, model=step.model or STUB_PROVIDER, scenario=scenario
            ),
        )
    litellm = _litellm()
    kwargs = _call_kwargs(body, step, seconds_left)
    kwargs.pop("stream", None)
    async with _within(seconds_left):
        with _translating(step):
            response = await litellm.acompletion(**kwargs)
    return response.model_dump() if hasattr(response, "model_dump") else dict(response)


async def open_stream(
    body: dict, step: ResolvedStep, *, scenario: str | None, seconds_left=None,
    remaining=None,
):
    """Start the streaming call; return an async iterator of chunk dicts.

    Split from the iteration deliberately. ``StreamingResponse`` emits
    the HTTP response start before it pulls the first item, so a refusal
    raised while the call is being SET UP — a 400 for a parameter the
    model does not take, a 401 for a bad deployment credential — would
    reach the caller after a 200 was already committed, as a stream that
    ends before its first event with the translated status nowhere
    (Codex P2). Everything that can be refused happens in this function,
    which the route awaits before it returns a response.

    Priming the whole generator instead would have done it too, and
    would have been wrong: the span is opened inside the response's
    task, and pulling its first item from the request's task detaches an
    OTel context token in a task that did not create it.
    """
    # Validated BEFORE the provider is chosen, stub included.
    # ``forwarded`` is what refuses a routing or credential field —
    # ``api_base``, ``api_key`` — and it lived only inside
    # ``_call_kwargs``, which the stub branch below returns before
    # reaching. So keyless mode accepted a request a real provider would
    # refuse with ``parameter_not_allowed``, and an agent developed
    # against the demo broke on its first credentialled call (Codex P2).
    # Keyless mode exists to prove the wiring end to end; a check it
    # skips is wiring it does not prove.
    #
    # It runs again inside ``_call_kwargs``. Not redundant: that is the
    # last line before the gateway's own credential is attached, and a
    # boundary that decides where tenant content and a provider key are
    # sent is worth holding in both places.
    forwarded(body, CHAT_PARAMS)
    # The deadline applies in keyless mode too. I exempted the stub in
    # §12 decision 68 on the grounds that it neither spends nor
    # overruns — true, and not the whole reason the scope exists. A
    # spent invocation is REFUSED against a real provider and ANSWERED
    # by the stub, so a deadline-sensitive flow passes the demo and CI
    # and fails the moment credentials are enabled (Codex P2).
    #
    # That is decision 72 applied to decision 68, by the reviewer, one
    # round later: an exemption is a claim about one thing the branch
    # skips, not about the branch.
    if is_stub(step):
        def _produce():
            completion = stub_provider.complete(
                body, model=step.model or STUB_PROVIDER, scenario=scenario
            )
            return list(stub_provider.stream(completion))

        chunks = await _bounded_sync(seconds_left, _produce)

        async def _from_stub():
            # Bounded like the provider's iteration, and for the same
            # reason: what keyless mode does and what a credentialled
            # call does have to be the same shape, or the demo proves
            # something the deployment will not do.
            async with _within(remaining() if remaining else None):
                for chunk in chunks:
                    yield chunk

        return _from_stub()

    litellm = _litellm()
    kwargs = _call_kwargs(body, step, seconds_left)
    kwargs["stream"] = True
    kwargs["stream_options"] = {"include_usage": True}
    async with _within(seconds_left):
        with _translating(step):
            iterator = await litellm.acompletion(**kwargs)

    async def _from_provider():
        # The translation covers the iteration too: a provider can drop
        # the connection after the first chunk. That one cannot become a
        # status — the response has left — but it should still be one
        # refusal rather than an unexplained traceback.
        #
        # And so does the invocation's clock. LiteLLM's ``timeout``
        # applies to the REQUEST, not to reading chunks off it, so a
        # stream could be iterated long after the phase ended with
        # nothing in this process stopping it (Codex P1). The budget is
        # re-read here rather than reused from setup, because setting up
        # took time too. Entered and exited in the response's task —
        # this body first runs when the response pulls a chunk — which
        # is what keeps the scope from being detached in a task that did
        # not create it.
        async with _within(remaining() if remaining else None):
            with _translating(step):
                async for chunk in iterator:
                    yield chunk.model_dump() if hasattr(chunk, "model_dump") else dict(chunk)

    return _from_provider()


async def embed(body: dict, step: ResolvedStep, *, seconds_left=None) -> dict:
    # Validated BEFORE the provider is chosen, stub included.
    # ``forwarded`` is what refuses a routing or credential field —
    # ``api_base``, ``api_key`` — and it lived only inside
    # ``_call_kwargs``, which the stub branch below returns before
    # reaching. So keyless mode accepted a request a real provider would
    # refuse with ``parameter_not_allowed``, and an agent developed
    # against the demo broke on its first credentialled call (Codex P2).
    # Keyless mode exists to prove the wiring end to end; a check it
    # skips is wiring it does not prove.
    #
    # It runs again inside ``_call_kwargs``. Not redundant: that is the
    # last line before the gateway's own credential is attached, and a
    # boundary that decides where tenant content and a provider key are
    # sent is worth holding in both places.
    kwargs = forwarded(body, EMBEDDING_PARAMS)
    # The deadline applies in keyless mode too. I exempted the stub in
    # §12 decision 68 on the grounds that it neither spends nor
    # overruns — true, and not the whole reason the scope exists. A
    # spent invocation is REFUSED against a real provider and ANSWERED
    # by the stub, so a deadline-sensitive flow passes the demo and CI
    # and fails the moment credentials are enabled (Codex P2).
    #
    # That is decision 72 applied to decision 68, by the reviewer, one
    # round later: an exemption is a claim about one thing the branch
    # skips, not about the branch.
    if is_stub(step):
        return await _bounded_sync(
            seconds_left,
            lambda: stub_provider.embed(body, model=step.model or STUB_PROVIDER),
        )
    litellm = _litellm()
    kwargs["model"] = step.target
    timeout = bounded_timeout(step.timeout_seconds, seconds_left)
    if timeout is not None:
        kwargs["timeout"] = timeout
    attach_route(kwargs, step)
    async with _within(seconds_left):
        with _translating(step):
            response = await litellm.aembedding(**kwargs)
    return response.model_dump() if hasattr(response, "model_dump") else dict(response)


def cost_usd(response: dict, step: ResolvedStep) -> float | None:
    """What this call cost, from LiteLLM's own table.

    A **stub** call is costed too, against the model it stands in for:
    keyless mode is meant to exercise the real telemetry, and a span
    whose cost attribute only ever appears with a provider account would
    be a span nobody could test.
    """
    usage = response.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    if not prompt and not completion:
        return None
    model = step.model or step.target
    if not model:
        return None
    try:
        litellm = _litellm()
        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model, prompt_tokens=prompt, completion_tokens=completion
        )
        return float(prompt_cost) + float(completion_cost)
    except Exception as exc:  # noqa: BLE001 — an unpriced model is not an error
        logger.info(
            "llm_cost_unavailable",
            model=model,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None
