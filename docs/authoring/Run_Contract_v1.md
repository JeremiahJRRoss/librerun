# Run Contract v1 — the container agent wire protocol

*Blueprint B12a, locked decision L11(b). Status: **v1, stable**. The
chassis speaks this contract to every `runtime: container` agent; any
process that serves these four endpoints is a LibreRun agent, whatever
language or framework it is built with. This is a deliberately minimal
HTTP+SSE contract — it is NOT a resurrection of the retired
predecessor's gRPC stack, which stays dead (§2.3 of the blueprint).*

---

## Model

One contract **invocation** = one execution of one phase of one chassis
**run** (blueprint S1, decision L18). The chassis owns the manifest's
phase list, the approval gates between phases, all persistence, and all
user-facing state; the agent container owns nothing but the work. For a
two-phase manifest the chassis makes two invocations, parking between
them if the second phase declares `approval: true`. The container never
sees the gate — it sees a fresh `POST /v1/runs` when (and only when) the
chassis decides the phase should execute.

Everything the agent needs arrives in the `POST` body: the phase name,
the (already manifest-schema-validated, already PII-redacted) user
inputs, the prior phase's output when there is one, and the user's edit
text on a re-run. Everything the chassis needs back arrives as SSE
events, the last of which carries the phase's output payload.

```
chassis                                    agent container
   │  GET /healthz                              │
   │──────────────────────────────────────────► │   liveness probe
   │  POST /v1/runs   (bearer T)                │
   │──────────────────────────────────────────► │   → {"invocation_id": R}
   │  GET /v1/runs/R/events   (bearer T)        │
   │──────────────────────────────────────────► │   SSE stream
   │  ◄── event: progress  …                    │
   │  ◄── event: log       …                    │
   │  ◄── event: completed {"output": …}        │   terminal
   │  (GET /v1/runs/R/output — fallback)        │
```

## Authentication

The chassis mints a **per-invocation bearer token** (unguessable,
single-invocation scope) and sends it as `Authorization: Bearer <token>`
on the `POST` and on every subsequent request for that invocation. The
agent MUST bind the token seen on `POST /v1/runs` to the returned
`invocation_id` and MUST reject (`401`) any events/output request for
that invocation carrying a different or
missing token. Agents never mint tokens.

That bearer is also the invocation's **run token** — it names this run,
its tenant, its agent and the trace the run was minted with — and it is
what authenticates every call back into the platform. There are exactly
three such surfaces and no others:

| You call back to | Where it is | Credential |
|---|---|---|
| the **run-scoped MCP server** — capabilities (B13) | `run.mcp.url` in the POST body | this bearer, as `Authorization: Bearer` |
| the **OTLP relay** — your interior traces and logs (S4) | `OTEL_EXPORTER_OTLP_ENDPOINT` | this bearer, as `Authorization: Bearer` |
| the **LLM gateway** — model calls (S4a) | `OPENAI_BASE_URL` / `LIBRERUN_GATEWAY_URL` | this token in `X-LibreRun-Run-Token`, beside the agent key |

Only the first is advertised in the POST body, because only the first is
per-run: the relay and the gateway are fixed addresses of the
deployment, so they reach the container through its environment and the
contract stays a contract about one invocation.

Deploy containers on a network where the chassis can reach them and
users cannot; the token is run-scoping, not a substitute for network
isolation.

## Endpoints

### `GET /healthz`

Liveness. `200` with any JSON body (`{"status": "ok"}` by convention).
The chassis probes this before starting a phase and fails the phase
fast — with a clear operator-facing error — when it cannot be reached.

### `POST /v1/runs`

Start one phase invocation. Request body:

```json
{
  "contract": "v1",
  "agent_id": "echo-v1",
  "phase": "echo",
  "deadline_seconds": 3600,
  "run": {
    "id": "6f1c…",
    "case_id": "6f1c…",
    "tenant_id": "a000…",
    "rerun": false
  },
  "input": { "…the user_inputs payload…": "…" },
  "prior_output": { "…previous phase's output…": "…" },
  "user_edits": null
}
```

