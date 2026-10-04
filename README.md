# LibreRun

## Overview

LibreRun is a self-hosted educational environment for designing services that use AI agents. It provides the surrounding application so you can study infrastructure, model and tool access, user interaction, and observability together.

Use it to run packaged agents, develop your own, or inspect how a request becomes a result. Users submit forms, review progress, approve work, and read reports. Developers can connect other interfaces through the API; a chat interface is not included.

Agents declare their inputs, execution phases, model steps, and required capabilities. LibreRun validates requests, invokes each phase, enforces deadlines, and pauses for approval where declared. Agents run inside the Python backend or as separate services.

The intended host is a small personal Linux server or a sufficiently capable Linux desktop. A Linux virtual machine is also supported. The project has no verified minimum hardware specification.

### Packaged agents

| Agent | Purpose |
|---|---|
| VITA | Investigates interoperability between two vendor products, with human approval before investigation. |
| Echo | Demonstrates a Python container agent. |
| LangGraph Triage | Demonstrates an agent running inside the backend. |
| LlamaIndex Summarize | Extracts information and produces a summary. |
| Vercel AI SDK Answer | Demonstrates a TypeScript container agent. |

The demo includes all five. Each supports fixed model responses without provider credentials.

## Services

Use `./compose.sh` from the repository root. It selects Docker Compose or Podman Compose and prepares the gateway’s agent-key file.

| Service name | Purpose | Profile |
|---|---|---|
| `backend`, `frontend` | API, forms, agent execution, approvals, reports, accounts, and PII redaction. | `app` |
| `postgres` | Persistent application records. | Always included |
| `redis` | Valkey service for temporary state and caching. | Always included |
| `gateway` | LLM proxy and router; holds provider credentials and routes declared steps to configured models. | Always included |
| `vector` | Receives and routes telemetry. | Always included |
| `jaeger` | Local trace viewer. | `viewer` |
| `echo-agent`, `llamaindex-agent`, `vercel-agent` | Packaged container examples. VITA and LangGraph run in the backend. | `demo` |
| Added agent services | Container agents created with the authoring CLI. | `agents` |
| `edge`, `edge-control` | Caddy HTTPS reverse proxy and certificate management. | `tls` |
| `otel-bridge` | Exports traces to configured external observability systems. | `obs` or `cribl` |

“Always included” applies to `up` without an explicit service list. Keep the core services running while using the application. The `full` profile is an alias for `app` plus `viewer`; it does not enable every optional service.

## Quick start

### 1. Run the local demo

Install Git and a container runtime on Linux. For Docker, use Engine 24 or later and Compose 2.24.0 or later. The wrapper also supports Podman with a compatible Compose implementation.

Your account must be able to run the container engine. The first build needs internet access to download images and dependencies.

```bash
git clone https://github.com/JeremiahJRRoss/librerun.git
cd librerun
./scripts/demo.sh
```

On a fresh checkout, the script creates configuration and credentials, builds the application, and starts the packaged agents and Jaeger. No model-provider keys, external observability account, or manual proxy permissions are required.

