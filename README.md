# LibreRun

**LibreRun is an educational software environment for teaching the
design, development and operation of AI agents using multiple
agent-development frameworks. Its scope is agent conception and
planning, framework selection, security, operating environments,
observability, evaluation and optimization. Its architecture uses
Docker Compose or Podman Compose to orchestrate its containers within a
Linux virtual machine.**

At its centre is a self-hosted chassis for AI agents, built for
educational purposes: a complete, working platform to learn from — how
an agent is conceived, what a framework gives it and what the platform
gives instead, how a run is observed, where the security boundaries
lie, and what an agent needs from the environment it runs in. The
chassis is real, not a mock: intake, a human approval gate, PII
redaction before anything is stored, per-step model configuration,
reports and one trace per run. You bring an agent, or study the five in
the box, and LibreRun brings everything around it.

Two things the scope names are taught otherwise. Agent conception and
planning are taught in person; the tree carries the anatomy of an agent.
Evaluation and optimization are taught with the chassis's own
instruments — the gateway, which is the
LLM router every model call passes through; the edge proxy; the cache;
and the tracing and observability tools, all designed to teach
evaluation — and the pages that teach them are not written yet:
[what 1.0 does not do yet](#what-10-does-not-do-yet) says so. LibreRun
runs on Linux only.

An agent plugs in as a self-contained package — a manifest, an input
schema, and the phases it runs — and inherits the whole platform without
building any of it. The chassis knows nothing about any particular agent:
the run list, the gate, the report page and the traces are the same for
every agent, driven by what its manifest declares.

Five agents ship in the box, all keyless: **VITA**, the demo agent that
investigates cross-vendor integration failures, which comes pre-loaded
and runs the moment the stack is up; an **echo** reference agent; and
**LangGraph**, **LlamaIndex** and **Vercel AI SDK** examples.

**DOCI** is an add-on agent, installed as a module rather than shipped
in the box: it is maintained separately at
<https://github.com/JeremiahJRRoss/librerun-doci> and plugs into a
running LibreRun the way any agent does.

---

## The three promises

### 1. Five minutes to a gated, traced run — no keys, no config

`git clone`, one command, log in with the printed credentials, click a
sample, watch the run, approve the gate, read the report, open the trace.
All five bundled agents run keyless through the gateway's built-in stub
provider, so the whole loop is visible before a single provider key
exists.

### 2. Your agent in an hour, and the platform snaps in

Two paths, both proven by CI: a **LangGraph** graph in-process through
`librerun-langgraph`, or **any language, any framework** as a container
on the [Run Contract](docs/authoring/Run_Contract_v1.md) with the
`librerun-agent` Python SDK — TypeScript authors get a complete
reference server in a single file. Through the SDK your agent gets the
platform's services without building them: PII stripping, the model
chosen per step in the admin UI, knowledge search, run storage, audit.
Every model call goes through the **LibreRun gateway**, so your agent
never holds a provider key. `librerun init` scaffolds either path;
`librerun battery` tells you when you are done.

### 3. Every run is one trace, in the tool you already use

Jaeger in the box: one tree from intake through your agent's own spans,
with the model, tokens and cost of every LLM call on it. An audit log
per run. PII redacted before anything is stored — and tested overlays
that ship the same traces and logs to **Datadog**, **Elastic** or
**Splunk**.

---

## What you learn here

LibreRun exists to be read as much as run. Each concern around an agent
is built the way a production system builds it, tested, and documented
against the tree: the documentation workflow in CI fails on a command
that does not run, a link that breaks or a spec that has drifted, so a
page here says what the code does, not what it promises.

| Scope | You learn | Where the tree teaches it |
|---|---|---|
| **Agent conception and planning** | Taught in person. The tree carries the anatomy — an agent's manifest, its phases and steps, the gate between phases, and the invariants the chassis holds for every agent | [`docs/authoring/Agents_Design.md`](docs/authoring/Agents_Design.md) and [`docs/authoring/Manifest.md`](docs/authoring/Manifest.md) |
| **Framework selection** | What a framework gives an agent and what the chassis gives instead: one small agent as three templates — in-process on LangGraph, a Python container on the SDK, a TypeScript container on the Run Contract — and four examples, on LangGraph, LlamaIndex, the Vercel AI SDK and the echo agent on the Python SDK; PII redaction, the model chosen per step, the gate and the trace come from the chassis whichever you pick. The rules the tree states for the choice: an agent you do not trust runs in a container, an existing graph runs in-process, any other language serves the Run Contract. A page that compares the frameworks is not written yet | the three templates in [`docs/authoring/Quickstart.md`](docs/authoring/Quickstart.md), the four examples under `backend/agents/_examples/`, [`docs/authoring/LangGraph.md`](docs/authoring/LangGraph.md), [the trust model](docs/platform/Security.md#the-runtime-trust-model-in-three-lines), [`docs/authoring/Run_Contract_v1.md`](docs/authoring/Run_Contract_v1.md), and the conformance battery that says when an agent is done |
| **Security** | Where the boundaries lie — the trust model in three lines, tenancy on every query, PII redaction that fails closed, the gateway as the only process that holds a provider key, and what is *not* protected | [`docs/platform/Security.md`](docs/platform/Security.md#the-runtime-trust-model-in-three-lines), [`docs/authoring/LLM_Gateway.md`](docs/authoring/LLM_Gateway.md), [`SECURITY.md`](SECURITY.md) |
| **Operating environments** | What an agent needs from the environment it runs in — Linux only; Docker Compose or Podman Compose orchestrating the containers, typically inside a Linux virtual machine; the run modes, the ports, what a container agent can reach and the egress its manifest declares, which secret lives in which process, the per-OS install layer, and `librerun doctor` | [`docs/platform/Install.md`](docs/platform/Install.md), [`docs/platform/Install_CentOS_Ubuntu.md`](docs/platform/Install_CentOS_Ubuntu.md), [`docs/authoring/Container_Agents.md`](docs/authoring/Container_Agents.md), [what a container agent can reach](docs/platform/Security.md#what-a-container-agent-can-reach) |
| **Observability** | How a run is observed — one trace per run across the gate, the model, tokens and cost of every LLM call, three stamped telemetry planes, and the same telemetry into Jaeger, Datadog, Elastic or Splunk. The tracing and observability tools are designed to teach evaluation as well | [`docs/platform/Observability.md`](docs/platform/Observability.md), [`docs/platform/Browser_Observability.md`](docs/platform/Browser_Observability.md) |
| **Evaluation** | Taught with the core code's instruments, which are designed for it: the gateway — the LLM router, one span per call with model, tokens and cost — the edge proxy, the cache, the trace, the scenarios, the feedback thumbs and the conformance battery. The pages that teach evaluation with them are not written yet, and 1.0 has no scored runs | [`docs/authoring/LLM_Gateway.md`](docs/authoring/LLM_Gateway.md), [`docs/platform/Observability.md`](docs/platform/Observability.md), [HTTPS at the edge](docs/platform/Install.md#https-at-the-edge), [what 1.0 does not do yet](#what-10-does-not-do-yet) |
| **Optimization** | Taught with the same instruments: the model, temperature, token limit and timeout chosen per step in the admin UI with nothing restarted, and the cost on every LLM span. The pages are not written yet; a cost panel, smart routing and budgets are v1.2 | [`docs/authoring/Quickstart.md` §6](docs/authoring/Quickstart.md#6-change-the-model-without-touching-code), [`docs/authoring/LLM_Gateway.md`](docs/authoring/LLM_Gateway.md), [the roadmap](docs/release/v1.0.0.md#roadmap) |

None of this makes the chassis a toy. It is built and tested the way a
production system is, because production practice cannot be learned
from anything less, and the release page says plainly
[what 1.0 does not do yet](docs/release/v1.0.0.md#what-10-does-not-do-yet).

---

## Quick start

A Linux machine with Docker or Podman running, and nothing else —
LibreRun supports Linux only, not macOS or Windows. No `.env`, no
provider key:

```bash
git clone https://github.com/JeremiahJRRoss/librerun.git && cd librerun
./scripts/demo.sh
```

> Working from a checkout you already have? Skip the clone. There are no
> images to pull — a release is source only — so the script builds the
> stack from this checkout, inside fifteen minutes: 3 min 34 s and 5 min
> 5 s as measured on a CI runner.

The script writes a demo `.env` — a generated secret, a generated admin
password, demo mode, the stub LLM, the five agents, a local Jaeger —
builds and starts the stack, waits for the backend, then prints the URL,
the credentials and the trace viewer's address.

Open <http://localhost:3000>, sign in with the printed credentials, then:
**+ New Run** → pick an agent card → **Try a sample** → submit → watch
the steps → **Approve** at the gate → read the report → **View trace**.

The stack needs ports 3000 and 8000 free, and 5432, 6379, 4317, 4318,
8090 and 16686 on localhost; if another program holds one,
[`docs/platform/Install.md` §11](docs/platform/Install.md#11-port-conflicts)
moves it. If the script stops with an error, start at
[Troubleshooting](docs/platform/Install.md#troubleshooting). Stop the
stack with `./compose.sh down`, and add `-v` to delete its data as well.

A banner says this is demo mode, which is not for production. To leave
it: set a real `APP_SECRET_KEY`, unset `LIBRERUN_DEMO`, set
`LIBRERUN_STUB_LLM=false`, and put your provider keys in `gateway.env`.
[`docs/platform/Install.md`](docs/platform/Install.md) covers every mode,
including running the backend and frontend as local processes.

> `librerun demo` does the same thing once the CLI is installed — see
> below.

---

## Bring your agent

```bash
pipx install "git+https://github.com/JeremiahJRRoss/librerun#subdirectory=cli"
librerun init my-agent --template langgraph
librerun up
librerun run --agent my-agent --wait
librerun battery --agent my-agent
```

`init` scaffolds the agent; `--template container-python` or
`container-ts` takes the container path instead. `up` rebuilds and starts
the stack, and the agent appears on the new-run page. `run --wait`
submits its sample and follows it to the end. `battery` is the
conformance battery: green means done. Run them from inside the clone.
Already have a clone? `pipx install ./cli` installs the CLI from it,
which needs pipx and Python 3.11 or later.

**In-process** (`langgraph`): your graph runs inside the backend, reaches
the platform through `ctx.caps`, and is the fastest path if you are
already on LangGraph.

**As a container** (`container-python`, `container-ts`): your agent is
its own image speaking the Run Contract over HTTP+SSE, network-isolated
on an internal network, reaching the platform through three doors that
the run token opens. Any language works; the Python SDK and the
TypeScript reference server are starting points, not requirements.

Either way you declare your LLM steps in `agent.yaml` and never name a
model in code — an admin retargets a step in the UI and nothing
restarts. [`docs/authoring/Quickstart.md`](docs/authoring/Quickstart.md)
is the hour path end to end.

---

## What you get

| | |
|---|---|
| **Intake** | A wizard generated from the agent's declared input schema, with a PII redaction preview before anything is stored |
| **Run lifecycle** | Phases, live step progress, a human approval gate between phases, edit-and-re-run, soft delete |
| **PII redaction** | Presidio NER plus a four-stage regex pipeline at intake, upload, the run boundary, the MCP tool, telemetry and the gateway's outbound leg — failing closed when the detector is not ready |
| **Model configuration** | Per step, per tenant, in the admin UI; the gateway resolves provider, model and key at request time, so no redeploy and no restart |
| **The gateway** | One door to every model. Agents never hold a provider key; keyless stub mode runs the whole pipeline with none configured |
| **Reports** | HTML in the page and PDF export from the same Jinja template, so what you read is what you download |
| **Feedback** | Per-section thumbs, persisted, with an admin dashboard |
| **Observability** | OpenTelemetry through a bundled Vector router; Jaeger in the box; tested Datadog, Elastic and Splunk overlays; three stamped telemetry planes |
| **Multi-tenancy** | Tenant scoping on every query, email/password plus Google and Microsoft SSO, JWT sessions, an audit log per run |
| **Operations** | Secrets partitioned per process, `<NAME>_FILE` for container secret stores, sops + age encryption at rest, `librerun doctor`, `librerun key rotate` |

Built on Python 3.12 + FastAPI, Next.js 14 + TypeScript, PostgreSQL 16
and Valkey 8, orchestrated by Docker Compose or Podman Compose —
`compose.yaml` with `agents.compose.yaml`, driven by `compose.sh` — on
Linux.

---

<a id="what-10-does-not-do-yet"></a>

## What the beta does not do yet

Stated here rather than discovered later, and line for line the beta's
announcement. Each has a place in the
[roadmap](docs/release/v1.1.0-beta.1.md#roadmap), except the gRPC
transport, which is not planned.

**Interaction (v1.1, October 2026)**

- No mid-run human input inside a phase: gates sit *between* the phases
  an agent declares.
- No reject-with-note at the gate. You approve, or edit and re-run.
- No artifact store, so an agent's intermediate drafts cannot be
  downloaded.
- No automatic resume of a phase the backend was restarted under. The
  run is marked `error` with the reason at the next boot.

**Authoring (v1.1–v1.2)**

- No TypeScript SDK. The Vercel example is a reference server, not a
  package.
- No in-process adapters beyond LangGraph; everything else is a
  container.
- No gRPC transport, deliberately. HTTP, server-sent events and MCP are
  the control plane; a gRPC binding is considered only if someone needs
  the transport rather than the functions.

**Evaluation and cost (v1.2, November 2026)**

- No evaluation beyond scenarios that prefill and submit — no `expect`
  blocks, no scored runs.
- No measured answer-quality baseline for the VITA demo agent. A keyless
  run proves the wiring, not the judgement.
- No per-run cost panel in the UI. The cost is on the trace.
- No smart cost-versus-quality routing between models.
- No page yet teaches evaluation or optimization. The instruments are in
  the tree — the gateway, the edge proxy, the cache, the trace — and the
  lessons that use them are not written.

**UX (v1.2)**

- No live event stream. The run page polls every 2 s while a run is
  going, and stops when it ends.

**Deployment**

- TLS only at the edge, and only when you turn it on: the opt-in `tls`
  profile serves the UI and the API over HTTPS on one port, while the
  plain ports (3000 and 8000) stay on `127.0.0.1`, their default, which
  `compose.sh` enforces — on Docker the binding, not a host firewall,
  keeps a port off the network
  ([`Install.md`](docs/platform/Install.md#https-at-the-edge)). An `.env`
  copied from 1.0's example keeps both ports on every interface until its
  two lines are edited.
- Behind the edge, LibreRun's own services — Postgres, the cache, the
  gateway, telemetry — still talk plain HTTP on the compose network.
  TLS between them is on the roadmap, v1.2 and later.

Nothing in this list is a bug report. They are the edges of what the
beta claims, and the list is checked against the roadmap at release.

---

## Documentation

**Start here** — [`docs/platform/Install.md`](docs/platform/Install.md)
to run it, [`docs/authoring/Quickstart.md`](docs/authoring/Quickstart.md)
to build on it.

| Platform — running LibreRun | |
|---|---|
| [`docs/platform/Install.md`](docs/platform/Install.md) | Installation on Linux, run modes, secrets, upgrades, troubleshooting |
| [`docs/platform/Install_CentOS_Ubuntu.md`](docs/platform/Install_CentOS_Ubuntu.md) | The per-OS layer: CentOS Stream 10 and Ubuntu 26.04 |
| [`docs/platform/Security.md`](docs/platform/Security.md) | The trust model, tenancy, PII, where each secret lives, every flow that leaves the box |
| [`docs/release/License_Scope_Map.md`](docs/release/License_Scope_Map.md) | Which licence covers which path, the provenance the tree can show, the maintainer's attestation |
| [`docs/release/Distribution_Surface_Matrix.md`](docs/release/Distribution_Surface_Matrix.md) | Every source of bytes a source build fetches, how each is pinned, and who moves it next |
| [`docs/platform/Observability.md`](docs/platform/Observability.md) | The telemetry planes and the Datadog, Elastic and Splunk overlays |
| [`docs/platform/Browser_Observability.md`](docs/platform/Browser_Observability.md) | The UX plane: the RUM envelope, the relay, the privacy model |

| Authoring — building an agent | |
|---|---|
| [`docs/authoring/Quickstart.md`](docs/authoring/Quickstart.md) | Your agent in an hour: the `librerun` CLI and the three templates |
| [`docs/authoring/LangGraph.md`](docs/authoring/LangGraph.md) | Bringing a graph from another framework in-process |
| [`docs/authoring/Container_Agents.md`](docs/authoring/Container_Agents.md) | The container path, walked end to end |
| [`docs/authoring/SDK.md`](docs/authoring/SDK.md) | The `librerun-agent` Python SDK |
| [`docs/authoring/LLM_Gateway.md`](docs/authoring/LLM_Gateway.md) | One door to every model: steps, credentials, outbound redaction, keyless mode |
| [`docs/authoring/Capabilities_and_MCP.md`](docs/authoring/Capabilities_and_MCP.md) | The six capabilities and the run-scoped MCP tools |
| [`docs/authoring/Manifest.md`](docs/authoring/Manifest.md) | Every `agent.yaml` field, generated from the models |
| [`docs/authoring/Run_Contract_v1.md`](docs/authoring/Run_Contract_v1.md) | The normative HTTP+SSE wire spec |
| [`docs/authoring/Agents_Design.md`](docs/authoring/Agents_Design.md) | The agent contract, the lifecycle and the platform invariants |
| [`docs/authoring/Agents_Install.md`](docs/authoring/Agents_Install.md) | Installing an agent: directory layout, discovery, packaging |

| API | |
|---|---|
| [`docs/api/openapi.yaml`](docs/api/openapi.yaml) | OpenAPI 3.1, generated from the running app and drift-checked in CI |

| The demo agent | |
|---|---|
| VITA | Comes pre-loaded with every install: no download, no key, selectable on the new-run page from the first boot |
| [`docs/agents/vita/User_Manual.md`](docs/agents/vita/User_Manual.md) | The customer and administrator manual |
| [`backend/agents/vita_v1/agent.yaml`](backend/agents/vita_v1/agent.yaml) | The demo agent's manifest: its LLM steps and settings, with their defaults |

| Add-on agents | |
|---|---|
| [DOCI](https://github.com/JeremiahJRRoss/librerun-doci) | An add-on agent, installed as a module from its own repository, which documents it; not in the box |

| Release | |
|---|---|
| [`docs/release/v1.1.0-beta.1.md`](docs/release/v1.1.0-beta.1.md) | The beta's announcement: what LibreRun is for, the promises, what the beta adds and does not do yet, what a beta means, the roadmap, how it was certified |
| [`docs/release/v1.0.0.md`](docs/release/v1.0.0.md) | The 1.0 record: certified on 2026-09-22 and never published |
| [`docs/platform/Releasing.md`](docs/platform/Releasing.md) | How a release is cut: the version, the tag, what it publishes, and the checks that stand in its way |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed, batch by batch |

---

## License and marks

LibreRun is licensed under **AGPL-3.0-only**, the GNU Affero General
Public License, version 3 only ([`LICENSE`](LICENSE)). Four directories
an agent builds on — `sdk/`, `backend/adapters/`, the CLI's templates
and the examples — are Apache-2.0 instead, so an agent does not take on
the AGPL by importing or copying them. [`NOTICE`](NOTICE) carries the
copyright statement and the notices of the third-party material in the
tree; [`THIRD_PARTY.md`](THIRD_PARTY.md) lists what a build downloads;
the [licence scope map](docs/release/License_Scope_Map.md) says which
licence covers which path. If you modify LibreRun and let others use it
over a network, the AGPL asks you to offer them your version's source.
LibreRun comes **with no warranty**: it is provided as is, without
warranty of any kind, express or implied, as sections 15 and 16 of the
licence state, and nothing in this repository adds one.

The licence covers the software, not the name:
[`TRADEMARKS.md`](TRADEMARKS.md) says what a fork may call itself.

Contributions come in under a [Developer Certificate of
Origin](https://developercertificate.org/) sign-off — `git commit -s` —
which is an attestation, not a copyright assignment, and no CLA. [`CONTRIBUTING.md`](CONTRIBUTING.md) has the development
loop and the house rules, including the one this project will not bend:
never weaken a check to make it pass.
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) applies everywhere the
project happens, and vulnerabilities go through
[`SECURITY.md`](SECURITY.md) — privately, never a public issue.
