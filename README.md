# LibreRun

LibreRun is a self-hosted educational environment for learning how to design, develop, and operate services that use AI agents.

A request enters through an interface. An agent processes it, calls models and tools, and produces a result. The surrounding service must control access, protect data, obtain approval, and record what happened. LibreRun provides these components so you can study and develop the complete service.

## 1. Overview

### Why it exists

LibreRun teaches the environmental, tooling, and user-experience considerations of agent service design:

- **Environment:** processes, containers, networks, credentials, and storage.
- **Tools:** model routing, external services, configuration, and observability.
- **User experience:** input, progress, human review, results, and feedback.

### What it provides

| Component | What it does |
|---|---|
| Web interface | Generates forms from an agent’s input schema. Displays runs, progress, approval controls, results, and feedback. |
| Agent runtime | Discovers agents, executes their phases, enforces deadlines, and waits for approval where declared. |
| LLM gateway and router | Routes declared model steps to configured providers. Holds provider credentials and applies request controls and outbound redaction. |
| HTTPS reverse proxy | An optional Caddy service provides HTTPS and routes browser requests to the frontend or API. |
| PII redaction | Detects supported personally identifiable information in input and at persistence, telemetry, and model-request boundaries. |
| Platform services | Provide knowledge search, temporary run storage, audit records, agent configuration, and tool-secret retrieval. |
| Storage | PostgreSQL stores application records. Valkey provides temporary state and caching. |
| Reports | Display structured results or agent-supplied HTML. Export results as HTML or PDF; PDF generation falls back to HTML if rendering fails. |
| Observability | OpenTelemetry records activity. Vector routes telemetry. Jaeger provides the included trace viewer. |
| Development tools | Provide agent templates, a Python SDK, a LangGraph adapter, a command-line interface, and conformance checks. |
| Access management | Provides authentication, roles, and tenant-scoped application records and configuration. |

A **tenant** is a group with its own users, records, and configuration.

### Included agents

| Agent | Runtime | Purpose |
|---|---|---|
| VITA | Python inside the backend | Investigates interoperability problems between two vendor products. Includes a human approval step. |
| Echo | Python container | Demonstrates the Python SDK and container contract. |
| LangGraph Triage | LangGraph inside the backend | Demonstrates graph integration. |
| LlamaIndex Summarize | Python container | Demonstrates a LlamaIndex workflow with separate extraction and summarisation steps. |
| Vercel AI SDK Answer | TypeScript container | Demonstrates the execution contract and model access through the Vercel AI SDK. |

The standard application configuration discovers VITA. The demo configuration also discovers the four examples and starts their required containers.

All five can use the gateway’s fixed test responses without model-provider keys.

### DOCI

