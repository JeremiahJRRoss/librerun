# The LLM gateway — one door to every model

Every model call an agent makes goes through the LibreRun gateway: in
process or in a container, on the SDK or through a framework that only
knows how to talk to OpenAI. It is a separate service on purpose —
egress code inside the backend would keep provider credentials in the
one process every `python-package` agent shares, and a credential an
agent can read is a credential an agent has.

What you get for going through it:

- **No provider key in your agent.** The gateway holds them. You present
  the invocation's run token, or your agent key, or both.
- **The model is the admin's choice, not yours.** You name a *step*; the
  gateway resolves provider, model, temperature, token limit and timeout
  from that tenant's configuration at request time. Changing a model is
  an edit in the UI, with nothing restarted.
- **Outbound redaction.** Every string the model will read is redacted;
  every identifier is checked and the request refused rather than
  rewritten. You cannot accidentally ship a customer's address in a tool
  description built from tenant data.
- **One LLM span, with the cost on it.** Model, token counts and
  `librerun.cost_usd`, inside the run's trace.
- **Keyless mode for free.** `LIBRERUN_STUB_LLM=true` makes `stub` the
  provider for every step. Your code does not change, and the span, the
  redaction and the cost are all still real.
- **An instrument for evaluation and optimization.** The gateway is the
  LLM router every call passes through, so one span per call carries
  the model, the tokens and the cost, and the same step can be pointed
  at another model from the admin UI with nothing restarted — which is
  how a course compares two models on one scenario. The gateway, the
  edge proxy, the cache and the tracing tools are designed to teach
  evaluation; the pages that do so are not written yet
  ([what 1.0 does not do yet](../release/v1.0.0.md#what-10-does-not-do-yet)).

## 1. Declare your steps

A step is one LLM call your agent makes. Declare them in `agent.yaml`,
with the defaults you ship:

```yaml
capabilities: [llm]          # without this, every call is refused
llm:
  redact_outbound: true      # the default; see §7
  steps:
    - id: analyze
      label: Analysis
      provider: openai
      model: gpt-4o
      temperature: 0.0
      max_tokens: 2000
      timeout_seconds: 60
```

Every field but `id` is optional — you may declare a step and leave the
whole model choice to the admin. An id the manifest does not declare is
refused (`400 unknown_step`): an invented one would have no provider,
model or limits anyone chose. `kb_embed` is reserved by the platform.

## 2. Call it

**On the SDK** (`librerun-agent`), from your handler:

```python
answer = await ctx.llm.text("analyze", "Summarise this incident.")

# …or the full OpenAI response, for tools and structured output:
response = await ctx.llm.complete(
    "analyze",
    [{"role": "user", "content": prompt}],
    response_format={"type": "json_schema",
                     "json_schema": {"name": "analysis", "schema": SCHEMA}},
)
```

**In process** (a `python-package` agent), through the granted
capability — the same call, and the run token is already in it:

```python
response = await ctx.capabilities.llm.complete("analyze", messages)
```

**From a framework that reads `OPENAI_BASE_URL`**, with no LibreRun code
at all: point it at the gateway and name the step in the model string.

```python
client = OpenAI(
    base_url=os.environ["OPENAI_BASE_URL"],          # http://gateway:8090/v1
    api_key=os.environ["OPENAI_API_KEY"],            # your LibreRun AGENT KEY
    default_headers={"X-LibreRun-Run-Token": run_token},
)
client.chat.completions.create(model="librerun/analyze", messages=messages)
```

The compose fragment sets `OPENAI_BASE_URL` and `OPENAI_API_KEY` for
you. The run token is per invocation: build the client inside the
handler, not at import.

## 3. The two credentials, and why both

| | names | authenticates |
|---|---|---|
| **Run token** (`X-LibreRun-Run-Token`) | this run, its tenant, its agent | everything |
| **Agent key** (`Authorization: Bearer`) | the agent, and nothing else | `GET /v1/models` alone |

One container serves every tenant, so an agent key cannot say whose data
a call is about or which run pays for it. That is why a model call needs
the run token beside it **in every mode**, single-tenant demo included —
`401 run_token_required` without one. A rule that relaxed on one
deployment shape would be a rule nobody could reason about.

Agent keys are provisioned before `up`, because compose expands
`${LIBRERUN_AGENT_KEY_<ID>}` while no LibreRun service is running:
`scripts/demo.sh` writes one per bundled agent, `librerun init` does the
same for a new one, and the gateway registers what it finds at boot.
`<ID>` is your agent id upper-cased with every character outside
`[A-Z0-9]` replaced by `_` (`my-agent` → `MY_AGENT`), because compose
variable names admit no hyphens.

To rotate one: move the old value to `LIBRERUN_AGENT_KEY_<ID>_PREVIOUS`,
put the new one on the main line, recreate the gateway. Both work until
you remove the `_PREVIOUS` line. A key issued from the admin UI — the
agent page's **Keys** tab, a platform admin's — rotates there instead,
with a grace window of 0 to 720 hours, 24 by default; its value is shown
once and stored nowhere.

### And the third credential you never see: the provider key

Neither of the two above is a provider key, and as an agent author you
will never hold one. The operator puts them in **`gateway.env`** at the
repository root — `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GOOGLE_AI_API_KEY`, one line each, from `gateway.env.example`:

```bash
cp gateway.env.example gateway.env && chmod 600 gateway.env
```

— or a platform admin pastes one in **Admin → Application Settings →
Model providers** (K7, decision L33). The page seals it in the browser
to the gateway's public key, over HTTPS or on `localhost`; the backend
stores a blob it cannot open; the gateway opens it, keeps it under its
own store key, `LIBRERUN_GATEWAY_SECRETS_KEY` in `gateway.env`, and uses
it from the next call, nothing restarted. A key set there wins over
`gateway.env`'s for its provider — `openai` serves the `openai` and
`azure` steps, `anthropic` its own, `google` the `gemini`, `google` and
`vertex_ai` ones — and Clear hands the provider back to `gateway.env`.
Either way the key is the gateway's alone, and your agent's calls do not
change.

The `gateway` service is the only one that loads that file (decision
L28), and `required: false`, so a start without one boots; the demo
writes one holding only the store key. The provider keys are
deliberately **not** in the root `.env`, which compose, the backend
and the frontend build all read: the backend is the single process every
`python-package` agent shares, so a provider credential there would be a
credential every agent in the deployment has. That is the same reason
`agent-keys.env` exists rather than the gateway receiving `.env` whole.

The variable your framework reads as `OPENAI_API_KEY` inside an agent
container is your **agent key**, not a provider key — same spelling,
different credential, which is why the value always starts `lr_agent_`.
A provider key pasted into that variable is refused by the gateway
rather than forwarded anywhere.

## 4. What the trace shows

One span per call, inside the run's tree: `gen_ai.request.model`,
`gen_ai.usage.input_tokens` / `output_tokens`, `librerun.cost_usd`,
`librerun.step_id`, and the prompt and completion when
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` allows it. All four
values of that variable are honoured here: `SPAN_ONLY` writes the span
attribute and no event, `EVENT_ONLY` the event and no attribute,
`SPAN_AND_EVENT` both, `NO_CONTENT` neither. (Backend-side OpenInference
instrumentors cannot make that distinction and treat either partial mode
as capture — see `docs/authoring/Agents_Design.md`.)

Whatever carries the answer is what gets recorded, not `content` alone:
a tool call, a legacy `function_call`, a safety `refusal`, or an audio
reply's transcript. An embeddings span keeps the count and dimensions of
the vectors instead; an audio reply's base64 payload is kept only as its
size, never as bytes.

The content the span keeps is **always** walked, whatever
`llm.redact_outbound` says: that switch governs what the *model* sees,
never what telemetry keeps. The resolved provider, model and operation
are the exception, and not one you can use: they are what the PLATFORM
chose, stamped as chassis-written so the walk leaves them alone — and so
is the span's name, which the convention builds from the model. Without
that, `chat claude-sonnet-4-6` reaches the viewer as
`chat [REDACTED_PERSON_1]`, since the recognizers read a model name as a
person's. Nothing you send is stamped.

The model that answered is also written to `run:{id}:step_models` and
joined into `GET /runs/{id}/progress`, so the run page shows which model
answered each step.

Send a `traceparent` and the span hangs from it — as long as its trace
id is the one your run token was minted with (`403 trace_mismatch`
otherwise: a valid token must not be usable to hang cost telemetry in
somebody else's trace). Send none and it hangs from your invocation's
phase span, so a framework that drops the header still lands in the one
tree.

## 5. Which parameters the gateway forwards

The gateway is an OpenAI-compatible ingress, and what it forwards is an
**allowlist**: the model-input fields of the request — `messages`,
`tools` and `functions`, `tool_choice`, `response_format`, `stop`, `n`,
`top_p`, the penalties, `logit_bias`, `logprobs`, `seed`, `modalities`,
`audio`, `prediction`, `reasoning_effort`, `metadata`, `user`, and the
streaming fields. Each of those that carries text the model reads —
including `prediction` and `metadata` — is walked as content by §7, not
merely checked; a field is on this list because the redactor knows what
is inside it.

Three groups are not yours to send:

- **The step's**: `model`, `temperature`, `max_tokens`,
  `max_completion_tokens` and `timeout`. Send them and they are
  *ignored*, not refused — a framework fills them in because that is
  what its API takes — and the admin's values are used instead (L25).
  Both spellings of the output budget are the step's: they are the same
  limit, and honouring one of them would be a way around it.
- **The platform's**: `librerun`, the request extension your keyless
  fixture travels in. It never reaches a provider.
- **Everything else**, which is refused with `400
  parameter_not_allowed` naming the parameter. That includes every
  LiteLLM transport and credential argument — `api_base`, `base_url`,
  `api_key`, `api_version`, `custom_llm_provider`, `extra_headers`,
  `proxy`, the vertex and bedrock routing — because this is the process
  that holds the provider credential, and a request that could choose
  the endpoint could have that credential sent to it. Where an OpenAI
  step goes is the deployment's `OPENAI_BASE_URL`, which applies to
  chat and embeddings alike. It also includes
  `store` and `service_tier`: provider-side retention of a completion is
  the deployment's decision, not an agent's.

If a field you need is missing from the list, that is a gap to report
rather than something to work around; refusing an unknown parameter is
deliberate, because a parameter silently dropped at the gateway looks
exactly like a parameter the model ignored.

## 6. When the provider says no

A status the provider chose is passed through rather than flattened:
`400 provider_refused` with the provider's own message for a request it
will not accept (an unsupported parameter for the model the admin
chose, say), `401`/`403` for a credential the deployment got wrong,
`429` when you are rate-limited. Anything with no usable status — a
connection that never landed, a provider 5xx — is `502
provider_unavailable`. The distinction matters because the chassis
retries the second class and not the first: a call that can never
succeed should fail once, with the reason, not four times and then as an
outage. The gateway's own provider keys are removed from the forwarded
message.

## 6a. How long your call may take

Three clocks bound one model call, and only two of them are policy:

| Clock | Set by | Enforced by |
| --- | --- | --- |
| Phase deadline | `phases[].deadline_seconds`, under `LIBRERUN_MAX_PHASE_SECONDS` | the chassis, around the whole invocation |
| Step timeout | `llm.steps[].timeout_seconds`, overridable per tenant | the gateway, on the provider call |
| Transport ceiling | nobody — it is derived | the HTTP client, as a backstop |

The **step timeout** is the one you configure and the one that should
normally fire: the gateway hands it to the provider call, so a slow model
is cut off with a status that says so. The **phase deadline** is the
operator's ceiling over the whole invocation; a step timeout larger than
it is moot, because the phase is cancelled first.

A fourth bound sits inside the gateway and is not a policy either: the
provider call is capped at the run token's **remaining lifetime**,
measured when the call is made rather than when the request arrived, so
a call admitted with seconds of invocation left cannot run for the step's
full timeout. The gateway also holds its own wall-clock scope around the
whole operation, stream iteration included, and gives you `504
invocation_deadline` if the invocation runs out mid-call — or if it had
already run out when the call arrived, in which case nothing is sent to
the provider at all. It is the only one of these bounds that can actually
stop a call in flight — the others live in processes that can close a
socket but cannot cancel the gateway's work.

The **transport ceiling** is not a third policy. It exists so a dead
socket cannot hold a worker, and it is derived from the invocation's
remaining budget rather than chosen — so it can never hang up on a call
your step timeout still allows. It was once a flat 120s while the bundled
agent shipped a 180s step, and the result was a call killed mid-flight,
billed, reported as an unreachable gateway, and retried twice.

If the gateway does take the request and never answer, you get `504
gateway_timeout` — not `gateway_unreachable`, which means nothing
answered at all. Both are retryable; only one of them is a reason to go
and look at the gateway.

## 6b. What the reply tells you about itself

The reply is an ordinary OpenAI chat completion, plus one key the
platform adds: `librerun`, the same envelope your keyless fixture
travels in on the way out. On the way back it carries the step as the
gateway **resolved** it:

```json
"librerun": {"provider": "anthropic", "model": "claude-sonnet-4-6", "step_id": "assess_skills"}
```

Read it when you need to record which model actually answered — an audit
row, a report footer, a drift report. The alternative is your manifest's
declared default, which is wrong for exactly the tenants who overrode it.
`provider` is the name the admin chose (`google`), not the egress
library's routing prefix.

Streamed replies do not carry it: they are passed through chunk by chunk
and an extra key in those chunks would break the format.

## 7. Outbound redaction, and how to switch it off

With `llm.redact_outbound: true` (the default), every string in your
request is in one of two classes and there is no third:

- **rewritable — redacted**: message content (string or text parts),
  `messages[].name` (dropped if redaction changes it, since providers
  constrain that field to an identifier pattern), tool-result content,
  tool-call and `function_call` `arguments` (parsed, redacted,
  re-serialised, still valid JSON), `tools[].function.description`,
  every `description` and `title` in a schema, every string leaf under
  `enum`, `const`, `examples` and `default`, and embeddings input.
- **unrewritable — checked, and the request refused**: tool and
  tool-call names, call ids, `tool_choice` and `json_schema` names,
  every object key, and the structural schema keywords the model needs
  verbatim (`pattern`, `required` entries, `format`, `$ref`). Rewriting
  any of them would break the tool-call round trip or the schema, so you
  get `400 pii_in_identifier` naming the **path** — never the value.

Numbers are checked too: a phone number as a JSON number cannot take a
textual placeholder without changing its type, so it refuses the request
(`400 pii_in_structured_value`).

Two things the gateway cannot read are refused rather than forwarded:
**media parts** (`400 binary_not_redactable`) and **token-id embeddings
input** (`400 tokens_not_redactable`). If your agent needs multimodal
input, set `llm.redact_outbound: false`. That is an explicit opt-out the
manifest records and the admin page shows in amber — and it still does
not change what the span keeps.

The top-level `user` field is replaced by an opaque per-tenant value: it
is provider-side metadata rather than model input, so the provider's own
rate limiting keeps working and the field carries nothing about a
person. That replacement happens **whatever `llm.redact_outbound` says**
— it is not a redaction. The switch is yours to set for what the model
reads; `user` is never read by a model, it is the handle an abuse system
files your requests under and the provider keeps, so putting an end
user's address there would register that person with the provider under
their own name. You may opt your agent out of redaction; you may not opt
someone else into that. A request that carries no `user` still gets
none: the field is replaced, never invented.

An assistant message's `refusal` is treated as content, not as
structure: it is prose the model reads, so it is rewritten like
`content` rather than merely checked. Treated as structure it did both
possible wrong things — a name in a refusal reached the provider
verbatim, and an email in one refused the whole request.

**When the redactor itself is down.** Redaction here is the chassis's
own pipeline, and its named-entity stage — the one that finds a person,
a place or an organisation — needs a model that can be missing or can
fault. When it cannot run, the gateway refuses the call with
`pii_detector_unavailable` (503) instead of forwarding a request it
could only half redact; `GET /healthz` carries
`pii_detector: {state, coverage}` so you can tell that case from a
provider outage without reading a log. This applies to a step with
`llm.redact_outbound` **on**: with redaction off for your agent nothing
here is walked, and nothing here refuses. The policy, the readiness
states and the `LIBRERUN_PII_ALLOW_DEGRADED` opt-out that restores the
regex-only behaviour are one thing across both processes — see the
production checklist in `docs/platform/Install.md` and blueprint S4c.

## 8. Keyless mode

`LIBRERUN_STUB_LLM=true` makes `stub` the provider for every step. The
reply is chosen deterministically:

1. an `X-LibreRun-Scenario` header naming a script in
   `services/gateway/stub/` — how a test asks for a particular reply;
2. your own `librerun.stub_reply` in the request body, if you ship
   keyless fixtures — the platform carries no content for your agent;
3. an instance synthesised from your `response_format` schema, so a
   structured-output agent works keyless with no fixture at all;
4. a forced tool call, when `tool_choice` names one;
5. otherwise a marked reply keyed by the input hash.

Every generated string says it is a fixture. A stub call is costed
against the model it stands in for, so keyless mode exercises the same
telemetry a credentialled one produces.

`LIBRERUN_STUB_LLM` is set on the **gateway**, not on the backend: the
gateway is the process that decides. A provider key pasted in Admin →
Application Settings while keyless mode is on is kept, and waits unused
until it is turned off; the page says so. An in-process agent that wants to
say so in its output — "this report came from fixtures" — asks
`await ctx.capabilities.llm.stub_mode()`, which reads the gateway's
`/healthz` and is answered once per invocation. It is not a branch to
take instead of calling: `complete` works keyless and is the path that
produces a span and a cost.

## 9. Reading your configuration

`ctx.config.steps()` returns your declared steps with this tenant's
overrides applied, read live over the run-scoped MCP server — so it
shows an admin's edit that the manifest baked into your image knows
nothing about. `GET /v1/models` lists the same steps as
`librerun/<id>` model names, and is the one call your agent key
authenticates alone.

**Your steps appear on the admin page whatever runtime you are.** The
page is served from your manifest's `llm.steps[]` and the tenant's
rows — both of which the platform holds — so a container agent's steps
are editable there with no Python surface of its own. That is what makes
it safe to declare a step with no `provider` and no `model` and let the
admin choose: the page shows it as needing one, and calls to it refuse
with `step_not_configured` until it has one. The *settings* are the same
since K5a: declared in the manifest's `settings[]` with their defaults and
valued per tenant on the page's Settings tab, a container's included, and
read by a run through `config_get` (`ctx.config.settings()`).

## 10. The refusals, in one place

| Code | Status | Means |
|---|---|---|
| `invalid_json` | 400 | the body is not a JSON object |
| `credential_required` | 401 | no run token and no agent key |
| `agent_key_invalid` | 401 | the bearer is not a LibreRun agent key (a provider key never is) |
| `run_token_invalid` / `run_token_ended` | 401 | unknown or expired token; the invocation has ended |
| `run_token_required` | 401 | a model call with an agent key alone |
| `credential_mismatch` | 401 | the key and the token name different agents |
| `agent_unknown` | 403 | no current manifest snapshot for this agent |
| `llm_not_granted` / `kb_not_granted` | 403 | the manifest does not grant it |
| `trace_mismatch` | 403 | the `traceparent` names another trace |
| `unknown_step` | 400 | the manifest does not declare that step |
| `step_required` | 400 | no step named, and no `default` step declared |
| `step_not_configured` | 400 | the step has no provider and model anywhere |
| `kb_embed_bounds` | 400 | beyond what knowledge search already allows |
| `pii_in_identifier` | 400 | personal data where the gateway may not rewrite |
| `pii_in_structured_value` | 400 | a number that looks like personal data |
| `pii_detector_unavailable` | 503 | the redaction pipeline's named-entity stage cannot run (§7) |
| `binary_not_redactable` | 400 | a media part, with redaction on |
| `tokens_not_redactable` | 400 | token-id embeddings input, with redaction on |
| `parameter_not_allowed` | 400 | a request parameter that is neither model input nor the step's (§5) |
| `provider_refused` | the provider's | the provider will not accept this call as sent (§6) |
| `provider_unavailable` | 502 | the provider call failed with no usable status (§6) |
| `request_too_large` | 400 | too deeply nested or too large to walk (§5) |
| `unrewritable_value` | 400 | not JSON the gateway can classify (§5) |
| `invocation_deadline` | 504 | the invocation's budget ran out, or had already (§8) |
| `schema_too_large` | 400 | a keyless `response_format` schema asking for more than a fixture |
| `schema_unsatisfiable` | 400 | a keyless `response_format` schema whose own bounds exclude every value |

Every refusal names a path, never a value: a message that echoed the
offending text would leak exactly what the refusal exists to keep in.

This table is checked against the code rather than maintained by hand
(`services/gateway/tests/test_refusal_catalogue.py`). A code the gateway
can raise and this table omits leaves an author with nothing to read; a
code this table carries and no module can raise is a promise the gateway
does not keep, which a reader cannot tell from a real one. Both fail the
suite — and so does a refusal whose code is computed in a way the check
cannot follow, rather than that refusal going quietly undocumented.