- `contract` — always `"v1"`; reject other values with `400`.
- `agent_id` / `phase` — from the manifest. An agent serving several
  phases dispatches on `phase`; an unknown phase is a `400`.
- `deadline_seconds` (since S4, additive) — this invocation's wall-clock
  budget: the manifest phase's `deadline_seconds` under the platform
  ceiling `LIBRERUN_MAX_PHASE_SECONDS` (default 3600), or the ceiling
  itself when the phase declares none. The chassis fails the phase when
  it passes, so an agent that watches it can stop cleanly rather than
  be cut off; ignoring it still conforms.
- `run.id` — the chassis run id: the object the platform calls a *run*,
  labelled `run_number` in the platform API. `run.case_id` is the pre-S1
  spelling of the same value, sent alongside for one release and dropped
  at v1.1 — read `run.id`. `run.rerun` is `true` when the
  phase is being re-executed after a user edit; `user_edits` then
  carries the edit text.
- `run.mcp.url` (since blueprint B13) — where this chassis serves its
  **run-scoped MCP tools** (`kb_search`, `run_store_get`,
  `run_store_set`, `audit_log`, since S4 `redact`, since S4a
  `config_get` — this run's effective LLM step configuration and, since
  K5a, its settings, under no grant — and since K8a `secret_get`, one of
  the tool secrets the manifest declares in `secrets[]`, this tenant's
  value, also under no grant; the pre-S1
  names `case_store_get` / `case_store_set` are still accepted on
  `tools/call` for one release, not advertised on `tools/list`).
  Authenticate MCP requests with the SAME bearer token this POST
  carried; tools are gated by the agent's manifest `capabilities:`
  grants, all but `config_get` and `secret_get`. `progress` intentionally stays on this contract's SSE stream,
  because an agent→platform event is not tool-shaped. **Model calls are
  not here either**, and not because a container cannot make them: they
  go to the LLM gateway over its own OpenAI-compatible HTTP surface
  ("Model calls" below), carrying this same token in
  `X-LibreRun-Run-Token`. Agents that need no platform services may
  ignore the key entirely.
- `input` — the intake payload, already validated against the agent's
  manifest-declared JSON Schema and already PII-redacted by the
  chassis. Containers do not re-validate for safety, only for their own
  robustness.
- `prior_output` — the previous phase's `output`, or `null` on the
  first phase.

Response: `201` (or `200`) with `{"invocation_id": "<opaque string>"}` —
the agent's handle for this one phase execution, deliberately not the
chassis run id. `{"run_id": …}` is the pre-S1 spelling of the same
field: the chassis reads `invocation_id` first and accepts `run_id` for
one release; agents SHOULD send both until v1.1 and `invocation_id`
alone after. The id
is opaque to the chassis, which percent-encodes it as exactly one path
segment in the follow-up URLs — ids containing reserved characters
(`/`, `?`, `#`, …) therefore work, but agents SHOULD keep ids URL-safe
so their own routers don't need decoding. The agent may execute
synchronously in the background or lazily on the events request — the
chassis only promises to connect to the events stream promptly after
the `POST`.

### `GET /v1/runs/{invocation_id}/events`

`Accept: text/event-stream`. The agent streams standard SSE frames
(`event:` + `data:` lines, `data` is one JSON object, frames separated
by a blank line). Event vocabulary:

| event | data | meaning |
|-------|------|---------|
| `progress` | `{"step_id": str, "status": "running"\|"completed"\|"failed", "detail": str\|null}` | Live progress; the chassis maps the wire statuses onto its own progress vocabulary (`completed`→`complete`, `failed`→`error`; unknown values degrade to `running`) before feeding its run-progress UI. |
| `log` | `{"level": "info"\|"warning"\|"error", "message": str}` | Operator-facing log line; the chassis re-emits it into its own structured log stream tagged with the agent id. |
| `completed` | `{"output": {…}}` | Terminal success. `output` is the phase's result payload (JSON object). |
| `failed` | `{"error": str}` | Terminal failure. The chassis marks the run errored and surfaces `error` to operators (not end users). |

