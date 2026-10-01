# LibreRun — Installing an Agent

How to add a new pluggable agent to a LibreRun deployment. This is the
mechanical "get it to load" doc; see
[`docs/authoring/Agents_Design.md`](./Agents_Design.md) for the behavioural
contract.

---

## Purpose & audience

LibreRun is a **chassis for pluggable agents**; VITA is the first agent
shipped on it, not a privileged one. An agent installs **two ways**, with
the identical runtime contract either way:

1. **Directory install** — one self-contained directory under an
   agents directory (`LIBRERUN_AGENTS_PATH`: one directory, or several
   separated by `:`; default `backend/agents/<name>/`).
2. **Pip install** — a package whose distribution declares a
   `librerun.agents` entry point (VITA ships this way too:
   `pip install ./backend/agents/vita_v1` installs the `vita-agent`
   distribution).

The shell discovers agents from both sources at startup and the
frontend (`AgentPicker`, `DynamicForm`, `AgentConfigEditor`) renders
them with no changes. No frontend build, no Alembic migration, no
router edit.

Read this doc if you are:

- An engineer adding a new agent to this repo.
- A platform operator trying to diagnose why an agent is not appearing
  in `/runs/new` or `/admin`.

---

## Prerequisites

- Backend running locally (`./compose.sh up -d` for postgres + redis,
  then `cd backend && uvicorn app.main:app --reload --port 8000`). See
  [`docs/platform/Install.md`](../platform/Install.md) for first-time setup.
- Python 3.12 on your `PATH`.
- The `librerun` CLI (`pipx install "git+https://github.com/JeremiahJRRoss/librerun#subdirectory=cli"`),
  whose `init` command scaffolds an agent from a template —
  [`docs/authoring/Quickstart.md`](./Quickstart.md) — and a look
  at [`backend/agents/vita_v1/agent.py`](../../backend/agents/vita_v1/agent.py)
  (the worked example of a hand-written `AgentProtocol` agent).

---

## Quick start

From the repo root:

```bash
# 1. Scaffold an agent (the langgraph template runs in-process; the two
#    container templates are a Python SDK agent and a TypeScript server)
librerun init my-agent --template langgraph

# 2. Edit backend/agents/my_agent/agent.yaml — the manifest (required):
#    id, name, description, runtime, the phase list, output mode, the
#    grants and the llm.steps defaults. Field reference:
#    docs/authoring/Manifest.md.

# 3. Edit backend/agents/my_agent/agent.py. The class's agent_id must
#    equal the manifest id and its display_name the manifest name; both
#    are also emitted on every span this agent produces — see
#    "Observability" below — so pick values you're happy to filter by.

# 4. (Optional) Drop demo scenario JSON files into
#    backend/agents/my_agent/scenarios/ (the template ships one).

# 5. Rebuild and restart the backend.
#    Containers:  librerun up      (or ./compose.sh --profile app up -d --build backend)
#    Local dev:   Ctrl-C, re-run uvicorn.

# 6. Verify — the agent is registered.
curl -s http://localhost:8000/api/v1/agents | jq
```

You should see your agent in the response and a log line like:

```
agent_registered agent_id=my-agent module=agents.my_agent display_name='My Agent'
```

---

## Directory layout

Everything an agent needs lives under `backend/agents/<name>/`.

| File | Required? | Purpose |
|------|-----------|---------|
| `agent.yaml` | **required** | The manifest (blueprint B7): id, name, runtime, phase list, output mode, feedback sections, scenarios dir, UI hints, grants, `llm.steps`. Field reference: [`docs/authoring/Manifest.md`](./Manifest.md); the behavioural contract: [`docs/authoring/Agents_Design.md`](./Agents_Design.md). |
| `agent.py` | recommended | Holds the `AgentProtocol` subclass. The registry prefers this file. |
| `__init__.py` | required if no `agent.py` | Marks the directory as a Python package. Can be empty. |
| `config.json` | optional, legacy | Nothing the platform edits. An agent's settings are declared in `agent.yaml` (`settings[]`, K5a) and its LLM steps in `llm.steps[]`, both valued per tenant on the agent's admin page; a file like this is the agent's own. The demo agent declares `settings[]` since K5b and ships no `config.json`. |
| `scenarios/` | optional | Demo scenario JSON files served at `GET /agents/{id}/scenarios` and offered as intake prefill. See [`backend/agents/vita_v1/scenarios/`](../../backend/agents/vita_v1/scenarios/). |
| `report.html` | optional | Jinja template for rendered reports (see [`backend/agents/vita_v1/report.html`](../../backend/agents/vita_v1/report.html)). |
| `README.md` | optional | Agent-specific notes. |
| `tests/` | optional | Agent-owned pytest suite. Layout: see [`backend/agents/vita_v1/tests/`](../../backend/agents/vita_v1/tests/). |
| Any other module | optional | Step modules, prompts, helpers, custom LLM / search services. |

