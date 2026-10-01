# Run a LangGraph agent on LibreRun in 10 minutes

*Blueprint B16. The first adapter of the family (decision L11) — the
pattern every later one follows. Moved here from `docs/LangGraph_Quickstart.md`
at S6; `docs/authoring/Quickstart.md` is the hour path that starts with
`librerun init --template langgraph`.*

You have a compiled LangGraph graph. LibreRun gives it a schema-driven
intake form, a human approval gate, live progress, persistence, PII
redaction, feedback capture, and a trace link — without changing the
graph. This page is the whole integration.

---

## 1. What you write (three files)

```
my_agent/
  agent.yaml          # the manifest: id, phases, output mode, capabilities
  agent.py            # binds your graph(s) to the manifest's phases
  input_schema.json   # JSON Schema — becomes the intake form
  scenarios/demo.json # optional: a one-click demo input
```

A complete, runnable version of all four lives in
`backend/agents/_examples/langgraph_triage/`. The quickest start is the
CLI's template, which is that example reduced to one phase and one model
call:

```bash
librerun init my-agent --template langgraph     # backend/agents/my_agent/, ready to edit
```

## 2. `agent.py` — the only code the adapter needs

Install the adapter first — it is a distribution of its own, so the
import below resolves:

```bash
pip install ./backend/adapters        # provides librerun-langgraph
```

Working inside this checkout without installing anything? The package
is importable at its in-tree path, `adapters.librerun_langgraph`. The
example carries a two-line fallback so it works either way.

```python
from librerun_langgraph import LangGraphAgent

class MyAgent(LangGraphAgent):
    def __init__(self):
        super().__init__(
            agent_id="my-agent",              # must equal agent.yaml's id
            display_name="My Agent",
            description="What it does",
            input_schema=my_schema,           # dict, JSON Schema
            graph=my_compiled_graph,          # StateGraph(...).compile()
        )
```

That is a single-phase agent. For phases the user steps through — with
an approval gate between them — hand the adapter one graph per phase:

```python
            graphs={"analyze": triage_graph, "investigate": deep_graph},
```

and declare the same names in `agent.yaml`:

```yaml
phases:
  - name: analyze
  - name: investigate
    approval: true        # parks the run until a human approves
```

The gate is the chassis'. **LangGraph never sees it** — the graph for a
phase runs to completion, the platform holds the run, and the next
graph starts when a human says so. That is deliberate: human review is
a product concern, and modelling it inside a graph would make every
adapter reimplement it.

Each node is a progress row on the run page, reported as
`<phase>:<node>`. Give the rows the labels the page should show with
`phases[].steps[]` (blueprint S7); a row declared nowhere still shows,
under its raw id, and `framework: langgraph` puts the badge on the
agent's card:

```yaml
framework: langgraph
phases:
  - name: analyze
    steps:
      - id: "analyze:classify"
        label: Classify the incident
      - id: "analyze:summarise"
        label: Summarise the triage
```

## 3. What your graph gets, and what it owes

The adapter writes these into the graph's initial state:

| Key | What it is |
|---|---|
| `user_inputs` | the intake form's values, already PII-redacted |
| `prior_analysis` | the previous phase's structured output (`None` on the first) |
| `user_edits` | free-text the user added when re-running a phase |
| `run_id` | the run's id, as a string |
| `case_id` | the same id under its pre-1.0 name — declared and injected for one release (blueprint S1) so graphs written against the earlier contract keep working; gone at v1.1. New graphs declare `run_id` |

**Declare every one you want to read** in your state schema. LangGraph
filters state through that schema and *silently drops* anything
undeclared — so a `TriageState` without `user_edits` doesn't fail, it
just leaves your node unable to see the edit a user typed.

All of them are plain JSON data, and that is a rule rather than a
coincidence: **state is what a checkpointer persists.** The capability
façade is therefore *not* in state — it arrives in the run config, §4.

