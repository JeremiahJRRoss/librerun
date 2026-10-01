"""The model call end to end: step, redaction, stub egress, span, cost."""
from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from gateway.auth import RUN_TOKEN_HEADER, SCENARIO_HEADER, STEP_HEADER

FIXTURE = "pii-test@example.com"


@pytest.fixture
def spans(monkeypatch):
    """Collect this process's spans without touching the export path the
    gateway installs — the walking exporter has its own tests."""
    from gateway import telemetry

    telemetry.init()
    exporter = InMemorySpanExporter()
    telemetry._provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    exporter.clear()


@pytest.fixture(autouse=True)
def keyless(monkeypatch):
    from gateway import steps

    monkeypatch.setattr(steps.settings, "LIBRERUN_STUB_LLM", True)


def _finished(exporter):
    return [s for s in exporter.get_finished_spans() if s.name.startswith(("chat ", "embeddings "))]


@pytest.mark.asyncio
async def test_a_keyless_chat_call_completes_with_model_tokens_and_cost(
    client, run_token, spans
):
    token = await run_token()

    response = await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think"},
        json={"model": "librerun/think", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["choices"][0]["message"]["content"]
    assert body["usage"]["total_tokens"] > 0

    span = _finished(spans)[-1]
    attributes = dict(span.attributes)
    # The step's configured model, not the caller's string: the admin
    # picks the model (L25), and keyless mode still says which one it
    # stood in for.
    assert attributes["gen_ai.request.model"] == "gpt-4o"
    assert attributes["gen_ai.usage.input_tokens"] > 0
    assert attributes["gen_ai.usage.output_tokens"] > 0
    assert attributes["librerun.cost_usd"] > 0
    assert attributes["librerun.scope"] == "run"
    assert attributes["librerun.step_id"] == "think"


@pytest.mark.asyncio
async def test_a_model_call_without_a_run_token_is_refused(client, installed):
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {installed.key}"},
        json={"model": "librerun/think", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "run_token_required"


@pytest.mark.asyncio
async def test_an_undeclared_step_never_reaches_a_provider(client, run_token, spans):
    token = await run_token()

    response = await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "invented"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_step"
    assert _finished(spans) == []


@pytest.mark.asyncio
async def test_the_prompt_reaches_the_provider_redacted(client, run_token, spans):
    """The stub records what it was asked for, so the provider-side view
    is the reply's own echo of the request: assert on what the stub was
    given by asking it to repeat the input hash — and, more directly, on
    the span, whose content is walked either way."""
    token = await run_token()

    response = await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think"},
        json={
            "model": "librerun/think",
            "messages": [{"role": "user", "content": f"write to {FIXTURE}"}],
        },
    )

    assert response.status_code == 200
    span = _finished(spans)[-1]
    captured = json.loads(span.attributes["gen_ai.input.messages"])
    assert FIXTURE not in json.dumps(captured)
    assert "[REDACTED_EMAIL_ADDRESS_1]" in json.dumps(captured)


@pytest.mark.asyncio
async def test_a_reply_carrying_personal_data_is_returned_but_not_recorded(
    client, run_token, spans
):
    """The gateway does not rewrite a completion — that would change the
    agent's structured output under its feet — but content reaches
    telemetry only through the walker, so the span must show
    placeholders where the reply has an address and a phone number."""
    token = await run_token()

    response = await client.post(
        "/v1/chat/completions",
        headers={
            RUN_TOKEN_HEADER: token,
            STEP_HEADER: "think",
            SCENARIO_HEADER: "pii-in-reply",
        },
        json={"model": "librerun/think", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    reply = response.json()["choices"][0]["message"]
    assert "oncall@example.com" in reply["content"]  # the agent gets the truth

    span = _finished(spans)[-1]
    recorded = span.attributes["gen_ai.output.messages"]
    assert "oncall@example.com" not in recorded
    assert "415-555-0132" not in recorded
    assert "REDACTED_EMAIL_ADDRESS" in recorded
    assert "REDACTED_PHONE" in recorded
    # …including inside the tool call's arguments, which is the position
    # a completion most easily smuggles a value through.
    arguments = json.loads(recorded)[0]["tool_calls"][0]["arguments"]
    assert "oncall@example.com" not in arguments
    assert "415-555-0132" not in arguments


@pytest.mark.asyncio
async def test_with_no_content_capture_the_span_carries_no_content(
    client, run_token, spans, monkeypatch
):
    from gateway import telemetry

    # The real environment outranks the settings value, so set it there —
    # which is also how an operator turns this off.
    monkeypatch.setenv(telemetry._CONTENT_VAR, "NO_CONTENT")
    token = await run_token()

    await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think"},
        json={"model": "librerun/think", "messages": [{"role": "user", "content": "hi"}]},
    )

    span = _finished(spans)[-1]
    assert "gen_ai.input.messages" not in span.attributes
    assert "gen_ai.output.messages" not in span.attributes
    # …but the cost and the token counts are not content and stay.
    assert "librerun.cost_usd" in span.attributes


@pytest.mark.asyncio
async def test_a_foreign_traceparent_is_refused(client, run_token, spans):
    token = await run_token()
    foreign = "00-" + ("a" * 32) + "-" + ("b" * 16) + "-01"

    response = await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think", "traceparent": foreign},
        json={"model": "librerun/think", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "trace_mismatch"


@pytest.mark.asyncio
async def test_an_unusable_traceparent_still_lands_in_the_run_tree(
    client, run_token, spans
):
    """A header the propagator cannot parse extracts to an EMPTY context,
    and a span started from an empty context is a new root — in a trace
    of its own, invisible from the run. The call is not worth failing
    over a bad header; landing the span outside the tree is.
    """
    token = await run_token()
    from gateway import auth

    expected = (await auth.read_run_token(token))["trace_id"]

    response = await client.post(
        "/v1/chat/completions",
        headers={
            RUN_TOKEN_HEADER: token,
            STEP_HEADER: "think",
            "traceparent": "not-a-traceparent",
        },
        json={"model": "librerun/think", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    span = _finished(spans)[-1]
    assert format(span.context.trace_id, "032x") == expected
    assert span.parent is not None


@pytest.mark.asyncio
async def test_a_traceparent_with_the_right_trace_and_a_dead_span_id_lands_in_the_tree(
    client, run_token, spans
):
    """The subtler half: the trace id matches, so the mismatch check is
    happy, and the span id is all zeros — which W3C defines as invalid.
    The context comes back empty exactly as above.
    """
    token = await run_token()
    from gateway import auth

    expected = (await auth.read_run_token(token))["trace_id"]

    response = await client.post(
        "/v1/chat/completions",
        headers={
            RUN_TOKEN_HEADER: token,
            STEP_HEADER: "think",
            "traceparent": f"00-{expected}-{'0' * 16}-01",
        },
        json={"model": "librerun/think", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    span = _finished(spans)[-1]
    assert format(span.context.trace_id, "032x") == expected
    assert span.parent is not None


@pytest.mark.asyncio
async def test_an_unusable_traceparent_naming_another_trace_is_still_refused(
    client, run_token, spans
):
    """Falling back must not become a way around the refusal: a header
    too broken to parse can still name somebody else's trace in its
    trace-id field, and that is the case the 403 is for."""
    token = await run_token()

    response = await client.post(
        "/v1/chat/completions",
        headers={
            RUN_TOKEN_HEADER: token,
            STEP_HEADER: "think",
            "traceparent": "00-" + ("a" * 32),
        },
        json={"model": "librerun/think", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "trace_mismatch"


def test_a_run_token_with_an_unusable_phase_pointer_gives_a_root_span():
    """The last resort. Nothing the caller sends can reach this — it is
    the chassis's own write being unreadable — so it must not raise."""
    from gateway import telemetry
    from gateway.auth import Principal

    principal = Principal(
        agent_id="probe-v1",
        snapshot=None,
        run_id="r",
        tenant_id="t",
        trace_id=None,
        traceparent="00-not-hex",
    )

    assert telemetry.parent_context(principal, None) is None
    assert telemetry.parent_context(principal, "also-not-a-traceparent") is None


@pytest.mark.asyncio
async def test_a_call_with_no_traceparent_lands_under_the_phase_span(
    client, run_token, spans
):
    """A framework that drops the header still lands in the one tree: the
    runner wrote the invocation's phase span into the token record."""
    token = await run_token()
    from gateway import auth

    record = await auth.read_run_token(token)
    expected = record["trace_id"]

    await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think"},
        json={"model": "librerun/think", "messages": [{"role": "user", "content": "hi"}]},
    )

    span = _finished(spans)[-1]
    assert format(span.context.trace_id, "032x") == expected
    assert span.parent is not None


@pytest.mark.asyncio
async def test_a_streaming_refusal_arrives_as_its_status(client, run_token, spans, monkeypatch):
    """``StreamingResponse`` emits the response start before it pulls the
    first item, so a provider refusal during setup used to arrive after a
    200 was already committed: the caller saw a stream that ended before
    its first event, and the translated status was nowhere."""
    from gateway import egress

    async def _refuse(body, step, *, scenario, seconds_left=None, remaining=None):
        raise egress.errors.GatewayError(
            401, "provider_refused", "the provider rejected the key",
        )

    monkeypatch.setattr(egress, "open_stream", _refuse)
    token = await run_token()

    response = await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think"},
        json={
            "model": "librerun/think",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "provider_refused"


@pytest.mark.asyncio
async def test_the_response_is_committed_only_after_the_call_is_accepted(monkeypatch):
    """The invariant behind the fix, stated directly: nothing returns a
    response object until ``open_stream`` has come back. A refusal raised
    while the call is being set up therefore leaves the route with no
    response to have already committed, which is what lets it be a
    status; once the provider has accepted, it cannot be one, and the
    route must not hold the stream back pretending otherwise."""
    from fastapi.responses import StreamingResponse

    from gateway import egress, errors, main, steps
    from gateway.auth import Principal

    order: list[str] = []
    step = steps.ResolvedStep(step_id="think", provider="stub", model="stub")
    principal = Principal(agent_id="probe", snapshot=None, run_id=None, tenant_id=None)

    async def _accepts(body, step_, *, scenario, seconds_left=None, remaining=None):
        order.append("provider accepted")

        async def chunks():
            yield {"choices": [{"index": 0, "delta": {"content": "hi"}}]}

        return chunks()

    monkeypatch.setattr(egress, "open_stream", _accepts)

    response = await main._stream_completion(
        principal, step, {"messages": []}, None, scenario=None, traceparent=None
    )
    order.append("response returned")

    assert isinstance(response, StreamingResponse)
    assert order == ["provider accepted", "response returned"]

    async def _refuses(body, step_, *, scenario, seconds_left=None, remaining=None):
        raise errors.bad_request("provider_refused", "no")

    monkeypatch.setattr(egress, "open_stream", _refuses)

    with pytest.raises(errors.GatewayError):
        await main._stream_completion(
            principal, step, {"messages": []}, None, scenario=None, traceparent=None
        )


@pytest.mark.asyncio
async def test_a_streaming_call_with_a_foreign_traceparent_never_reaches_the_provider(
    client, run_token, spans, monkeypatch
):
    """Moving the provider call ahead of the span moved it ahead of the
    trace check too, so a header naming somebody else's trace was
    accepted and billed and the 403 arrived after a 200 (Codex P2)."""
    from gateway import egress

    reached = []

    async def _record(body, step, *, scenario, seconds_left=None, remaining=None):
        reached.append(body)

        async def chunks():
            yield {"choices": [{"index": 0, "delta": {"content": "hi"}}]}

        return chunks()

    monkeypatch.setattr(egress, "open_stream", _record)
    token = await run_token()
    foreign = "00-" + ("a" * 32) + "-" + ("b" * 16) + "-01"

    response = await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think", "traceparent": foreign},
        json={
            "model": "librerun/think",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "trace_mismatch"
    assert reached == [], "the prompt reached the provider before the refusal"


@pytest.mark.asyncio
async def test_a_streaming_setup_failure_still_leaves_a_span(
    client, run_token, spans, monkeypatch
):
    """The non-streaming path records a refusal on its span. With the
    span opened inside the generator, a refusal during setup exited
    before the generator ever ran and left the run's trace with no sign
    that a model call was attempted at all."""
    from gateway import egress, errors

    async def _refuse(body, step, *, scenario, seconds_left=None, remaining=None):
        raise errors.GatewayError(401, "provider_refused", "bad deployment key")

    monkeypatch.setattr(egress, "open_stream", _refuse)
    token = await run_token()

    response = await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think"},
        json={
            "model": "librerun/think",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert response.status_code == 401
    span = _finished(spans)[-1]
    assert span.attributes["librerun.refusal_code"] == "provider_refused"
    assert span.status.status_code.name == "ERROR"
    # …and it is in the run's tree, not a root of its own.
    assert span.parent is not None


@pytest.mark.asyncio
async def test_a_streaming_call_streams_and_still_costs(client, run_token, spans):
    token = await run_token()

    response = await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think"},
        json={
            "model": "librerun/think",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    chunks = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert chunks and chunks[0]["object"] == "chat.completion.chunk"
    assert response.text.rstrip().endswith("data: [DONE]")

    span = _finished(spans)[-1]
    assert span.attributes["librerun.cost_usd"] > 0
    assert span.attributes["gen_ai.usage.output_tokens"] > 0


@pytest.mark.asyncio
async def test_a_forced_tool_call_comes_back_as_one(client, run_token):
    token = await run_token()
    tool = {
        "type": "function",
        "function": {
            "name": "lookup",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        },
    }

    response = await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think"},
        json={
            "model": "librerun/think",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [tool],
            "tool_choice": {"type": "function", "function": {"name": "lookup"}},
        },
    )

    assert response.status_code == 200
    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "lookup"
    assert "q" in json.loads(call["function"]["arguments"])


@pytest.mark.asyncio
async def test_a_structured_request_gets_a_schema_shaped_reply(client, run_token):
    """Keyless mode has to work for agents that parse structured output,
    and it does it without any agent-specific fixture in the platform:
    the stub synthesises an instance of the schema it was handed."""
    token = await run_token()
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "signals": {"type": "array", "items": {"type": "string"}},
            "score": {"type": "number", "minimum": 1},
        },
        "required": ["summary", "signals", "score"],
    }

    response = await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think"},
        json={
            "model": "librerun/think",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": schema},
            },
        },
    )

    assert response.status_code == 200
    parsed = json.loads(response.json()["choices"][0]["message"]["content"])
    assert set(parsed) == {"summary", "signals", "score"}
    assert isinstance(parsed["signals"], list) and parsed["signals"]
    assert parsed["score"] >= 1
    assert "stub-llm fixture" in parsed["summary"]


@pytest.mark.asyncio
async def test_the_model_used_is_written_onto_the_run(client, run_token, redis_client):
    """D13's other half: the run page can show which model answered.

    In a key of its own. The progress entry has one writer and a closed
    shape, and the orchestrator writes the step's terminal status AFTER
    the call — so a model merged into that entry was overwritten by the
    next write every time, which is how a whole keyless run finished
    with no step naming a model.
    """
    from app.services.run_boundary import progress_key
    from gateway import auth, progress

    redis, written = redis_client
    token = await run_token()
    record = await auth.read_run_token(token)
    run_id = record["run_id"]
    key = progress.step_models_key(run_id)
    written.append(key)
    written.append(progress_key(run_id))

    # The orchestrator's write, as it really lands: whole, closed, and
    # after the call.
    await redis.hset(
        progress_key(run_id),
        "think",
        json.dumps({"status": "running", "duration_ms": None, "detail": None}),
    )

    await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think"},
        json={"model": "librerun/think", "messages": [{"role": "user", "content": "hi"}]},
    )

    await redis.hset(
        progress_key(run_id),
        "think",
        json.dumps({"status": "complete", "duration_ms": 12, "detail": None}),
    )

    assert await redis.hget(key, "think") == "gpt-4o"
    # And the read path joins them, which is what the run page shows.
    from app.schemas.run import StepProgress

    entry = json.loads(await redis.hget(progress_key(run_id), "think"))
    models = await redis.hgetall(key)
    step = StepProgress(step_id="think", model=models.get("think"), **entry)
    assert step.status == "complete" and step.model == "gpt-4o"


@pytest.mark.asyncio
async def test_kb_embed_runs_keyless_and_is_attributed_to_the_run(
    client, run_token, installed, manifest, spans
):
    await installed.reinstall(
        manifest(installed.id, capabilities=["kb"], llm={"steps": []})
    )
    token = await run_token(grants=["kb"])

    response = await client.post(
        "/v1/embeddings",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "kb_embed"},
        json={"model": "librerun/kb_embed", "input": ["a query", "another"]},
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["data"]) == 2
    span = _finished(spans)[-1]
    assert span.attributes["librerun.step_id"] == "kb_embed"
    assert span.attributes["librerun.run_id"]
    assert "librerun.cost_usd" in span.attributes


@pytest.mark.asyncio
async def test_kb_embed_bounds_hold_with_outbound_redaction_off(
    client, run_token, installed, manifest
):
    """The platform step's bounds are a SPEND boundary, so the agent's
    privacy switch must not move them.

    With `llm.redact_outbound: false` the redactor stops refusing
    token-id input, and the bounds check filtered non-strings out before
    counting — so a tokenized query was measured as no input at all and
    an agent could spend without limit through the platform's own step
    (Codex P2).
    """
    await installed.reinstall(
        manifest(
            installed.id,
            capabilities=["kb"],
            llm={"steps": [], "redact_outbound": False},
        )
    )
    token = await run_token(grants=["kb"])

    tokenized = await client.post(
        "/v1/embeddings",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "kb_embed"},
        json={"input": [[1] * 100_000 for _ in range(50)]},
    )

    assert tokenized.status_code == 400, tokenized.text
    assert tokenized.json()["error"]["code"] == "kb_embed_bounds"
    assert "not text" in tokenized.json()["error"]["message"]

    # And the character bound still applies with the switch off.
    long_query = await client.post(
        "/v1/embeddings",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "kb_embed"},
        json={"input": ["x" * 5000]},
    )
    assert long_query.status_code == 400
    assert long_query.json()["error"]["code"] == "kb_embed_bounds"


@pytest.mark.asyncio
async def test_kb_embed_is_bounded_and_a_kb_only_agent_cannot_chat(
    client, run_token, installed, manifest
):
    await installed.reinstall(
        manifest(installed.id, capabilities=["kb"], llm={"steps": []})
    )
    token = await run_token(grants=["kb"])

    over = await client.post(
        "/v1/embeddings",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "kb_embed"},
        json={"input": ["x"] * 21},
    )
    assert over.status_code == 400
    assert over.json()["error"]["code"] == "kb_embed_bounds"

    chat = await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "kb_embed"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert chat.status_code == 400
    assert chat.json()["error"]["code"] == "unknown_step"

    refused = await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "llm_not_granted"


# ---------------------------------------------------------------------------
# The resolved call is the chassis's, and survives the walk as such
# ---------------------------------------------------------------------------

# A real model name the intake recognizers read as a person's name. The
# platform's own strings keep doing this — S4 found `librerun-backend`
# the same way — so the test names one rather than inventing a string.
PERSONISH_MODEL = "claude-sonnet-4-20250514"


def test_the_walk_would_destroy_a_model_name_that_is_not_stamped():
    """The premise the stamping rests on, asserted rather than assumed.

    If the recognizers ever stop reading this as a person, the exemption
    below stops proving anything and this test says so first.
    """
    from app.services import pii_service

    redacted, _ = pii_service.redact(
        PERSONISH_MODEL, skip_entities=pii_service.BOUNDARY_SKIP_ENTITIES, quiet=True
    )
    assert redacted != PERSONISH_MODEL
    assert "REDACTED" in redacted


def test_the_resolved_model_and_provider_are_stamped_on_the_span():
    """What the chassis resolved is the chassis's, so the export leaves
    it alone: the provider, the model, and the operation. Nothing from
    the request body is — the caller's `model` field is
    `librerun/<step>` and never survives resolution.
    """
    from app.observability import otlp_walk
    from gateway import steps, telemetry
    from gateway.auth import Principal

    telemetry.init()
    step = steps.ResolvedStep(
        step_id="think",
        provider="anthropic",
        model=PERSONISH_MODEL,
        temperature=0.0,
        max_tokens=100,
    )
    principal = Principal(
        agent_id="probe-v1",
        snapshot=None,
        run_id="9a2b7c3d-0000-4000-8000-000000000002",
        tenant_id="3f1c5a1e-0000-4000-8000-000000000001",
    )

    with telemetry.llm_span(
        principal, step, operation="chat", traceparent=None
    ) as span:
        context = span.get_span_context()
        telemetry.record_response(
            span,
            {
                "model": PERSONISH_MODEL,
                "id": "resp-1",
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
            step,
            0.000123,
            operation="chat",
        )

    stamped = otlp_walk.stamps.take_span(context.trace_id, context.span_id)
    assert stamped["gen_ai.request.model"] == PERSONISH_MODEL
    assert stamped["gen_ai.system"] == "anthropic"
    assert stamped["gen_ai.operation.name"] == "chat"
    # The identity pairs recorded when the span opened are still there:
    # the reply's amendment adds, it does not replace.
    assert stamped["librerun.step_id"] == "think"
    assert stamped["librerun.run_id"].endswith("002")
    # The provider echoed the chassis's own string back, so it is vouched
    # for too.
    assert stamped["gen_ai.response.model"] == PERSONISH_MODEL
    # Never the provider's own id.
    assert "gen_ai.response.id" not in stamped
    # And the span's NAME, which the GenAI convention builds from the
    # model — the field the viewer shows first, and CONTENT to the walk.
    from app.observability import otlp_walk as _walk

    assert stamped[_walk.STAMPED_NAME_KEY] == f"chat {PERSONISH_MODEL}"


def test_a_provider_model_the_chassis_did_not_choose_is_not_stamped():
    """A reply naming a different model came from outside this
    deployment. A mangled name in the trace is a smaller price than a
    position where a provider's text is exempt by construction.
    """
    from app.observability import otlp_walk
    from gateway import steps, telemetry
    from gateway.auth import Principal

    telemetry.init()
    step = steps.ResolvedStep(step_id="think", provider="anthropic", model="claude-x")
    principal = Principal(agent_id="probe-v1", snapshot=None, run_id="r", tenant_id="t")

    with telemetry.llm_span(
        principal, step, operation="chat", traceparent=None
    ) as span:
        context = span.get_span_context()
        telemetry.record_response(
            span, {"model": PERSONISH_MODEL, "usage": {}}, step, None, operation="chat"
        )

    stamped = otlp_walk.stamps.take_span(context.trace_id, context.span_id)
    assert stamped["gen_ai.request.model"] == "claude-x"
    assert "gen_ai.response.model" not in stamped


# --------------------------------------------------------------------------
# What the span records for a streamed answer
# --------------------------------------------------------------------------


def _assembled(chunks: list[dict]) -> list[dict]:
    from gateway.main import _StreamAssembly

    assembly = _StreamAssembly()
    for chunk in chunks:
        assembly.add(chunk)
    return assembly.choices()


def test_a_streamed_tool_call_is_recorded_whole():
    """The whole answer of a tool-calling stream is in `tool_calls`
    deltas, so accumulating only `delta.content` recorded an EMPTY
    completion on the span — evidence that the model returned nothing
    (Codex P2)."""
    choices = _assembled(
        [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": '{"q'},
                                }
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '":"acme"}'}}
                            ]
                        },
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )

    assert len(choices) == 1
    call = choices[0]["message"]["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["function"]["name"] == "lookup"
    # The arguments arrive in pieces and are JSON only once joined.
    assert json.loads(call["function"]["arguments"]) == {"q": "acme"}
    assert choices[0]["finish_reason"] == "tool_calls"


def test_two_streamed_choices_stay_two():
    """`n > 1` is allowed, and every choice was merged into a fabricated
    choice 0 with one finish reason."""
    choices = _assembled(
        [
            {
                "choices": [
                    {"index": 0, "delta": {"content": "first"}},
                    {"index": 1, "delta": {"content": "second"}},
                ]
            },
            {
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"},
                    {"index": 1, "delta": {}, "finish_reason": "length"},
                ]
            },
        ]
    )

    assert [c["index"] for c in choices] == [0, 1]
    assert choices[0]["message"]["content"] == "first"
    assert choices[1]["message"]["content"] == "second"
    assert choices[0]["finish_reason"] == "stop"
    assert choices[1]["finish_reason"] == "length"