Two hard requirements: a valid `agent.yaml`, and one top-level class in
`agent.py` (or `__init__.py` if no `agent.py` exists) that inherits from
`app.agents.protocol.AgentProtocol`, can be instantiated with no
arguments, and whose `agent_id` equals the manifest `id`.

A minimal agent is three files:

```
backend/agents/my_agent/
├── __init__.py        # empty
├── agent.yaml         # the manifest (settings[] included) — docs/authoring/Manifest.md is the field reference
└── agent.py           # class MyAgent(AgentProtocol): ...
```

---

## Runtime `container` — agents in any language (blueprint B12a)

A `runtime: container` agent is a **running container (or any process)
speaking Run Contract v1** — four HTTP+SSE endpoints documented in
[`docs/authoring/Run_Contract_v1.md`](./Run_Contract_v1.md). No Python, no import:
the agent directory ships only the manifest and its assets:

```
backend/agents/my_container_agent/
├── agent.yaml           # runtime: container + container.url
├── input_schema.json    # REQUIRED — the intake schema (no code to ask)
└── scenarios/           # optional demo scenarios, same as ever
```

```yaml
runtime: container
container:
  url: ${MY_AGENT_URL}   # compose service name or any preconfigured URL
input_schema: input_schema.json
```

The chassis expands `${VAR}` references from its environment at
discovery and registers a proxy that drives the contract per phase —
intake wizard, approval gates, structured results, feedback, and one
trace per run all work exactly as for python-package agents. The
chassis **addresses** containers; it does not launch or schedule them
(compose/systemd/k8s own the lifecycle). Start from the reference agent
in
[`backend/agents/_examples/echo_container/`](../../backend/agents/_examples/echo_container/)
(Dockerfile included; `docs/authoring/Container_Agents.md` is the
walkthrough).

**Under compose (S4):** agent containers are services in
[`agents.compose.yaml`](../../agents.compose.yaml), which `compose.yaml`
includes (Compose v2.20+ / podman-compose 1.1+). The reference echo
service there is the shape to copy:

```yaml
services:
  my-agent:
    build: { context: ./path/to/my_agent }
    labels: { librerun.agent_id: my-agent-v1 }   # how the CLI maps agent → service
    environment:
      OTEL_EXPORTER_OTLP_ENDPOINT: http://backend:8000/api/v1/_o/otlp   # the chassis relay
    networks:
      - agents          # internal: the chassis is all the container can reach
      # - egress        # only with `network.egress: true` in agent.yaml
    logging: { driver: none }   # docker logs <agent> is empty by construction
```

The `agents` network is `internal: true`: the backend sits on it beside
its default network, Vector, Postgres and Redis do not, and an internal
network has no route off the host — so an agent reaches the Run
Contract's MCP server and OTLP relay at `http://backend:8000` and
nothing else, not Vector, not the Internet. An agent that needs outbound
access adds `egress` to its service **and** declares
`network.egress: true` in its manifest; the admin page shows the opt-out.
`logging: driver: none` means Docker persists no agent stdout or stderr:
the Python SDK turns a `print()` into a run-plane log record under the
invocation's trace, delivered through the relay, and a container
without the SDK uses the Run Contract `log` event.

Container agents that use chassis capabilities (blueprint B13) reach
them at the `run.mcp.url` advertised in each Run Contract POST, which
the chassis builds from **`LIBRERUN_PUBLIC_URL`**. That value must be
routable *from the agent's container*: compose defaults it to
`http://backend:8000` (the service name), and the `http://localhost:8000`
dev default only works when the agent shares the host's network
namespace. A localhost value inside a container points at the agent
itself.

---

## Install mode 2: pip package (blueprint B12)

An agent can install with **no agents-directory presence at all**. The
recipe, using VITA's own packaging
([`backend/agents/vita_v1/pyproject.toml`](../../backend/agents/vita_v1/pyproject.toml))
as the worked example:

1. The package uses **relative imports internally** (`from .steps import
   …`), so the same code imports as `agents.<name>` in-tree and as a
   top-level package when installed.
2. `pyproject.toml` maps the directory to a top-level package and
   declares the entry point:

   ```toml
   [project.entry-points."librerun.agents"]
   my-agent = "my_agent_pkg"        # entry name = manifest id
   ```

3. `agent.yaml`, the report template, `scenarios/` and any data file of
   the agent's own ship as **package-data** — discovery reads the
   manifest (and serves scenarios) from the installed package directory.
4. Install **into the chassis environment**:
   `pip install ./backend/agents/vita_v1` (or from any path/index). The
   chassis' `app.*` modules are a host requirement, not a pip
   dependency — an agent venv without the chassis can't run it.

At startup the registry enumerates `librerun.agents` entry points,
resolves each to its package directory, and applies **the same manifest
contract** as the directory mode. A directory agent with the same id as
an installed one **wins** (it registers last) — the iterating
developer's checkout beats the installed snapshot.

---

## Discovery rules

Source of truth:
`discover_agents` in
[`backend/app/agents/registry.py`](../../backend/app/agents/registry.py).

- The filesystem roots are `LIBRERUN_AGENTS_PATH` — one directory, or
  several separated by the OS path separator (`agents:agents/_examples`
  is what the demo runs), scanned in order, a later root's agent
  replacing an earlier one with the same id (`agent_register_duplicate`
  is logged). Empty/unset = the `agents/` directory next to `app/`,
  i.e. `backend/agents/` — inside the container image, `/app/agents`. A
  relative value resolves against the process working directory.
- Installed (entry-point) agents are enumerated first, then the agents
  directory — same id → the directory copy wins. Enumeration or load
  failures (`agent_entry_point_enumeration_failed`,
  `agent_entry_point_load_failed`, `agent_entry_point_not_a_module`,
  `agent_entry_point_no_location`) are logged skips, never crashes.
- Agents must live directly under the agents directory. Nested
  directories are ignored.
- Directory names starting with `_` or `.` are **skipped** (the
  bundled examples live under `_examples/`, which is why the demo puts
  that directory on `LIBRERUN_AGENTS_PATH` explicitly).
- The directory must contain either `__init__.py` **or** `agent.py` —
  otherwise it is not a Python package and is skipped silently.
- The directory must ship a **valid `agent.yaml` manifest** (blueprint
  B7). Missing → `agent_manifest_missing`; unparseable or invalid →
  `agent_manifest_invalid`; either way the directory is skipped.
- `runtime: container` manifests validate but are skipped with
  `agent_runtime_unsupported` until the container runner lands
  (blueprint B12a).
- Discovery walks the package and picks the **first** top-level class
  that is a proper subclass of `AgentProtocol` (the base class itself
  is ignored). Classes imported from elsewhere are skipped — only
  classes *defined* in the module count.
- The class is instantiated with no arguments and registered under its
  `agent_id` class attribute, which must equal the manifest `id`
  (mismatch → `agent_manifest_id_mismatch`, skipped). A differing
  `display_name` vs manifest `name` only warns.
- A failing import logs `agent_discovery_failed` with `exc_info=True`
  and moves on. **One bad agent never prevents the others from
  loading, and never crashes the server.**
- A duplicate `agent_id` logs `agent_register_duplicate` and the later
  registration wins.

---

## Startup sequence

From `lifespan` in [`backend/app/main.py`](../../backend/app/main.py):

1. OTEL is initialized at module scope in `main.py` (before the first
   ASGI call — see `app/observability/otel_init.py`), so the tracer
   provider exists before anything else runs.
2. `discover_agents()` — populates the registry.
3. `await get_redis()` — opens the pool.
4. FastAPI starts accepting traffic.

Agents are process-global singletons after step 2. Every router,
background task, and test looks them up by id via
`app.agents.registry.get_agent`.

---

## Observability

When trace export is enabled (`OTEL_EXPORTER_OTLP_ENDPOINT` set, or
`OTEL_DEBUG=true`), `init_otel` builds the OTLP pipeline plus an
`AgentSpanEnricher`
([`backend/app/observability/span_enricher.py`](../../backend/app/observability/span_enricher.py)).
You do not need to emit spans yourself — the enricher wires in
automatically for every registered agent.

