# The LibreRun agent SDK — the one control surface

`librerun-agent` (`sdk/python/librerun-agent`) turns one async handler
into a Run Contract v1 agent, and it is **the one documented control
surface** for what an agent needs from the platform (blueprint S4,
decision L27): every control function — start, resume, status,
progress, gates, configuration, capabilities, logs — is a `RunContext`
member or a Run Contract event. This page maps each function to the
member or event that delivers it. `docs/authoring/Container_Agents.md`
is the ten-minute walkthrough; `docs/authoring/Run_Contract_v1.md` is the wire
contract the SDK implements, for agents in other languages.

```python
from librerun_agent import serve, RunContext

async def handler(ctx: RunContext) -> dict:
    ctx.progress("running", step="gather", label="Gathering context")
    hits = await ctx.capabilities.kb_search(ctx.input["question"], top_k=3)
    clean = await ctx.pii.redact(ctx.input["question"])          # the intake pipeline, on demand
    answer = await ctx.llm.text("analyze", clean)                # a STEP you declared, never a model
    await ctx.capabilities.audit_log("kb_queried", {"hits": len(hits)})
    return {"answer": answer, "sources": [h["url"] for h in hits]}

app = serve(handler)   # ASGI: /healthz, /v1/runs, /v1/runs/{id}/events, /v1/runs/{id}/output
```

Install it from a LibreRun checkout — it is not published to PyPI, and
a package of that name there is not the project's:
`pip install "./sdk/python/librerun-agent[uvicorn,otel]"`. The core needs
nothing; `uvicorn` serves the app (`librerun_agent.run(app, port=8090)`
or any ASGI server); `otel` exports traces and logs to the chassis
relay.

## The control surface