def test_plain_text_still_joins_in_order():
    choices = _assembled(
        [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "he"}}]},
            {"choices": [{"index": 0, "delta": {"content": "llo"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
    )

    assert choices == [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "hello"},
        }
    ]


def test_a_stream_with_no_choices_still_gives_one():
    """A usage-only final chunk must not leave `choices` empty, which
    every consumer of an OpenAI response assumes is non-empty."""
    choices = _assembled([{"usage": {"prompt_tokens": 1}}])

    assert len(choices) == 1
    assert choices[0]["message"]["content"] is None


def test_a_malformed_chunk_does_not_break_the_assembly():
    """Chunks come from a provider. One that is not the shape we expect
    must not take the stream down — the client already has the bytes."""
    choices = _assembled(
        [
            {"choices": "not a list"},
            {"choices": [None, {"index": 0, "delta": {"content": "ok"}}]},
            {"choices": [{"index": 0, "delta": "not a dict"}]},
        ]
    )

    assert choices[0]["message"]["content"] == "ok"


def test_a_streamed_legacy_function_call_is_recorded_whole():
    """The legacy spelling is forwarded on purpose, so it has to be
    recorded. Streamed, its name arrives once and its arguments in
    pieces, exactly like `tool_calls` (Codex P2)."""
    choices = _assembled(
        [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"function_call": {"name": "lookup", "arguments": '{"q'}},
                    }
                ]
            },
            {
                "choices": [
                    {"index": 0, "delta": {"function_call": {"arguments": '":"acme"}'}}}
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "function_call"}]},
        ]
    )

    call = choices[0]["message"]["function_call"]
    assert call["name"] == "lookup"
    assert json.loads(call["arguments"]) == {"q": "acme"}
    assert choices[0]["message"]["content"] is None
    assert choices[0]["finish_reason"] == "function_call"