Exactly one terminal event (`completed` or `failed`) ends the stream;
the agent closes the connection after sending it. Unknown event names
are ignored by the chassis (forward compatibility). If `completed.data`
omits `output`, the chassis falls back to `GET /v1/runs/{invocation_id}/output`.

**What the chassis does with what you send (S4).** Intake is not the
only door PII can enter by, so every free-text field of these events is
redacted by the chassis PII pipeline before it is logged, stored or
forwarded — `log.message`, `progress.detail`, `failed.error` — with the
same placeholders intake uses (dates and place names are left alone at
this boundary; they are content, not identity). `progress.step_id`
becomes the field name of the run's progress record, so it is checked
as an **identifier**, never rewritten: an event whose `step_id` is
flagged is dropped with a chassis-side warning naming the position, and
the run continues. `completed.output` is **walked** before it is
persisted: string leaves are redacted in place, object keys and numbers
are checked (Luhn for card numbers, libphonenumber for phone numbers,
nine digits under a social-security key), and a flagged key or number
ends the run `error` with reason `pii_in_output` — the JSON path named,
never the value, nothing stored. Carry an epoch under a time-named key
(`created_at`, `ts_ns`) or as an ISO string; a ten-digit phone number
as a JSON number under any key is refused wherever it sits.

Reconnects: v1 keeps this simple — the stream is consumed once, start
to finish, over one connection. Agents SHOULD buffer events until the
stream is attached and MAY replay from the start on a reconnect;
`Last-Event-ID` resumption is not part of v1. A frame written without
its trailing blank line before the socket closes is still honored.

**Deadline.** One phase invocation gets a wall-clock budget covering the
whole exchange: the manifest phase's `deadline_seconds`, under the
platform ceiling `LIBRERUN_MAX_PHASE_SECONDS` (default 3600 s), sent to
you as `deadline_seconds` in the `POST /v1/runs` body. Emitting
`progress` forever does not extend it — the chassis holds run state open
for the duration, so a stream that never reaches a terminal event is
failed rather than waited on, with the deadline named in the run's
error. The run token expires one minute after the deadline at the
latest, whether or not the invocation ended.

The deadline is also the only clock you have to satisfy: **going quiet
does not cost you anything the deadline still allows.** The chassis
derives its HTTP read timeout from that same `deadline_seconds`, so a
phase that emits one `progress` event and then works silently until it
finishes is not cut off — and if it does run out of time, the failure
names the deadline rather than a transport error. You are free to emit
`progress` as often or as rarely as suits the work; frequent events buy
your users a better run page, not a longer budget.

### `GET /v1/runs/{invocation_id}/output`

Fallback/companion to the terminal event: `200` with
`{"output": {…}}` once the invocation completed, `404` before completion
or for unknown invocation ids, `409` (with `{"error": …}`) if it failed.

## Output semantics

The phase's `output` object is what the chassis persists:

- **Non-final phase** — `output` lands in the run snapshot's `analysis`
  and is shown on the approval screen (shape-driven rendering); it is
  handed to the next phase as `prior_output`.
- **Final phase** — `output` lands in `structured_data`. For manifest
  `output.mode: structured` the UI renders it generically and export
  uses the generic document. `output.mode: html_report` containers
  additionally return the rendered report as an `output.report_html`
  string (the only reserved key in v1).

## Manifest binding

```yaml
runtime: container
container:
  url: http://echo-agent:8090      # or ${ECHO_AGENT_URL}
input_schema: input_schema.json    # REQUIRED for container agents
```

- `container.url` — where the chassis reaches the agent: a compose
  service name or any preconfigured URL. `${VAR}` references expand
  from the chassis environment at discovery; an unresolvable variable
  skips registration (logged), never crashes. **v1 addresses running
  containers — the chassis does not launch or schedule them** (no
  orchestrator; compose/systemd/k8s own the lifecycle).
- `input_schema` — container agents cannot serve their intake schema
  from code, so the manifest MUST name a JSON Schema file shipped next
  to it. Everything else (phases, output mode, feedback sections,
  scenarios, intake steps) works exactly as for `python-package`
  agents — the generic intake wizard, approval gates, structured
  results, feedback, and trace links come for free.

