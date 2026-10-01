# Your agent in an hour — the `librerun` CLI and the three templates

*Blueprint S6, promise 2: bring your own agent, and the platform snaps
in. This page is the hour path, start to finish: install the CLI, start
the demo, scaffold an agent from a template, run it, prove it with the
battery, change its model from the admin page, rotate its key.*

LibreRun is an educational software environment for teaching the
design, development and operation of AI agents; this page is the
development half, and `docs/platform/Install.md` is the operation half.

The three templates are the three ways an agent runs on LibreRun:

| Template | What it is | Derived from |
|---|---|---|
| `langgraph` | a compiled LangGraph graph running **in the backend process** through `librerun-langgraph` | `backend/agents/_examples/langgraph_triage/` |
| `container-python` | one async handler on the **`librerun-agent` SDK**, served as a Run Contract v1 container | `backend/agents/_examples/echo_container/` |
| `container-ts` | the **Run Contract served directly** from one TypeScript file, with the Vercel AI SDK making the model call | `backend/agents/_examples/vercel_ai_answer_ts/server.ts` |

Every template is the smallest complete agent of its kind, and all three
share one shape: one phase, `structured` output, the `llm` and `pii`
grants, one `llm.steps` entry called through the platform — so the model
an admin picks on the configuration page is the model that answers
(D13), keyless runs answer from the agent's own fixture and say so, and
`librerun battery` tells you when you are done.

## 0. Before you start

A Linux machine with Docker or Podman — LibreRun supports Linux only —
and a clone of the repository:

```bash
git clone https://github.com/JeremiahJRRoss/librerun.git && cd librerun
pipx install "git+https://github.com/JeremiahJRRoss/librerun#subdirectory=cli"
librerun doctor
```

The CLI is standard-library Python, so `pipx install` takes seconds and
needs nothing else. `librerun doctor` reports what this machine and this
checkout can do — the engine, the checkout, `.env`, `gateway.env`, the
agents on disk and their keys, the ports, and the stack when it is up —
and **fails loudly without Docker or Podman**: nothing below works
without one, and it says so first.

Every command finds the checkout by walking up from the working
directory to `compose.yaml` and `compose.sh`, or takes `--root <path>`.

## 1. The demo, up

```bash
librerun demo
```

On a checkout with no `.env` this writes one — a generated secret and
admin password, demo mode, the stub LLM, the bundled agent and the
examples, Jaeger, one gateway key per agent — then builds and starts
the stack, waits for the backend, and prints the URL, the credentials
and the trace viewer's address. It is `scripts/demo.sh` in Python; both
leave the same stack behind. Sign in, click a card, watch a run. That is
promise 1 (`docs/platform/Install.md`); the rest of this page is promise 2.

## 2. Scaffold an agent

```bash
librerun init my-agent --template langgraph
```

or `--template container-python`, or `--template container-ts`. The id
is the manifest's rule — lowercase letters, digits and hyphens — and
the directory is the id with hyphens as underscores, under
`backend/agents/`: a package the backend imports for the in-process
template, a manifest plus its assets for the container ones.

What you get, per template:

- **`langgraph`** — `agent.yaml`, `agent.py` (the graph: an `answer`
  node that calls the model and falls back to a rule, a `summarise`
  node that shapes the output, and the `MyAgentAgent` class discovery
  instantiates), `input_schema.json`, `scenarios/demo.json`, a
  `README.md`.
- **`container-python`** — `agent.yaml` (`runtime: container`, the
  service's URL), `agent.py` (the handler `librerun_agent.serve` turns
  into a server), `input_schema.json`, `scenarios/demo.json`, a
  `Dockerfile` and a `requirements.txt`, a `README.md`.