def test_a_stream_with_neither_spelling_carries_neither_key():
    choices = _assembled([{"choices": [{"index": 0, "delta": {"content": "hi"}}]}])

    assert "function_call" not in choices[0]["message"]
    assert "tool_calls" not in choices[0]["message"]


def test_a_non_streamed_legacy_function_call_reaches_the_span():
    """The whole answer of a legacy response is in `function_call` with
    `content` null, so reading only `tool_calls` recorded an empty
    choice — the same wrong-evidence failure, one spelling over."""
    from gateway import telemetry

    walked = telemetry.walked_choices(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "function_call",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "function_call": {
                            "name": "notify",
                            "arguments": '{"to":"Dana Whitfield"}',
                        },
                    },
                }
            ]
        }
    )

    call = walked[0]["function_call"]
    assert call["name"] == "notify"
    # …and its arguments went through the walk like every other position
    # the span keeps.
    assert "Dana Whitfield" not in call["arguments"]
    assert "REDACTED" in call["arguments"]


def test_a_non_streamed_response_without_the_legacy_key_has_no_such_entry():
    from gateway import telemetry

    walked = telemetry.walked_choices(
        {"choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}]}
    )

    assert "function_call" not in walked[0]


# --------------------------------------------------------------------------
# What an embeddings span records, and what it must not claim
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_embeddings_span_does_not_claim_the_model_said_nothing(
    client, run_token, installed, manifest, spans
):
    """An embeddings reply has ``data``, not ``choices``. Reading it with
    the chat walker set ``gen_ai.output.messages: []`` on every embeddings
    span — an affirmative statement that the model returned no messages,
    when what it returned was vectors. Evidence that is wrong rather than
    missing, the same failure as the streamed tool call and the legacy
    ``function_call``, a third response shape over.
    """
    await installed.reinstall(
        manifest(installed.id, capabilities=["kb"], llm={"steps": []})
    )
    token = await run_token(grants=["kb"])

    response = await client.post(
        "/v1/embeddings",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "kb_embed"},
        json={"model": "librerun/kb_embed", "input": ["a query", "another"]},
    )
    assert response.status_code == 200, response.text

    span = _finished(spans)[-1]
    assert "gen_ai.output.messages" not in span.attributes, (
        "the embeddings span claims the model returned messages"
    )
    assert not [
        e for e in span.events if "gen_ai.output.messages" in (e.attributes or {})
    ], "the same false claim, as an event"

    # Silence is not the whole answer: what came back still gets recorded,
    # as the shape it has. Counts, not content, so no capture switch.
    assert span.attributes["librerun.embeddings.count"] == 2
    assert span.attributes["librerun.embeddings.dimensions"] > 0


