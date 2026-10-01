# Container agents — Python in ten minutes, and what the contract is

A container agent is a running HTTP server the chassis **addresses**
per phase (it never launches or schedules it — compose does). This page
gets a Python agent running on the LibreRun agent SDK in ten minutes,
then says what the contract is for every other language.
`docs/authoring/SDK.md` is the SDK's control-surface reference;
`docs/authoring/Run_Contract_v1.md` is the wire contract.

## Python, in ten minutes

**1. Write the handler.** One async function; the SDK serves it.

```python
# my_agent/agent.py
from librerun_agent import RunContext, run, serve

async def handler(ctx: RunContext) -> dict:
    ctx.progress("running", step="think", label="Thinking")
    question = ctx.input["question"]                 # already schema-validated and PII-redacted by the chassis
    clean = await ctx.pii.redact(question)           # anything you fetch yourself: the intake pipeline on demand
    ctx.log(f"{len(clean)} chars in")                # an operator-facing line, redacted at ingestion
    answer = await ctx.llm.text("think", clean)      # the gateway: your STEP's id, never a model name
    ctx.progress("completed", step="think")
    return {"answer": answer, "phase": ctx.phase}    # the phase output: a JSON object

app = serve(handler)

if __name__ == "__main__":
    run(app, port=8090)          # the uvicorn extra
```

**2. Describe it.** `my_agent/agent.yaml` and an input schema:

```yaml
manifest_version: 1
id: my-agent-v1
name: My Agent
runtime: container
container:
  url: ${MY_AGENT_URL}           # expanded from the chassis environment at discovery
input_schema: input_schema.json
phases:
  - name: think
    deadline_seconds: 300        # optional; under LIBRERUN_MAX_PHASE_SECONDS
output:
  mode: structured
capabilities: [pii, llm]         # the grants: kb, run_store, audit, pii, llm
llm:
  steps:                         # DEFAULTS; the admin page edits them per tenant
    - id: think                  # and the gateway resolves them at request time
      label: Thinking
      provider: openai
      model: gpt-4o-mini
      temperature: 0.0
      max_tokens: 256
      timeout_seconds: 30
# network:
#   egress: true                 # only if the compose fragment also joins `egress`
```

```json
{ "type": "object", "required": ["question"],
  "properties": { "question": { "type": "string", "x-pii": true } } }
```

**3. Build it.** A Dockerfile that installs the SDK and your file:

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir "librerun-agent[uvicorn,otel]"   # until the package is published: COPY the SDK tree from this repository, as the echo agent's Dockerfile does
COPY agent.py /app/agent.py
CMD ["python", "/app/agent.py"]
```

**4. Run it under compose.** A service in `agents.compose.yaml`, on the
internal `agents` network, logs off, the OTLP endpoint at the relay:

```yaml
services:
  my-agent:
    build: { context: ./my_agent }
    labels: { librerun.agent_id: my-agent-v1 }
    environment:
      OTEL_EXPORTER_OTLP_ENDPOINT: http://backend:8000/api/v1/_o/otlp
      # The LLM gateway (blueprint S4a). Both spellings: the SDK reads
      # LIBRERUN_GATEWAY_URL, a framework that only speaks OpenAI reads
      # OPENAI_BASE_URL — and pointing it here is that framework's whole
      # integration.
      LIBRERUN_GATEWAY_URL: http://gateway:8090
      OPENAI_BASE_URL: http://gateway:8090/v1
      # Your LibreRun AGENT KEY, never a provider key. `:?` rather than
      # `:-`: an unprovisioned agent must stop `up` with a named error
      # instead of starting a container whose every model call is
      # refused, which reads like a model outage rather than a missing
      # line in `.env`.
      OPENAI_API_KEY: ${LIBRERUN_AGENT_KEY_MY_AGENT_V1:?agent key not provisioned}
    networks: [agents]
    logging: { driver: none }