## Chassis capabilities over MCP (blueprint B13)

The chassis is also an MCP server, scoped per run: a stateless
JSON-RPC 2.0 endpoint (streamable HTTP, one JSON response per POST) at
the `run.mcp.url` advertised in the POST body. `initialize`,
`tools/list`, and `tools/call` are supported; every request after
`initialize` requires the run's bearer token, `tools/list` shows only
the tools the manifest grants, and calls are executed through the same
capability façade in-process agents use. Any framework's native MCP
client — or ~30 lines of plain HTTP — consumes it with no per-framework
glue.

| tool | grant | arguments | result |
|------|-------|-----------|--------|
| `kb_search` | `kb` | `{queries: [str], top_k?: int}` | `{results: [{title, url, snippet, relevance_score}]}` |
| `run_store_get` | `run_store` | `{key}` | `{key, value}` |
| `run_store_set` | `run_store` | `{key, value}` | `{ok, key}` — **scratch, not storage**: see the lifetime note below |
| `audit_log` | `audit` | `{action_type, detail}` | `{ok}` — the row carries the run owner's id and email, stamped by the chassis; attribution is never an argument |
| `redact` (S4) | `pii` | `{text}` | `{text}` — the intake PII pipeline on demand, same recognizers and placeholders; the Python SDK's `ctx.pii.redact()` is its client |
| `config_get` (S4a; settings K5a) | none | `{}` | `{steps: [{step_id, label, provider, model, temperature, max_tokens, timeout_seconds, overridden}], settings: [{key, value}]}` — the agent's declared `llm.steps[]` with **this tenant's** admin overrides applied, and its declared `settings[]` with **this tenant's** values (every declared key; the default where the tenant chose none), read at call time so an admin's edit is visible to the next call; the SDK's `ctx.config.steps()` and `ctx.config.settings()` are its clients. The run's own configuration is the agent's data, not a platform capability, so it is listed and served under no grant (before K5a it was listed under `llm`) |
| `secret_get` (K8a) | none | `{name}` | `{value}` — one of the tool secrets the manifest declares in `secrets[]`: **this tenant's** value, else every tenant's default, from the platform's store alone (a container's own environment is its own, and the chassis never reads its environment for one). The one result that carries a secret, sent to the declaring run alone; the chassis scrubs every value a run was delivered from what the run persists — its output, report, error text, progress, `audit_log` details and `run_store_set` values — replacing each with `[REDACTED_SECRET]`. `-32005` for an undeclared name, `-32006` for a declared one with no value |