| Function | Delivered by | Notes |
|---|---|---|
| **Start** — one invocation of one phase | the chassis's `POST /v1/runs`; the SDK calls `handler(ctx)` | one call per phase per run; `ctx.phase` names it; the handler runs in its own context (see *Logs*) |
| **Resume** — the phase after an approval | the next `POST /v1/runs` with `ctx.prior_output` set | the gate is the chassis's: the agent never sees it; a human approved `prior_output` and this phase continues from it |
| **Re-run** — the user edited the parked output | `ctx.rerun` is `True`, `ctx.user_edits` carries the edit | repeat the phase with the edit as the meaningful new input |
| **Status** — running, completed, failed | the handler's return (`completed`), an exception (`failed`), the deadline | the return value is the phase output, a JSON object; a non-dict return fails the invocation |
| **Progress** — what the run page shows | `ctx.progress(status, step=..., label=..., detail=...)` | the Run Contract `progress` event; `status` is `running` / `completed` / `failed`; the step id is an identifier the chassis checks, the detail is redacted at ingestion |
| **Gates** — human approval between phases | the manifest (`phases[].approval`) | nothing in the handler: the chassis parks the run and calls the next phase later |
| **Deadline** | `ctx.deadline` (UTC datetime) / `ctx.seconds_left` | the manifest phase's `deadline_seconds` under the platform ceiling; the chassis fails the phase when it passes, the SDK cancels the handler at the same moment |
| **Configuration** | `await ctx.config.steps()`, `await ctx.config.step(step_id)`, `await ctx.config.settings()` | your declared `llm.steps[]` with **this tenant's** admin overrides applied, and your declared `settings[]` with **this tenant's** values, read live through the run-scoped `config_get` MCP tool, which needs no grant — so it shows an edit the manifest baked into your image knows nothing about. A step row is `{step_id, label, provider, model, temperature, max_tokens, timeout_seconds, overridden}`; read it to *report* configuration, never to pick a model yourself (the gateway resolves the same values at request time). `settings()` is a `{key: value}` mapping with every key your manifest declares, a tenant that never changed one reading its default, and `{}` if you declare none (K5a) |
| **Capabilities** | `ctx.capabilities.kb_search(query, top_k)`, `.run_store_get(key)`, `.run_store_set(key, value)`, `.audit_log(action_type, detail)` | the run-scoped MCP tools at `run.mcp.url`, under the manifest's grants (`kb`, `run_store`, `audit`); `CapabilityNotGranted` (-32002) when the grant is missing; `PiiRefused` (-32003) when the walk refuses a key, a name or a number — the message names the argument and the path, never the value; `CapabilityUnreachable` (-32000) when the endpoint could not be reached at all, which is transport rather than an answer. **All four are `CapabilityError`**, so one `except CapabilityError` covers every way a capability call can fail — including a refused connection, a DNS failure, a timeout, or a reply that is not JSON, none of which used to leave through this client's own exception |
| **Tool secrets** | `await ctx.secrets.get(name)` | a key of your own for a service you call — a search API, a vector store — declared by name in the manifest's `secrets[]` and valued per tenant on the agent page's Secrets tab: **this tenant's** value, else every tenant's default, over the run-scoped `secret_get` MCP tool, which needs no grant (K8a, K8b). The chassis answers from those two rows only, never from its own environment, so a value your container keeps in its own environment is a fallback you read yourself. `SecretNotSet` (-32006) is a declared name nobody has set: read it as "no key" and carry on without the service. `SecretNotDeclared` (-32005) is a name your manifest does not declare, a bug. **Both are `CapabilityError`**. The value is this invocation's alone: hand it to the tool that needs it and never put it in the output, a progress detail or a log line — the chassis scrubs every value it delivered from what the run persists, as a backstop rather than a licence |
| **PII on demand** | `await ctx.pii.redact(text)` | the intake pipeline's redaction of `text`, under the `pii` grant (the MCP `redact` tool). `PiiUnavailable` (-32004) means the detector could not run and the chassis refused rather than hand back half-redacted text: stop, do not retry with different text |
| **Model calls** | `await ctx.llm.complete(step_id, messages, **kwargs)`, `await ctx.llm.text(step_id, prompt)`, or `ctx.llm.client()` / `ctx.llm.step(step_id)` when a framework makes the call | the LibreRun gateway, under the `llm` grant, carrying the invocation's **run token** — which the SDK holds, so there is nothing to configure per call. You name a declared step, never a model, and your agent holds no provider key: the gateway resolves provider, model, temperature, token limit and timeout from this tenant's configuration at request time. `LlmError` carries the gateway's own `code` and `status`. See *Model calls* below |
| **Logs** | `ctx.log(message, level="info")`; `print()`; the `logging` module | `ctx.log` is the Run Contract `log` event, re-emitted into the chassis's structured stream and redacted at ingestion. `print()` and `logging` lines emitted inside an invocation — on its task, in a thread it started, in an executor job it submitted — reach the platform as log records of **that invocation** (OTLP through the relay when export is configured, `log` events otherwise); a write with no invocation has no owner and is dropped, and `docker logs` on the container is empty by construction |
| **Traces** | the `otel` extra; `OTEL_EXPORTER_OTLP_ENDPOINT` | the handler runs inside a span whose parent is the chassis phase span (the incoming `traceparent`), so every span the agent's own instrumentation opens joins the run's one tree; export goes to the chassis relay, one request per run token, and a span the SDK does not own is dropped |
| **Identity** | `ctx.run_id`, `ctx.invocation_id`, `ctx.tenant_id`, `ctx.traceparent`, `ctx.tracestate` | read-only; the relay stamps `agent.id`, `run.id`, `tenant.id` on exported telemetry from the token, never from the agent |

## Model calls

Everything you need to write one is here; `docs/authoring/LLM_Gateway.md`
is the full reference for the rest of the door — every request parameter,
every refusal code, the trace, keyless mode.

**Your agent holds no provider key and names no model.** It names a
**step** it declared, and the LibreRun gateway — a separate service,
because a credential a process can read is a credential every agent in
that process has — resolves that step to a provider, a model, a
temperature, a token limit and a timeout from the tenant's admin
configuration at request time (L25, D13). Changing a model is an edit in
the admin UI, with nothing rebuilt and nothing restarted.

**1. Declare the step** in `agent.yaml`, with the defaults you ship:

```yaml
capabilities: [llm]          # without this grant every call is refused
llm:
  redact_outbound: true      # the default: every string the model reads is redacted
  steps:
    - id: analyze
      label: Analysis
      provider: openai       # every field but `id` is optional
      model: gpt-4o
      temperature: 0.0
      max_tokens: 2000
      timeout_seconds: 60
```

