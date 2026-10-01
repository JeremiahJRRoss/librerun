# __AGENT_NAME__

A TypeScript container agent that serves Run Contract v1 itself,
scaffolded by `librerun init __AGENT_ID__ --template container-ts` from
the reference server (`backend/agents/_examples/vercel_ai_answer_ts/server.ts`).
There is no LibreRun package in it: `@librerun/agent` is v1.1, and until
it ships this file — four endpoints on `node:http`, the Vercel AI SDK
making the model call — is the documented starting point for TypeScript.

| File | What it is |
|---|---|
| `agent.yaml` | the manifest: id, `runtime: container` with the service's URL, one phase, `structured` output, the `llm` and `pii` grants, the `answer` step's model defaults |
| `server.ts` | the whole agent: the four Run Contract endpoints, the bearer bound to the invocation, one `generateText` per invocation |
| `input_schema.json` | the intake form (a container cannot serve its schema from code) |
| `scenarios/demo.json` | the sample the new-run page offers |
| `Dockerfile`, `package.json`, `tsconfig.json` | the image (exact pins, `--omit=dev`) and `npm run typecheck` |

`librerun init` also appended a service `__SERVICE__` to
`agents.compose.yaml` — the gateway environment, this agent's key
(`__KEY_VARIABLE__` in `.env`), the internal `agents` network, the
`librerun.agent_id` label, no persisted logs — and provisioned the key.

## Run it

```bash
librerun up                                  # builds the image, rebuilds the backend so the manifest is discovered
librerun run --agent __AGENT_ID__ --wait     # submits scenarios/demo.json and follows the run
librerun battery --agent __AGENT_ID__        # the Run Contract battery, against the running container
cd backend/agents/__PACKAGE__ && npm install && npm run typecheck   # the type checker (tsx strips types without checking them)
```

The container is the artifact: after an edit, `librerun up` rebuilds it
and the battery drives the new one. It exports no telemetry of its own
(the package that would is v1.1); its work is still in the run's one
trace — the chassis phase span and the gateway's LLM span under it.

## Change the model without touching code

Admin → Agents → __AGENT_NAME__ → Configuration: the `answer` step's
provider, model, temperature and limits are this tenant's data (L25,
D13). The next run uses them; the container is not rebuilt, restarted or
told.

## Tool secrets

A key of this agent's own for a service it calls — a search API, a
vector store — is a tool secret; a model provider's key never is (the
gateway alone holds those). Uncomment the `secrets:` line in
`agent.yaml`, name it there, and read it in `server.ts` with
`await secretGet(mcpUrl, bearer, "search_api_key")`: this tenant's
value, else every tenant's default, over the run's MCP `secret_get`
tool, or `null` while nobody has set it (`-32006 secret_not_set`) — the
agent then carries on without the service; any other refusal throws,
naming its code. A tenant admin sets this tenant's value on the agent
page's Secrets tab, a platform admin every tenant's default, and neither
is ever shown again. The value is the invocation's alone: never log it,
emit it or put it in the output, which the chassis persists
(`docs/authoring/Container_Agents.md`).

## Break it on purpose, to see what the battery says

In `server.ts`, make the phase output a list instead of an object:

```ts
  inv.output = ["broken"] as unknown as Record<string, unknown>;
```

`librerun up`, then `librerun battery --agent __AGENT_ID__`: red, and the
report says the `completed` event carried an output that is not a JSON
object — the chassis could not have stored it. Put the object back,
`librerun up`, and it is green again.

## Keyless, and honest about it

With `LIBRERUN_STUB_LLM=true` the gateway answers the `answer` step from
the fixture this server hands it (`librerun.stub_reply`, added to the
request body by the wrapped `fetch` in `server.ts`, because the AI SDK
sends no extension fields itself), and the output's `answer_source` says
`stub-fixture`; with a provider key in `gateway.env` it says `model`.
When no model answers at all it says `rules`. The same wrapper reads the
gateway's `librerun.provider` off the reply, which the SDK does not
surface either — the two are what make the label a fact rather than a
guess.

## Rotate its key

```bash
librerun key rotate __AGENT_ID__             # new key on the main line, the old one kept as _PREVIOUS; gateway and container recreated
librerun key rotate __AGENT_ID__ --finish    # the old key retired; only the gateway recreated
```