Error codes on `tools/call`: `-32001` unknown or expired token (HTTP
401), `-32002` the tool's grant is not in the manifest, `-32602`
arguments outside the tool's declaration — every tool refuses an
argument it does not declare, so a `user_email` on `audit_log` is an
error, not a column — and, since S4, `-32003` **refused by the walk**:
what a call persists is walked exactly as an in-process call is — a
`run_store_set` key or an `audit_log` action type is an identifier, a
value or a detail is walked with string leaves redacted and keys and
numbers checked — and the message names the reason (`pii_in_store`,
`pii_in_audit`, or since K8a `secret_in_output` for a key or name holding
one of the run's tool secrets), the argument and the JSON path, never the
value; since K8a, `-32005 secret_not_declared` and `-32006 secret_not_set`
from `secret_get`.

**The run store is scratch space with a lifetime.** Since S5 every
`run:`-shaped Redis hash a run owns — its progress, the model that
answered each step, and the `run_store` — expires **seven days after
its last write**, sliding forward on every write. Nothing deleted them
before, so they were immortal. An agent may rely on the store across
the invocations of one working run; it may not rely on it as the record
of anything, and a run parked on a human gate for longer than a week
comes back with the store empty. Persist anything that must outlive the
run in the agent's output, which the chassis writes to Postgres.

## Model calls: the LLM gateway (blueprint S4a)

A container reaches a model through the **LibreRun gateway**, a separate
service that holds the deployment's provider credentials. A container
agent holds none, and names no model — it names a **step** its manifest
declares, and the gateway resolves that step to a provider, a model, a
temperature, a token limit and a timeout from the tenant's admin
configuration at request time (L25, D13). This is not part of the four
endpoints above: the gateway is an OpenAI-compatible HTTP ingress, so a
framework that already speaks OpenAI needs no LibreRun code at all.

```python
client = OpenAI(
    base_url=os.environ["OPENAI_BASE_URL"],          # http://gateway:8090/v1
    api_key=os.environ["OPENAI_API_KEY"],            # your LibreRun AGENT KEY
    default_headers={"X-LibreRun-Run-Token": bearer},  # the POST's bearer
)
client.chat.completions.create(model="librerun/analyze", messages=messages)
```

`bearer` is the token this invocation's `POST /v1/runs` carried — the
same one you bound to the `invocation_id`. Build the client inside the
handler, not at import: the run token is per invocation.

**Two credentials, and which one a model call cannot do without.** The
**run token** is that one: a request carrying only an agent key is
`401 run_token_required`, **in every mode**, single-tenant demo
included, because one container serves every tenant and a key that
names the agent cannot say whose data a call is about or which run pays
for it. The **agent key** (`Authorization: Bearer`, the compose
fragment's `OPENAI_API_KEY`, never a provider key) names the agent and
nothing else: it authenticates `GET /v1/models` alone, and otherwise it
is what an OpenAI client library puts in the API-key slot it insists on
filling. Present both, as the fragment's shape does, and they must name
the same agent (`401 credential_mismatch`). Present the run token alone
— which is what the Python SDK does — and the call is served.

**What the gateway does with the call**, in one paragraph: the step's
own fields (`model`, `temperature`, `max_tokens`,
`max_completion_tokens`, `timeout`) are *ignored* rather than refused,
because a framework fills them in and the admin's values are the ones
that apply; model-input fields are forwarded; anything else is `400
parameter_not_allowed`. Every string the model would read is redacted
first unless the manifest sets `llm.redact_outbound: false`, and
personal data in a position the gateway may not rewrite — a tool name,
a schema key, a call id — refuses the request (`400 pii_in_identifier`,
naming the path, never the value). The reply is an ordinary OpenAI
completion plus a `librerun` key carrying the step as **resolved**
(`{"provider", "model", "step_id"}`), which is the only honest answer to
"which model wrote this"; streamed replies omit it. `LIBRERUN_STUB_LLM`
on the gateway makes `stub` the provider for every step, so a keyless
deployment exercises the same span, redaction and cost.

**One trace.** Send the `traceparent` you received and the gateway's LLM
span hangs from it; send none and it hangs from the invocation's phase
span, so a framework that drops the header still lands in the run's one
tree. A `traceparent` whose trace id is *not* the one the run token was
minted with is `403 trace_mismatch` — the same rule the OTLP relay
applies, for the same reason: a valid token must not be usable to hang
cost telemetry in somebody else's trace.

`docs/authoring/LLM_Gateway.md` is the normative reference for this
surface — every forwarded parameter, the full refusal catalogue, the
span's attributes, the keyless fixture protocol and how agent keys are
provisioned and rotated.

## Framework fit (L11)

The contract was reviewed against the L11 target list. The test is
always the same: can the framework's ordinary entrypoint be wrapped in
four small HTTP handlers?

| Framework | Fit |
|-----------|-----|
| LangChain / LangGraph | Python/JS server wraps `graph.invoke()`; graph-node callbacks map 1:1 onto `progress` events. |
| LlamaIndex | Query/agent pipelines return a result object → `completed.output`; instrumentation events → `progress`. |
| CrewAI | A crew kickoff per run; task-level callbacks → `progress`. |
| OpenAI Agents SDK (Python & JS) | `Runner.run_streamed()` yields streamed run items → forward as `progress`/`log`; final output → `completed`. |
| Google Genkit (Node/TS) | A flow per phase; flow streaming callbacks → SSE. Express handlers are idiomatic Genkit hosting. |
| Google ADK | Agent runners expose event streams → direct SSE translation. |
| Anthropic Claude Agent SDK (Python & TS) | `query()` yields message/tool events → `progress`; final result → `completed.output`. |
| MCP | Not an agent runtime — MCP is how agents will consume *chassis capabilities* (blueprint B13). An MCP-only "agent" still needs a 4-endpoint shim to be runnable; progress streaming stays on this contract, not MCP. |

Node/TS frameworks are first-class exactly because the contract is
plain HTTP+SSE: `express` + `res.write()` is a complete transport
implementation.

## Observability boundary

The chassis observes a container agent at its **edges**, not inside it:

- Chassis-side spans wrap the `POST /v1/runs` call and the SSE
  consumption, stamped `librerun.scope="run"` with the full identity
  set (`agent.id`, `run.id` — with `case.id` duplicated for one release
  — ...) exactly like an in-process run
  (`docs/authoring/Agents_Design.md`, "Observability contract").
- The progress events you emit become chassis progress records — they
  are the platform-visible record of what your run did, and the smoke
  gate asserts on them.
- Your **interior is dark to the platform by design**: the chassis
  neither injects telemetry into your process nor requires any.

Two facts stated as contract rather than left implied:

1. **Trace context crosses the wire (S4, additive).** Every request of
   an invocation — `POST /v1/runs`, the events stream, the output
   fetch — carries the W3C `traceparent` and, when the run has vendor
   state, `tracestate` headers of the chassis **phase span**, itself a
   child of the run's root span, so the trace id is the run's in every
   phase, before and after an approval. Adopt `traceparent` as the
   parent of the spans your process emits and they join the run's one
   tree (the Python SDK does this for you); ignore both and you still
   conform — absence of either header must never be an error, and a
   v1 agent written before S4 keeps working unchanged.
2. **Your interior telemetry reaches the platform through the chassis
   OTLP relay (S4).** Export OTLP/HTTP protobuf to
   `POST {LIBRERUN_PUBLIC_URL}/api/v1/_o/otlp/v1/traces` and
   `/v1/logs` — on compose,
   `OTEL_EXPORTER_OTLP_ENDPOINT=http://backend:8000/api/v1/_o/otlp`,
   which the Python SDK reads — with the run token as the bearer:
   `Authorization: Bearer <token>`. There is no other path: agent
   containers join only the internal `agents` network, so
   `vector:4317` and the Internet are unreachable by construction, and
   the relay is the one door. **One token, one trace**: the relay
   accepts a request only if every span and log record in it carries
   the trace id of the run the token was minted for (the one in the
   `traceparent` you received) — `403 trace_mismatch` otherwise, the
   whole request refused — so an agent writes into no other run's
   trace and never into the platform plane; an SDK serving overlapping
   invocations partitions its batches by trace and sends one request
   per token. The runner marks the token `ended` the moment the
   invocation completes or fails: the MCP server refuses it from then
   on (`-32001`), the relay alone still honours it for a short grace
   (30 s) so a flush that straddles your `completed` still lands, and
   after the grace it answers `401`. Before forwarding to the
   platform's collector the relay stamps `librerun.scope=run`,
   `agent.id`, `run.id`, `tenant.id` and `run.number` onto the
   resource and every span and record (your `service.name` stays
   unless it is empty or `librerun-backend`, which becomes your agent
   id — a container never masquerades as the chassis) and **walks**
   the request: string positions redacted with the intake
   placeholders, attribute keys and numbers checked (a flagged span is
   stripped to its identity, a flagged log record dropped), `bytes`
   values dropped, the protocol's own scalars untouched. Adopt the
   incoming `traceparent` as the parent of your spans and your interior
   appears under the run's one tree in the trace viewer.

---

## Reference implementation

[`backend/agents/_examples/echo_container/`](../../backend/agents/_examples/echo_container/)
ships a ~100-line stdlib-Python echo agent speaking the full contract
(token binding included), its manifest, input schema, demo scenario,
and a Dockerfile. The chassis-side test suite runs it as a live server
(`backend/tests/test_container_runner.py`).

## Versioning

`contract: "v1"` in the POST body is the version handshake. Breaking
changes mean `v2` endpoints (`/v2/runs`) and a new doc — v1 agents keep
working against a v1-speaking chassis.