@pytest.mark.asyncio
async def test_a_chat_span_still_records_what_the_model_said(
    client, run_token, installed, manifest, spans
):
    """The other side of the same predicate: narrowing the completion
    walker to chat must not have narrowed it out of chat."""
    await installed.reinstall(manifest(installed.id))
    token = await run_token()

    response = await client.post(
        "/v1/chat/completions",
        headers={RUN_TOKEN_HEADER: token, STEP_HEADER: "think"},
        json={
            "model": "librerun/think",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert response.status_code == 200, response.text

    span = _finished(spans)[-1]
    assert "gen_ai.output.messages" in span.attributes
    assert "librerun.embeddings.count" not in span.attributes


def test_every_operation_the_gateway_opens_a_span_with_is_one_it_can_record():
    """The structural half. ``record_response`` handles operations by
    name and records nothing as content for a name it does not know —
    honest, but it means a new endpoint could be added whose replies the
    span silently never describes. So the two lists are pinned together:
    every ``operation=`` in ``main.py`` must be one ``telemetry``
    declares it records.
    """
    import pathlib
    import re

    from gateway import telemetry

    source = (
        pathlib.Path(telemetry.__file__).resolve().parent / "main.py"
    ).read_text()
    used = set(re.findall(r'operation="([a-z_]+)"', source))

    assert used, "no operations found — the scan is looking at the wrong file"
    unhandled = used - set(telemetry.RECORDED_OPERATIONS)
    assert not unhandled, (
        f"{sorted(unhandled)} open an LLM span but record_response does not "
        f"know the shape of their replies, so those spans describe nothing"
    )