```

`<ID>` in `LIBRERUN_AGENT_KEY_<ID>` is your agent id upper-cased with
every character outside `[A-Z0-9]` replaced by `_` (`my-agent-v1` →
`MY_AGENT_V1`), because compose variable names admit no hyphens. Keys
are provisioned before `up` — compose expands the variable while no
LibreRun service is running — so `scripts/demo.sh` writes one per
bundled agent and the gateway registers what it finds at boot.

Set `MY_AGENT_URL=http://my-agent:8090` for the backend, put the agent
directory on `LIBRERUN_AGENTS_PATH`, restart the backend: the agent is
in the picker, with the wizard from your schema, the run page's
progress, one trace per run in the viewer, and feedback — none of it
written by you.

**5. Prove it.** The container battery drives your URL through the
contract from the host, with a relay recorder for your spans:

```bash
cd backend && python -m adapter_kit.run_contract \
  --url http://localhost:8090 --agent-dir ../my_agent --relay-port 18080 --expect-spans
```

It checks schema-valid output (a JSON object the chassis walk accepts),
progress in the contract vocabulary, exactly one `completed`, token
binding, that every span and log record your container exported arrived
under the minted token carrying the chassis trace id, that an invocation
sent with **no** `traceparent` still conforms, and what your agent asked
the chassis for over MCP.
`.github/workflows/container-battery.yml` runs it against the reference
echo agent (`backend/agents/_examples/echo_container/`, the shape to
copy).

**The battery serves `run.mcp.url` for you.** It advertises a run-scoped
MCP endpoint in the POST body exactly as the chassis does, so an agent
that calls `ctx.capabilities.*`, `ctx.pii.redact` or `ctx.config.step`
works under the battery instead of dying on
`-32601 this invocation advertised no run.mcp.url`. That endpoint is a
**driver, not a second chassis**: `tools/list` is the chassis's own tool
table filtered by your manifest's grants, `redact` answers through the
chassis's own PII pipeline, and `config_get` returns your manifest's
`llm.steps[]` with no tenant overrides and its `settings[]` at their
defaults, under no grant as the chassis serves it — but `kb_search` finds nothing,
`run_store` lives only as long as the battery, and `audit_log` writes no
row. It records every request, and reports:

| `mcp` | what it means |
|-------|---------------|
| `pass` | your agent called, and every request carried the bearer the invocation was given |
| `skip` | your agent never called. Conformant — the contract says an agent may ignore the key entirely |
| `fail` | a request arrived with a missing or foreign bearer, or a malformed JSON-RPC envelope; or `--expect-mcp` was given and nothing called |
| `incomplete` | `--expect-mcp` was given with `--no-mcp`, so the check was asked for and could not be taken |

A `-32002` refusal — a tool your manifest does not grant — is **not** a
finding: it is an answer you are entitled to receive and handle, and the
battery records it as a clean request.

Two flags govern the endpoint. `--mcp-advertise-host` is how **your
agent** reaches the battery, which is not how the battery reaches your
agent: `127.0.0.1` in-process, `host.docker.internal` from a container
started with `--add-host=host.docker.internal:host-gateway`, and — under
`librerun battery` — the **driver's own container name**, which the CLI
passes for you. `--no-mcp` turns the endpoint off entirely, and then the
body carries no `mcp` key at all — an absent key has a defined behaviour
in the contract, while an advertised URL that answers nothing has none.

**Not the compose service name**, if you drive the battery yourself with
`compose run`. A one-off container does not answer to the service it was
started from — `docker compose run --help` carries `--use-aliases`
precisely because those aliases are opt-in — so `backend:<port>` reaches
the *long-running* backend container, where the driver is not. That is
its own kind of trap: `backend:8000` works, because the chassis really
is serving MCP there, so the network looks fine and only the battery's
ephemeral port is refused. Both container templates call `redact` and
then **warn and carry on** when it is unreachable, which is the right
thing for an agent to do and is exactly what hides this: the battery
reports `mcp: skip`, which is a legal verdict, and nothing is red.