Your graph owes exactly one thing back: an answer. The adapter looks for
`structured`, then `result`, then `output` in the final state; if it
finds none, the whole final state (minus the keys above) becomes the
structured output. For an agent that renders its own report, put an HTML
string in `report_html` and set `output.mode: html_report` in the
manifest.

Whatever comes back is coerced to something JSON can hold, because the
platform stores it in a JSONB column. LangChain message objects become
their fields, so a `MessagesState` graph that declares no output key
still persists; anything with no JSON form at all degrades to its
`repr` rather than failing the run at the last step.

**Checkpointers work.** Compile with one if you want durability — the
adapter passes `configurable.thread_id` on every call, scoped per run
*and* phase (`{run_id}:{phase}`), so each phase's graph gets its own
thread and re-running a phase resumes that phase's state.

## 4. Platform capabilities inside a node

Declare what you need in the manifest — the list is an enforced grant,
not documentation:

```yaml
capabilities: [kb, run_store, audit]
```

Then use them from any node. Declare a second `config` parameter —
LangGraph passes it to any node that asks — and read the façade from it:

```python
from librerun_langgraph import capabilities_of

async def gather_context(state, config):
    caps = capabilities_of(config)
    if caps is None or not caps.granted("kb"):
        return {"context": ["no knowledge-base capability granted"]}
    summary = (state.get("prior_analysis") or {}).get("summary", "")
    hits = await caps.kb.search(queries=[summary], top_k=3)
    return {"context": [str(h) for h in hits]}
```

**Ask `granted`, and do not reach for `getattr`.** The obvious spelling,
`kb = getattr(caps, "kb", None)` with a branch on `None`, does not work
and fails in the worst way — it looks like it works. The façade raises
`CapabilityNotGranted`, which is a `RuntimeError`, and `getattr`'s
default only swallows `AttributeError`, so the branch you wrote can
never run and the node dies instead of degrading. The shipped example
was written that way and nothing noticed, because its manifest happened
to grant `kb`. `granted(name)` is the predicate the façade exposes for
exactly this question; it never raises.

Note where `summary` comes from. Each phase is a **separate graph**, so
a channel written by the analyze graph does not exist in the investigate
graph — only `prior_analysis` crosses the boundary. Reading
`state["summary"]` in a later phase silently searches for `""`, which is
a bug that runs clean and returns nothing.

**Config, not state, and it matters.** A checkpointer serializes state,
and this façade holds live chassis handles — LangGraph's serializer
refuses it outright (`Type is not msgpack serializable`), so a graph
compiled with a checkpointer would die on its first checkpoint. It is
also scoped to one run, carrying that run's run id, tenant id and
grants; a checkpoint resumed later must not act under a scope captured
when it was written.

`kb`, `run_store`, `audit` and `progress` are served in-process here, and
the *same* surface is reachable over MCP for container agents (blueprint
B13) — so a graph that later moves into a container keeps working.

`llm` is the exception and does not travel that way: `routers/mcp.py`
serves the KB, run-store, audit and PII tools only, and says so. A
container agent reaches the gateway over its own OpenAI-compatible HTTP
surface, with its agent key and the invocation's run token, rather than
through `caps.llm` — so model calls are the one part of a graph that
changes shape when it moves into a container. Asking for a capability the manifest does not grant raises
`CapabilityNotGranted`; write your node to degrade rather than die when
a capability is granted but unconfigured, as the example does.

## 5. Calling a model

A node calls a model through the `llm` capability, and it names a
**step**, never a model:

```yaml
capabilities: [kb, llm]

llm:
  steps:
    - id: classify
      label: "Severity and signals from the incident report"
      provider: "openai"       # a DEFAULT, not a decision
      model: "gpt-4o-mini"
      temperature: 0.0
```

```python
answer = await caps.llm.complete(
    "classify",                      # the step id, not a model name
    [{"role": "user", "content": report}],
    response_format={"type": "json_schema", "json_schema": {
        "name": "classify", "strict": True, "schema": CLASSIFY_SCHEMA}},
    librerun={"stub_reply": json.dumps(my_fallback_answer)},
)
```