**Where spans go.** All spans are exported as standard OTLP to
`OTEL_EXPORTER_OTLP_ENDPOINT` (blank = tracing off; containers default
to the bundled Vector router, `http://vector:4317`), stamped with
`service.name` = `OTEL_SERVICE_NAME` (default `librerun-backend`).
Run feedback is emitted as a structured ``run_feedback`` log event
through the same pipeline (blueprint B5).

**Automatic span tagging.** While an agent's `analyze` or
`investigate` is running, every span created in the process is
stamped server-side with:

- `agent.id`   — your class's `agent_id` (e.g. `my-agent`)
- `agent.name` — your class's `display_name` (e.g. `My Agent`)
- `tenant.id`, `run.id`, `run.number`, `phase`
- `session.id`, `user.id` (mirroring the OpenInference attribute names)

This includes the phase root span, orchestrator step spans, and the
auto-instrumented LLM spans from every instrumentation family —
GenAI-convention (OpenAI, Google GenAI) and OpenInference (Anthropic)
alike. Filter your trace viewer by `agent.id = my-agent` to see
*everything* one agent produced in a trace, including its LLM calls.

**Plane stamp.** Every span additionally carries
`librerun.scope` — `"run"` while your agent's work executes, `"platform"`
otherwise — and every log line the matching `librerun_scope` field, so
operators can route the two planes to different sinks without inferring
from attribute presence. LLM prompt/completion content is captured onto
run-plane spans by default
(`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_AND_EVENT`;
set `NO_CONTENT` in `.env` to opt out). The full contract, including
routing recipes, lives in `docs/authoring/Agents_Design.md` under
"Observability contract".

No code is required in the agent itself — the chassis's
`AgentSpanEnricher` (`backend/app/observability/span_enricher.py`)
stamps these at span start from the run context that
`backend/app/services/agent_runner.py` binds around each phase, and
every phase span is a child of the run's root span `run` — one trace
per run, across the approval gate (`app/observability/run_trace.py`).
If you spawn background work from inside `analyze` / `investigate`,
capture the OTEL context at the call (`opentelemetry.context.get_current()`)
and attach it in the worker so downstream spans stay parented and
tagged.

---

## Verification checklist

After restarting the backend:

- [ ] Backend log contains `agent_registered agent_id=<your-id>`
      (with your manifest's phase list).
- [ ] `GET /api/v1/agents` returns your agent with its manifest fields
      (`phases`, `ui`, `output`, `capabilities`, `has_scenarios`).
      `has_config` is `true` iff the agent page has a Steps, a Settings
      or a Secrets tab: your manifest declares `llm.steps[]`,
      `settings[]` or `secrets[]` (or, until v1.2, a deprecated
      `config_meta()` returns settings).
- [ ] `GET /api/v1/agents/<your-id>/input-schema` returns your JSON
      Schema.
- [ ] If you shipped scenarios,
      `GET /api/v1/agents/<your-id>/scenarios` lists them and the
      intake page offers "Load scenario".
- [ ] `/runs/new` shows your agent in `AgentPicker`. If yours is the
      only registered agent, the picker auto-selects it.
- [ ] `/admin` renders your agent's card.
- [ ] If you declared `llm.steps[]`, `settings[]` or `secrets[]`,
      `/admin/agents/<your-id>/config` shows its Steps, Settings or
      Secrets tab; a value saved on the Settings tab is what the next run
      reads (`caps.config.settings()`, or `ctx.config.settings()` in a
      container), and a secret set on the Secrets tab is what
      `caps.secrets.get` (`ctx.secrets.get` in a container) answers —
      shown there as set, with its fingerprint, never its value.
- [ ] If `OTEL_EXPORTER_OTLP_ENDPOINT` is set, run a phase-1 run and
      confirm spans arrive at your OTLP receiver tagged with
      `agent.id = <your-id>` (or set `OTEL_DEBUG=true` and watch them
      on stderr).

---

## Troubleshooting

| Log event | What it means | Fix |
|-----------|--------------|-----|
| `agent_discovery_skipped reason=directory_missing` | `backend/agents/` does not exist. | Create the directory. |
| `agent_discovery_skipped reason=not_a_directory` | Something at `backend/agents/` is a file, not a directory. | Remove the file. |
| *(silent skip, no log)* | Directory has no `__init__.py` or `agent.py`, or the name starts with `_` / `.`. | Add `__init__.py`, or rename the directory. |
| `agent_manifest_missing` | No `agent.yaml` in the directory. | Write one — `librerun init` scaffolds a complete agent, and [`docs/authoring/Manifest.md`](./Manifest.md) is the field reference; the manifest is required from blueprint B7 on. |
| `agent_manifest_invalid` | `agent.yaml` is unparseable YAML or fails schema validation. | The `error` field names the offending field; field reference in [`docs/authoring/Agents_Design.md`](./Agents_Design.md). |
| `agent_runtime_unsupported` | A pip **entry point** resolved to a `runtime: container` manifest. | Entry points are python packages by definition; container agents register from the agents directory (their manifest names a url, not code). |
| `agent_container_url_unresolved` | The manifest's `container.url` references an unset environment variable. | Set the variable (e.g. `ECHO_AGENT_URL`) in the chassis environment, or hardcode the url. |
| `agent_container_schema_missing` | `runtime: container` but the `input_schema` file isn't next to the manifest. | Ship the JSON Schema file — container agents can't serve their intake schema from code. |
| `agent_manifest_id_mismatch` | Manifest `id` and class `agent_id` disagree. | Make them identical; the registry refuses ambiguous identity. |
| `agent_discovery_no_class` | Package imported, but no `AgentProtocol` subclass was *defined* in it. | Confirm your class inherits from `app.agents.protocol.AgentProtocol` and is defined in `agent.py` (not imported from elsewhere). |
| `agent_discovery_failed` | Import-time crash. | Read the `error` / `error_type` / stack in the same log event — usually a missing dep, bad `config.json` JSON, or an import cycle. |
| `agent_register_duplicate` | Two agents share an `agent_id`. | Rename one's `agent_id`. The later registration overwrites the earlier one — deliberate in exactly one case: a directory checkout overriding the pip-installed copy of the same agent (blueprint B12). Any other collision is a misconfiguration. |
| `agent_scenario_invalid` | A file in `scenarios/` is not valid scenario JSON, or its `user_inputs` is not a submittable `POST /runs` body. | Shape: `{"name": str, "description"?: str, "user_inputs": dict}` with `user_inputs` satisfying the agent's own `input_schema()` (per-agent since B8); the file is skipped until fixed. |
| `agent_entry_point_enumeration_failed` | Reading installed `librerun.agents` entry points blew up. | A broken distribution's metadata — `pip check` the environment; discovery continues with the agents directory. |
| `agent_entry_point_load_failed` | An entry point exists but importing its target crashed. | Read the `error` in the event — usually the agent's deps aren't installed in the chassis venv. |
| `agent_entry_point_not_a_module` | The entry point resolves to a class/function, not a package. | Point it at the agent package itself, e.g. `my-agent = "my_agent_pkg"`. |
| `agent_entry_point_no_location` | The entry point's package has no on-disk directory (namespace package). | Ship the agent as a regular package so `agent.yaml` and assets have a home. |

The request `GET /api/v1/agents` is the fastest health check — if your
agent is not in the list, discovery never succeeded.

---

## Removing or renaming an agent

1. Stop the backend.
2. Delete or rename the directory under `backend/agents/`.
3. Restart.

Existing runs keep whatever `agent_id` string they were created with
(it is a plain string column on `runs`). If that agent is no longer
registered, any attempt to re-run the run will log
`run_unknown_agent` and set the run `status=error`. Restore the
agent directory (or register a stand-in with the same `agent_id`) to
bring those runs back.

---

## Where to go next

- [`docs/authoring/Agents_Design.md`](./Agents_Design.md) — the behavioural
  contract: what `analyze` / `investigate` must return, how progress
  and config surfaces work, platform invariants.
- [`docs/authoring/Quickstart.md`](./Quickstart.md) — the hour
  path: `librerun init` with the three templates, `librerun run`,
  `librerun battery`.
- [`backend/agents/vita_v1/agent.py`](../../backend/agents/vita_v1/agent.py)
  — a full real-world agent with a 10-step pipeline, a config surface,
  drift auditing, and a custom report template.
- [`docs/authoring/LangGraph.md`](./LangGraph.md) — bringing
  an agent that is already written for another framework onto LibreRun
  through an adapter, rather than rewriting it against `AgentProtocol`.
- [`docs/authoring/Run_Contract_v1.md`](./Run_Contract_v1.md) — the HTTP+SSE wire
  spec, if your agent runs as a container in any language.
- [`backend/adapter_kit/`](../../backend/adapter_kit/) — the conformance
  battery. If you are writing an *adapter* rather than an agent, this is
  the suite it has to pass, and passing it is what "the adapter works"
  means.