[DOCI — Debug Observability and Codebase Inspector](https://github.com/JeremiahJRRoss/librerun-doci) is a separately maintained support-agent project. It investigates tickets using observability evidence, known issues, and source code, then produces a diagnosis and drafts for human review.

Its current evaluation build uses Deep Agents and Arcade.dev. Its [LibreRun integration remains planned](https://github.com/JeremiahJRRoss/librerun-doci/blob/main/docs/BUILD_STATUS.md). DOCI is not included in the LibreRun installation described here.

### When to use LibreRun

Use LibreRun to:

- Learn what an agent needs from its operating environment.
- Develop agents with input, approval, reporting, and telemetry already available.
- Compare framework integration approaches.
- Inspect model calls, execution time, token usage, errors, and available cost estimates.
- Study how user input and human review affect a service.

Conformance checks verify integration behaviour. They do not score answer quality.

### Where it runs

The server runs on Linux, directly or inside a Linux virtual machine. It is intended for a small personal Linux server or a sufficiently capable Linux desktop.

Docker Compose or Podman Compose manages the containers. Native Windows, macOS, Docker Desktop, and WSL2 deployments are outside the project’s supported server environments.

The project does not publish a verified minimum for processor count, memory, or disk space. Capacity depends on the enabled services, image builds, and concurrent runs.

### Who uses it

**Users** submit requests, review intermediate results, approve work, and read reports.

**Developers** implement agents and connect interfaces. The included interface uses forms and run pages. A developer can connect a chat interface through the API; LibreRun does not currently include one.

**Administrators** manage accounts, model settings, credentials, and the installation.

## 2. How agents work

### The agent package

An agent declares its behaviour through three main elements:

| Element | Responsibility |
|---|---|
| `agent.yaml` | Identity, runtime, phases, approval requirements, capabilities, model steps, and output mode. |
| Input schema | Required fields, types, and validation rules. The web interface uses it to generate the form. |
| Implementation | The code that executes each phase and produces its output. |

The backend discovers agents at startup. A changed manifest or newly added agent must reach the backend image, followed by a backend restart.

### The run lifecycle

A **run** is one submitted request. A **phase** is a declared part of that run. An **invocation** is one execution of a phase.

1. The user selects an agent and submits input.
2. LibreRun validates and redacts the input.
3. The runtime invokes the first phase.
4. The agent reports progress and returns a result.
5. LibreRun either starts the next phase or waits for approval.
6. At an approval gate, the user approves the result or supplies edits and repeats the phase under review.
7. The final phase produces the completed result.

Declare approval on the phase that must wait:

```yaml
phases:
  - name: analyze
  - name: investigate
    approval: true
```

LibreRun completes `analyze`, presents its output, and waits before invoking `investigate`.

The first phase cannot require approval. Submission authorises it. The current release supports approval between phases, without interactive human input during a phase.

The browser polls unfinished runs approximately every two seconds. Container agents stream events to the backend using server-sent events; that stream is separate from browser polling.

A backend restart interrupts executing phases. The platform marks interrupted runs as errors; it does not automatically resume those phases.

### Execution options

| Option | Implementation | Platform access |
|---|---|---|
| In process | Python code running inside the backend. A supplied adapter supports LangGraph. | Python capability interfaces. |
| Container | A separate, running service implementing Run Contract v1. | HTTP, server-sent events, and run-scoped Model Context Protocol tools. |

In-process agents share the backend’s process privileges. Capability declarations do not isolate their code.

Container templates join an internal agent network. They reach the backend and model gateway, but have no direct internet access by default. An agent that needs internet access must declare `network.egress: true` and join the `egress` network in its Compose service.

Compose starts agent containers. The backend invokes them at the URLs declared in their manifests.

### Model routing and credentials

An agent calls a declared step, such as `analyze`. The gateway resolves that step’s provider, model, temperature, token limit, and timeout from the tenant’s configuration.

The administrator changes these values on the agent’s **Steps** tab. The next request uses the effective configuration without rebuilding the agent.

| Credential | Purpose |
|---|---|
| Provider key | Authenticates the gateway to a model provider. Held by the gateway. |
| LibreRun agent key | Identifies an agent. Framework clients may receive it as `OPENAI_API_KEY`. It does not independently authorise a model call. |
| Run token | Identifies the current invocation, run, tenant, and agent. Required for model calls and run-scoped platform access. |
| Tool secret | Authenticates an agent to a separate service, such as web search. Declared in the agent manifest. |

Framework clients use the gateway’s OpenAI-compatible Chat Completions interface and a model value such as `librerun/analyze`.

Routing follows configured steps. Automatic selection based on cost or answer quality is not implemented.

The gateway records token usage when available and calculates USD cost estimates when usage and pricing information are available. Demo calls can contain simulated estimates; they do not represent provider charges.

## 3. Commands and profiles

Run commands from the repository root.

Use `./compose.sh` to manage services. It selects Docker Compose or Podman Compose, prepares the gateway’s agent-key file, and checks configuration required by the HTTPS profile.

The scripts and CLI use this wrapper:

| Command | Role |
|---|---|
| `./scripts/demo.sh` | Prepares demo configuration where needed, provisions keys, starts demo services, and checks startup. |
| `./compose.sh …` | Builds, starts, inspects, and stops selected services. |
| `librerun init` | Creates an agent from a template. |
| `librerun run` | Submits an agent’s sample request. |
| `librerun battery` | Runs agent conformance checks. |
| `librerun doctor` | Inspects installation and configuration. |

The optional `librerun up` command also exists. It provisions missing agent keys, calls `compose.sh` with `app`, `viewer`, `demo`, and `agents`, and checks startup. The instructions below use `compose.sh` explicitly.

### Profiles

| Profile | Adds |
|---|---|
| `app` | Backend and frontend. |
| `viewer` | Jaeger. |
| `demo` | Echo, LlamaIndex, and Vercel example containers. |
| `agents` | Container services created by `librerun init`. |
| `tls` | Caddy and its certificate-control service. |

PostgreSQL, Valkey, Vector, and the gateway have no profile restriction and participate in a normal `up`.

Profiles select services. They do not change model mode or agent discovery. For example, `--profile demo` does not set `LIBRERUN_STUB_LLM=true`.

## 4. Quick start

You need Linux, Git, and a compatible container runtime. The first build downloads images and dependencies.

For Docker, use Engine 24 or later and Compose 2.24.0 or later. The current Compose file uses `env_file.required`, introduced in Compose 2.24.0. See [Docker’s environment-file documentation](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/).

The wrapper also supports Podman. A verified minimum Podman and podman-compose combination is not established by this source review; validate the configuration with your installed versions before relying on them.

### Local demo

```bash
git clone https://github.com/JeremiahJRRoss/librerun.git
cd librerun
./scripts/demo.sh
```

On a fresh checkout, the script creates configuration, generates credentials, builds the services, and starts the demo.

Open [http://localhost:3000](http://localhost:3000). Sign in with the printed credentials.

1. Select a VITA sample.
2. Review and submit the input.
3. Wait for the first phase.
4. Review its result and approve the investigation.
5. Open the completed report.
6. Follow **View trace** to Jaeger.

The fresh demo uses fixed model responses and binds published ports to the local machine.

If `.env` already exists, the script preserves its settings. Running the script does not automatically convert an existing installation to demo or stub mode.

### Demo from another machine

Before the first build:

```bash
./scripts/demo.sh --env-only
```

Set these entries in `.env`:

```ini
FRONTEND_PORT=0.0.0.0:3000
NEXT_PUBLIC_API_URL=/api/v1
BACKEND_INTERNAL_URL=http://backend:8000
```

Keep one entry per variable, then start:

```bash
./scripts/demo.sh
```

Open `http://<host-address>:3000` from the client machine. Replace `<host-address>` with the server’s reachable IP address or hostname.

Sign in using `INITIAL_ADMIN_EMAIL` and `INITIAL_ADMIN_PASSWORD` from `.env`.

This exposes the frontend over plain HTTP. Use it on a trusted network. The backend and Jaeger remain local to the server.

### VITA with real models

Use the full installation below. VITA’s default steps use OpenAI and Anthropic. Tavily supplies optional web-search results.

## 5. Full installation

This procedure runs all application services in containers and enables local trace viewing.

For a fresh installation, create new credentials. When configuring an existing installation, preserve its database credentials, application secret, and encryption keys. Generate only missing values.

### 5.1 Prepare the host and checkout

Install:

- Git and a compatible container runtime.
- Python 3 for secret generation and JSON inspection.
- `curl` for the checks below.

Python and Node.js used by the application are installed inside its images.

```bash
git clone https://github.com/JeremiahJRRoss/librerun.git
cd librerun
```

If the checkout already exists, use it.

### 5.2 Create configuration files

For a fresh installation:

```bash
cp .env.example .env
cp gateway.env.example gateway.env
chmod 600 .env gateway.env
```

Edit existing files instead of overwriting them.

| File | Contents |
|---|---|
| `.env` | Application settings, database credentials, bootstrap accounts, backend encryption key, and tool credentials. |
| `gateway.env` | Provider credentials and the gateway encryption key. |

Keep these files out of version control.

### 5.3 Generate secrets

For a new installation:

```bash
python3 - <<'PY'
import base64
import secrets

def encryption_key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()

print("APP_SECRET_KEY=" + secrets.token_urlsafe(64))
print("POSTGRES_PASSWORD=" + secrets.token_hex(32))
print("INITIAL_ADMIN_PASSWORD='Admin-" + secrets.token_urlsafe(24) + "!7'")
print("LIBRERUN_BACKEND_SECRETS_KEY=" + encryption_key())
print("LIBRERUN_GATEWAY_SECRETS_KEY=" + encryption_key())
PY
```

Copy the complete assignments into their files:

- First four assignments: `.env`.
- `LIBRERUN_GATEWAY_SECRETS_KEY`: `gateway.env`.

The command prints values without editing files.

The two encryption keys enable stored credentials in the administration interface. They must differ. Preserve them with database backups.

### 5.4 Configure the application

Set the following in `.env`. Replace placeholders and retain one entry per variable.

```ini
APP_ENV=staging
APP_SECRET_KEY=<generated-application-secret>

POSTGRES_DB=librerun
POSTGRES_USER=librerun
POSTGRES_PASSWORD=<generated-database-password>

LIBRERUN_BACKEND_SECRETS_KEY=<generated-backend-encryption-key>

CREDENTIALS_ENABLED=true
INITIAL_ADMIN_EMAIL=<your-email-address>
INITIAL_ADMIN_PASSWORD='<generated-admin-password>'
INITIAL_USER_EMAIL=
INITIAL_USER_PASSWORD=

LIBRERUN_DEMO=false
LIBRERUN_STUB_LLM=false
LIBRERUN_AGENTS_PATH=
LIBRERUN_PII_ALLOW_DEGRADED=false

FRONTEND_PORT=127.0.0.1:3000
BACKEND_PORT=127.0.0.1:8000
NEXT_PUBLIC_API_URL=/api/v1
BACKEND_INTERNAL_URL=http://backend:8000
APP_CORS_ORIGINS=http://localhost:3000

TAVILY_API_KEY=

VECTOR_VIEWER=1
TRACE_VIEWER=jaeger
TRACE_VIEWER_BASE_URL=http://localhost:16686
VECTOR_CRIBL=
LIBRERUN_OBS_VENDOR=

LOG_LEVEL=INFO
OTEL_DEBUG=false
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT
```

Leave other sample settings unchanged.

Important settings:

- Empty `LIBRERUN_AGENTS_PATH` uses the standard agents directory, containing VITA and any agents you add there.
- `LIBRERUN_DEMO` controls demo behaviour. `LIBRERUN_STUB_LLM` independently controls fixed model responses.
- Add `TAVILY_API_KEY` to enable VITA’s web search. Without it, VITA has no web-search results.
- Fill both `INITIAL_USER_*` values to create an optional second account. Use a separate password.
- `NO_CONTENT` omits model prompt and completion content from trace capture.

### 5.5 Configure providers

Set these entries in `gateway.env`:

```ini
LIBRERUN_GATEWAY_SECRETS_KEY=<generated-gateway-encryption-key>
OPENAI_API_KEY=<your-openai-key>
ANTHROPIC_API_KEY=<your-anthropic-key>
GOOGLE_AI_API_KEY=
```

Supply credentials for every provider your configured steps use. VITA’s defaults reference OpenAI and Anthropic.

Leave unused provider entries blank. To use fixed responses, set `LIBRERUN_STUB_LLM=true` in `.env`; adding provider keys alone does not disable stub mode.

### 5.6 Validate, build, and start

Check whether the installed Compose implementation accepts the configuration:

```bash
./compose.sh --profile app --profile viewer config > /dev/null
```

Resolve any reported error before starting.

```bash
./compose.sh --profile app --profile viewer up -d --build
./compose.sh --profile app --profile viewer ps
```

The backend applies migrations before serving requests. It also attempts to create the configured accounts.

### 5.7 Verify readiness

Check the backend:

```bash
curl -fsS http://localhost:8000/api/v1/health \
  | python3 -m json.tool
```

Confirm:

```json
"pii_detector": {
  "state": "ready",
  "coverage": "ner"
}
```

Other fields will also be present. The top-level `"status": "ok"` alone does not establish that redaction is ready.

Check gateway reachability and model mode:

```bash
curl -fsS http://localhost:8000/api/v1/meta \
  | python3 -m json.tool
```

For this installation, confirm:

```json
"gateway": "ok",
"stub_llm": false,
"trace_viewer_configured": true
```

Check the gateway’s own redaction detector:

```bash
./compose.sh exec -T gateway python -c \
'import json, urllib.request; print(json.dumps(json.load(urllib.request.urlopen("http://127.0.0.1:8090/healthz")), indent=2))'
```

Its `pii_detector` must also report `state: ready` and `coverage: ner`.

Then open [http://localhost:3000](http://localhost:3000), sign in, and complete a VITA sample. Inspect its trace for successful calls to the configured providers.

A completed report alone does not prove that a provider answered: example agents can use fallback results.

If a check fails:

```bash
./compose.sh logs --tail=100 backend gateway vector
```

### 5.8 Remove bootstrap passwords

After successful sign-in, save the account password in your password manager and clear its bootstrap value:

```ini
INITIAL_ADMIN_PASSWORD=
INITIAL_USER_PASSWORD=
```

Apply the change:

```bash
./compose.sh --profile app --profile viewer up -d
```

The accounts remain. Populated bootstrap values reset their account passwords during subsequent backend starts.

### 5.9 Enable HTTPS for remote access

Choose a hostname that resolves to the server from the client machines. For example, set:

```ini
LIBRERUN_TLS_DOMAIN=librerun.example.lan
LIBRERUN_TLS=internal
LIBRERUN_HTTPS_PORT=8443
APP_CORS_ORIGINS=https://librerun.example.lan:8443
```

Retain these values:

```ini
FRONTEND_PORT=127.0.0.1:3000
BACKEND_PORT=127.0.0.1:8000
NEXT_PUBLIC_API_URL=/api/v1
BACKEND_INTERNAL_URL=http://backend:8000
```

Build and start the HTTPS services:

```bash
./compose.sh --profile app --profile viewer --profile tls up -d --build
```

Export the local certificate authority:

```bash
docker cp \
  librerun-edge:/data/caddy/pki/authorities/local/root.crt \
  librerun-edge-root.crt
```

Use `podman cp` if running Podman.

Copy the certificate to each client machine and import it into the relevant operating-system or browser trust store. Then open:

```text
https://librerun.example.lan:8443
```

Caddy forwards API requests to the backend and other requests to the frontend. Internal service traffic remains unencrypted on the Compose network.

Jaeger remains local to the server. From a client with SSH access:

```bash
ssh -N -L 16686:127.0.0.1:16686 <user>@<host-address>
```

While the tunnel is open, use [http://localhost:16686](http://localhost:16686) on that client.

### 5.10 Stop services

For the HTTPS installation:

```bash
./compose.sh --profile app --profile viewer --profile tls down
```

Omit `--profile tls` if it was not enabled. Include any additional profiles you started.

Named data volumes remain. Adding `-v` deletes them.

## 6. Develop agents

These instructions assume the **local demo is running**, its `.env` exists, and example-agent keys have been generated. Use a separate development installation if your existing installation serves other users.

### 6.1 Install the authoring CLI

The CLI requires Python 3.11 or later and pipx.

From the repository root:

```bash
pipx install ./cli
librerun doctor
```

The CLI does not replace Compose. Use it for scaffolding, sample submission, and conformance checks.

### 6.2 Understand the generated files

| File | Edit it to define |
|---|---|
| `agent.yaml` | Identity, phases, capabilities, model steps, output mode, and container URL where applicable. |
| `input_schema.json` | Input fields and validation. |
| `agent.py` or `server.ts` | Agent behaviour. |
| `scenarios/demo.json` | A sample request with a `user_inputs` object matching the schema. |
| `requirements.txt` or `package.json` | Container dependencies. |
| `Dockerfile` | Container build and startup. |

Container scaffolding also adds a service to `agents.compose.yaml`. When `.env` already exists, it writes the new agent’s gateway key there.

The `framework` manifest field labels the agent. The `runtime` field determines how it executes.

### 6.3 LangGraph

Create the agent:

```bash
librerun init my-graph --template langgraph
```

Edit `backend/agents/my_graph/agent.py`:

- Keep the generated `LangGraphAgent` subclass.
- Replace or extend its graph nodes.
- Declare every adapter-supplied state field your nodes read.
- Return the intended result under `structured` in the final state.

The adapter supplies:

| State field | Meaning |
|---|---|
| `user_inputs` | Redacted request input. |
| `prior_analysis` | Previous phase output. |
| `user_edits` | Instructions supplied when repeating a phase. |
| `run_id` | Current run identifier. |

Inside a node, use the generated `capabilities_of` import:

```python
caps = capabilities_of(config)

response = await caps.llm.complete(
    "answer",
    [{"role": "user", "content": state["user_inputs"]["question"]}],
)
```

Declare `answer` in `llm.steps` and grant `llm` in `capabilities`.

Keep capability objects out of graph state. They contain live, run-scoped services and are unsuitable for checkpoint persistence.

For multiple phases, supply `graphs={"phase_name": compiled_graph, ...}` and declare matching phase names in the manifest.

Update the input schema and sample whenever you change expected input.

Complete reference: [LangGraph Triage](https://github.com/JeremiahJRRoss/librerun/tree/main/backend/agents/_examples/langgraph_triage).

### 6.4 Python SDK

Create a container agent:

```bash
librerun init my-python --template container-python
```

Edit `backend/agents/my_python/agent.py`.

The asynchronous `handler(ctx)` receives:

- `ctx.input`: current request input.
- `ctx.phase`: phase to execute.
- `ctx.prior_output`: previous phase output.
- `ctx.user_edits` and `ctx.rerun`: instructions for repeated work.
- `ctx.seconds_left`: remaining invocation time.
- Clients for model calls and platform services.

Keep the generated server setup. Return a JSON object from the handler. An exception fails the invocation.

Call models through `ctx.llm`; call steps declared in the manifest. Preserve the template’s distinction between model responses, stub fixtures, and fallback results.

Add dependencies to `requirements.txt`. Add any additional source files to the Dockerfile’s copy instructions: the generated Python Dockerfile initially copies `agent.py` as the application code.

### 6.5 LlamaIndex

Use the Python SDK container to host a LlamaIndex Workflow.

Create a separate agent:

```bash
librerun init my-summary --template container-python
```

Copy the working example’s implementation, dependencies, schema, and sample:

```bash
cp backend/agents/_examples/llamaindex_summarize/agent.py \
  backend/agents/my_summary/agent.py

cp backend/agents/_examples/llamaindex_summarize/requirements.txt \
  backend/agents/my_summary/requirements.txt

cp backend/agents/_examples/llamaindex_summarize/input_schema.json \
  backend/agents/my_summary/input_schema.json

cp backend/agents/_examples/llamaindex_summarize/scenarios/demo-summarize.json \
  backend/agents/my_summary/scenarios/demo.json
```

In the copied `agent.py`, change the server’s telemetry name:

```python
app = serve(handler, name="my-summary")
```

Replace `backend/agents/my_summary/agent.yaml` with:

```yaml
manifest_version: 1
id: my-summary
name: My Summary
description: Extracts key points from a document and writes a brief.

runtime: container
framework: llamaindex

container:
  url: http://my-summary:8090

input_schema: input_schema.json

phases:
  - name: summarize
    deadline_seconds: 300
    steps:
      - id: extract
        label: Extract key points
      - id: summarize
        label: Write the brief

output:
  mode: structured

capabilities: [llm]

llm:
  steps:
    - id: extract
      provider: openai
      model: gpt-4o-mini
      temperature: 0.0
      max_tokens: 500
      timeout_seconds: 30
    - id: summarize
      provider: openai
      model: gpt-4o-mini
      temperature: 0.2
      max_tokens: 600
      timeout_seconds: 30

scenarios: scenarios
```

The example constructs its workflow and `OpenAILike` clients for each invocation. `ctx.llm.client()` supplies the gateway address and invocation headers; `ctx.llm.step()` supplies values such as `librerun/extract`.

Retain this per-invocation construction when adapting the workflow. A shared client must not retain a previous invocation’s token.

Complete reference: [LlamaIndex Summarize](https://github.com/JeremiahJRRoss/librerun/tree/main/backend/agents/_examples/llamaindex_summarize).

### 6.6 Vercel AI SDK and TypeScript

Create the agent:

```bash
librerun init my-typescript --template container-ts
```

Edit `backend/agents/my_typescript/server.ts`, primarily `work()`.

Retain:

- Per-invocation bearer-token binding.
- The gateway client and `X-LibreRun-Run-Token` header.
- Trace-context propagation.
- Deadline cancellation.
- Progress and terminal events.
- Fixture and fallback attribution.

The model call uses:

```typescript
model: gateway.chat("librerun/answer")
```

Use the Chat Completions client. Declare `answer` in the manifest’s `llm.steps`.

Update `package.json`, the input schema, and the sample as needed.

There is no LibreRun TypeScript SDK package. The template implements the HTTP contract directly. It has no exporter for its own interior spans; the platform phase and gateway model calls still appear in the run’s trace.

Complete reference: [Vercel AI SDK Answer](https://github.com/JeremiahJRRoss/librerun/tree/main/backend/agents/_examples/vercel_ai_answer_ts).

### 6.7 Build, discover, and check an agent

After scaffolding or editing:

```bash
./compose.sh \
  --profile app \
  --profile viewer \
  --profile demo \
  --profile agents \
  up -d --build
```

This rebuilds the backend so it discovers the agent package and builds any new container service.

Wait for the readiness checks in section 5.7, then inspect the registration:

```bash
librerun doctor
```

Submit and check the chosen agent. For example:

```bash
librerun run --agent my-python --wait
librerun battery --agent my-python
```

Replace `my-python` with the ID you created.

If bootstrap passwords have been cleared:

```bash
librerun run --agent my-python --wait \
  --email you@example.com --password-stdin
```

Enter the password at the prompt.

A gated run is reported as waiting for approval. Open its run page to review and approve it.

The conformance battery checks integration behaviour. For container agents, its CLI path reports the interior-span check as skipped; inspect a real run’s trace to verify your instrumentation.

### 6.8 Configure discovery outside the demo

Starting a container and registering its agent are separate requirements.

- `LIBRERUN_AGENTS_PATH` controls the directories scanned by the backend.
- `container.url` tells the backend where to invoke a container agent.
- Compose profiles determine which containers start.

An empty `LIBRERUN_AGENTS_PATH` scans the standard `backend/agents` directory. To include bundled examples in the standard backend container:

```ini
LIBRERUN_AGENTS_PATH=agents:agents/_examples
```

New agents created by the CLI are already under the standard directory.

After changing discovery settings or manifests, rebuild and recreate the backend using the relevant `compose.sh … up -d --build` command.

### 6.9 Other languages and frameworks

Implement Run Contract v1:

| Endpoint | Responsibility |
|---|---|
| `GET /healthz` | Return service health. |
| `POST /v1/runs` | Accept one phase invocation and bind its bearer token to it. |
| `GET /v1/runs/{id}/events` | Stream progress and logs, ending with exactly one completed or failed event. |
| `GET /v1/runs/{id}/output` | Return the completed phase output. |

Each invocation receives input, phase information, a deadline, and access to run-scoped services.

Use the invocation token for platform requests. Preserve trace context. Send model requests to the gateway with `model: "librerun/<step-id>"`.

The detailed protocol is [Run Contract v1](https://github.com/JeremiahJRRoss/librerun/blob/main/docs/authoring/Run_Contract_v1.md).

## 7. Python SDK reference

The SDK supports Python 3.11 or later. Install it from the checkout in a Python virtual environment:

```bash
pip install "./sdk/python/librerun-agent[uvicorn,otel]"
```

The generated Python container performs this installation. The project does not publish the package to PyPI.

| Need | Interface |
|---|---|
| Serve the handler | `serve(handler)` |
| Read invocation data | `ctx.input`, `ctx.phase`, `ctx.run_id`, `ctx.invocation_id`, `ctx.tenant_id` |
| Continue or repeat work | `ctx.prior_output`, `ctx.user_edits`, `ctx.rerun` |
| Check time | `ctx.deadline`, `ctx.seconds_left` |
| Report progress | `ctx.progress(status, step=..., label=..., detail=...)` |
| Write a run log | `ctx.log(message, level="info")` |
| Call a model step | `await ctx.llm.text(step_id, prompt)` or `await ctx.llm.complete(step_id, messages)` |
| Connect a framework client | `ctx.llm.client()`, `ctx.llm.step(step_id)` |
| Read effective configuration | `await ctx.config.steps()`, `await ctx.config.step(step_id)`, `await ctx.config.settings()` |
| Search the knowledge base | `await ctx.capabilities.kb_search(query, top_k=3)` |
| Read temporary run data | `await ctx.capabilities.run_store_get(key)` |
| Write temporary run data | `await ctx.capabilities.run_store_set(key, value)` |
| Record an audit event | `await ctx.capabilities.audit_log(action_type, detail)` |
| Redact text | `await ctx.pii.redact(text)` |
| Read a declared tool secret | `await ctx.secrets.get(name)` |

### Capabilities and configuration

Declare the platform capabilities the agent uses in `agent.yaml`. Recognised capability names include `llm`, `kb`, `run_store`, `progress`, `audit`, and `pii`.

Configuration reads and declared tool-secret reads require no separate capability grant.

Container progress travels through Run Contract events. Model calls use the gateway. Other listed remote services use the run-scoped MCP endpoint.

### Knowledge search

The `kb` capability requires a configured Pinecone API key, environment setting, index, and searchable data in the tenant’s namespace. Query embeddings use the gateway.

Missing configuration or a search failure can return an empty result list. An empty list does not establish that no relevant document exists.

### Temporary storage

The run store holds JSON values scoped to a run. Its entries expire. Use it for temporary working data, not durable records.

### Personal information

LibreRun uses Presidio and pattern matching to detect supported PII. Its named-entity detection currently uses English.

Input submitted through the platform is redacted before the agent receives it. Redact independently fetched text before using it:

```python
clean_text = await ctx.pii.redact(fetched_text)
```

If this call fails, stop processing that content or omit it. Do not substitute the original text.

Gateway outbound redaction is enabled by default. This does not mean every provider response is rewritten before reaching the agent; storage and telemetry boundaries perform their own checks.

Test detection against the languages and data formats your service handles.

### Tool secrets

Declare names under `secrets`:

```yaml
secrets:
  - search_api_key
```

Read the value for the invocation:

```python
key = await ctx.secrets.get("search_api_key")
```

The platform resolves the tenant’s value, then the agent-wide default. `SecretNotSet` means neither is configured. A container does not receive an automatic fallback from the backend’s environment.

Keep secret values out of prompts, output, progress, and logs.

### Logs and traces

The Python SDK captures `print()` and Python logging inside an invocation and attributes them to that run.

With the `otel` extra and the configured relay endpoint, its exporter sends traces and logs through LibreRun’s authenticated relay. The generated Python container includes this configuration.

Agent-container logs are routed through the platform. The supplied Compose services disable the container logging driver, so `docker logs` is not the agent-log interface.

## 8. Licensing

| Material | License |
|---|---|
| First-party platform code, VITA, and documentation | AGPL-3.0-only |
| `sdk/` | Apache-2.0 |
| `backend/adapters/` | Apache-2.0 |
| `cli/src/librerun/templates/` | Apache-2.0 |
| `backend/agents/_examples/` | Apache-2.0 |

Third-party material retains its own licenses and notices.

If you modify the AGPL-covered program and allow remote network interaction, your modified version must offer those users its Corresponding Source as required by the license.

The Apache licenses cover the listed components. They do not determine every licensing obligation of an agent combined with other code, including an in-process integration.

Preserve applicable notices when reusing or distributing material. LibreRun is provided without warranty. DOCI has separate licensing status in its own repository.

See [LICENSE](https://github.com/JeremiahJRRoss/librerun/blob/main/LICENSE), [NOTICE](https://github.com/JeremiahJRRoss/librerun/blob/main/NOTICE), and the [license scope map](https://github.com/JeremiahJRRoss/librerun/blob/main/docs/release/License_Scope_Map.md). The LibreRun name is governed separately by [TRADEMARKS.md](https://github.com/JeremiahJRRoss/librerun/blob/main/TRADEMARKS.md).