- **`container-ts`** — `agent.yaml`, `server.ts` (the four endpoints,
  the bearer bound to the invocation, one `generateText` per invocation
  with a wrapped `fetch` that carries the keyless fixture out and reads
  the gateway's envelope back), `package.json` and `tsconfig.json`,
  `input_schema.json`, `scenarios/demo.json`, a `Dockerfile`, a
  `README.md`.

For the two container templates `init` also appends a service to
`agents.compose.yaml` in the examples' exact shape — the gateway
environment (`LIBRERUN_GATEWAY_URL`, `OPENAI_BASE_URL`), this agent's
own LibreRun gateway key expanded `:?` so an unprovisioned agent stops
`up` by name, the internal `agents` network, the `librerun.agent_id`
label, `logging: driver: none`, the `egress` line commented — and writes
a fresh `LIBRERUN_AGENT_KEY_MY_AGENT=lr_agent_…` line into `.env`. That
key is the platform's own credential (D10), never a provider's: no
template holds a provider key, and none mentions the stub — keyless is
the gateway's concern.

Each template's `README.md` says what to edit and how to break the
agent on purpose.

## 3. Start it

```bash
librerun up
```

`up` tops up the key of any agent `.env` predates, then runs the stack
under compose with every profile — the platform, the viewer, the
examples, and `agents` (the profile scaffolded services carry) — with
`--build`, and waits for the backend. The backend image is rebuilt so
the new directory is discovered; a container template's image is built
from its Dockerfile. When it returns, the agent is on the new-run page
with its sample, and `librerun doctor` lists it as registered.

## 4. Run it

```bash
librerun run --agent my-agent --wait
```

`run` logs in as you when you say who — `--email` and the password on
stdin (`--password-stdin`: one line from a pipe, or typed with no echo),
or `LIBRERUN_EMAIL` and `LIBRERUN_PASSWORD` exported in the shell — and
otherwise with the demo's credentials from `.env`. Never put a password on
the command line, which the process list shows to every user of the
machine (`--password` still works for this release, and says so); nothing
is sent until `/api/v1/meta` answers as LibreRun. It loads the agent's
first sample (`--scenario <id>` picks
another), submits it, prints the run number and the run page's URL, and
with `--wait` follows the run to `complete` — non-zero if it ends in
`error`. A run that parks at an approval gate is reported as parked;
`--approve` presses the button once for you. Open the URL: the progress
list, the structured output with its `answer_source` (`stub-fixture` on
the keyless demo), the feedback controls, and "View trace" into Jaeger,
one tree from intake through your agent's own spans — none of it written
by you.

## 5. Prove it

```bash
librerun battery --agent my-agent
```

One command, two batteries: the **adapter battery** for an in-process
agent (schema-valid output, streamed progress, spans, an output that
survives persistence, run through the chassis's own intake redaction),
the **Run Contract battery** for a container (`/healthz`, per-invocation
token binding probed from every direction, progress, exactly one
`completed`, an output the chassis walk accepts). Both run inside a
one-off backend container — that is where the chassis's Python lives,
and the only place a container agent's address resolves, since agent
containers sit on the internal `agents` network and publish no port.

Your agent's directory is bind-mounted over the image's copy, so the
in-process battery tests **the file you just edited**, with no rebuild
in between. A container is the artifact: after an edit, `librerun up`
rebuilds it and the battery drives the new one.

Now break it, to see what red looks like — each README names the edit:

- `langgraph`: make `summarise` return `{"structured": {}}` — *phase
  'answer' produced no structured output ({}), but the agent declares
  output.mode=structured*;
- `container-python`: `return ["broken"]` from the handler — *the
  handler must return a dict: the phase output*;
- `container-ts`: `inv.output = ["broken"]` — *`completed` carried an
  `output` that is list, not a JSON object, so the chassis could not
  have consumed that invocation*.

Revert, and it is green again. The battery is what the CI matrix runs
against all three templates on every change
(`.github/workflows/template-matrix.yml`): `init`, `up`, `run`,
`battery`, the break, the red, the revert.

## 6. Change the model without touching code

Admin → Agents → your agent → Configuration. The `answer` step's
provider, model, temperature and limits are this tenant's data (L25):
edit them, run the sample again, and the run page's step list and the
trace both show the new model on that step. Nothing was rebuilt or
restarted — the gateway resolves the step at request time, which is the
whole reason the templates name a **step** and never a model (D13).

Keyless (`LIBRERUN_STUB_LLM=true`), every step is answered by the
gateway's stub provider from the fixture the agent hands it, with the
same span, the same redaction and a cost. `docs/authoring/LLM_Gateway.md`
is the door's reference.

## 7. Keys, and rotating one

A container template's service holds exactly one credential: its
LibreRun gateway key in `OPENAI_API_KEY`, provisioned in `.env` before
`up` because compose expands the variable while no LibreRun service is
running. It names the agent and nothing else; every model call carries
the invocation's run token beside it, which the SDK and the TypeScript
template send for you.

To rotate it with a rolling changeover:

```bash
librerun key rotate my-agent
librerun key rotate my-agent --finish
```

`rotate` moves the current value to `LIBRERUN_AGENT_KEY_MY_AGENT_PREVIOUS`,
writes a fresh one on the main line, and recreates the gateway and the
agent's container (named explicitly, so its profile is active): the
gateway accepts both values, the container holds the new one. `--finish`
removes the `_PREVIOUS` line and recreates the gateway alone — the
container's environment has not changed, so compose leaves it as it is —
and the old value is refused from that boot. `librerun doctor` says
when a rotation is in flight.

## 8. Doctor, logs, down

```bash
librerun doctor
librerun logs backend gateway -f
librerun down
librerun down --volumes
```

`logs` is for the platform's services; an agent container's own
`docker logs` is empty by construction — its `print()` and `ctx.log()`
lines are records of the run, under its trace.

**Behind the HTTPS edge** (the `tls` profile; `docs/platform/Install.md`,
"HTTPS at the edge"), `doctor` and `run` take the edge's origin and the
CA its certificate is verified against — from the command line, or from
two variables of your shell, never `.env` lines:

```bash
export LIBRERUN_URL=https://librerun.example.lan:8443       # the UI and /api/v1 on one origin
export LIBRERUN_CA_FILE=$PWD/librerun-edge-root.crt       # the root copied out of librerun-edge
librerun doctor --email you@example.com --password-stdin
librerun run --agent my-agent --wait --base-url https://librerun.example.lan:8443 --cacert librerun-edge-root.crt
```

The CA file is trusted alone, as curl's `--cacert`, and a missing one is
refused by its path before anything is sent. Given no `--base-url`,
`doctor` sends your password only once the engine says this checkout's
edge is running and publishing that port — a certificate is no proof,
since two clones of one directory name share the edge's local CA — and
then to the address it publishes on, under the host name the certificate
carries. `librerun up` and `librerun demo` never start the edge.

## Where to go next

- `docs/authoring/LangGraph.md` — the in-process path in full: state,
  config, capabilities inside a node, calling a model, checkpointers.
- `docs/authoring/Container_Agents.md` — the container path in full,
  and what the contract is for every other language.
- `docs/authoring/SDK.md` — the Python SDK's control surface.
- `docs/authoring/LLM_Gateway.md` — every model call's door.
- `docs/authoring/Manifest.md` — the `agent.yaml` field reference,
  generated from the models.
- `docs/authoring/Run_Contract_v1.md` — the wire contract a container serves.

## The commands

| Command | What it does |
|---|---|
| `librerun demo [--env-only] [--no-wait]` | the zero-config demo: write `.env` if absent, build, start, wait, print the URL and credentials |
| `librerun up [--no-build] [--no-wait]` | top up agent keys, build and start every profile, wait for the backend; `--no-build` starts what is built, and builds and pulls nothing |
| `librerun down [--volumes]` | stop the stack; `--volumes` deletes its data |
| `librerun logs [service…] [-f] [--tail N]` | the platform's logs (backend and gateway by default) |
| `librerun init <name> --template langgraph \| container-python \| container-ts [--name "Display Name"]` | a new agent under `backend/agents/`, plus its compose service and key for the container templates |
| `librerun run --agent <id> [--scenario <id>] [--wait] [--approve] [--email <address> --password-stdin] [--base-url <url>] [--cacert <file>]` | submit a sample; print the run's number, status and URL; non-zero on `error`. The base is `--base-url`, else `LIBRERUN_URL`, else the backend's published port; an https one is verified against `--cacert`, else `LIBRERUN_CA_FILE` |
| `librerun battery --agent <id>` / `--url <url> --agent-dir <dir>` | the conformance battery, in-process or Run Contract |
| `librerun doctor [--email <address> --password-stdin] [--base-url <url>] [--cacert <file>]` | engine, checkout, `.env`, `gateway.env`, agents, keys, ports, backend, gateway, trace endpoint; given credentials (or `LIBRERUN_EMAIL` and `LIBRERUN_PASSWORD`), who you sign in as and whether you administer the platform — sent only to `--base-url` or to this checkout's own stack (its HTTPS edge for an `https` `LIBRERUN_URL`), and never taken on the command line |
| `librerun key rotate <id> [--finish] [--service <name>] [--no-up]` | rotate an agent's gateway key with a rolling changeover |