Three things are doing work there:

- **the step id.** Which model answers, at what temperature and within
  what budget is the tenant's configuration, resolved by the gateway per
  request — that is what makes a model change an edit in the admin UI
  instead of a redeploy (L25, D13). An id the manifest does not declare
  is refused (`400 unknown_step`), and `llm.steps` without the `llm`
  grant is refused at manifest load.
- **`response_format`.** Ask for JSON and you get JSON to read, rather
  than prose to parse.
- **`librerun.stub_reply`, your own keyless fixture.** With no provider
  key configured the gateway answers from its stub, and absent a fixture
  it synthesises an instance of your schema — valid and deterministic,
  but always the *first* member of an enum, so every incident would come
  back with your first severity. Hand it your fallback answer instead
  and a keyless run shows something true. The gateway honours it only
  when the resolved provider is the stub, so it is inert the moment a
  real key exists and can never make a credentialled deployment answer
  for a provider it did not call. Your fixtures stay in your agent,
  which is where they belong (L13).

Make the call an improvement on a deterministic answer rather than a
dependency of it, and say in your output **who** answered.

Catching `Exception` around the call is right — a gateway that is down
should cost you a model's opinion, not the phase. **Do not widen it to
`BaseException`.** The platform enforces `phases[].deadline_seconds`
with `asyncio.timeout`, which works by cancelling your task, and
`CancelledError` is a `BaseException`: catch that and every deadline
becomes a silent fall back to your deterministic answer, with the run
reporting `complete` after its budget ran out. `Exception` is safe here
for a reason worth knowing — httpx propagates a cancellation untouched
rather than mapping it to an `HTTPError` the way it maps a timeout, so
the clause never sees it.

That last part is subtler than it sounds. Keyless, the gateway returns
your fixture as an ordinary successful completion — same shape, same
status, parses the same way — so "the call succeeded" and "a model
decided" are different questions and the content cannot tell them apart.
The answer is in the reply: the gateway names the resolved provider in
its own `librerun` envelope, and keyless that reads `stub`.

```python
provider = (response.get("librerun") or {}).get("provider")
```

Label from that, not from whether the call threw. And when the envelope
is missing, say *unattributed* rather than *model*: defaulting to
"a model answered" is how a gateway that stopped sending the envelope
would quietly turn your fixtures back into judgements.

**Validate the reply before you use it, with the validator the chassis
already uses.** `response_format` is a request the gateway *forwards*;
it cannot make a provider honour it, and D13 exists so an admin can
retarget your step to one that honours it loosely. `jsonschema` is a
pinned chassis dependency and `intake.py` already validates agent input
with it, so an in-process agent has it for free:

```python
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

try:
    Draft202012Validator(CLASSIFY_SCHEMA).validate(answer)
except ValidationError:
    answer = None          # fall back to your deterministic answer
```

Do not hand-roll this. Checking the one field your branch reads and
then consuming the rest is how a valid `severity` beside an integer
`signals` raises `TypeError` and kills the phase — the opposite of the
fallback you promised. A container agent does not share this process,
so it declares `jsonschema` in its own image; that is the only thing
that changes across the runtime boundary.

The shipped example does all of this in about sixty lines:
`backend/agents/_examples/langgraph_triage/`.

## 6. Progress and traces come free

The adapter drives your graph with `astream(..., stream_mode="updates")`,
so **each node completion is a progress event** on the run page — no
callbacks to wire. It also opens a span per phase, nested under the
chassis' run span, and records each node completion as a timestamped
event on it:

```
BackgroundTask _runner
└── investigate                  (chassis phase span)
    └── langgraph:investigate    (adapter)
        • node:gather_context    (event)
        • node:draft             (event)
```

