# __AGENT_NAME__

A Python container agent on the LibreRun agent SDK, scaffolded by
`librerun init __AGENT_ID__ --template container-python`.

| File | What it is |
|---|---|
| `agent.yaml` | the manifest: id, `runtime: container` with the service's URL, one phase, `structured` output, the `llm` and `pii` grants, the `answer` step's model defaults |
| `agent.py` | the handler `librerun_agent.serve` turns into a Run Contract v1 server |
| `input_schema.json` | the intake form (a container cannot serve its schema from code) |
| `scenarios/demo.json` | the sample the new-run page offers |
| `Dockerfile`, `requirements.txt` | the image: the SDK from this repository's tree, then your dependencies |

`librerun init` also appended a service `__SERVICE__` to
`agents.compose.yaml` — the gateway environment, this agent's key
(`__KEY_VARIABLE__` in `.env`), the internal `agents` network, the
`librerun.agent_id` label, no persisted logs — and provisioned the key.

## Run it

```bash
librerun up                                  # builds the image, rebuilds the backend so the manifest is discovered
librerun run --agent __AGENT_ID__ --wait     # submits scenarios/demo.json and follows the run
librerun battery --agent __AGENT_ID__        # the Run Contract battery, against the running container
```

The container is the artifact: after an edit, `librerun up` rebuilds it
and the battery drives the new one. (`docker logs` on it is empty by
construction — its `print()` and `ctx.log()` lines are records of the
run, under its trace.)

## Change the model without touching code

Admin → Agents → __AGENT_NAME__ → Configuration: the `answer` step's
provider, model, temperature and limits are this tenant's data (L25,
D13). The next run uses them; the container is not rebuilt, restarted or
told.

## Tool secrets

A key of this agent's own for a service it calls — a search API, a
vector store — is a tool secret; a model provider's key never is (the
gateway alone holds those). Uncomment the `secrets:` line in
`agent.yaml`, name it there, and read it in `agent.py` with
`await _tool_secret(ctx, "search_api_key")`: this tenant's value, else
every tenant's default, over the run's MCP `secret_get` tool, or `None`
while nobody has set it — the agent then carries on without the
service. A tenant admin sets this tenant's value on the agent page's
Secrets tab, a platform admin every tenant's default, and neither is
ever shown again. The value is the invocation's alone: never log it or
put it in the output, which the chassis persists (`docs/authoring/SDK.md`,
`docs/authoring/Container_Agents.md`).

## Break it on purpose, to see what the battery says

Make the handler in `agent.py` return a list instead of an object:

```python
    return ["broken"]
```

`librerun up`, then `librerun battery --agent __AGENT_ID__`: red, and the
report says the invocation failed — the SDK refuses a phase output that
is not a JSON object, because the chassis could not have stored it. Put
the `return {…}` back, `librerun up`, and it is green again.

## Rotate its key

```bash
librerun key rotate __AGENT_ID__             # new key on the main line, the old one kept as _PREVIOUS; gateway and container recreated
librerun key rotate __AGENT_ID__ --finish    # the old key retired; only the gateway recreated
```
