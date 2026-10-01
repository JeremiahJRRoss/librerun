# __AGENT_NAME__

A LangGraph graph running inside the LibreRun backend, scaffolded by
`librerun init __AGENT_ID__ --template langgraph`.

| File | What it is |
|---|---|
| `agent.yaml` | the manifest: id, one phase, `structured` output, the `llm` and `pii` grants, the `answer` step's model defaults |
| `agent.py` | the graph (`answer` → `summarise`) and the `__CLASS_NAME__` class discovery instantiates |
| `input_schema.json` | the intake form |
| `scenarios/demo.json` | the sample the new-run page offers |

## Run it

```bash
librerun up                                  # rebuilds the backend image so the agent is discovered
librerun run --agent __AGENT_ID__ --wait     # submits scenarios/demo.json and follows the run
librerun battery --agent __AGENT_ID__        # the adapter conformance battery, against this directory
```

The battery runs in a one-off backend container with **this directory
bind-mounted over the image's copy**, so it tests the file you just
edited — no rebuild between an edit and a battery. A run through the
platform (`librerun run`, the new-run page) uses the image, so after an
edit `librerun up` first.

## Change the model without touching code

Admin → Agents → __AGENT_NAME__ → Configuration: the `answer` step's
provider, model, temperature and limits are this tenant's data (L25,
D13). The next run uses them; nothing is rebuilt or restarted.

## Break it on purpose, to see what the battery says

Make `summarise` in `agent.py` return an empty result:

```python
    return {"structured": {}}
```

`librerun battery --agent __AGENT_ID__` goes red and names the reason:
the phase produced an empty structured result the run page could not
render. Put the three keys back and it is green again.

## Keyless, and honest about it

With `LIBRERUN_STUB_LLM=true` the gateway answers the `answer` step from
the fixture this graph hands it (`librerun.stub_reply`), and the output's
`answer_source` says `stub-fixture`; with a provider key in
`gateway.env` it says `model`. When no model answers at all it says
`rules`. A fixture is never displayed as a judgement.