If your own harness advertises a name, check the verdict rather than the
exit code. `skip` on an agent you know calls back means the URL you
advertised did not reach you.

**The `traceparent` the contract lets you ignore is really sent absent.**
One invocation of every battery run carries no `traceparent` and no
`tracestate`, and must still answer, stream and reach `completed`:
"absence of either header must never be an error"
([Run_Contract_v1.md](Run_Contract_v1.md#observability-boundary)).
There is no `skip` for this one — every agent owes it — and an agent that
starts a trace of its own for that invocation is behaving correctly, so
the span check does not hold its spans to any id.

The binding check runs **several** invocations, each after the last has
finished, and cross-uses every one's token against every other's `events`
and `output` — both directions, every ordered pair. How many depends on
your manifest: one for the phase under test, one for each later phase the
chassis could reach, one more for each of those that is gated (the
rerun), and **always** one extra run of the phase under test itself. A
single-phase manifest is therefore two invocations; a three-phase
manifest with one gate is five. Count on more than two, and on one of
them repeating a phase you have already served.

Cross-using every pair is the only way to tell a binding from an
allowlist: a token the battery invents is refused by any agent that
remembers what it issued, while a token the agent *itself* accepted, for
a different invocation, must still be answered `401`. Both directions,
because an agent that hands each invocation the tokens issued up to that
point refuses the newer token on the older invocation and accepts the
older on the newer.

**Every** invocation the battery starts — not only the first — is also
asked the two questions that need no token you issued: what it answers a
request carrying **no** `Authorization` header, and one carrying a token
the battery invented. A guard shaped `if header and header != expected:
401` refuses every other probe in this check and still serves a later
invocation's `events` and `output` to a request with no bearer at all.

So your agent has to be able to start a second invocation — sequentially
is enough; nothing here asks you to serve two at once. If it will not,
the battery reports `token_binding: fail` with **UNPROVEN** rather than a
pass, because it could not reach the case that matters. A two-phase
manifest makes two invocations of your agent anyway.

The second invocation is a transition your **manifest** actually permits,
and the report names each one it used:

- a later phase in `phases` — the battery invokes it under the same
  `run.id`, with your previous output as `prior_output`, exactly as the
  chassis does on a multi-phase run;
- **a later phase that is gated** (`approval: true`) — then that phase
  has *two* producible transitions and the battery uses both. An edit
  while the run is parked reruns the parked phase, holding `run.id`
  **and** `phase` constant, which is a request the later-phase probe
  cannot imitate;
- **a second run of the phase under test — always, whatever else the
  manifest permits.** When there is no later phase it is the only
  transition available, since the chassis invokes that phase once per run
  and the same-run case cannot arise. When there *are* later phases it is
  still sent, because every other transition above changes the `phase`,
  and a token pool keyed by phase would refuse all of them and be
  certified. It carries a new `run.id`, no `prior_output`, and the phase
  under test's own `deadline_seconds`. It is not part of the sequence, so
  a legal `failed` anywhere in the walk does not cancel it.

**It walks the whole reachable sequence, not one step of it.** The
chassis runs phases in a loop and only stops at a failure, at a gate, or
after the last one, so a manifest with no gates runs *every* phase in one
run with no human in between — and a gate stops the run, not the
sequence: the user approves and it carries on. The battery follows the
same path, so the WALK through a four-phase manifest is four invocations
and each of them is probed — plus the unconditional second run above, and
plus a rerun for every gate, so four is the walk alone and never the
total. If your agent is bound per invocation for the first two and then
reaches for "any token this run issued" on the third, that is where it
will be caught. Every phase is probed under its
own name, so a report says `the third phase` rather than `the next
phase`.

Where both apply the battery sends them in the only order the chassis
can: **the rerun first, then the later phase.** Completing the final
phase ends the run, and an edit is accepted only while the run is parked,
so a rerun arriving after the gate has been passed is one the chassis
could never have sent — and if you model your run's lifecycle you are
right to refuse it. For the same reason the later phase receives the
*rerun's* output as its `prior_output`, not the original invocation's.

**A failed phase costs the walk only what no run can reach.** If one of
those invocations ends `failed`, that RUN is over — a non-final phase
that fails sets the run to error, so the chassis would invoke nothing
further in it. The battery does not advance that run, and you are not
penalised for a legitimate `failed`. It does not stop, either: a gated
phase is also reached by the user simply **approving** the original
invocation instead of editing it, so the later phase has a path that
never touches the rerun. The battery takes that path — it starts a
replacement run, waits for your phase to succeed in it, **replays
whichever phases the rest of the plan needs**, and probes the remaining
transitions there, naming each leg in the report as `fresh run 1`'s
such-and-such phase. So expect a failed rerun to be followed by a fresh
`run.id` that repeats phases you have already served, one at a time and
each carrying the last one's output, up to the phase the plan resumes
at — and no further, because asking a run for a phase whose predecessor
never ran there would be a request the chassis cannot make. Every one of
those legs is cross-used like any other invocation, including one that
ends `failed`.

The plan only shortens when your agent will not let that replacement run
get there, and then only for a **legitimate `failed`** on one of its
legs: the report says how far the run got and what stopped it, and the
transitions behind it are not counted against you. A leg that answers
`500`, outruns its deadline, sends two terminal events or produces an
output the chassis could not have read is a different matter — that is an
invocation the chassis can produce and could not consume, so the binding
check reports that it did not conclude rather than quietly asking for
less.

Outside a gate it never sends a rerun. An edit is accepted only while a
run is parked awaiting approval, and a run parks only when a later phase
is gated, so a single-phase or final phase can never receive one —
refusing a rerun there is correct and the battery will not ask for one.

Every invocation the battery starts is cross-used against every other, in
both directions — not just against the first one. "Bound to its
invocation" is a claim about each pair, and an agent that accepts the
token of whichever invocation immediately preceded it fails only the pair
nobody asked about.

Each invocation arrives with its own trace context: the `traceparent` your
agent receives carries its **run's** trace id and a **fresh parent span**,
because the chassis opens a phase span around every invocation and injects
whatever span is current. Two invocations of one run (a phase and its
rerun, or a phase and the next one) share a trace id under different
parents; a second run is a different trace entirely. An agent that records
the parent it was given will never be handed the same one twice.

`completed` need not carry the output inline. If it does not, the battery
fetches `/output` with that invocation's own token, exactly as the chassis
does, and that is what the following phase receives as `prior_output`.
"Does not" means the key is **absent or null** — a `completed` carrying
`output: []`, or any other non-object, is a failure rather than an
invitation to look at `/output`, because that is what the chassis does
with it. And every invocation the battery starts must end its stream with
`completed` or `failed`: a stream that simply stops is one the chassis
would have ended `ContainerAgentError`, so the battery treats it as an
invocation it could not have consumed rather than as one it drained.

**Exactly one** terminal event, on every invocation and not only the
first. The chassis returns at the first `completed` or `failed` it
parses, so anything you send after it is a frame production never reads —
which makes a stray second terminal the kind of bug nothing downstream
will ever report for you. The battery reports it, and it follows the
first terminal the way the chassis does: a `completed` followed by a
stray `failed` is a phase that **succeeded**, and your run carries on to
the next one. The report says which terminals arrived and in what order.

The battery never advertises a `deadline_seconds` larger than it will
wait for, and never waits longer than it advertised. `--timeout` is its
`LIBRERUN_MAX_PHASE_SECONDS` in full: each phase is sent `min(your
declared budget, that ceiling)` by the chassis's own resolver, and the
battery then gives that invocation exactly that many seconds of wall
clock before recording a failure — the same bound
`asyncio.timeout(deadline)` puts on a phase in production, where
overrunning it ends the run `PhaseDeadlineExceeded`. Raise `--timeout` to
exercise a declared budget above this ceiling; lower it to prove your
agent respects a tighter one. The ceiling is per *phase*, so a manifest
whose phases declare different budgets is waited on per phase too.

An agent that keeps emitting progress is not thereby exempt: the bound is
on the invocation, not on the gap between chunks. Nor is an agent that is
slow to *answer* the POST, or slow to serve `/output` after `completed`
carried no inline result — one budget covers the whole exchange, the way
`asyncio.timeout(deadline)` wraps all of `agent.run_phase` in production.

What that budget does NOT cover is the battery's own binding probes,
which no chassis ever makes. **Not one of them is sent until your
invocation's last leg has finished** — your events stream, and the
`/output` fetch when `completed` carried no inline result. So while you
are serving an invocation the only requests in front of you are ones
production would also have made, and nothing the battery does can be
charged to your deadline: not a probe's answer, and not a probe's place
in your queue.

That second half needs saying out loud, because it is not obvious. If you
serve one request at a time, a probe merely SENT during your stream would
sit in your accept queue ahead of the `/output` fetch — which the battery
cannot send until the stream has closed. Your own fallback would then be
served behind it, inside your budget, and you would be failed for
outrunning a deadline the battery had spent. Probes used to run beside
the stream for the opposite reason, to keep them out of the invocation's
clock; deferring the send keeps them out of its queue as well, which is
strictly more.

Once they do start they ask nothing extra of you: they are sent one at a
time, and each is allowed five seconds of its own rather than sharing
your phase's deadline, so a plan of them cannot fail you for being long.
Those five seconds run from the send — and by then you are free, which is
the whole point of waiting.

`--timeout` must be at least one second. `deadline_seconds` is a positive
integer on the wire, so a sub-second ceiling would advertise a budget
larger than the one enforced, and the battery refuses it rather than
rounding it up to a number you did not ask for.

The battery drains that second invocation before it returns, so an agent
that starts work lazily when the events stream is opened is not left with
an invocation nobody reads. If the invocation's **own** token is refused
by its own events stream, that is a binding failure: the chassis could not
have consumed it either.

Three other ways to land on UNPROVEN, each named in the failure so you
can act on the right one: the second request fails at the transport; the
agent answers it but names no `invocation_id`; or it answers with the
*same* `invocation_id`, having replaced the first invocation rather than
started a second — in which case there is no second invocation to
cross-use and the battery says so rather than accusing you.

Answer the unauthorized probes with `401`. Closing the connection is not
rejecting: the battery records it as a binding failure with the transport
error named, because a request the contract says you MUST reject with a
status did not get one.

## What you get, and what you must not expect

**The platform is all you can reach, and it is three doors.** Each is
opened by the invocation's **run token** — the bearer the chassis sent on
`POST /v1/runs` — so what reaches any of them expires with the phase. The
long-lived credentials a container holds are its own agent key, which by
itself buys no model call, and any tool secret it keeps in its own
environment; or it asks for a tool secret per run, with `secret_get`.

| Door | Address | Credential | What for |
|---|---|---|---|
| run-scoped MCP server | `run.mcp.url`, in the POST body | the bearer | `kb_search`, `run_store_get` / `_set`, `audit_log`, `redact`, `config_get`, `secret_get` |
| OTLP relay | `OTEL_EXPORTER_OTLP_ENDPOINT` | the bearer | your interior spans and log records |
| LLM gateway | `LIBRERUN_GATEWAY_URL` / `OPENAI_BASE_URL` | the run token, in `X-LibreRun-Run-Token` — plus your agent key as the bearer when a client insists on an API key | model calls; `GET /v1/models` on the key alone |

The `agents` network is internal, so nothing else is reachable — not
Vector, not Postgres, not the Internet. `network.egress: true` in the
manifest plus `egress` in the fragment is the documented opt-out.

- **No provider key is yours to hold.** The gateway holds them, in its
  own process, and resolves your step to a provider and a model from the
  tenant's admin configuration at request time — so an admin changes the
  model in the UI and your container is not rebuilt, restarted or even
  told. `OPENAI_API_KEY` in your fragment is your **LibreRun agent key**;
  a provider key presented there is refused (`401 agent_key_invalid`).
  That key alone cannot buy a model call — `401 run_token_required`, in
  every mode, single-tenant demo included — because one container serves
  every tenant and a key that names the agent cannot say whose data a
  call is about or which run pays for it. The run token can, and on the
  SDK path it is the only credential sent; the agent key is there for
  the client libraries that insist on an API key.
- **`docker logs` is empty by construction.** `print()` and `logging`
  inside an invocation reach the platform as log records of that
  invocation through the SDK; a write with no invocation is dropped.
- **Everything you hand the chassis is walked.** Output, audit and
  run-store arguments, progress step ids, event text, and everything
  you export: string positions redacted, keys and numbers checked, a
  flagged one refused — `pii_in_output` ends the run, `PiiRefused`
  fails the call, a flagged span is stripped to its identity. Carry an
  epoch under a time-named key or as an ISO string; a phone number as
  a JSON number is refused wherever it sits.
- **The deadline is real.** `deadline_seconds` in the POST body is when
  the chassis fails the phase; the SDK cancels your handler then.

## What the contract is (other languages)

Four HTTP endpoints, one bearer token per invocation, SSE for events —
`docs/authoring/Run_Contract_v1.md` is normative; in short:

| Endpoint | You |
|---|---|
| `GET /healthz` | answer `200` with any JSON |
| `POST /v1/runs` | bind the bearer to the invocation, read `phase`, `input`, `prior_output`, `user_edits`, `deadline_seconds`, `run.mcp.url`, adopt the `traceparent` header, answer `201 {"invocation_id": …, "run_id": …}` |
| `GET /v1/runs/{id}/events` | with the same bearer only; stream `progress` / `log` events and exactly one `completed {"output": {…}}` or `failed {"error": …}` |
| `GET /v1/runs/{id}/output` | `200 {"output"}` after completion, `404` before, `409` after failure |

Capabilities are the MCP `tools/call` JSON-RPC endpoint at
`run.mcp.url` with the same bearer (`kb_search`, `run_store_get`,
`run_store_set`, `audit_log`, `redact`, `config_get`, `secret_get`;
`-32002` without the grant — `config_get`, your own steps and settings,
needs none, and neither does `secret_get` — `-32003` when the walk
refuses). `secret_get {name}` answers `{value}`: one of the tool secrets
your manifest declares in `secrets[]`, this tenant's value, else every
tenant's default, never the platform's own environment; `-32005
secret_not_declared` for a name the manifest does not declare, and
`-32006 secret_not_set` for a declared one nobody has set, which an
agent reads as "no key". The value is the invocation's alone: never put
it in an event, the output or your telemetry. Model calls are the
gateway's OpenAI-compatible ingress at `OPENAI_BASE_URL`: `POST
/v1/chat/completions` with `model: "librerun/<your step id>"`, your
agent key as the bearer and the run token in `X-LibreRun-Run-Token` —
any OpenAI client library does it with no LibreRun code, and
`docs/authoring/LLM_Gateway.md` is that surface's reference. Telemetry
is OTLP/HTTP protobuf to
`{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces` and `/v1/logs` with the same
bearer, every span and record carrying the trace id from the
`traceparent` you received (`403 trace_mismatch` otherwise), one request
per token. The SDK is the reference implementation for Python, and the
echo agent next to it the smallest complete example
(`backend/agents/_examples/echo_container/`);
`backend/agents/_examples/llamaindex_summarize/` is the same SDK
carrying a LlamaIndex Workflow. For TypeScript there is no package yet
— `@librerun/agent` is v1.1 (L20) — and
`backend/agents/_examples/vercel_ai_answer_ts/server.ts` is the
reference server instead: the whole of this table in one file, on
`node:http`, with the Vercel AI SDK making the model call. Copy it, or
read this table and `docs/authoring/Run_Contract_v1.md` and serve the four
endpoints directly. No gRPC transport exists (L27).