**Why events and not spans per node.** The update stream reports a node
only *after* it has finished, so a span opened at that moment would
enclose nothing and advertise a near-zero duration under the node's
name — an invitation to read timings that were never measured. Events
carry an accurate timestamp and claim nothing more. Spans that genuinely
surround node execution need instrumentation via LangGraph's callbacks;
that is a planned follow-up, and until it lands the adapter reports only
what it can observe. Spans your own node code opens still nest under the
phase span as normal.

## 7. Run it

Under compose, the CLI rebuilds the backend image so the new directory
is discovered, then submits the agent's own sample:

```bash
librerun up                                  # rebuild + start; the agent appears on the new-run page
librerun run --agent my-agent --wait         # scenarios/demo.json, followed to `complete`
```

Locally, next to the demo agent, from a checkout — both roots, which is
what the demo runs:

```bash
LIBRERUN_AGENTS_PATH=agents:agents/_examples uvicorn app.main:app --reload
```

or pip-install it as a package that declares a `librerun.agents` entry
point (blueprint B12) — no directory placement needed.

## 8. Prove it with the adapter kit

Before you ship, run the battery every adapter must pass. From the CLI,
against the directory on disk (a one-off backend container with your
agent's directory bind-mounted over the image's copy, so it tests the
file you just edited):

```bash
librerun battery --agent my-agent
```

From Python, in a test of your own:

```python
from adapter_kit import Scenario, run_battery

result = await run_battery(MyAgent(), Scenario.from_file("scenarios/demo.json"),
                           phases=["analyze", "investigate"],
                           output_mode="structured")   # your agent.yaml's output.mode
assert result.passed, result.summary()
```

Pass `output_mode`. The two modes render from **different fields**, and
the run page honours your manifest rather than taking whatever it finds:
an agent declaring `structured` that fills only `report_html` produces a
*complete* run whose results view says "No structured result available."
Without the mode the battery can only ask the weaker question — did
anything renderable come back.

Its core is the three things the chassis cannot do for you: your
scenario satisfies your own input schema, you stream progress while
working, and you emit spans — the latter two **per phase**, so an agent
that reports during its first phase and goes dark in its second does not
pass.

Around that it holds you to what the chassis will actually accept:

- **Your scenario goes through intake first** — validated against your
  schema, then PII-redacted exactly as `POST /runs` does, so your nodes
  see what they would see in production. Including the surprise that
  Presidio rewrites the timestamps in a pasted log as
  `[REDACTED_DATE_TIME_1]`. `result.redactions` reports how many were
  applied.
- **Your output has to survive persistence** — the chassis stores it in
  a JSONB column, so a set, a `UUID` or a `NaN` nested anywhere inside
  an otherwise ordinary dict is a conformance failure here rather than a
  dead run at COMMIT. So is finishing with an empty result and no
  report: the run page would say "No structured result available."
- **It stops where production stops.** A rejected scenario, a failed
  redaction, an unsuccessful status, output the runner cannot coerce —
  each ends the battery exactly where the real run would end, so it
  never bills you for a phase that could not have happened.

`backend/tests/test_adapter_kit.py` runs it against the example — and
against deliberately broken agents, so the battery is known to fail when
it should.

---

## Adapter roadmap

LangGraph is the first, and the kit above is what makes each next one
cheap. These are **post-blueprint work items, not committed
deliverables** — listed so the order is deliberate rather than
whatever arrives first:

| Adapter | Tier | Note |
|---|---|---|
| **LangGraph** | in-process | shipped at B16 — the template |
| OpenAI Agents SDK | in-process | supersedes Swarm; no separate Swarm adapter |
| CrewAI | in-process | crew → phases mapping is the open design question |
| LlamaIndex | in-process | workflow events map onto progress the same way |
| Genkit | container | via Run Contract v1 (B12a) |
| Google ADK | container | via Run Contract v1 |
| Claude Agent SDK (TS) | container | via Run Contract v1 + MCP capabilities (B13) |

The in-process tier wraps a Python object, as here. The container tier
speaks the HTTP+SSE Run Contract instead, and reaches the same
capabilities over MCP — which is why no adapter needs the chassis to
learn its framework.