Declare a step with no `provider` and no `model` and the admin chooses
them: the page shows the step as needing one, and calls to it refuse
with `step_not_configured` until it has one. An id the manifest does not
declare is refused too (`400 unknown_step`) — an invented one would have
no provider, model or limits anyone chose.

**2. Call it** from the handler:

```python
answer = await ctx.llm.text("analyze", "Summarise this incident.")

# …or the full OpenAI response, for tools and structured output:
response = await ctx.llm.complete(
    "analyze",
    [{"role": "user", "content": prompt}],
    response_format={"type": "json_schema",
                     "json_schema": {"name": "analysis", "schema": SCHEMA}},
)
text = response["choices"][0]["message"]["content"]
resolved = response["librerun"]   # {"provider", "model", "step_id"} — what ANSWERED
```

Read `response["librerun"]` when you need to record which model actually
answered — an audit row, a report footer, a drift report. Your manifest's
declared default is the wrong answer for exactly the tenants who
overrode it. (Streamed replies carry no such key: an extra key in those
chunks would break the format.)

`complete` takes no `timeout`, and passing one raises rather than being
ignored: the step's timeout is the tenant admin's to set, and a shorter
client timeout would only close this socket while the gateway goes on
running the provider call and billing it. The call is already bounded by
what is left of the invocation's deadline.

An in-process (`python-package`) agent makes the same call through its
granted capability — `await ctx.capabilities.llm.complete("analyze",
messages)` — and the run token is already in it.

**When a framework makes the call instead of you.** LlamaIndex, the
Vercel AI SDK and `openai` all build their own HTTP client, and all they
will take is a base URL, a key, some headers and a model name.
`ctx.llm.client()` returns those first three for **this invocation**, and
`ctx.llm.step(step_id)` returns the one model string the gateway parses
as a step id:

```python
client = ctx.llm.client()          # .base_url, .api_key, .default_headers
llm = OpenAILike(                  # llama-index-llms-openai-like
    model=ctx.llm.step("analyze"), # "librerun/analyze" — a STEP, not a model
    api_base=client.base_url,
    api_key=client.api_key,        # your LibreRun agent key, never a provider's
    default_headers=client.default_headers,   # carries X-LibreRun-Run-Token
    is_chat_model=True,
)
```

Build it **inside the handler**, once per invocation. `default_headers`
carries this invocation's run token, so a client built at import time
carries the first run's token into every later run: on a multi-tenant
deployment the gateway then resolves the wrong tenant's configuration,
and once that token expires it refuses the call outright. The returned
object is frozen, so a framework cannot retarget it at a provider. The
two shipped examples are
`backend/agents/_examples/llamaindex_summarize/agent.py` and, for
TypeScript with no SDK at all,
`backend/agents/_examples/vercel_ai_answer_ts/server.ts`.

**3. Point the container at the gateway.** The SDK reads
`LIBRERUN_GATEWAY_URL`, or the `OPENAI_BASE_URL` a framework-shaped
deployment already sets (it strips the `/v1`), so both spellings resolve
to one base. The compose fragment sets both — `agents.compose.yaml`'s
echo service is the shape to copy:

```yaml
environment:
  LIBRERUN_GATEWAY_URL: http://gateway:8090
  OPENAI_BASE_URL: http://gateway:8090/v1     # for a framework that only speaks OpenAI
  OPENAI_API_KEY: ${LIBRERUN_AGENT_KEY_MY_AGENT_V1:?agent key not provisioned}
```

Neither of those is a provider key. `OPENAI_API_KEY` holds your
**LibreRun agent key**: `<ID>` is your agent id upper-cased with every
character outside `[A-Z0-9]` replaced by `_`, and the key is provisioned
before `up` (`scripts/demo.sh` writes one per bundled agent) because
compose expands the variable while no LibreRun service is running.

**The two credentials, and which one you are using.** A **run token** is
minted per invocation and names this run, its tenant and its agent — it
is the same bearer the chassis sent on `POST /v1/runs`, and `ctx.llm`
sends it as `X-LibreRun-Run-Token` on every call, which is why the SDK
path needs no key from you at all. An **agent key** names the agent and
nothing else, so it authenticates `GET /v1/models` alone: one container
serves every tenant, so the key cannot say whose data a call is about or
which run pays for it. A model call therefore needs the run token — in
**every** mode, single-tenant demo included; a request carrying only the
key is `401 run_token_required`. A framework inside your container that
builds its own OpenAI client presents the agent key as its API key and
must add the
run token header itself, per invocation — which is what `ctx.llm.client()`
below hands you. `docs/authoring/LLM_Gateway.md` §2 has the same client
written out by hand, for a container serving the contract without this
SDK.