Open [http://localhost:3000](http://localhost:3000) and sign in with the printed credentials:

1. Select VITA and load a sample.
2. Submit it, review the first result, and approve the investigation.
3. Open the completed report and select **View trace**.

Select another packaged agent to try its form and sample.

The demo uses fixed model responses and publishes ports on the local machine. It preserves an existing `.env`; rerunning it does not reset an installation to demo mode.

For a demo on a remote Linux server, keep those bindings and forward the ports from your client:

```bash
ssh -N -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 \
  -L 16686:127.0.0.1:16686 <user>@<server>
```

Then open the same localhost URL on the client. For shared browser access without an SSH tunnel, configure HTTPS as described below.

### 2. Know what needs configuration

| Use | Required before use |
|---|---|
| Local demo | No manual application configuration. The script supplies application secrets and packaged-agent keys. |
| Real model calls | Provider credentials and outbound access from the gateway to those providers. |
| Shared HTTPS access | Hostname, certificate choice, browser trust where needed, and proxy URL settings. Follow the [HTTPS installation instructions](https://github.com/JeremiahJRRoss/librerun/blob/main/docs/platform/Install.md#https-at-the-edge). |
| A new container agent | A compatible agent package, service definition, agent key, and declared capabilities. |
| Direct internet access from an agent container | Explicitly add the `egress` network and declare `network.egress: true`. Model access through the gateway does not require this. |
| External telemetry export | Destination URLs, credentials with ingestion permission, and the matching observability configuration. |

The LLM gateway and HTTPS reverse proxy are separate services. The demo configures gateway access automatically. HTTPS can be added later; configure it before exposing the application to other machines. The supplied proxy trust is restricted to Caddy’s address; custom proxy deployments need their own trust configuration.

If your network requires an outbound proxy, arrange access for image downloads and dependency installation before the first build, and for provider requests before enabling real models.

### 3. Use real models with the packaged agents

After the local demo works, edit its existing files. Preserve generated secrets.

Add credentials to `gateway.env` for the providers the agents use. VITA’s defaults use both:

```ini
OPENAI_API_KEY=<your-openai-key>
ANTHROPIC_API_KEY=<your-anthropic-key>
```

In `.env`, set:

```ini
LIBRERUN_DEMO=false
LIBRERUN_STUB_LLM=false
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT
```

Keep `LIBRERUN_AGENTS_PATH=agents:agents/_examples` to retain all packaged agents. Add `TAVILY_API_KEY` to `.env` if you want VITA to retrieve web-search results.

Apply the changes:

```bash
./compose.sh --profile app --profile viewer --profile demo up -d --build
```

Submit a sample and inspect its trace for successful provider calls. A completed report alone is insufficient: examples may return fallback results.

This enables live calls on the local installation. Use the [installation guide](https://github.com/JeremiahJRRoss/librerun/blob/main/docs/platform/Install.md) before operating a shared service.

### 4. Enable or disable services

Profiles select containers. They do not change model mode, agent discovery, or telemetry destinations.

Start the application with local trace viewing:

```bash
./compose.sh --profile app --profile viewer up -d --build
```

Enable `demo`, `agents`, or a configured `tls` profile by adding the corresponding `--profile` argument.

Omitting a profile does **not** stop containers already running. Stop a specific optional service explicitly:

```bash
./compose.sh --profile demo stop llamaindex-agent
```

Start it again:

```bash
./compose.sh --profile demo up -d llamaindex-agent
```

Stopping a container does not unregister its agent; its form can remain visible and requests will fail. Agent discovery and removal belong in the [agent installation guide](https://github.com/JeremiahJRRoss/librerun/blob/main/docs/authoring/Agents_Install.md).

To stop the demo stack while retaining its data:

```bash
./compose.sh --profile app --profile viewer --profile demo down
```

Include any other profiles you started. Adding `-v` deletes named data volumes.

### 5. Choose observability settings

The demo sends traces through Vector to Jaeger at [http://localhost:16686](http://localhost:16686). It enables prompt and completion capture so you can inspect model interactions. Use sample data; set `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT` before processing sensitive input.

The supplied Jaeger service has no persistent volume. Export evidence you need to retain.

To enable local trace viewing, set these entries in `.env`, then start the `viewer` profile:

```ini
VECTOR_VIEWER=1
TRACE_VIEWER=jaeger
```

To disable the viewer, set:

```ini
VECTOR_VIEWER=
TRACE_VIEWER=off
```

Apply the settings and stop Jaeger:

```bash
./compose.sh --profile app up -d
./compose.sh --profile viewer stop jaeger
```

Leave `VECTOR_VIEWER` **empty** when disabling it. A value such as `0` or `false` still enables its forwarding configuration. Disabling the viewer does not disable instrumentation, application logs, or other telemetry destinations.

For Datadog, Elastic, Splunk, or Cribl, follow the [observability guide](https://github.com/JeremiahJRRoss/librerun/blob/main/docs/platform/Observability.md). Configure endpoints and ingestion permissions before starting `otel-bridge`. To disable external forwarding, clear `LIBRERUN_OBS_VENDOR` and `VECTOR_CRIBL`, recreate Vector, and stop the bridge:

```bash
./compose.sh up -d vector
./compose.sh --profile obs stop otel-bridge
```

Use `up -d` after environment changes; `restart` retains the old environment.

### 6. Add another agent

A compatible container agent needs its manifest, input schema, and service implementing [Run Contract v1](https://github.com/JeremiahJRRoss/librerun/blob/main/docs/authoring/Run_Contract_v1.md).

1. Add its package under `backend/agents/<agent-directory>`.
2. Follow its installation instructions to add the service to `agents.compose.yaml`, provision its agent key, and configure required tool credentials and network access.
3. Rebuild the backend to discover the package and start the new service. If it uses the `agents` profile:

```bash
./compose.sh --profile app --profile viewer --profile demo --profile agents up -d --build
```

Confirm the agent appears in the interface, submit its sample, and inspect its result and trace. Detailed manifests, permissions, and framework setup belong in the [agent documentation](https://github.com/JeremiahJRRoss/librerun/tree/main/docs/authoring).

**DOCI’s current status:** [DOCI — Debug Observability and Codebase Inspector](https://github.com/JeremiahJRRoss/librerun-doci) investigates support tickets using telemetry and source code. Its current build runs separately on Deep Agents and Arcade.dev. Its [LibreRun packaging is still planned](https://github.com/JeremiahJRRoss/librerun-doci/blob/main/docs/BUILD_STATUS.md), so cloning it into LibreRun does not load a working agent. Follow DOCI’s own quick start to evaluate it separately.