**When it refuses.** `from librerun_agent import LlmError` — it carries
the gateway's own `code` and `status`, so a handler branches on the code
and a log line names it; the message names a path, never a value. The
ones an author meets first are
`unknown_step`, `step_not_configured`, `llm_not_granted` (the manifest
has no `llm`), `pii_in_identifier` (personal data somewhere the gateway
may not rewrite — a tool name, a schema key) and `parameter_not_allowed`
(a request field that is neither model input nor the step's). The
step's own fields — `model`, `temperature`, `max_tokens` and `timeout` —
are *ignored* rather than refused if a framework sends them, because the
admin's values are the ones that apply.

**Keyless.** With `LIBRERUN_STUB_LLM=true` on the gateway, `stub` is the
provider for every step: your code does not change, and the span, the
redaction and the cost are all still produced. A structured-output agent
works keyless with no fixture at all — the gateway synthesises an
instance of your `response_format` schema — and every generated string
says it is a fixture.

## What the SDK does for you

- **Token binding.** The chassis mints a bearer per invocation; the SDK
  binds it to the invocation on `POST /v1/runs` and refuses the events
  and output requests without it (`401`), and every MCP call and relay
  export carries it.
- **The events stream.** Events are buffered from the first emit, so a
  stream attached late (or re-attached) replays them; a keep-alive
  comment goes out every 15 s while the handler works; exactly one
  terminal event ends the stream; the output endpoint answers `200`
  after `completed`, `409` after `failed`, `404` before either.
- **Context.** Each invocation runs in its own `contextvars` context.
  `threading.Thread.start` carries the caller's context into the thread
  (the `asyncio.to_thread` technique), so a `print()` from a worker the
  handler started lands under the invocation; executor workers carry no
  ambient context — instead the whole work item runs in the submitter's
  context, callbacks included, which also covers `asyncio`'s default
  executor. Two overlapping invocations never mix.
- **Export partitioned per token.** With the `otel` extra and
  `OTEL_EXPORTER_OTLP_ENDPOINT` set (compose: the chassis relay,
  `http://backend:8000/api/v1/_o/otlp`, no credential — the token
  travels with each invocation), the exporters group every batch by
  trace id and send one request per token with only that trace's spans
  and log records; a flush precedes `completed`; the token map outlives
  the invocation by a short grace, then a late record is dropped.
- **Deadline.** The handler is cancelled at `deadline_seconds` and the
  invocation fails with the deadline named, so a stuck handler never
  outlives what the chassis will accept.

Switches: `serve(handler, name=..., capture_stdio=..., otel_endpoint=...)`.
`name` is the `service.name` of exported telemetry (default
`OTEL_SERVICE_NAME`). `capture_stdio` (default on,
`LIBRERUN_AGENT_CAPTURE_STDIO=0` to turn off) is the stdout/stderr
capture and the thread-context plumbing; turn it off for a local
development server whose prints you want to see on your terminal.

## Testing your agent

`librerun_agent.testing.serve_in_thread(app)` serves the app on an
ephemeral port under uvicorn and returns its URL; the chassis's
container battery (`backend/adapter_kit/run_contract.py`) drives that
URL through the contract and asserts schema-valid output, progress,
`completed`, and that the container's spans carry the chassis trace id.
`.github/workflows/container-battery.yml` runs it against the reference
echo agent's image.

## Other languages, and L27

The SDK implements Run Contract v1 over HTTP+SSE with MCP for
capabilities — that is the control plane (decision L27), and any
language can implement it in four handlers
(`docs/authoring/Run_Contract_v1.md`; a TypeScript reference server showing the
same shape lands in S5, the `@librerun/agent` package in v1.1). No
gRPC transport exists: L27 retired it, and a gRPC *binding* of the same
contract is a v1.2 candidate only if the transport, not the functions,
is asked for — the functions above are delivered in full today.
