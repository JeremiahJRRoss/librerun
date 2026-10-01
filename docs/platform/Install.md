# LibreRun — Installation Guide

LibreRun is an educational software environment for teaching the
design, development and operation of AI agents, and this is the guide
to running it. At its centre is the chassis, built for educational
purposes: intake UI, run lifecycle with a human approval gate, PII
redaction before anything reaches the database, per-step LLM
configuration, feedback capture, report/PDF export, multi-tenant auth,
and tracing. **Agents** plug into it and inherit all of that, and every
part of it is here to be learned from as much as run.

**Linux only.** LibreRun runs on Linux — macOS and Windows are not
supported, not through Docker Desktop and not through WSL2. Docker
Compose or Podman Compose orchestrates its containers (`compose.yaml`
with `agents.compose.yaml`, driven by `compose.sh`), typically inside a
Linux virtual machine: CI's `ubuntu-latest` runners are one, and
[`Install_CentOS_Ubuntu.md`](Install_CentOS_Ubuntu.md) is the layer for
the two distributions with their own page.

**VITA** (Vendor Interoperability Troubleshooting Agent) ships in-tree at
`backend/agents/vita_v1` and is what the demo runs. Nothing in this guide
is VITA-specific except where it says so — a LibreRun install with zero
agents is a valid, healthy install, and the chassis boots and serves
honestly with an empty agents directory.

If you want to see it working before reading anything else, jump to
[§2 Demo it without API keys](#2-demo-it-without-api-keys).

---

## Prerequisites

**Container runtime** — pick one, on Linux. Needed for Postgres, Valkey
and the telemetry router in every mode; needed for everything in staging
mode. Docker Desktop is not in the table: LibreRun runs on Linux only,
where Docker Engine is the runtime.

| Runtime | Min Version | Install |
|---------|-------------|---------|
| Docker Engine + Compose plugin | 24+ / 2.20+ | https://docs.docker.com/engine/install/ |
| Podman + podman-compose | 4.5+ / 1.1+ (`compose.yaml` includes `agents.compose.yaml`, S4) | https://podman.io/getting-started/installation |

**For development mode** (backend and frontend as local processes):

| Tool | Version | Why that version |
|------|---------|------------------|
| Python | **3.12+** | `requires-python = ">=3.12"`; the backend image is `python:3.12-slim` |
| Node.js | **22.12+** | the frontend image is `node:22-slim`, pinned by digest (a reviewed refresh follows the 22.x line; R14); the floor is 22.12 because the frontend test tooling's engine range (`vite`) starts there within the 22.x line |

**API keys** come in two kinds, and they live in two different places.

| | What it is | Whose it is | Where it goes |
|---|---|---|---|
| **Provider key** | a credential for a model provider — OpenAI, Anthropic, Google AI | the **platform's**. Only the gateway process holds one; no agent and no other service ever sees it | **`gateway.env`** at the repository root (`cp gateway.env.example gateway.env`) |
| **Tool key** | a credential for a third-party service an agent calls itself — web search, a vector store | the **agent's**, declared by the agent | that agent's own service environment; for an in-process agent, the backend's — `.env` |

Agents reach models only through the LibreRun gateway, so an agent never
holds a provider key and never names a model in code: it declares its
steps in its manifest (`llm.steps`) and the admin agent-configuration
page chooses the provider and model per step, per tenant.
[`authoring/LLM_Gateway.md`](../authoring/LLM_Gateway.md) is the whole
story, and its §3 explains the two credentials the gateway itself uses.

| Key | Kind | Get it |
|-----|------|--------|
| `OPENAI_API_KEY` | provider → `gateway.env` | https://platform.openai.com/api-keys |
| `ANTHROPIC_API_KEY` | provider → `gateway.env` | https://console.anthropic.com/settings/keys |
| `GOOGLE_AI_API_KEY` | provider → `gateway.env` | https://aistudio.google.com/app/apikey |
| `TAVILY_API_KEY` | tool (the demo agent's web search) → set per tenant through the platform, or `.env` as the fallback (§6, "An agent's tool secrets") | https://tavily.com/ |

Optional: `PINECONE_API_KEY` (internal KB vector search, the platform's
`kb.pinecone_api_key` in Admin → Settings, or this line in `.env`),
`GOOGLE_CLIENT_ID`/`SECRET` and `AZURE_CLIENT_ID`/`SECRET`/`TENANT_ID`
(SSO, in `.env`).

**You do not need any of these to install and demo LibreRun** — see §2.

### Ubuntu / Debian host packages

> Setting up on **CentOS Stream 10 (EL10)** or **Ubuntu 26.04**
> specifically? [`Install_CentOS_Ubuntu.md`](Install_CentOS_Ubuntu.md)
> is the per-OS layer for both — dnf/apt package translations, Podman
> vs. Docker CE, SELinux/AppArmor, and firewall notes — wrapped around
> the same steps this guide documents.

These matter only in **development mode**, where the backend runs on your
host rather than in the image. The container image installs them for you;
a host install does not, and the failure is quiet.

```bash
sudo apt-get update && sudo apt-get install -y \
  python3-venv python3-dev build-essential \
  libgomp1 libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
  libfontconfig1 libffi-dev shared-mime-info
```

The list from `libgomp1` onward is exactly what `backend/Dockerfile`
installs, for exactly one reason worth repeating here:

> **Without the Pango/HarfBuzz/fontconfig libraries, PDF export does not
> fail — it quietly downgrades to HTML.** `report_service.py` catches the
> WeasyPrint error, logs `weasyprint_failed` with `fallback="html"`,
> writes `<task_id>.html` and sets the format to `html`; the download then
> serves `report_<task_id>.html` as `text/html`. So the task still reports
> `complete` and the user who asked for a PDF gets an HTML file instead.
> If your exports arrive as HTML, this package list is the fix — and
> `weasyprint_failed` in the backend log is the confirmation.

On Ubuntu 24.04 the default `python3` is already 3.12. On older releases
(22.04 and earlier) `python3` is too old for this project; install 3.12
from the deadsnakes PPA or use the container run mode instead. Check
before you start:

```bash
python3 --version    # must be 3.12 or newer
```

---

## 1. Clone & Configure

> **Just want to see it?** `./scripts/demo.sh` (§2) needs no `.env` at
> all — it writes one, builds the stack and prints the credentials. This
> section is the hand-configured path.

```bash
git clone https://github.com/JeremiahJRRoss/librerun.git
cd librerun
cp .env.example .env
```

Edit `.env`. A **keyless demo** (§2) needs **four** values and no
provider key:

```bash
APP_SECRET_KEY=           # Generate: python3 -c "import secrets; print(secrets.token_urlsafe(64))"
INITIAL_ADMIN_EMAIL=      # Your admin login — bootstrapped at backend startup (§4)
INITIAL_ADMIN_PASSWORD=
LIBRERUN_STUB_LLM=true    # Ships `false`; a real run needs a provider key without it
```

Add the customer-role pair too if you want to walk §2's demo as a
customer rather than as the admin — it is optional everywhere else:

```bash
INITIAL_USER_EMAIL=
INITIAL_USER_PASSWORD=
```

For a **real investigation**, add the provider keys: paste them in
**Admin → Application Settings → Model providers**, over HTTPS or on
`localhost`, where the browser seals each one to the gateway before it
leaves the page ("Model providers" below), or put them in
**`gateway.env`**, not in `.env`:

```bash
cp gateway.env.example gateway.env
chmod 600 gateway.env
# Fill in a key for every provider your steps reference:
#   OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_AI_API_KEY
```

Only the `gateway` service loads that file, and nothing else in the
deployment holds a provider credential (decision L28). `.env` is read by
compose, by the backend and by the frontend build — and the backend is
the one process every python-package agent shares, so a provider key
there is a key every agent has. A key left on an `OPENAI_API_KEY=` line
in `.env` reaches nothing: compose no longer passes those names to any
service. `gateway.env` is git-ignored and optional; the demo writes one
holding only the gateway's store key, which a key pasted in the admin
page is kept under, and a key set there wins over `gateway.env`'s for
its provider. Tool keys are separate: the demo agent's search key is the
agent's own credential, which a tenant's admin sets for the tenant and a
platform admin for every tenant (K8a), with `TAVILY_API_KEY` in `.env` as
the fallback for the process the agent runs in (§6, "An agent's tool
secrets", and [`authoring/LLM_Gateway.md`](../authoring/LLM_Gateway.md)
§3).

### The variable reference, by class

Every variable the deployment reads sits in exactly **one** class and in
exactly **one** file (K4, decision L29). The class says *why* it is in
the environment at all, not how important it is:

| Class | Meaning | Can it become a database row? |
|-------|---------|-------------------------------|
| **[1] Bootstrap** | It must exist before a process starts: Compose expands it at parse time, or a process reads it once at boot. | No — the process that would read the row is not up yet. |
| **[2] Posture** | It decides what the deployment is allowed to do: keep, capture, export, accept. | It could, and deliberately does not. A security posture changes by reviewed deployment, not by a web toggle. The admin UI shows these read-only, with their source: Application Settings' Deployment panel ([The admin surface](#the-admin-surface)). |
| **[3] Runtime default** | The seed value under a runtime override. Admin → Settings writes the row; **Reset** returns the setting to the value here. | It already is one — the sign-in ids too, since K6 — but `OPENAI_BASE_URL`, a seed no page edits yet. |
| **[4] Secret** | A credential with no bootstrap role, read at request time by exactly one process. | Since K6, a UI-managed one lives in the encrypted secrets store, sealed with `LIBRERUN_BACKEND_SECRETS_KEY`, with the file as its fallback: today the Microsoft sign-in secret, and the tool secrets at K8. |

A secret can be class [1]. `APP_SECRET_KEY`, `POSTGRES_PASSWORD`, the
`INITIAL_*` passwords and the per-agent gateway keys are all
credentials, and every one of them must exist before anything starts —
that is what makes them bootstrap rather than class [4], and they are no
less secret for it.

The **Where** column is the file you write, and it is not a preference:
no process receives a secret it does not read (decision L28), so each
file goes to the one service that reads it.

| File | Copy from | Loaded by | Optional? |
|------|-----------|-----------|-----------|
| `.env` | `.env.example` | compose (substitution), the backend, the frontend build | no |
| `gateway.env` | `gateway.env.example` | the `gateway` service, and nothing else | yes — the keyless demo needs no provider key |
| `observability.env` | `observability.env.example` | the `vector` service, and nothing else | yes |
| `observability-traces.env` | `observability-traces.env.example` | the `otel-bridge` service, and nothing else | yes |

`backend/tests/test_env_example_classes.py` holds the tables below to the
files: every variable read by either settings model, by `compose.yaml`,
by `agents.compose.yaml` or by `config/*.yaml` appears in exactly one
example file under exactly one class banner, and every row here names
the same file and the same default the example shows. A variable
documented nowhere fails the suite, because "I set it and nothing
happened" is a defect in the documentation as much as in the code.

#### [1] Bootstrap — must be in the environment

<!-- env-class-table:1:start -->

| Variable | Where | Default | Notes |
|----------|-------|---------|-------|
| `APP_ENV` | `.env` | `development` | `development` / `staging` / `production`. Read once at boot; it decides the demo banner and a handful of defaults. |
| `APP_PORT` | `.env` | `8000` | The port uvicorn binds inside the container or on the host in dev mode. |
| `APP_HOST` | `.env` | `0.0.0.0` | The interface uvicorn binds. `0.0.0.0` inside a container is the container's own network namespace, not the host's. |
| `APP_SECRET_KEY` | `.env` | *(blank)* | JWT signing key — **required**. Generate: `python3 -c "import secrets; print(secrets.token_urlsafe(64))"`. The shipped blank is accepted only in demo mode. |
| `DATABASE_URL` | `.env` | `postgresql+asyncpg://librerun:librerun_dev_pw@localhost:5432/librerun` | Dev mode only; in containers compose builds it from `POSTGRES_*`. Its password **must** match `POSTGRES_PASSWORD`. |
| `DATABASE_POOL_SIZE` | `.env` | `20` | SQLAlchemy pool size, read when the engine is created. |
| `DATABASE_MAX_OVERFLOW` | `.env` | `10` | Connections allowed above the pool size under load. |
| `REDIS_URL` | `.env` | `redis://localhost:6379/0` | Dev mode only; in containers compose points it at the `redis` service. |
| `POSTGRES_DB` | `.env` | `librerun` | Used by the Postgres container and embedded in the container's `DATABASE_URL`. A deployment created before blueprint S2 pins `vita` here — see "Upgrading an existing deployment". |
| `POSTGRES_USER` | `.env` | `librerun` | Same; the image creates this role on first start and never again. |
| `POSTGRES_PASSWORD` | `.env` | `librerun_dev_pw` | Same. `POSTGRES_PASSWORD_FILE` is the **image's** own convention and works, but `DATABASE_URL` must still match what it holds. |
| `LIBRERUN_PGDATA_VOLUME` | `.env` | `librerun-pgdata` | Named volume for the database. Pin the name your `docker volume ls` shows to keep a pre-S2 deployment's data. |
| `LIBRERUN_REDIS_VOLUME` | `.env` | `librerun-redis-data` | Named volume for the cache server's persistence (Valkey, under the `redis` name). |
| `LIBRERUN_FILES_VOLUME` | `.env` | `librerun-file-storage` | Named volume for uploaded files. |
| `LIBRERUN_STATE_VOLUME` | `.env` | `librerun-state` | Named volume for agent state, mounted at `/app/data/state`. |
| `POSTGRES_PORT` | `.env` | `5432` | Host port, bound to `127.0.0.1` only. |
| `REDIS_PORT` | `.env` | `6379` | Host port, bound to `127.0.0.1` only. |
| `BACKEND_PORT` | `.env` | `127.0.0.1:8000` | Host binding for the API, `[HOST:]PORT` — loopback by default (T1), so other machines reach LibreRun through the HTTPS edge; `0.0.0.0:8000` publishes plain HTTP on every interface, and `compose.sh` refuses it while the `tls` profile is requested (see "HTTPS at the edge"). Change the port and `NEXT_PUBLIC_API_URL` and `APP_CORS_ORIGINS` must follow. |
| `FRONTEND_PORT` | `.env` | `127.0.0.1:3000` | Host binding for the web UI, `[HOST:]PORT`, loopback by default like `BACKEND_PORT` and held to it by the same guard. |
| `GATEWAY_PORT` | `.env` | `8090` | Host port for the LLM gateway, bound to `127.0.0.1` only — the browser never talks to it. |
| `VECTOR_OTLP_GRPC_PORT` | `.env` | `4317` | Host port for Vector's OTLP/gRPC receiver, loopback only. A dev-mode backend ships to it. |
| `VECTOR_OTLP_HTTP_PORT` | `.env` | `4318` | Host port for Vector's OTLP/HTTP receiver, loopback only. |
| `JAEGER_UI_PORT` | `.env` | `16686` | Host port for the bundled Jaeger UI (profile `viewer`). Change it and `TRACE_VIEWER_BASE_URL` must follow. |
| `LIBRERUN_HTTPS_PORT` | `.env` | `8443` | The HTTPS edge's one published port, on every interface (profile `tls`; T1). `443` is `LIBRERUN_HTTPS_PORT=443` under rootful Docker, and a sysctl or a redirect under rootless Podman — see "HTTPS at the edge". Inert without the profile. |
| `LIBRERUN_TLS` | `.env` | `internal` | Where the edge's certificate comes from: `internal` (its own local CA, whose root you trust), an e-mail address (ACME — Let's Encrypt, then ZeroSSL — which needs the name to resolve publicly and 443 to reach the edge), or `/certs/<cert> /certs/<key>` (your own files, from `LIBRERUN_TLS_CERT_DIR`). Read by the edge alone. |
| `LIBRERUN_TLS_DOMAIN` | `.env` | `localhost` | The name browsers reach the edge by, which its certificate carries. A LAN names the host here; `localhost` sends no HSTS header. Read by the edge alone. |
| `LIBRERUN_TLS_CERT_DIR` | `.env` | `./tls` | The directory mounted read-only into the edge as `/certs`, for `LIBRERUN_TLS=/certs/…`. Point it at a directory LibreRun alone reads, owner-only (the mount relabels it for the container under SELinux); the default ships empty and git-ignored. |
| `LIBRERUN_TLS_CA` | `.env` | *(blank)* | A CA of your own for the edge to issue from, `/certs/<ca.crt> /certs/<ca.key>` in `LIBRERUN_TLS_CERT_DIR`, in place of the root it generated: the environment's way to keep one root across a restore, since the edge's volumes are in no backup. Blank keeps the generated root. A choice made in the admin UI's Certificates panel wins until “Use the environment's setting”. Read by the edge alone, at its start. |
| `LIBRERUN_EDGE_NET` | `.env` | `172.16.87` | The first three octets of the edge network, a `/28` at `.0`: the edge at `.2` — the one address the backend trusts forwarding headers from — and the backend, the web UI and, under the `tls` profile, `edge-control` from `.8`. Not inert: every start creates the network, since the backend and the web UI join it, and an engine refuses a subnet another network already holds, so a second LibreRun on this host (or a network already on `172.16.87.0/28`) moves it, e.g. `172.16.88`. |
| `LIBRERUN_IMAGE_PREFIX` | `.env` | *(blank)* | The name the local build takes: blank, `localhost/librerun`, a local name. Every first-party image is built from this checkout and never pulled (`pull_policy: build`, #133), since a release is source only; set it to name a registry of your own to push the build to — compose still builds it here. |
| `LIBRERUN_IMAGE_TAG` | `.env` | *(blank)* | Which tag the local build takes: blank, `dev`. |
| `NEXT_PUBLIC_API_URL` | `.env` | `http://localhost:8000/api/v1` | Baked into the **browser** bundle at image build time — override it before `./compose.sh --profile app build`, not after. The example lists the five layouts; behind the HTTPS edge it is `/api/v1`. |
| `BACKEND_INTERNAL_URL` | `.env` | *(blank)* | Server-side rewrite target for the Next.js process — `/api/v1/*` is proxied there — baked in at image build time like the one above (T1 measured a run-time-only value proxying nothing), so rebuild after changing it. Blank in local dev; `http://backend:8000` behind the HTTPS edge or when the backend is not exposed publicly. |
| `PLATFORM_TENANT_SLUG` | `.env` | `dev` | The tenant whose admins are the platform operators. The schema seeds `dev`; change it only if you created another tenant row first. |
| `INITIAL_ADMIN_EMAIL` | `.env` | *(blank)* | Admin account bootstrapped at backend startup (§4). Leave either half blank to skip the pair — the account already in the database stays. |
| `INITIAL_ADMIN_PASSWORD` | `.env` | *(blank)* | Re-applied on every start while it is set. **Blank it after the first sign-in** (§4). |
| `INITIAL_USER_EMAIL` | `.env` | *(blank)* | Optional `customer`-role account, same mechanism. |
| `INITIAL_USER_PASSWORD` | `.env` | *(blank)* | Same; the §2 demo signs in as a customer, so set the pair for that walkthrough. |
| `LIBRERUN_AGENTS_PATH` | `.env` | *(blank)* | Where filesystem agent discovery looks: one directory or several separated by `:`. Blank means `backend/agents` (`/app/agents` in the image). Pip-installed agents are found regardless. |
| `LIBRERUN_STATE_DIR` | `.env` | *(blank)* | The state directory: where agents keep file-shaped state only, never configuration — an agent's settings are valued per tenant in the database since K5b. The backend reads it, and before discovery copies a value set here into its environment, where an agent reads it. Compose sets `/app/data/state` on a named volume, so what is kept there survives container recreation. |
| `LIBRERUN_PUBLIC_URL` | `.env` | *(unset)* | Base URL at which agent **containers** reach this chassis; advertised as the run-scoped MCP endpoint. **Leave it commented.** `.env` reaches the containers too, so a value here outranks compose's `http://backend:8000` — and `http://localhost:8000`, from inside a container, is that container itself. Commented, containers get the service name and a local `uvicorn` gets `http://localhost:8000`. Set it only for an address right for both, or for agents outside this network. |
| `<AGENT>_AGENT_URL` | `.env` | *(unset)* | One per container agent, named by the agent's own manifest (`container.url`). The chassis knows none of them (L13); compose carries a default for each bundled example. |
| `LIBRERUN_GATEWAY_URL` | `.env` | *(unset)* | Where the backend sends model calls. **Leave it commented**, for the same reason as `LIBRERUN_PUBLIC_URL`: set to the localhost URL it points the backend container at itself, and `/api/v1/meta` reports the gateway `unreachable`. Commented, containers get `http://gateway:8090` and a local `uvicorn` gets `http://localhost:8090`. |
| `LIBRERUN_GATEWAY_ENV_FILE` | `.env` | `gateway.env` | The file the `gateway` service loads its provider keys and its store key from. Point it at a decrypted copy to run with no plaintext `gateway.env` on disk — see "Encrypting `.env` at rest". |
| `LIBRERUN_KEY_ROTATION_GRACE_HOURS` | `.env` | `24` | How long a rotated agent key's `_PREVIOUS` value keeps working. |
| `LIBRERUN_AGENT_KEY_<ID>` | `.env` | *(unset)* | One per container agent: `<ID>` is the agent id upper-cased with every character outside `[A-Z0-9]` replaced by `_`. They must exist **before** `up`. `compose.sh` derives `agent-keys.env` from them. |
| `PINECONE_ENVIRONMENT` | `.env` | *(blank)* | Vector-store region, e.g. `us-east-1-aws`. Not a credential; the key is class [4]. |
| `PINECONE_INDEX_NAME` | `.env` | `librerun-kb` | Index to use. Pin the old name when upgrading — indexes cannot be renamed (§10). |
| `VECTOR_VIEWER` | `.env` | *(unset)* | Set `1` to make Vector load `config/vector-viewer.yaml` and forward traces to the bundled Jaeger. Pair it with `--profile viewer`. |
| `VECTOR_JAEGER_ENDPOINT` | `.env` | `http://jaeger:4318/v1/traces` | Where that overlay forwards to. Point it elsewhere to feed any other OTLP/HTTP receiver. |
| `VECTOR_CRIBL` | `.env` | *(unset)* | Set `1` to load the Cribl log overlay and start the bridge for traces. The endpoints and tokens live in the two observability files. |
| `LIBRERUN_OBS_VENDOR` | `.env` | *(blank)* | `datadog`, `elastic`, `splunk`, or empty for none. Compose expands it while building Vector's and the bridge's command lines, which is why it is here and the vendors' own settings are not. An unknown value names no config, so nothing is forwarded on a guess. |
| `LIBRERUN_OBS_SERVICE_NAME` | `observability.env` | *(unset)* | `service.name` stamped on log lines that carry no OTLP resource of their own — the backend's JSONL file. Records arriving over OTLP keep their own. |
| `DATADOG_LOGS_ENDPOINT` | `observability.env` | *(unset)* | Scheme + host, **no path** — the sink appends `/api/v2/logs`. This is also where a non-default Datadog site is expressed. |
| `ELASTIC_URL` | `observability.env` | *(unset)* | Cluster or Elastic Cloud URL, **no path** — the sink appends `/_bulk`. |
| `ELASTIC_AUTH_SCHEME` | `observability.env` | *(unset)* | `ApiKey` is Elastic's documented scheme; a cluster on basic auth sets `Basic` and puts the base64 pair in `ELASTIC_API_KEY`. |
| `ELASTIC_LOGS_DATASET` | `observability.env` | *(unset)* | The `<dataset>` half of Elastic's `logs-<dataset>-<namespace>` data stream naming. |
| `ELASTIC_LOGS_NAMESPACE` | `observability.env` | *(unset)* | The `<namespace>` half. |
| `SPLUNK_HEC_URL` | `observability.env` | *(unset)* | HEC base URL, **no path** — the sink appends `/services/collector/event`. |
| `SPLUNK_INDEX` | `observability.env` | *(unset)* | Target index. |
| `SPLUNK_SOURCETYPE` | `observability.env` | *(unset)* | Sourcetype stamped on each event. |
| `CRIBL_HEC_ENDPOINT` | `observability.env` | *(unset)* | Base URL, **no path** — e.g. `https://default.main.<org>.cribl.cloud:8088`. |
| `VECTOR_OTLP_FORWARD_ENDPOINT` | `observability.env` | *(unset)* | An OTLP/HTTP `/v1/traces` receiver for the commented sink gallery in `config/vector.yaml` (Vector's OTLP sink is HTTP-only). Unused until you uncomment the matching sink. |
| `CLICKHOUSE_ENDPOINT` | `observability.env` | *(unset)* | Sink gallery: ClickHouse HTTP endpoint, e.g. `http://clickhouse:8123`. |
| `VECTOR_S3_BUCKET` | `observability.env` | *(unset)* | Sink gallery: bucket for archived logs. The S3 sink takes AWS credentials from Vector's own provider chain, not from a variable here. |
| `AWS_REGION` | `observability.env` | *(unset)* | Sink gallery: region for that bucket. |
| `LOKI_ENDPOINT` | `observability.env` | *(unset)* | Sink gallery: Loki push endpoint, e.g. `http://loki:3100`. |
| `DD_AGENT_OTLP_URL` | `observability-traces.env` | *(unset)* | The Datadog **Agent's** OTLP/HTTP traces URL, full path. Datadog's own intake is not OTLP, so the bridge sends the Agent no credential and needs none. |
| `ELASTIC_APM_OTLP_URL` | `observability-traces.env` | *(unset)* | APM OTLP intake URL, full path. |
| `ELASTIC_APM_AUTH_SCHEME` | `observability-traces.env` | *(unset)* | `ApiKey`, or `Bearer` for a self-managed APM server taking a secret token. |
| `SPLUNK_OTLP_URL` | `observability-traces.env` | *(unset)* | Splunk Observability's OTLP trace ingest, full path. |
| `CRIBL_OTLP_ENDPOINT` | `observability-traces.env` | *(unset)* | gRPC target `host:port`, **no path**. TLS is the default. |
| `LIBRERUN_SOURCE_URL` | `.env` | *(unset)* | Where the source of the running version can be fetched: `/api/v1/meta` serves it beside `license`, and the login page and the navigation bar link to it. Unset or blank means the default, the public repository at the running version's tag (`v` + the version), which names the unmodified release. **If you run a modified LibreRun, set it** to where the Corresponding Source of your version can be fetched (AGPL-3.0 §13); the production checklist says so too. |
| `LIBRERUN_BACKEND_SECRETS_KEY` | `.env` | *(blank)* | The key the backend seals the secrets set in Admin → Settings with: a comma-separated list of Fernet keys, the first current. Generate one with `head -c 32 /dev/urandom \| base64 \| tr '+/' '-_'`; `_FILE`-capable. Blank means the store is unconfigured — setting a secret answers `503 secrets_store_unconfigured` and each secret falls back to its variable — and a malformed entry stops the backend at boot. Back it up with the database. See "The secrets store key" below. |
| `LIBRERUN_GATEWAY_SECRETS_KEY` | `gateway.env` | *(blank)* | The key the **gateway** seals its own rows in the secrets store with — the provider keys pasted in Admin → Application Settings, and the keypair the browser seals them to: a comma-separated list of Fernet keys, the first current, generated like the backend's and **never the same key** (the gateway refuses to boot on a key that sealed another process's row). `_FILE`-capable, and never on an `environment:` line, which would override `gateway.env` and, blank, erase it. The demo writes one. Blank means no provider key can be kept from the admin page — it says so, and `gateway.env`'s keys serve — and a malformed entry stops the gateway at boot. See "The gateway's store key" below. |

<!-- env-class-table:1:end -->

#### [2] Posture — in the environment by policy

These decide what the deployment may do. They stay in the environment on
purpose: trace export wires at boot, and content capture, the PII
fail-open switch and the logging walk are security postures that should
change through a reviewed deployment rather than a web toggle. The admin
UI shows them read-only, with their source ([The admin surface](#the-admin-surface)).

<!-- env-class-table:2:start -->

| Variable | Where | Default | Notes |
|----------|-------|---------|-------|
| `LIBRERUN_STUB_LLM` | `.env` | `false` | Keyless mode — the **gateway** answers every model call from fixtures instead of calling a provider. Everything else is real, and reports produced this way say they are fixtures. Must be `false` in production. |
| `LIBRERUN_DEMO` | `.env` | `false` | What `scripts/demo.sh` boots: generated credentials, the stub LLM, the example agents and Jaeger. The only mode that accepts the shipped default `APP_SECRET_KEY`. Never in production. |
| `LIBRERUN_MAX_PHASE_SECONDS` | `.env` | `3600` | Ceiling on one phase invocation's wall-clock budget. A manifest's `phases[].deadline_seconds` applies under it; a phase that declares none gets exactly this. |
| `CREDENTIALS_ENABLED` | `.env` | `true` | Email/password auth. Must stay `true` for the bootstrapped accounts to sign in. |
| `BCRYPT_ROUNDS` | `.env` | `12` | Password hashing cost. |
| `PII_CONFIDENCE_THRESHOLD` | `.env` | `0.7` | Presidio NER confidence threshold. |
| `PII_PHONE_REGION` | `.env` | `US` | ISO 3166-1 alpha-2 region a bare number is parsed as a national phone number of. Read by the backend **and** the gateway. |
| `LIBRERUN_PII_ALLOW_DEGRADED` | `.env` | `false` | Leave it `false`. `true` keeps redacting with the regex stages alone when Presidio's named-entity stage cannot run — names, places and organisations are then stored and exported unredacted. Check `pii_detector.state` on `GET /api/v1/health` and the gateway's `/healthz`. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `.env` | *(unset)* | Leave it **commented** and containers default to `http://vector:4317`. Uncomment it **empty** to disable export in containers — the explicit blank is honoured. In dev mode set `http://localhost:4317`. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `.env` | `grpc` | `grpc` (4317) or `http/protobuf` (4318). |
| `OTEL_SERVICE_NAME` | `.env` | `librerun-backend` | `service.name` on every backend span. |
| `OTEL_DEBUG` | `.env` | `false` | Streams every finished span to stderr and turns on exporter DEBUG logs. Works without an endpoint. Verbose — leave it off in production. |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | `.env` | `NO_CONTENT` | The sensitivity switch for the **run** telemetry plane: `NO_CONTENT` / `SPAN_ONLY` / `EVENT_ONLY` / `SPAN_AND_EVENT`. Shipped `NO_CONTENT` so a fresh install keeps prompts and completions out of its traces; a process with it set nowhere still falls back to full capture. |
| `UX_TELEMETRY_ENABLED` | `.env` | `true` | Runtime kill switch for the browser plane: `false` and browsers receive 410 and stop sending, with no frontend rebuild. |
| `UX_TELEMETRY_USER_UNITS_PER_MINUTE` | `.env` | `600` | Quota per verified user; a unit is `max(records, KiB)` per request. Beyond it: 429 + `Retry-After`. |
| `UX_TELEMETRY_TENANT_UNITS_PER_MINUTE` | `.env` | `6000` | The same quota per tenant. |
| `UX_TELEMETRY_GLOBAL_UNITS_PER_MINUTE` | `.env` | `60000` | The same quota for the deployment. |
| `LOG_LEVEL` | `.env` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`, read by the backend **and** the gateway. Every record is walked before any handler sees it, so this is a volume dial, not a privacy one. The LLM SDK loggers are pinned to `INFO`/`WARNING` regardless. |
| `LOG_FORMAT` | `.env` | `json` | `json` or `console`. |
| `LOG_FILE_PATH` | `.env` | `./data/logs/backend.jsonl` | The JSONL file. Bind-mounted from `./data/logs` in container mode, which is what Vector tails. |
| `LOG_FILE_ROTATION_WHEN` | `.env` | `midnight` | `TimedRotatingFileHandler`'s `when`. |
| `LOG_FILE_BACKUP_COUNT` | `.env` | `14` | How many rotated files to keep. |
| `LOG_REDACT_PII` | `.env` | `true` | Walk every record for PII before any sink sees it. |
| `LOG_QUEUE_ONLY` | `.env` | `true` | Queue-only logging (S4): the walk happens on a listener thread, `print()` and raw descriptor writes are captured, and no handler can be attached that bypasses it. `false` restores the synchronous pipeline (the test suite's mode). |
| `LOG_STDERR_ENABLED` | `.env` | `true` | Whether the walked records also reach stderr. |
| `FILE_STORAGE_BACKEND` | `.env` | `local` | `local` is the only backend in 1.0. |
| `FILE_STORAGE_PATH` | `.env` | `./data/files` | Where uploads land. In containers this is the file-storage volume. |
| `KB_EMBED_MAX_QUERIES` | `gateway.env` | *(unset)* | Ceiling on the queries one knowledge search may embed. In `gateway.env` because the gateway in a container never reads `.env`. |
| `KB_EMBED_MAX_QUERY_CHARS` | `gateway.env` | *(unset)* | Ceiling on one query's length. The gateway **refuses** an over-long query rather than truncating it, so it never embeds something other than what was asked for. |

<!-- env-class-table:2:end -->

#### [3] Runtime defaults — the value until something overrides it

These are the seed layer proper. A row written by a **platform
operator** — an admin of `PLATFORM_TENANT_SLUG`, default `dev`, the
tenant your bootstrap credentials land in — outranks the value here, and
**Reset** in Admin → Settings returns the setting to it. The rows are
application-global, read by every tenant's requests, which is why other
tenants' admins cannot edit them.

Editable at `/admin/settings` today: `cors_origins` (sets a
restart-required flag), `session_timeout_minutes`, `max_upload_size_mb`,
`require_approval_before_phase2`, `default_llm_provider`, `kb.embed_model`,
the three trace-viewer link settings and, since K6, the sign-in settings
`auth.google_client_id`, `auth.azure_client_id`, `auth.azure_tenant_id`
and the secret `auth.azure_client_secret`, applied immediately — a
sign-in reads them when it happens. A row marked "no UI override yet" is
a seed whose page a later batch adds; until then the value here is the
only one there is.

<!-- env-class-table:3:start -->

| Variable | Where | Default | Notes |
|----------|-------|---------|-------|
| `APP_CORS_ORIGINS` | `.env` | `http://localhost:3000` | Comma-separated frontend origins. Admin → Settings overrides it (`cors_origins`) and sets a restart-required flag. |
| `JWT_EXPIRY_HOURS` | `.env` | `24` | Default session lifetime, and the default for the `session_timeout_minutes` app setting Admin → Settings overrides live. |
| `TRACE_VIEWER` | `.env` | `off` | Shapes the `trace_url` the run pages render as **View trace**. `off` until a viewer is really there; presets `jaeger` / `phoenix` / `tempo` / `langsmith` / `custom`. Overridden live in Admin → Settings. |
| `TRACE_VIEWER_BASE_URL` | `.env` | `http://localhost:16686` | Viewer UI base, opened by your **browser** — hence a host address, not a compose service name. Overridden live. |
| `TRACE_VIEWER_URL_TEMPLATE` | `.env` | *(blank)* | Overrides the preset; may use `{base}` and `{trace_id}`. Required by `langsmith` and `custom`. Overridden live. |
| `LIBRERUN_KB_EMBED_MODEL` | `.env` | `openai/text-embedding-3-small` | The model the platform embeds a knowledge-search query with, as `provider/model`. Admin → Settings overrides it (`kb.embed_model`); the gateway enforces the effective value. |
| `OPENAI_BASE_URL` | `.env` | *(blank)* | An OpenAI-**compatible** endpoint to send every OpenAI step to — a local server, a proxy, a gateway of your own. Applies to chat and embeddings alike. No UI override yet. |
| `GOOGLE_CLIENT_ID` | `.env` | *(blank)* | Google OAuth client id. Not a secret, and the whole of Google's configuration: sign-in verifies the ID token with the client id alone. Admin → Settings overrides it (`auth.google_client_id`); domain and email allow-lists are runtime settings in Admin → Auth Config. |
| `AZURE_CLIENT_ID` | `.env` | *(blank)* | Microsoft Entra ID application id. Admin → Settings overrides it (`auth.azure_client_id`). |
| `AZURE_TENANT_ID` | `.env` | *(blank)* | Tenant UUID for single-tenant, or `common` for multi-tenant. Admin → Settings overrides it (`auth.azure_tenant_id`). |

<!-- env-class-table:3:end -->

#### [4] Secrets — where each one lives

Each of these is read at request time by exactly **one** process, so
each lives where that one process can reach it and nowhere else
(decision L28). A key on the wrong line is not a smaller mistake than a
missing one — it reads as configured and reaches nothing.

<!-- env-class-table:4:start -->

| Variable | Where | Default | Notes |
|----------|-------|---------|-------|
| `OPENAI_API_KEY` | `gateway.env` | *(blank)* | Read by the `gateway` service and no other process. `cp gateway.env.example gateway.env && chmod 600 gateway.env`; the file is optional and the keyless demo needs none. |
| `ANTHROPIC_API_KEY` | `gateway.env` | *(blank)* | Same file, same one reader. |
| `GOOGLE_AI_API_KEY` | `gateway.env` | *(blank)* | Same file, same one reader. |
| `AZURE_CLIENT_SECRET` | `.env` | *(blank)* | The other half of `AZURE_CLIENT_ID`. Admin → Settings sets it in the encrypted secrets store (`auth.azure_client_secret`, K6), write-only; this line is its fallback while the store holds none. Google sign-in has no secret to set. |
| `TAVILY_API_KEY` | `.env` | *(blank)* | An **agent's** own third-party key (L20): the in-process fallback of the demo agent's declared `tavily_api_key`, read from the process environment by the secrets capability while neither this tenant's value nor every tenant's default is set (K8a) — which is why there is deliberately no `_FILE` spelling for it. Not deprecated (D35). |
| `PINECONE_API_KEY` | `.env` | *(blank)* | The knowledge base's vector-store key: the fallback of the platform's secret setting `kb.pinecone_api_key` (Admin → Settings, K8a). Its index coordinates are not secrets and are class [1]. |
| `DATADOG_API_KEY` | `observability.env` | *(unset)* | Read by `vector` alone. |
| `ELASTIC_API_KEY` | `observability.env` | *(unset)* | The **cluster** credential, read by `vector` alone. The APM key is a different value with a different name. |
| `SPLUNK_HEC_TOKEN` | `observability.env` | *(unset)* | From the HEC source's Auth Tokens tab. Read by `vector` alone. |
| `CRIBL_HEC_TOKEN` | `observability.env` | *(unset)* | From the Cribl HEC source's Auth Tokens tab. Read by `vector` alone. |
| `ELASTIC_APM_API_KEY` | `observability-traces.env` | *(unset)* | The **APM** credential, read by `otel-bridge` alone. Elastic issues it separately from the cluster key, and naming them separately is what lets each process hold only the one it reads. |
| `SPLUNK_ACCESS_TOKEN` | `observability-traces.env` | *(unset)* | Splunk Observability's access token, sent as `X-SF-Token`. Read by `otel-bridge` alone — a different product from the HEC source the log leg uses. |
| `CRIBL_OTLP_TOKEN` | `observability-traces.env` | *(unset)* | Optional Cribl source auth token, sent as `Authorization: Bearer …`. Read by `otel-bridge` alone. |

<!-- env-class-table:4:end -->

> **Password consistency:** the password in `DATABASE_URL` **must** match
> `POSTGRES_PASSWORD`. Change one, change the other.

> **Keep exactly one `.env`, at the repository root.** The backend reads
> two dotenv sources — the root file (anchored to `app/config.py`, so it
> is found from any directory) and `./.env` **relative to the working
> directory**, which wins. Dev mode runs `uvicorn` from `backend/`, so a
> stray `backend/.env` silently outranks the root one and you get a
> backend configured from a file you have forgotten about. If logins fail
> against credentials you are sure you set, look for that file first.

> **`gateway.env` is the second file, and there are two more.** It holds
> the three provider keys and nothing else, and one process reads it: the
> gateway. It sits beside `.env` at the repository root and is anchored
> the same way, so `uvicorn gateway.main:app` finds it from any working
> directory, and `gateway.env` outranks `.env` for the names both spell —
> which is what makes a leftover blank `OPENAI_API_KEY=` in an upgraded
> `.env` harmless. `chmod 600` it; it is git-ignored. The backend names it
> nowhere, on purpose.
>
> The other two are `observability.env` and `observability-traces.env`,
> loaded by `vector` and `otel-bridge` respectively (and by nothing
> else). Both are optional: with neither on disk the stack is the one
> that ships. Two files rather than one because a Splunk deployment
> takes a *different* credential for each leg — a HEC token for logs and
> an Observability access token for traces — and one shared file would
> hand each process the other's. Whatever moves off the host encrypted,
> encrypt all four the same way ("Encrypting `.env` at rest").

### Secrets as files (`<NAME>_FILE`)

Docker secrets, Podman secrets and Kubernetes' mounted secrets all
deliver a value the same way: they put it in a file and tell the process
where. LibreRun reads that convention directly — no entrypoint wrapper,
no `export $(cat …)` — so a secret store can reach the backend and the
gateway with nothing in between (decision L30).

For any secret variable `X`:

| Situation | What happens |
|---|---|
| `X` is set | It is used. A deployment that has never heard of this is unaffected. |
| `X` is blank and `X_FILE` is set | The value is the file's contents, with **one** trailing newline removed (`printf` writes one, every editor adds one, and a session key ending in `\n` works until someone rotates it by hand). |
| Both are set and **differ** | The process refuses to start: `SecretSourceConflict: X and X_FILE disagree`. No precedence is invented — one of them is what you meant and there is no way to tell which. The message names the variable and never a value. |
| Both are set and agree | Fine. |
| `X_FILE` is set but unreadable | Refused **by path**, at start, rather than three layers downstream with "your secret is empty". |

The value reaches the settings model and **never** the process
environment, which is the point: `docker inspect` shows a container's
environment, and an entrypoint that exported the file's contents would
hand the value straight back to anything that can read it.

**Which variables** — the backend, from `.env`: `APP_SECRET_KEY`,
`DATABASE_URL`, `REDIS_URL`, `AZURE_CLIENT_SECRET`,
`INITIAL_ADMIN_PASSWORD`, `INITIAL_USER_PASSWORD`, `PINECONE_API_KEY`,
`LIBRERUN_BACKEND_SECRETS_KEY`. The gateway, from `gateway.env`: `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `GOOGLE_AI_API_KEY`, `LIBRERUN_GATEWAY_SECRETS_KEY`,
plus `DATABASE_URL` and `REDIS_URL`. Both example files carry the
spelling as a comment beside each key.

Two variables that look like they belong and do not:

- **`TAVILY_API_KEY`** — an agent's own third-party key, the in-process
  fallback of the tool secret the demo agent declares, read out of the
  process environment by name rather than through the chassis' settings
  (L20, K8a). A `TAVILY_API_KEY_FILE` would bind nothing the agent reads:
  set the key for a tenant through the platform instead (§6, "An agent's
  tool secrets"), sealed in the store, or give this as an ordinary
  environment variable.
- **`POSTGRES_PASSWORD_FILE`** — real, and not ours: it is the Postgres
  image's own convention, read by that image's entrypoint. Use it the
  way the image documents it, and remember the password in `DATABASE_URL`
  must still match.

#### The compose overlay

Keep the default `compose.yaml` as it is — it has to stay startable with
nothing configured — and add an overlay beside it:

<!-- secrets-as-files-overlay:start -->
```yaml
# compose.secrets.yaml — secrets as files. Use it BESIDE the default
# file, never instead of it:
#   docker compose -f compose.yaml -f compose.secrets.yaml --profile app up -d
services:
  backend:
    environment:
      # Blank the plain variable. compose.yaml interpolates a value
      # for each of the backend's, and an inherited value is still a
      # value: leave it and the backend stops with SecretSourceConflict
      # rather than quietly preferring one source over the other. The
      # gateway's keys come from gateway.env, which an operator using
      # files simply does not write — blanked here anyway, so the
      # overlay says what it means whatever that file holds.
      APP_SECRET_KEY: ""
      APP_SECRET_KEY_FILE: /run/secrets/app_secret_key
      INITIAL_ADMIN_PASSWORD: ""
      INITIAL_ADMIN_PASSWORD_FILE: /run/secrets/initial_admin_password
      LIBRERUN_BACKEND_SECRETS_KEY: ""
      LIBRERUN_BACKEND_SECRETS_KEY_FILE: /run/secrets/backend_secrets_key
    secrets:
      - app_secret_key
      - initial_admin_password
      - backend_secrets_key
  gateway:
    environment:
      OPENAI_API_KEY: ""
      OPENAI_API_KEY_FILE: /run/secrets/openai_api_key
      # The gateway's store key (K7): never the backend's.
      LIBRERUN_GATEWAY_SECRETS_KEY: ""
      LIBRERUN_GATEWAY_SECRETS_KEY_FILE: /run/secrets/gateway_secrets_key
    secrets:
      - openai_api_key
      - gateway_secrets_key

secrets:
  app_secret_key:
    file: ./secrets/app_secret_key
  initial_admin_password:
    file: ./secrets/initial_admin_password
  backend_secrets_key:
    file: ./secrets/backend_secrets_key
  openai_api_key:
    file: ./secrets/openai_api_key
  gateway_secrets_key:
    file: ./secrets/gateway_secrets_key
```
<!-- secrets-as-files-overlay:end -->

Create the files it names, with no trailing newline of their own to
worry about either way:

```bash
mkdir -p secrets && chmod 700 secrets
printf '%s' "$(python3 -c 'import secrets;print(secrets.token_urlsafe(64))')" > secrets/app_secret_key
printf '%s' 'the-admin-password-you-chose'                                    > secrets/initial_admin_password
printf '%s' "$(head -c 32 /dev/urandom | base64 | tr '+/' '-_')"              > secrets/backend_secrets_key
printf '%s' 'sk-your-provider-key'                                            > secrets/openai_api_key
printf '%s' "$(head -c 32 /dev/urandom | base64 | tr '+/' '-_')"              > secrets/gateway_secrets_key
chmod 444 secrets/*        # see below
docker compose -f compose.yaml -f compose.secrets.yaml --profile app up -d
```

`chmod 444` on the files and `700` on the directory, and the pair is
deliberate. A compose `file:` secret is bind-mounted with the host
file's ownership and mode; both the backend and the gateway drop to an
unprivileged user before reading their settings, so a `600` root-owned
file is one the process cannot read and the container refuses to start
by path. The directory's `700` is what keeps other accounts on the host
out — the file mode is about the container, the directory mode is about
the host.

Podman reads the same file, and `podman secret` works the same way.
Under Kubernetes, mount the Secret as a volume and point `X_FILE` at the
mounted path — there is nothing LibreRun-specific to do.

> **What this does and does not buy you.** It keeps these values out of
> the container's environment and therefore out of `docker inspect` and
> out of every child process. Every *other* variable is still plain
> environment, in memory and in `inspect`, whatever the file on disk
> looks like — so `_FILE` and encryption at rest answer different
> questions, and a deployment that needs both wants both.

### The secrets store key

A secret a platform admin sets in **Admin → Settings** — today the
Microsoft sign-in secret, `auth.azure_client_secret` — is not written to
any file. It lives in the database's `secrets` table as MultiFernet
ciphertext, sealed with `LIBRERUN_BACKEND_SECRETS_KEY`, which only the
backend receives (K6, decisions L30 and L31). The page never shows the
value again: it shows where the value comes from and a fingerprint, a
keyed 12-character digest that changes with the key, so two deployments
can be compared without either revealing its secret. The next sign-in
uses a new value with nothing restarted.

**Generate** a key — 32 random bytes as url-safe base64, which is what a
Fernet key is; 64 hex characters are not one — and put it in `.env`:

```bash
head -c 32 /dev/urandom | base64 | tr '+/' '-_'
# LIBRERUN_BACKEND_SECRETS_KEY=<the 44 characters it printed>
```

`scripts/demo.sh` and `librerun demo` generate one for the demo. The key
is `_FILE`-capable like every backend secret (`LIBRERUN_BACKEND_SECRETS_KEY_FILE`,
in the overlay above). The backend parses it at boot: a malformed entry
stops it, naming the entry's position and never the key.

**Blank** means the store is unconfigured, in every mode. Setting a secret
answers `503` with `"code": "secrets_store_unconfigured"`, and each secret
setting reads its environment variable instead — `AZURE_CLIENT_SECRET` for
the Microsoft one — so a deployment that never sets a key loses nothing it
had before. `librerun doctor` says which it is.

**Rotate** by prepending, since the variable is a comma-separated list
whose first key seals and whose every key opens:

1. `LIBRERUN_BACKEND_SECRETS_KEY=<new>,<old>` in `.env`, and recreate the
   backend (`./compose.sh --profile app up -d --force-recreate backend`);
2. `./compose.sh exec backend python -m app.scripts.rewrap_secrets` —
   every row under an older key is re-encrypted under the new one, its
   fingerprint recomputed, in one transaction (`--dry-run` only names
   them);
3. drop `<old>` and recreate the backend again.

**A lost key** — or a database restored under a different one — leaves
rows no configured key opens. Nothing breaks: each such setting serves its
environment variable, and Admin → Settings marks it
`unreadable — replace or clear`. `python -m app.scripts.rewrap_secrets
--dry-run` names every one (exit code 3); **Replace** seals a new value
under the current key, and **Clear** removes the row. The page decides
from each row's key id and never reads its ciphertext, so a row damaged
in the database under a key that is still configured shows as set: the
sign-in that reads it logs `secret_unreadable` and uses the environment
variable, and the dry run, which opens every row, names it with the rest.

**Back the key up with the database.** A dump restored without its key
holds ciphertext nothing opens, so every backup of one needs the other —
encrypted, which is what "Encrypting `.env` at rest" below is for.

### The gateway's store key

A provider key pasted in **Admin → Application Settings → Model
providers** is kept by the gateway, never by the backend (K7, decisions
L33 and D33). The browser seals it to the gateway's public key; the
backend stores that blob as the `gateway` row `provider.<name>` of the
`secrets` table, which it cannot open; the gateway opens it with the
private half of its sealing keypair, re-seals it as MultiFernet
ciphertext under `LIBRERUN_GATEWAY_SECRETS_KEY`, and the next model call
uses it — within seconds, nothing restarted. The keypair is a row of the
same table under the same key, so every replica of the gateway seals to
one public key.

**Generate** a key as you generated the backend's, a second time — the
two must differ — and put it in **`gateway.env`**:

```bash
head -c 32 /dev/urandom | base64 | tr '+/' '-_'
# gateway.env: LIBRERUN_GATEWAY_SECRETS_KEY=<the 44 characters it printed>
```

`scripts/demo.sh` and `librerun demo` write one into the demo's
`gateway.env`, owner-only, or add one to a `gateway.env` that has neither
the key nor its `_FILE`, making that file owner-only first — and adding
nothing to one they may not make owner-only. The key is `_FILE`-capable
(`LIBRERUN_GATEWAY_SECRETS_KEY_FILE`, in the overlay above) and goes
nowhere else: not in `.env`, and not on an `environment:` line, which
would override `gateway.env` and, blank, erase it. The gateway parses it
at boot: a malformed entry stops it, naming the entry's position and
never the key, and so does a key that also sealed another process's row
— the backend's key given to both — naming both variables. `librerun
doctor` reports it: `[ok]` set, `[warn]` blank, `[FAIL]` not a Fernet key
or the backend's.

**Blank** means no provider key can be kept from the admin page: the
gateway publishes no public key, the page says so instead of offering a
field, and the keys in `gateway.env` serve as before.

**Rotate** by prepending, as the backend's key is rotated, with the
gateway's own command:

1. `LIBRERUN_GATEWAY_SECRETS_KEY=<new>,<old>` in `gateway.env`, and
   recreate the gateway (`./compose.sh --profile app up -d
   --force-recreate gateway`);
2. `./compose.sh run --rm gateway python -m gateway.rewrap` — every
   `gateway` row under an older key is re-encrypted under the new one, in
   one transaction (`--dry-run` only names them); the backend's rows are
   never touched;
3. drop `<old>` and recreate the gateway again.

`--rotate-keypair` replaces the sealing keypair instead: the private half
in its row and the public key the page reads, in one transaction. Stored
provider keys are Fernet rows and are unaffected; a key pasted but not
yet taken by the gateway when it runs is left `rejected`, to be pasted
again. The page's fingerprint then changes, and the gateway logs the new
one.

**A lost key** — or a database restored under a different one — is an
outage, not a fallback: the keypair no longer opens, and the gateway
refuses to start rather than replace a keypair another replica may be
sealing to. Blank the key, or discard what no key opens:

```bash
./compose.sh run --rm gateway python -m gateway.rewrap --discard-unopenable
```

It deletes, by name, every `gateway` row no configured key opens — the
keypair's included, which the gateway makes afresh at its next start —
and exits `3` without the flag, naming them. The provider keys that were
stored are then re-entered in the admin page, or served from
`gateway.env` meanwhile. Back the key up with the database, as the
backend's.

### Model providers

**Admin → Application Settings → Model providers** lists the three
provider keys the gateway holds — `openai` (the `openai` and `azure`
steps), `anthropic`, and `google` (`gemini`, `google` and `vertex_ai`);
Bedrock is not among them — with where each comes from: `runtime ·
<fingerprint>` for a key set here, `from gateway.env`, `not set`,
`pending` until the gateway has taken a new key, or `rejected` with the
reason. A key set here wins over `gateway.env`'s for its provider;
**Clear** returns the provider to `gateway.env`. The field is never
pre-filled — the server has no key to give — and is emptied after every
Set, Replace or Clear.

The page seals in the browser, so it needs a **secure context**: HTTPS
("HTTPS at the edge" below) or `localhost`. Over plain HTTP from any
other address the browser offers no WebCrypto; the page says so, points
at `gateway.env`, and sends nothing. It shows the fingerprint of the key
it seals to, `SHA256:<64 hex>`: compare it once with the gateway's boot
line, which must match —

```bash
./compose.sh logs gateway | grep gateway_sealing_key
```

— or the page is not sealing to your gateway. A key is one word of
visible ASCII of at most **318 bytes**, which every OpenAI, Anthropic and
Gemini API key is; a Vertex service-account JSON (about 2.3 KB) is not,
and goes in `gateway.env` as `GOOGLE_AI_API_KEY_FILE`. In keyless mode
(`LIBRERUN_STUB_LLM=true`) a key set here waits, unused, until keyless
mode is turned off.

### Encrypting `.env` at rest (sops and age)

`.env` and `gateway.env` are plaintext on the host, owner-only. That is
the right shape for the host itself: a scheme that decrypted a value on
the same machine, beside its own key, would protect nothing that
`chmod 600` does not (decision L30). What it does not cover is every
**copy** of the file — the ops repository, the backup, the second host,
the laptop the deployment was set up from. Those copies are encrypted
with [sops](https://github.com/getsops/sops) and
[age](https://github.com/FiloSottile/age): the file stays a dotenv file
with its keys readable and its values sealed, so it diffs, reviews and
commits like any other file, and one of two modes decides what the host
holds.

Verified with sops 3.13.3 and age 1.1.1; nothing here is
LibreRun-specific beyond one variable and one script behaviour, both
named below.

**Setup, once.** An age identity for whoever decrypts — an operator, or
the deploying host — and a `.sops.yaml` naming the public half:

```bash
sudo apt-get install -y age        # Ubuntu; on EL10 take the age and sops release binaries
mkdir -p ~/.config/sops/age && age-keygen -o ~/.config/sops/age/keys.txt
# Public key: age1...   ← the recipient; the file holds the private half (written owner-only)
cp .sops.yaml.example .sops.yaml   # then put YOUR recipient(s) in it
```

sops is not in most distributions' repositories; take the release
binary from its releases page and check its checksum file, or
`go install github.com/getsops/sops/v3/cmd/sops@v3.13.3`. `.sops.yaml`
holds public keys only and is safe to commit; it is read from the
working directory or any directory above it, and its one rule matches
every `*.env` file — `.env`, `gateway.env`, the observability pair.

**The encrypted copies keep the `.env` suffix**, in a directory that
says what they are:

```
encrypted/.env            # sops-encrypted copy of .env
encrypted/gateway.env     # sops-encrypted copy of gateway.env
```

Not `.env.enc`: sops tells the dotenv format from the suffix, and the
`exec-env` subcommand has no `--input-type` flag to say it any other
way, so a `.enc` name works for `sops -d` and fails for the strict
mode below. Keeping the suffix makes every subcommand read the file
correctly with no flags at all.

#### Mode A — copies encrypted, host plaintext owner-only

The common case. The encrypted copies live in the ops repository and
in backups; the host holds the plaintext, written once, mode 600.

<!-- encrypt-at-rest-mode-a:start -->
```bash
# Encrypt (from the host that has the plaintext, or wherever it was authored):
mkdir -p encrypted
sops -e .env         > encrypted/.env
sops -e gateway.env  > encrypted/gateway.env
# Commit encrypted/ to the ops repository. Never commit .env or gateway.env.

# Deploy (on a host that holds the age identity):
(umask 077 && sops -d encrypted/.env        > .env)
(umask 077 && sops -d encrypted/gateway.env > gateway.env)
./compose.sh --profile app up -d
```
<!-- encrypt-at-rest-mode-a:end -->

The `umask 077` subshell is what makes the plaintext owner-only from
its first byte, rather than world-readable for the instant before a
`chmod`. To change a value, edit the encrypted copy in place —
`sops encrypted/gateway.env` opens it decrypted in `$EDITOR` and
re-encrypts on save; `sops set` does one key non-interactively — then
decrypt again on the host. `git diff` on the encrypted file shows which
*keys* changed and never a value.

#### Mode B — no plaintext at rest on the host

Strict. Nothing is decrypted to disk that outlives the command. The
root file is handed to `compose.sh` through the **environment** —
`sops exec-env` decrypts it into the environment of one child process
and forwards nothing to disk — and the gateway's file is decrypted into
a private temporary file that exists only while the command runs,
named to compose through `LIBRERUN_GATEWAY_ENV_FILE`:

<!-- encrypt-at-rest-mode-b:start -->
```bash
sops exec-env encrypted/.env \
  'sops exec-file --no-fifo encrypted/gateway.env \
     "env LIBRERUN_GATEWAY_ENV_FILE={} ./compose.sh --profile app up -d"'
```
<!-- encrypt-at-rest-mode-b:end -->

Three things make this work, each of them checked by a test:

- **`compose.sh` reads the agent keys from the environment.** Compose
  itself reads the shell before `.env` when it expands
  `${LIBRERUN_AGENT_KEY_<ID>:?}` for an agent container, and since K3
  `compose.sh` derives `agent-keys.env` the same way — environment
  first, then the file, the same value under two names refused across
  both — so with no `.env` on disk the gateway still registers every
  key the containers present.
- **`LIBRERUN_GATEWAY_ENV_FILE`** is the path of the gateway's
  `env_file` in `compose.yaml` (`${LIBRERUN_GATEWAY_ENV_FILE:-gateway.env}`).
  Unset, it is `gateway.env` beside `compose.yaml`, as before. sops
  substitutes `{}` with the temporary file's path; `--no-fifo` makes
  that a regular file (mode 600, in a private directory) rather than a
  named pipe. A pipe is consumed by its first reader and blocks sops
  until one arrives, so it works only when exactly one process reads
  the file exactly once; a file survives however compose — or Podman's
  — chooses to read it.
- **The whole stack starts inside the command.** `up -d` returns once
  the containers are started, and they keep their environment: the
  temporary file is deleted the moment the command exits and the
  decrypted variables die with the child process. Every later command
  that needs no secret — `./compose.sh ps`, `logs`, `down` — runs
  bare; one that recreates the gateway or an agent container (`up`
  after a change, `restart` after a `.env` edit) is run through the
  same two `sops` calls again, and so is any command that names an
  agent profile (`--profile demo down`, say), because compose expands
  the fragment's `${LIBRERUN_AGENT_KEY_<ID>:?}` for every command that
  requests that profile.

The same shape covers the dev-mode backend, which reads `.env` from
disk through its settings model when there is one and from the
environment when there is not: `sops exec-env encrypted/.env
'uvicorn app.main:app --port 8000'`.

**Where the key lives is the honest limit of mode B.** The host no
longer holds the secrets; it holds the one identity that opens them —
`~/.config/sops/age/keys.txt`, or `SOPS_AGE_KEY_FILE`, or a key
produced on demand by `SOPS_AGE_KEY_CMD` from a password manager or a
hardware token — so what changes is what a copied disk, a backup or
a stray `scp` of the directory carries (nothing usable), and what a
compromise has to obtain (one file, or one token, rather than every
secret in the clear). It does not make a running host secret-free, and
it cannot: the containers hold their values in memory, and every
variable delivered through `environment:` or an `env_file` is visible
in `docker inspect` whatever the file on disk did. That is the same
sentence as the end of the `_FILE` section, from the other side:
`_FILE` keeps a value out of `inspect`, encryption at rest keeps it out
of copies, and a deployment that needs both wants both.

#### Rotation and recovery

- **A new operator, a lost laptop, a leaving colleague:** edit
  `.sops.yaml` — add the recipient, or remove it — then
  `sops updatekeys encrypted/.env encrypted/gateway.env` re-wraps
  each file's data key for the new set without touching the values.
  Removing a recipient stops future decryption with that identity; it
  does not un-know what that identity already read, so rotate the
  *values* too (`sops set`, or edit in place) when a key may have
  leaked. `sops rotate -i <file>` replaces the data key alone.
- **Losing every identity in `.sops.yaml` loses the file.** age has no
  recovery. Keep at least two recipients — an operator's and a
  deployment host's, or an age key and a KMS key — and keep the private
  halves where a backup of the encrypted copies cannot reach them.

#### A KMS instead of, or beside, the age key

The age recipient is one option, not the only one. sops **key
groups** name AWS KMS, GCP KMS, Azure Key Vault and HashiCorp Vault
keys in the same `.sops.yaml` (`.sops.yaml.example` has the four
lines), so a cloud deployment can decrypt under an instance role with
no key file on the host at all, every decryption shows up in the
KMS's audit log, and access is revoked from the KMS side. Several
groups on one rule mean any one of them can decrypt;
`shamir_threshold` asks for more than one. Nothing in the two modes
above changes: the file, the commands and the variable are the same
whichever key opens it.

---

## 2. Demo it without API keys

### The one-command demo (blueprint S3)

On a machine with Docker (or Podman) and nothing else:

```bash
./scripts/demo.sh
```

If there is no `.env`, the script writes one for **demo mode**: a
generated `APP_SECRET_KEY` and secrets-store key
(`LIBRERUN_BACKEND_SECRETS_KEY`, in Fernet's format, so Admin → Settings
can take a secret), `INITIAL_ADMIN_EMAIL=admin@librerun.example`
with a generated password, `LIBRERUN_DEMO=true`, `LIBRERUN_STUB_LLM=true`,
`LIBRERUN_AGENTS_PATH=agents:agents/_examples` (the bundled agent and the
LangGraph example side by side), `VECTOR_VIEWER=1` and `TRACE_VIEWER=jaeger`
(traces to the local Jaeger, "View trace" links pointing at it) and
content capture on so the viewer shows the conversation. It
then builds and starts the `app`, `viewer` and `demo` profiles, waits for
`/api/v1/health`, and prints the UI URL, the credentials and the Jaeger
URL. An existing `.env` is used as is, but for two top-ups that never
rewrite a line: a key for any agent it predates, and — in demo mode, when
neither `LIBRERUN_BACKEND_SECRETS_KEY` nor its `_FILE` is set — a store
key, on one dated line, the file made owner-only first; one you may not
make owner-only gets no store key, and the script says so.

Demo mode is the only mode in which the backend accepts the shipped
default secret; it logs a banner at startup, the UI shows one on every
page, and the login page says where the credentials are. `GET
/api/v1/meta` (public) reports `demo`, `stub_llm`,
`trace_viewer_configured` (with `trace_viewer`, the preset rendering the
links, and `trace_viewer_source`, `env` or `runtime`), the installed
agents, and `license` and `source_url` — the licence this LibreRun is
under and where the source of the version it runs is (`LIBRERUN_SOURCE_URL`,
§1), which the login page and the navigation bar link to — the facts the
UI and `scripts/demo.sh` read. To leave demo mode: set a real `APP_SECRET_KEY`, unset
`LIBRERUN_DEMO`, set `LIBRERUN_STUB_LLM=false` and add provider keys;
`./compose.sh --profile app --profile viewer up -d --force-recreate`
applies the change.

### The keyless pipeline by hand

`LIBRERUN_STUB_LLM=true` runs the pipeline **for real** — intake
validation, both phases, the approval gate, the report, the trace — and
answers only at the provider boundary, from canned per-step fixtures.
Nothing about the chassis is mocked; only the LLM call is. The demo script
sets it; with a hand-written `.env`:

```bash
# .env
LIBRERUN_STUB_LLM=true
OTEL_DEBUG=true          # dev mode only — see the note below; without a tracer there is no trace id
```

Then start the stack (§3–§5). There are two ways to see it work.

### The demo, in the UI

This is LibreRun as a user meets it, and it needs no shell, no
credentials on a command line and no Python:

1. Open <http://localhost:3000> and sign in — with the credentials the
   demo script printed, or the `INITIAL_USER_*` / `INITIAL_ADMIN_*` pair
   from §1 (either can submit a run).
2. **+ New Run** → under **Choose an agent**, every installed agent is a
   card: its name, its description, a badge naming the framework its
   manifest declares (or its runtime), and its sample scenarios as
   **Try a sample** chips (blueprint S7). Pick VITA's card if more than
   one agent is installed.
3. Press a **Try a sample** chip. That fills every field of the intake
   wizard from the agent's own demo scenario file — the same one CI
   drives — and opens the wizard on its **Review & Submit** step. VITA's
   wizard has seven steps: the six its manifest declares, then the
   Review step the chassis appends to any stepped intake; **Back** walks
   them if you want to see what was filled in. **Configs** declares no
   fields — it is an informational placeholder telling you config upload
   arrives later.
4. Submit. The run page shows the phase timeline and the labelled
   progress list; the run stops at the approval gate.
5. **Approve & continue**, and the report renders on the run page with
   **Export ▾** (HTML or PDF) and per-section feedback controls. A
   `structured` agent's result renders as collapsible sections with
   **Copy JSON** instead.
6. A run that ended in error says why in one sentence and offers **Run
   again**, which reopens the intake with that run's inputs; the
   operator-facing detail is on the admin run view.
7. Every run records a trace id. When a viewer is configured
   (`TRACE_VIEWER` — the demo's Jaeger, or your own per §5) the run page
   shows **View trace ↗**, which opens the viewer on this run; the admin
   run view shows the raw id under **Observability**. With no viewer
   configured there is no link: search the id where your traces go
   (Cribl, if you forward there).

> **Do not expect to watch it work — keyless mode is too fast to see.**
> The run page polls every 2s while a run is unfinished (and stops once
> it is), and fixture-backed answers have no provider latency to fill
> that gap. Measured over three runs against a
> warm backend: once the submit returns, the run reports `refining` for
> only ~0.11s before it reaches the gate, and `investigating` for ~1.0s
> after approval. So the page usually goes submitted → gate → finished
> report with neither the *"Analyzing your inputs…"* panel nor the
> per-step progress list ever on screen. Replaying those timings against
> a 3s poller, roughly two offsets in three never observe the
> investigating state at all.
>
> Nothing is lost: the steps are recorded and `/runs/{id}/progress`
> serves them — that endpoint is what the smoke gate asserts — and the
> run page lists them under **Steps** once the run is `complete`, with
> the model that answered each LLM step. Configure a real provider and
> both phases become easy to watch, because every step is then a real
> LLM call.

### The same thing, automated

`scripts/librerun_smoke.py` drives exactly that sequence over the API and
fails loudly if any step doesn't happen. It is what CI runs, and it is
useful locally when you want an exit code rather than a look:

```bash
python3 scripts/librerun_smoke.py \
  --base-url http://localhost:8000 --agent vita-v1 \
  --email="$LR_USER" --password="$LR_PW" \
  --admin-email="$LR_ADMIN" --admin-password="$LR_APW"
```

**`python3` is enough** — the script imports only the standard library,
so it runs in staging mode where no `backend/.venv` exists, and in
development mode without activating one.

**The `=` in `--password="$LR_PW"` is load-bearing.** Written with a space,
a value that begins with `-` is read by `argparse` as the next option, and
you get `argument --password: expected one argument` before the script ever
reaches the backend. The backend imposes no character rules on a password,
so `-secret` is a legitimate one. The `--opt=value` form has no such
ambiguity, and the same applies to all four credential flags.

Put the credentials in shell variables rather than typing them inline;
no quoting style is safe for every password, since a single-quoted string
cannot contain an apostrophe and double quotes still expand `$` and
backticks. A variable expanded as `"$LR_PW"` is not re-evaluated:

```bash
IFS= read -r  -p 'Customer email:    ' LR_USER
IFS= read -rs -p 'Customer password: ' LR_PW;   echo
IFS= read -r  -p 'Admin email:       ' LR_ADMIN
IFS= read -rs -p 'Admin password:    ' LR_APW;  echo
```

> `read` is line-oriented, so it cannot carry a password containing a
> **newline** — the backend accepts one, and this would submit only the
> first line. If yours has one, use the UI instead.

> **Do not `source` `.env` to fill them.** `set -a && . ./.env && set +a`
> looks like the obvious shortcut and is worse: bash *evaluates* the file,
> so it and the backend disagree about your password. Measured against
> what the backend reads:
>
> | in `.env` | backend reads | `source` gives you |
> |---|---|---|
> | `pa$$word1` | `pa$$word1` | `pa5114word1` — the shell's PID |
> | `pw$(echo BAD)x` | `pw$(echo BAD)x` | `pwBADx` — **it ran the command** |
> | ``pw`echo BAD`x`` | ``pw`echo BAD`x`` | `pwBADx` — **likewise** |
> | `pw;echo BAD` | `pw;echo BAD` | truncated, and `BAD` executed |
> | `pass word` | `pass word` | bash exits 127 |

Exit code 0 means the stack booted, the agent was discovered, intake
validated and persisted, both phases ran through a human gate, at least
one progress step was streamed, a report exists containing what the agent
promised, and the run carries a trace id.

> **A local `uvicorn` needs tracing switched on, or the last assertion
> fails.** With `OTEL_EXPORTER_OTLP_ENDPOINT` unset the backend logs
> `otel_init_skipped`, no SDK tracer is installed, and the run carries no
> trace id — so the script completes the whole demo and *then* exits with
> `SmokeFailure`. Any one of these fixes it:
>
> - `OTEL_DEBUG=true` — installs a tracer with no exporter and no extra
>   containers. Simplest, and what the `.env` above uses.
> - `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317` — also ships the
>   spans to the Vector container from §3.
> - `--no-require-trace` on the command — drops that half of the gate
>   rather than satisfying it. Say so if you report the result.
>
> Container mode is unaffected: `compose.yaml` defaults the endpoint to
> `http://vector:4317`.

> **Every fixture says it is a fixture.** Reports produced in stub mode
> are smoke artifacts and are marked as such in their own text. Never
> enable this where a real answer is expected.

> **Turn it off before running the test suite.** `pytest` reads the same
> `.env`, and the provider-boundary tests assert on real call behaviour —
> leaving `LIBRERUN_STUB_LLM=true` set makes 24 of them fail in a way
> that looks like a code defect and isn't.

The same mode is what CI's `librerun-smoke` workflow uses to prove the
stack on a runner with no keys.

---

## 3. Start Infrastructure (Postgres + Valkey + Vector)

```bash
./compose.sh up -d
./compose.sh ps
```

`compose.sh` auto-detects Docker vs Podman. Force one with
`COMPOSE_ENGINE=docker` or `COMPOSE_ENGINE=podman`. `compose.yaml` uses
fully-qualified image names (`docker.io/library/...`) so both runtimes
work without configuration, each pinned by tag and digest (R14).

All three containers (`librerun-postgres`, `librerun-redis`,
`librerun-vector`) should show `healthy`. The schema loads automatically
on first start from `backend/db/schema.sql`, mounted into the
Postgres `docker-entrypoint-initdb.d` directory. Vector is the bundled
telemetry router (`config/vector.yaml`): the backend ships OTLP traces to
it and it tails the backend's JSON log file. Watch everything flow with
`./compose.sh logs -f vector`.

Verify the database:

```bash
psql postgresql://librerun:librerun_dev_pw@localhost:5432/librerun -c '\dt'
```

> **Upgrading a pre-LibreRun checkout:** containers were renamed
> `vita-*` → `librerun-*` in the rebrand. On your next `up -d` they are
> recreated under the new names (use `--force-recreate`, or `down` first,
> if your compose version leaves the old ones behind). Data is
> unaffected — the named volumes (`librerun-pgdata`, `librerun-redis-data`,
> `librerun-file-storage`, `librerun-state`) keep their names and contents.
> A deployment created before blueprint S2 has volumes under the old names:
> §10 says how to keep them. One created before A3 ran Redis, whose
> `librerun-redis-data` Valkey cannot read: §10 says to remove it.

---

## 4. Admin Account (Bootstrap)

No user accounts are seeded in the database. The backend bootstraps them
at startup from environment variables
(`backend/app/scripts/bootstrap_admin.py`):

| Variables | Role | Notes |
|-----------|------|-------|
| `INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD` | `admin` | Leave either blank to skip |
| `INITIAL_USER_EMAIL` / `INITIAL_USER_PASSWORD` | `customer` | Optional second account |

Set the admin pair in `.env` (§1) before starting the backend. The
backend, Alembic, and the bootstrap script all load the repository-root
`.env` automatically, whatever directory they run from, so one file
serves both run modes. The bootstrap is idempotent and **always
re-applies the env values on startup** — rotate a password by editing
`.env` and restarting. `CREDENTIALS_ENABLED` must stay `true`.

- **Staging mode (all containers):** nothing to do — the backend
  entrypoint runs the bootstrap after migrations.
- **Development mode (local backend):** run it once after migrations —
  it's part of the Terminal 1 setup in §5.

#### After the first sign-in, blank the password (K4)

`INITIAL_ADMIN_PASSWORD` is a bootstrap value: it exists to get the
first account into a database that seeds none. Once you have signed in,
delete the value and leave the empty line:

```bash
# .env — after the first sign-in
INITIAL_ADMIN_EMAIL=you@example.com
INITIAL_ADMIN_PASSWORD=
```

Then `./compose.sh --profile app up -d` to restart. **A blank half skips
that pair and leaves the account exactly as it is** — it is not deleted,
not deactivated, and its role is unchanged. The backend log says
`bootstrap_user_skipped` with `reason="email or password env var blank"`,
which is the line to look for.

Why it matters, in the two directions it goes wrong:

- While the pair is filled in, the password sits in plaintext on disk
  for the deployment's lifetime — in `.env`, in every backup of it, and
  in whatever copies the ops repository holds. The bootstrap needed it
  for about a second.
- The script **always overwrites**: it rehashes the environment's value
  on every boot. So a password you changed in the UI is silently
  reverted the next time a container restarts, and the old one — the one
  on disk — is the one that works again. Blanking the line is what stops
  that.

Setting the pair again restores it: fill both halves in, restart, and
the account's password is the new value. That is the recovery path for a
lost admin password, not the steady state. The same applies to
`INITIAL_USER_*`.

If you must keep the value on the host — an unattended rebuild, an
immutable image — use `INITIAL_ADMIN_PASSWORD_FILE` and a secret store
("Secrets as files" in §1), or keep the plaintext only inside
`sops exec-env` ("Encrypting `.env` at rest"). Both keep it out of a
file that survives the boot that needed it.

### The admin surface

There are two admin roles. An admin of the **platform tenant** — the
tenant your bootstrap credentials land in (`PLATFORM_TENANT_SLUG`,
default `dev`) — operates the deployment. An admin of any other tenant
administers that tenant alone: its users, its sign-in, its runs and
audit. The server holds the line (L31): a platform route answers a
tenant admin `403`, and the page says "Platform operators only." with
the reason, instead of an error.

Every field says where its value lives, on a chip beside it:

| Chip | Where the value lives | Who changes it |
|------|-----------------------|----------------|
| **deployment** | the deployment's environment; read-only in the UI | whoever deploys: edit `.env`, then restart |
| **platform** | a row every tenant reads | a platform admin |
| **this tenant** | a row of this tenant alone | an admin of this tenant |
| **this agent** | one agent's value for every tenant: a tool secret's default | a platform admin |
| **this agent · this tenant** | one agent's value in this tenant alone | an admin of this tenant |

The pages, each with its scope:

- **Users & Access** and **Auth Configuration** — this tenant.
- **Application Settings** — platform: the settings rows, **Model
  providers**, **Certificates** (the HTTPS edge) and the read-only
  **Deployment** panel. The panel shows the version, licence and source;
  the gateway's version, when it last reported and whether it is keyless;
  each posture and bootstrap name with its value, `environment` or
  `default`, and where to change it; whether each OTLP header variable
  is set, never its value; and the transport this page came over —
  through the edge, a link to Certificates; on plain HTTP, a pointer to
  "HTTPS at the edge". "Environment" there means the process environment,
  compose's defaults included. No secret is on it: it is an allowlist of
  names, and a URL loses its userinfo and query.
- **Observability** — the deployment's pipeline, platform admins only.
- **An agent's page** — its **Steps** and **Settings** (this agent ·
  this tenant), its **Secrets** (this tenant's row and the default,
  this agent), and, for a platform admin, its **Keys**.

The **Keys** tab holds the agent's gateway keys, a platform admin's
alone, since a key answers for every tenant's runs. **Issue** gives an
agent with no key its first; **Rotate** replaces it, and the old key
keeps working for the grace window you choose (0 to 720 hours, 24 by
default) so a container still holding it has time to be recreated;
**Revoke** drops every key issued there. A new value is shown **once**,
read-only with Copy, and is stored nowhere: after Done, the next read or
leaving the page, only its eight-character prefix remains, and a lost
value is rotated rather than recovered. A key from the environment
(`LIBRERUN_AGENT_KEY_<ID>`) offers neither button: it is rotated in
`.env`, with `librerun key rotate <id>`. A key installed for an id no
agent is registered under is named so, and the admin hub lists such ids
and has an agent-id field that opens an agent's page for a first key.

A tenant admin sees their tenant's pages; the hub marks Application
Settings and Observability "platform operators only", and the agent page
shows no Keys tab.

`librerun doctor`, signed in as a platform admin, prints the same view
in a section of its own, "Deployment (as the backend reads it)": each
name with its value and source, each header variable set or not, the
gateway's version and report, and the transport. Signed in as a tenant
admin it does not ask, and says so; its "Trace endpoint" section reads
`.env`, printing the endpoint without its userinfo or query.

---

## 5. Choose a Run Mode

### Development Mode (recommended for active development)

Postgres, Valkey and Vector run in containers; backend and frontend run as
local processes with hot reload.

**Terminal 1 — Backend:**

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# First time only: spaCy NLP model for PII redaction. NOT optional
# since blueprint S4c — without it the named-entity stage cannot run
# and the chassis refuses every intake, upload and agent output
# rather than storing text it could only half redact. `GET
# /api/v1/health` says which.
python -m spacy download en_core_web_lg

# First time only: stamp Alembic baseline and apply migrations
alembic stamp 0001_baseline
alembic upgrade head

# Create accounts from INITIAL_* in .env (idempotent)
python -m app.scripts.bootstrap_admin

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**Terminal 2 — Frontend:**

```bash
cd frontend
npm install
npm run dev
```

| Service | URL |
|---------|-----|
| Frontend | http://localhost:3000 |
| Backend API | http://localhost:8000/api/v1/health |
| Swagger docs | http://localhost:8000/docs |

### Staging Mode (all containers)

```bash
./compose.sh --profile app up -d --build
./compose.sh --profile app logs -f
```

Same endpoints, every service a container, orchestrated by Docker
Compose or Podman Compose. Mirrors production.

### HTTPS at the edge

The `tls` profile puts HTTPS between the browser and LibreRun (decision
L35): the `edge` service — Caddy, pinned by version and digest, configured
by `config/Caddyfile` — terminates TLS on one published port and hands
`/api/v1/*` to the backend and everything else to the web UI. While it is
on, the plain ports stay on this host's loopback, which `compose.sh`
enforces. Behind the edge the services still speak plain HTTP to each
other on the compose network (TLS between them is on the roadmap, v1.2
and later). The keyless demo never uses it: `http://localhost:3000` is
already a secure context, and the demo stays there.

**Turn it on** — three lines in `.env`, then build:

```bash
# .env
LIBRERUN_TLS_DOMAIN=librerun.example.lan   # the name browsers use; the certificate carries it
NEXT_PUBLIC_API_URL=/api/v1                # the browser calls the edge's own origin
BACKEND_INTERNAL_URL=http://backend:8000   # a start without the profile still serves the UI
```

```bash
./compose.sh --profile app --profile tls up -d --build
```

and open `https://librerun.example.lan:8443`. `--build` is not optional:
both URLs are baked into the web UI when it is built, and a bundle that
calls an absolute `http://` API from an `https` page is blocked by the
browser as mixed content. The ports need no line: `BACKEND_PORT` and
`FRONTEND_PORT` bind `127.0.0.1` by default.

**The guard.** `compose.sh` refuses any command that requests the profile
— a `--profile` argument or `COMPOSE_PROFILES` (the environment or `.env`)
naming `tls`, or `*`, which is every profile — and exits 4 before anything
starts, naming each line to set, unless `BACKEND_PORT` and `FRONTEND_PORT`
are `127.0.0.1:<port>` or `[::1]:<port>`, `NEXT_PUBLIC_API_URL` is
`/api/v1` and `BACKEND_INTERNAL_URL` is `http://backend:8000`, each read
as compose reads it (the environment first, even empty, then `.env`, then
`compose.yaml`'s default). The binding is the guard because nothing else
can be: Docker publishes a port with its own iptables rules, ahead of
ufw's and firewalld's, so a host firewall does not close it, and no
override file can remove a publish. It holds every such command, `down`
included; fix the line, or stop the edge with `docker stop librerun-edge`.
Plain HTTP on every interface is still yours to choose without the
profile — `BACKEND_PORT=0.0.0.0:8000` and `FRONTEND_PORT=0.0.0.0:3000` —
and it carries every password typed into the login page in the clear.

**Where the certificate comes from** — `LIBRERUN_TLS`, which the Caddyfile
substitutes into its `tls` directive:

| `LIBRERUN_TLS` | Certificate | Needs |
|---|---|---|
| `internal` (the default) | Caddy's own local CA, made in the edge's data volume on first start | every browsing machine trusts its root (below) |
| an e-mail address | ACME — Let's Encrypt, then ZeroSSL — renewed automatically | `LIBRERUN_TLS_DOMAIN` resolving publicly to this host, and port 443 on it reaching the edge: validation is TLS-ALPN-01, since the edge's HTTP port is never published |
| `/certs/<cert> /certs/<key>` | your own PEM chain and key, from `LIBRERUN_TLS_CERT_DIR` (default `./tls`), mounted read-only as `/certs` | a directory LibreRun alone reads, owner-only: the mount's `Z` relabels it under SELinux, so copy the two files in rather than pointing it at `/etc/letsencrypt` or another shared directory |

A LAN names the host in `LIBRERUN_TLS_DOMAIN`, with `internal` or its own
CA's files. `localhost` is only this machine — and a site named
`localhost` sends no HSTS header, so the plain `http://localhost:<port>`
URLs keep working.

**Trust the local CA** (`LIBRERUN_TLS=internal`). Copy the root out of the
edge, then trust it on every machine that browses to LibreRun:

```bash
docker cp librerun-edge:/data/caddy/pki/authorities/local/root.crt librerun-edge-root.crt   # podman cp, likewise
```

| System | Trust step |
|---|---|
| Ubuntu / Debian | `sudo cp librerun-edge-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates` |
| CentOS Stream / Fedora | `sudo cp librerun-edge-root.crt /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust` |
| Chromium on Linux | `certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n librerun-edge -i librerun-edge-root.crt` |
| Firefox | Settings → Privacy & Security → Certificates → View Certificates → Authorities → Import, trusted for websites |

The root and its key live in the edge's volume, `librerun-edge-data`:
`down -v` deletes them, and the next start makes a root nobody trusts yet.
Whoever holds that key can mint a certificate for any name your browsers
will then accept, so it never leaves the volume. A CA, or a certificate and
key, loaded in the admin UI (below) is written into the edge's other
volume, `librerun-edge-control`, which only the edge and `edge-control`
mount — never the backend, the web UI or an agent — and is never read back
or shown again. Neither volume is in any backup (L42).

**Certificates in the admin UI.** Admin → Application Settings →
**Certificates** shows what the edge serves — the site's names, the issuer,
the certificate's dates and SHA-256, and the root's — where it comes from
and why, and what it needs next, each need with its action. A platform
admin changes it there without a shell or a restart, and each choice has
the environment line that would set it instead:

| In the admin UI | In the environment | Needs |
|---|---|---|
| **Load a CA** — its certificate and key, PEM | `LIBRERUN_TLS_CA=/certs/<ca.crt> /certs/<ca.key>`, the two files in `LIBRERUN_TLS_CERT_DIR` | a CA: `basicConstraints` `CA:TRUE`, `keyCertSign`, in date, an ECDSA (P-256, P-384, P-521), RSA (2048 bits or more) or Ed25519 key; every browsing machine trusts it |
| **A certificate and key of your own** — the chain and its key | `LIBRERUN_TLS=/certs/<cert> /certs/<key>` | names covering `LIBRERUN_TLS_DOMAIN`, in date, the key matching |
| **ACME** — an account e-mail | `LIBRERUN_TLS=<e-mail>` | the name resolving publicly to this host, and port 443 reaching the edge |
| **Use the environment's setting** | `LIBRERUN_TLS` (default `internal`) and `LIBRERUN_TLS_CA` | — |

The admin UI's choice wins, across restarts, until **Use the environment's
setting**; the page names the source in effect and why. Each change is
checked before the edge sees it, applied by a reload within seconds, and
undone if the edge refuses it: the files it replaces are deleted only once
the new ones serve. The needs: *trust the root* (the edge's own CA: its
fingerprint and a download; mark it trusted once every browsing machine
does), *the root changed* (it is not the one last marked trusted — after a
restore, say), *your certificate ends within 30 days*, *the CA ends within
90 days*, *ACME needs a public name and port 443*, *restart the edge* (its
admin socket answers nothing) and, without the `tls` profile, *the edge is
off*. ACME that cannot issue leaves the edge with no certificate to serve,
this page included; go back from a shell with `docker exec
librerun-edge-control rm /control/choice` and `docker restart
librerun-edge` (`podman`, likewise), which returns the edge to the
environment's setting.

The changes reach `edge-control` — the backend's image in a container of
its own, with the control volume and the Caddyfile and no database, secret
or agent — through the edge alone, after the edge asks the backend whether
you are a platform admin, sending your request's headers and never its
body. So a key you upload never enters the backend's process, where an
in-process agent runs (L42; [`Security.md`](Security.md), "Transport").
Each change is audited, `config_change` with `surface` `tls`.

**Restore: bring your CA back.** The edge's volumes are in no backup, so a
restore — the database back, the volumes empty — starts a new root, which
every browser must trust again, and the page says *the root changed*. To
keep one root across a restore, keep your own CA and load it again: upload
it on the Certificates panel, or name it in the environment before the
first start,

```bash
# .env — the CA's two files copied into LIBRERUN_TLS_CERT_DIR (./tls)
LIBRERUN_TLS_CA=/certs/ca.crt /certs/ca.key
```

and the first certificate the edge issues chains to it. LibreRun never
hands a CA's key back: the copy you keep is the only one outside the edge.

**Port 443.** The edge listens on 8443 inside its container and publishes
`LIBRERUN_HTTPS_PORT` (default `8443`), so rootless Podman starts it
without a sysctl. For 443: under rootful Docker, `LIBRERUN_HTTPS_PORT=443`;
under rootless Podman, either `sudo sysctl
net.ipv4.ip_unprivileged_port_start=443` (persisted in `/etc/sysctl.d/`)
and `LIBRERUN_HTTPS_PORT=443`, or keep 8443 and redirect 443 to it in the
host firewall (`firewall-cmd --add-forward-port=port=443:proto=tcp:toport=8443`).
ACME needs 443 reachable from outside whichever you pick.

**The CLI.** `librerun up` and `librerun demo` never start the edge: they
pass four fixed `--profile` flags, and Docker Compose reads
`COMPOSE_PROFILES` only when no `--profile` is given (podman-compose reads
it at all only from 1.6). Start and stop the edge with `compose.sh` as
above. With the profile on, the UI is reached through the edge, and the
fourth line keeps `http://localhost:3000` working for a start without it.
`librerun doctor` and `librerun run` reach the edge with
`LIBRERUN_URL=https://<domain>:8443` and `LIBRERUN_CA_FILE=<the root>` in
your shell, or `--base-url` and `--cacert`; given no `--base-url`, doctor
signs in only once the engine says this checkout's edge publishes that
port (`authoring/Quickstart.md` §8).

**What it sends.** On every response: `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`
and `Content-Security-Policy: default-src 'self'; script-src 'self'
'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:
blob:; font-src 'self' data:; connect-src 'self'; form-action 'self';
frame-ancestors 'none'; base-uri 'self'; object-src 'none'` (K7), which
confines every load and every send to this origin — the web UI calls
`/api/v1` on it, which the edge sends to the backend — with inline script
for Next's bootstrap and inline style for the report's own `<style>`;
and, for any site name but `localhost`, `Strict-Transport-Security:
max-age=31536000` (a year, no `includeSubDomains`, no `preload`). The
policy is the edge's: a start without the `tls` profile sends none. The backend believes the
`X-Forwarded-For` the edge sets from the edge's fixed address alone, so
audit and session rows record the browser's address rather than the
edge's — [`Security.md`](Security.md), "Transport".

**Renewal.** The internal CA and ACME renew their certificates themselves
while the edge runs. Files are yours to replace: upload the new pair on the
Certificates panel, which the edge serves at once, or copy it over the
mounted pair and `docker restart librerun-edge`.

**Turning it off.** `./compose.sh --profile app --profile tls down`, then
`./compose.sh --profile app up -d`. The plain lines stay loopback until
you edit them back by hand. A browser that saw the HSTS header keeps
upgrading `http://<domain>` to `https` for its `max-age`, a year: clear it
in Chromium at `chrome://net-internals/#hsts` ("Delete domain security
policies") and in Firefox with "Forget About This Site" from the history.

**Rootless Podman — the walk still to run.** No CI runner has had
Podman, so this walk is recorded as planned until the maintainer runs it:

1. On rootless Podman 5 with podman-compose, the same three lines and
   `./compose.sh --profile app --profile tls up -d --build`.
2. `podman cp librerun-edge:/data/caddy/pki/authorities/local/root.crt .`,
   then `curl --cacert root.crt https://<domain>:8443/api/v1/meta` answers
   with `"gateway": "ok"`.
3. `podman inspect librerun-edge --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}'`
   prints `172.16.87.2` (`.2` of `LIBRERUN_EDGE_NET`), the address the
   backend's `FORWARDED_ALLOW_IPS` names. If podman-compose could not pin
   it, record that: the fallback is to trust the edge network's subnet,
   which the frontend shares.
4. Sign in from another machine through the edge and read the newest
   `sign_in` row in Admin → Audit log: record whether its IP address is
   that machine's, or an address rootlessport substitutes for every
   client.
5. The source build pulls nothing (A2, R12): with a listener on
   `127.0.0.1:5000` that logs every connection (the Python one
   `source-build` writes, in `.github/workflows/librerun-smoke.yml`),
   `./compose.sh --profile app --profile viewer --profile demo down`, then
   `LIBRERUN_IMAGE_PREFIX=127.0.0.1:5000/r21 ./compose.sh --profile app
   --profile viewer --profile demo up -d` — no `--build` — reaches
   `/api/v1/health`, `podman images` lists the six images under that
   prefix, and the listener's log is empty. Record which binary answered:
   `compose.sh` prefers `podman-compose`.
6. The edge's admin socket (T2): with the `tls` profile up, `podman exec
   librerun-edge-control stat -c '%a %U' /control/admin.sock` prints `666`
   and its owner, and Admin → Application Settings → Certificates shows the
   root the edge serves — which `edge-control` reads through that socket,
   as `librerun` — rather than *restart the edge*. Load a CA there, and
   `curl --cacert <ca.crt> https://<domain>:8443/api/v1/meta` answers within
   30 seconds; `podman inspect librerun-backend --format '{{range
   .Mounts}}{{.Name}} {{end}}'` names neither of the edge's volumes. Record
   the socket's mode and owner, and whether `edge-control` reached it.
7. The digested references pull (A3, R14): on Podman 5 and again on the 4.5
   floor (the runtime table's Podman row), `./compose.sh --profile app
   --profile tls pull` pulls every third-party image by its
   `name:tag@sha256:` reference, and `podman images --digests` lists each
   digest `compose.yaml` names. Record the Podman version of each run, and
   any reference it refused.
8. Valkey boots (A3, L38): on a fresh `librerun-redis-data`, `podman exec
   librerun-redis valkey-cli ping` answers `PONG`, `podman logs
   librerun-redis` names Valkey 8, and the stack reaches `/api/v1/health`.
   If the 4.5 floor cannot pull or boot the digested references, record it:
   the floor rises to the first release that works, by its own pull request.

### Trace viewer (optional, any mode)

The bundled Jaeger all-in-one shows every run's trace. It takes **two**
knobs, because compose profiles cannot alter a service's command:

```bash
# .env
VECTOR_VIEWER=1          # Vector loads config/vector-viewer.yaml and forwards traces
TRACE_VIEWER=jaeger      # the run pages' "View trace" links point at it (default: off)

./compose.sh --profile viewer up -d                    # infra + viewer
./compose.sh --profile app --profile viewer up -d      # everything + viewer
```

Open a run and follow **View trace**: both run pages render the link
once `TRACE_VIEWER` names a viewer (the admin run view's
**Observability** section shows the trace id beside it). Or browse
http://localhost:16686 yourself and pick service `librerun-backend`.
Jaeger stays outside the default startup profiles — it comes up only
with `--profile viewer`.

### Forward telemetry to Cribl (optional, any mode)

The bundled Vector router has a built-in Cribl overlay
(`config/vector-cribl.yaml`): backend logs and OTel log records go to a
Cribl **HEC** source, and every trace span goes — via the bundled
`otel-bridge` service — to a Cribl **OpenTelemetry** source configured
with **Protocol: gRPC** (its default; any OTLP version works). The
bridge exists because Cribl's OTel source accepts **binary protobuf
only** (OTLP/JSON is answered with a 501), and Vector cannot emit OTLP
protobuf — so Vector posts JSON to the in-network bridge, a stock
OpenTelemetry Collector, which re-exports gRPC + protobuf + TLS. Every
hop off the box dials out; nothing new listens on the host. Enable with
both knobs, mirroring the viewer pattern:

The selector goes in `.env` and the endpoints and tokens go in the two
observability files — each read by the one process that needs it (K4,
decision L28):

```bash
# .env — the selector only; compose expands it at parse time
VECTOR_CRIBL=1

# observability.env — the LOG leg, read by `vector` alone
CRIBL_HEC_ENDPOINT=https://default.main.<org>.cribl.cloud:8088   # base URL, no path
CRIBL_HEC_TOKEN=...                                              # HEC source → Auth Tokens

# observability-traces.env — the TRACE leg, read by `otel-bridge` alone
CRIBL_OTLP_ENDPOINT=default.main.<org>.cribl.cloud:4317          # gRPC host:port, NO path (TLS by default)
CRIBL_OTLP_TOKEN=...        # optional — only if the OTel source enforces a token

./compose.sh --profile cribl up -d   # profile starts the bridge; env changes
                                     # RECREATE containers; `restart` is not enough
```

Copy each file from its example first (`cp observability.env.example
observability.env`, and the same for the traces one); both are optional
and both are `chmod 600` material. A token in `.env` instead reaches
neither process — compose passes those names to no service.

On the Cribl side, "Extract spans" on the OTel source gives you one
event per span for routing and search. Verify with
`./compose.sh logs -f vector otel-bridge` (misconfiguration fails
loudly) and a Live Data capture on each Cribl source. Before forwarding
run-plane traces off-box, consider
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT` — by
default those spans carry LLM prompt/completion content.

---

## 6. Agents

An agent is a self-contained package the chassis discovers at startup.
Two install routes, one runtime contract:

**Directory install** — drop a package into the agents directory and
restart:

```bash
librerun init my-agent --template langgraph      # or container-python | container-ts
librerun up                                      # rebuild; the agent appears on the new-run page
export LIBRERUN_AGENTS_PATH=/opt/librerun/agents # or point elsewhere
```

**Pip install** — an agent exposing a `librerun.agents` entry point
installs into the chassis environment with no agents-directory presence
at all. VITA itself is packaged this way:

```bash
pip install ./backend/agents/vita_v1     # installs the vita-agent distribution
```

Discovery merges both sources at startup; a directory checkout with the
same agent id overrides the installed copy. **Zero agents is a valid
configuration** — the chassis boots, discovers none, and says so.

Full contract, manifest reference (`agent.yaml`), capability grants and
observability: [`Agents_Install.md`](../authoring/Agents_Install.md) and
[`Agents_Design.md`](../authoring/Agents_Design.md). Adapting an agent written for
another framework: [`authoring/LangGraph.md`](../authoring/LangGraph.md); the
CLI and the three templates: [`authoring/Quickstart.md`](../authoring/Quickstart.md).
Running an agent as a container over HTTP+SSE:
[`Run_Contract_v1.md`](../authoring/Run_Contract_v1.md).

### An agent's tool secrets

An agent's own third-party keys — the demo agent's web-search key, say —
are named in its manifest's `secrets[]` and valued per tenant (K8a), on
the agent page's **Secrets** tab (`/admin/agents/<id>/config`, K8b):
each name shows this tenant's value and every tenant's default, set or
not, with a fingerprint and when a run last read it, never the value. A
tenant's admin sets and clears this tenant's; the default is a platform
admin's. The same, through the API:

```bash
# TOKEN is an admin's bearer token: the access_token that
# POST /api/v1/auth/login answers. The value goes in on stdin, never as
# an argument.
API=http://localhost:8000/api/v1
# This tenant's value (a tenant's admin), or every tenant's default
# (scope agent: a platform admin). The value is never returned.
curl -X PUT "$API/agents/vita-v1/secrets/tenant/tavily_api_key" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d @- <<'JSON'
{"value": "tvly-..."}
JSON
curl "$API/agents/vita-v1/secrets" -H "Authorization: Bearer $TOKEN"   # set or not, fingerprints, where it comes from
curl -X DELETE "$API/agents/vita-v1/secrets/tenant/tavily_api_key" -H "Authorization: Bearer $TOKEN"
```

A run reads this tenant's value, else every tenant's default, else — for
an agent running inside the backend — the upper-cased name in the
backend's environment (`TAVILY_API_KEY`), the fallback it has always had
and keeps. A container agent reads the first two alone; its own
environment is its own. Values are sealed with the store key, like any
secret set in the admin UI (see "The secrets store key"), so without one
a `PUT` answers `503 secrets_store_unconfigured`. The runner scrubs every
value it delivered from what a run persists, replacing it with
`[REDACTED_SECRET]` — which is why a stored value, or an environment
fallback, must be at least 8 characters: a shorter one is treated as
unset.

---

## 7. Verify the Installation

```bash
# 1. Health check
curl -s http://localhost:8000/api/v1/health | python3 -m json.tool

# 2. Login (use your INITIAL_ADMIN_* values)
curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"YOUR_ADMIN_EMAIL","password":"YOUR_ADMIN_PASSWORD"}' | python3 -m json.tool

# 3. Open the frontend
xdg-open http://localhost:3000
```

For an end-to-end check that actually drives a run, use
`scripts/librerun_smoke.py` from §2 — it works with real keys too.

---

## 8. Database Management

### Reset (destroys all data)

```bash
./compose.sh down -v
./compose.sh up -d
```

The schema reloads on first start. Accounts are re-bootstrapped from
`INITIAL_*` when the backend container starts; in development mode re-run
the §5 first-time setup (`alembic stamp 0001_baseline && alembic upgrade
head && python -m app.scripts.bootstrap_admin`).

### Migrations

```bash
cd backend
alembic upgrade head                                  # apply
alembic revision --autogenerate -m "short description"  # create
```

---

## 9. Start, Stop, Restart

### Infrastructure only

| Action | Command |
|--------|---------|
| Start | `./compose.sh up -d` |
| Stop | `./compose.sh stop` |
| Restart | `./compose.sh restart` |
| Tear down + delete all data | `./compose.sh down -v` |
| View logs | `./compose.sh logs -f` |
| Check status | `./compose.sh ps` |

### All containers (staging mode)

| Action | Command |
|--------|---------|
| Start | `./compose.sh --profile app up -d --build` |
| Stop | `./compose.sh --profile app stop` |
| Rebuild one service | `./compose.sh --profile app up -d --build backend` |
| Rebuild everything | `./compose.sh --profile app up -d --build --force-recreate` |
| Tear down + delete all data | `./compose.sh --profile app down -v` |
| Tail backend logs | `./compose.sh logs -f backend` |

### What `up` builds, per engine

Every first-party service carries `pull_policy: build` and a
`localhost/librerun/…` image name (#133), so neither engine pulls one; what
differs is when each builds. Read from Docker Compose's specification and
from podman-compose's source at 1.1.0, the floor, and 1.6.0:

| Command | Docker Compose | podman-compose |
|---|---|---|
| `up -d` | builds every first-party image on each start, from cache when nothing changed, so a changed `NEXT_PUBLIC_API_URL` or `BACKEND_INTERNAL_URL` is baked in | builds only an image that is missing, so a changed build argument needs `--build` |
| `up -d --no-build` | builds nothing, pulls nothing, and stops on a missing image | builds nothing, and hands a missing image to `podman create`, whose `missing` default asks the loopback address for it and fails |
| `run` | builds the service first, from cache | builds the service's image if it is missing |

`--build` is in every command on this page because on podman-compose it is
what re-bakes the web UI's two URLs; on Docker it says out loud what `up`
does anyway. `librerun up --no-build` passes compose's own `--no-build`.

---

## 10. Upgrading to 1.1.0-beta.1

Containers are always recreated on upgrade; data lives in named volumes.
This is the upgrade from 1.0, or from any tree before the beta, to
1.1.0-beta.1, as one ordered sequence. **Back up first**
(["Backup and restore"](#backup-and-restore), below): a beta may change
anything in the next one. A deployment from before 1.0 reads
["Upgrading an existing deployment"](#upgrading-an-existing-deployment)
first, then comes back here.

**What changes, and what to decide before the steps.**

- **Migrations.** In compose the backend's entrypoint applies them at
  every start (`alembic upgrade head`); in development mode run `cd
  backend && alembic upgrade head` yourself. A migration that fails now
  stops the backend, with Alembic's error in its log, instead of booting
  it on a stale schema, and `restart: unless-stopped` retries.
  `alembic current` verifies the result: the last step.
- **The two store keys.** `LIBRERUN_BACKEND_SECRETS_KEY` in `.env` is the
  backend's, for the secret settings and the agents' tool secrets set in
  the admin UI; `LIBRERUN_GATEWAY_SECRETS_KEY` in `gateway.env` is the
  gateway's, for the provider keys pasted on Application Settings and its
  sealing keypair. Each is a Fernet key, and the two must differ:

  ```bash
  python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
  ```

  Generate one for each, or leave either blank. Blank means that store is
  unconfigured: setting a secret answers `503
  secrets_store_unconfigured`, each secret setting reads its environment
  variable instead, and with the gateway's blank, Application Settings
  offers no provider key to seal. A deployment that sets neither loses
  nothing it had. Each key is backed up with the database it opens
  (["Backup and restore"](#backup-and-restore)).
- **The cache server is Valkey (A3, L38).** The `redis` service runs
  Valkey 8 now, under the same name, volume and `REDIS_URL`. A deployment
  from before A3 keeps a Redis 7.4 snapshot in its `redis-data` volume,
  and Valkey cannot read it (C-18): it stops at start with "Can't handle
  RDB format version 12". So the steps below remove that volume —
  `librerun-redis-data`, or the name `LIBRERUN_REDIS_VOLUME` pins — and
  Valkey starts on a fresh one. Nothing in it needs carrying over, since
  every key expires within seven days; a run in flight ends in `error`,
  and the last week's step lists go. Never reuse the volume, and a
  rollback to Redis takes the same step.
- **The demo agent's settings (K5b)** — Tavily search depth, Pinecone top
  K and retries per step — are data: each tenant's admin sets them on the
  agent page's Settings tab (Admin → Agents), and a value saved there
  survives `up -d --force-recreate`, which the CI smoke proves on every
  change. Values set before K5b are **not carried over** (D18): they
  lived in an overlay on the `librerun-state` volume, one value for every
  tenant — or, on an installation from before S2, in the old container's
  `config.json`, with the step models beside them — and each setting now
  starts at the agent's default. While an old overlay is still on the
  volume, the backend logs `vita_legacy_settings_overlay` at start,
  naming the file: set those values again on the Settings tab (and, from
  before S2, the models on the Steps tab); the file is no longer read,
  and deleting it silences the line.
- **The plain ports.** Since T1 the backend and the web UI publish on
  `127.0.0.1` by default. An `.env` copied from 1.0's example still says
  `BACKEND_PORT=8000` and `FRONTEND_PORT=3000`, which publish on every
  interface, until those two lines are edited to `127.0.0.1:8000` and
  `127.0.0.1:3000` — or to `0.0.0.0:<port>`, every interface chosen out
  loud.
- **HTTPS at the edge** is opt-in: the `tls` profile's three lines in
  `.env` and a rebuild (["HTTPS at the edge"](#https-at-the-edge)). The
  edge's network takes `172.16.87.0/28` by default; where another network
  holds that subnet, `LIBRERUN_EDGE_NET` names three other octets.
- **Certificates.** A CA is loaded in Admin → Application Settings →
  Certificates, or named in the environment by `LIBRERUN_TLS_CA`
  (["HTTPS at the edge"](#https-at-the-edge)).
- **Every first-party image is built, never pulled.** Each carries
  `pull_policy: build` (§9, ["What `up` builds, per
  engine"](#what-up-builds-per-engine)), and `./scripts/demo.sh` and
  `librerun demo` refuse `--pull` until 1.1.0 removes it.
- **`GOOGLE_CLIENT_SECRET` is unread:** Google sign-in verifies the ID
  token with the client id alone, so a line left in `.env` reaches
  nothing.
- **`TAVILY_API_KEY`** stays the demo agent's fallback in the backend's
  environment: a key set on its Secrets tab, for a tenant or as every
  tenant's default, wins over it.

**The steps, in order.** Run no `up` of the old tree between the pull
and the volume's removal: its Redis could write a new snapshot there.

1. `git pull`, or check out the release's tag.
2. Take the stack down with the profiles you run:
   `./compose.sh --profile app down`.
3. Remove the volume Redis 7.4 wrote:
   `docker volume rm librerun-redis-data` (`podman volume rm …`).
4. Edit `.env` and `gateway.env` as above: the two ports, the two store
   keys, and the edge's lines if you turn it on.
5. Start once, building from this checkout:
   `./compose.sh --profile app up -d --build` (with `--profile tls` for
   the edge).
6. Check the schema: `./compose.sh exec backend alembic current` prints
   `0020_gateway_status (head)`; in development mode, `cd backend &&
   alembic current`.

### Upgrading an existing deployment

A deployment created before 1.0 — before blueprint S2 renamed the
database and the volumes, and S1 the platform noun — reads this first,
then takes the steps above.

**Names (blueprint S2).** New installs use database `librerun`, role
`librerun`, and volumes `librerun-pgdata`, `librerun-redis-data`,
`librerun-file-storage`, `librerun-state` (exact names, no project
prefix). A deployment created earlier has database and role `vita` inside
a volume named `<project>_vita-pgdata` (and `..._vita-redis-data`,
`..._vita-file-storage`; `docker volume ls` shows the exact names). Pick
one:

- *Keep the old names* — pin them in `.env` and nothing moves:

  ```bash
  POSTGRES_DB=vita
  POSTGRES_USER=vita
  POSTGRES_PASSWORD=<what it already is>
  LIBRERUN_PGDATA_VOLUME=<project>_vita-pgdata
  LIBRERUN_REDIS_VOLUME=<project>_vita-redis-data
  LIBRERUN_FILES_VOLUME=<project>_vita-file-storage
  PINECONE_INDEX_NAME=vita-kb    # only if PINECONE_API_KEY is set and this never was
  ```

- *Adopt the new names* — copy each volume once, with the stack stopped
  (`./compose.sh --profile app stop`), then start without the pins:

  ```bash
  for pair in "<project>_vita-pgdata librerun-pgdata" \
              "<project>_vita-redis-data librerun-redis-data" \
              "<project>_vita-file-storage librerun-file-storage"; do
    set -- $pair
    docker volume create "$2"
    docker run --rm -v "$1":/from:ro -v "$2":/to alpine sh -c 'cp -a /from/. /to/'
  done
  ```

  The database and role inside the copied volume are still `vita` — keep
  `POSTGRES_DB=vita POSTGRES_USER=vita` pinned unless you also rename them
  in Postgres.

  Runs created before the rename keep their `VITA-` labels; a tenant
  created afterwards numbers its runs `RUN-1000`, `RUN-1001`, … (blueprint
  S1).

**Internal KB index.** A deployment that set `PINECONE_API_KEY` and never
`PINECONE_INDEX_NAME` filled an index named `vita-kb`; the default is now
`librerun-kb`. Pinecone cannot rename an index, so with either recipe keep
the `PINECONE_INDEX_NAME=vita-kb` pin, or create `librerun-kb`, load it,
and drop the pin. Without the pin every KB search goes to an index that
does not exist: the run still completes, with no internal-KB evidence, and
the backend logs `kb_search_failed` naming the index — a regression that is
easy to miss, which is why the pin sits in the same block as the database
and volume names.

**Redis keys (blueprint S1).** Progress and scratch keys moved from
`case:{id}:*` to `run:{id}:*` with no fallback. A run in flight across
the upgrade shows an empty progress list until its next step writes;
upgrade between runs if that matters.

**Jaeger traces (viewer profile).** `up -d --force-recreate` recreates the
viewer containers too, and Jaeger's store is in-memory: traces from before
the recreate are gone. The viewer profile is a development aid; a
deployment that keeps traces sends them to a real backend (`docs/authoring/Agents_Design.md`,
"Observability contract").

### Backup and restore

Back up before every upgrade. What is backed up goes together, because
each part needs the others:

| What | Where it is | Why it goes with the rest |
|---|---|---|
| The database: every run, setting, user and audit row, the `secrets` rows, and the gateway's sealing keypair, whose private half is sealed under the gateway's store key | `librerun-pgdata` | the rest is what opens it |
| Uploaded files | `librerun-file-storage` | runs name them |
| The agents' state | `librerun-state` | agents read it |
| `.env`, with `APP_SECRET_KEY` and `LIBRERUN_BACKEND_SECRETS_KEY` | the checkout | the backend's store key opens its `secrets` rows, and the secret key signs every session |
| `gateway.env`, with `LIBRERUN_GATEWAY_SECRETS_KEY` | the checkout | the gateway's store key opens its keypair and the provider keys pasted in the UI |
| `observability.env` and `observability-traces.env`, if you use them | the checkout | the vendors' endpoints and credentials |

Keep the copies of the three files owner-only, as the files themselves
are: they hold every key above.

What is **not** backed up:

- **The edge's two volumes**, on the engine
  `<project>_librerun-edge-data` and `<project>_librerun-edge-control`
  (`compose.yaml` names neither). The private key of the CA the edge
  issues from never leaves them (L42), so a restore starts a new root,
  which every browser must trust again — unless you load your own CA at
  restore, on the Certificates panel or by `LIBRERUN_TLS_CA` before the
  edge first starts ("Restore: bring your CA back", under ["HTTPS at the
  edge"](#https-at-the-edge)). A certificate and key of your own, in
  `LIBRERUN_TLS_CERT_DIR` (`./tls` by default), are yours to keep, like
  `.env`.
- **The cache volume**, `librerun-redis-data`: it holds nothing that must
  survive, and it is never reused (A3).

**The backup**, with the stack running, `podman` for `docker` on
Podman. Each database command reads `POSTGRES_USER` and `POSTGRES_DB`
inside the `postgres` container, where `.env` put them, so the recipe
is the same for the `librerun` defaults and for the names a deployment
from before blueprint S2 pins:

```bash
docker exec librerun-postgres sh -c 'pg_dump --clean --if-exists -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > librerun-backup.sql
docker cp librerun-backend:/app/data/files ./librerun-files
docker cp librerun-backend:/app/data/state ./librerun-state
```

`--clean --if-exists` is not optional. On an empty volume the `postgres`
service loads `backend/db/schema.sql` at its first start, so the restore
replaces what that load made rather than colliding with it.

**The restore**, in this order — Postgres alone first, then the dump,
then the rest. A full `up` on empty volumes would migrate, bootstrap the
admin and insert a fresh gateway keypair, and a dump restored after it
collides with all three:

```bash
./compose.sh --profile app down -v
./compose.sh --profile app up -d postgres
until docker exec librerun-postgres sh -c 'pg_isready -q -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'; do sleep 2; done
docker exec -i librerun-postgres sh -c 'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < librerun-backup.sql
./compose.sh --profile app up -d
docker cp ./librerun-files/. librerun-backend:/app/data/files/
docker cp ./librerun-state/. librerun-backend:/app/data/state/
./compose.sh --profile app restart backend
```

with the same `.env` and `gateway.env` in place, and every profile you
run named, `--profile tls` among them. Postgres answers over TCP only
once it has finished loading the schema into the new volume, which is
what the `until` line waits for. The restart lets the backend's
entrypoint give the copied files back to its own user.

**A restore under another key.** The keys are what open the rows, so a
dump restored beside the wrong one shows it on each side:

- **The backend's** (`LIBRERUN_BACKEND_SECRETS_KEY`): each backend row
  sealed under the old key is `unreadable — replace or clear` in Admin →
  Settings and on the Secrets tabs, each such setting serves its
  environment variable meanwhile, and `python -m
  app.scripts.rewrap_secrets --dry-run` names every one and exits 3. Put
  the old key back — first in the list, or anywhere in it — or replace
  or clear each row (["The secrets store key"](#the-secrets-store-key)).
- **The gateway's** (`LIBRERUN_GATEWAY_SECRETS_KEY`): the gateway refuses
  to boot, naming its sealing keypair's row, which it never replaces,
  since every provider key sealed to it would be lost without a word.
  Put the old key back, or discard what no key opens with `python -m
  gateway.rewrap --discard-unopenable` and paste the provider keys again
  (["The gateway's store key"](#the-gateways-store-key)).

`librerun-smoke` → `backup-restore` runs the database half of this
recipe on every pull request, its three commands read out of this page,
under a database and a role named other than the defaults: a secret
setting and a provider key sealed to the gateway, both fingerprints read
back after the restore, and the same dump under a fresh backend key
named by `rewrap --dry-run`, exit 3.

---

## 11. Port Conflicts

Override defaults in `.env`, then restart:

```bash
POSTGRES_PORT=5433
REDIS_PORT=6380
BACKEND_PORT=127.0.0.1:8001
FRONTEND_PORT=127.0.0.1:3001
JAEGER_UI_PORT=16687
```

`BACKEND_PORT` and `FRONTEND_PORT` are Compose host bindings, loopback by
default since T1: keep the address when you change the port. A bare port
(`BACKEND_PORT=8001`) publishes plain HTTP on every interface, and
`compose.sh` refuses it while the `tls` profile is requested ("HTTPS at
the edge", §5). `JAEGER_UI_PORT` is a bare port, the viewer's mapping binds
to 127.0.0.1 already. Three values
follow those ports: `NEXT_PUBLIC_API_URL` (baked into the frontend image
at build time), `APP_CORS_ORIGINS` (the origin the backend accepts) and
`TRACE_VIEWER_BASE_URL` (the "View trace" links). `./scripts/demo.sh`
derives them from the ports — and from the bound address, when one is
given and is not loopback, which it names `localhost` — when you have not
set them, and prints the URLs to open; starting the stack by hand, set
all three to match and rebuild the frontend.

The edge network conflicts the same way. Every start creates it, `tls`
profile or not, on `172.16.87.0/28`, and an engine refuses a subnet
another network already holds (`Pool overlaps with other one on this
address space`) — a second LibreRun on this host, or a network of your
own there. Move its first three octets, and the edge's address and the
backend's trust follow:

```bash
LIBRERUN_EDGE_NET=172.16.88
```

---

## Podman-Specific Notes

Podman runs rootless by default.

**Volume permissions:** the `:Z` suffix on bind mounts in `compose.yaml`
handles SELinux relabeling. On non-SELinux systems (Ubuntu, Debian),
permission errors mean you should remove the `:Z`.

**Podman socket:** some tools expect the Docker socket:

```bash
systemctl --user enable --now podman.socket
export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/podman/podman.sock
```

**SELinux:** `:Z` handles it; `sudo setenforce 0` is the blunt fallback.

## Docker-Specific Notes

### Staging mode SSO

The `compose.yaml` backend service already passes `GOOGLE_CLIENT_ID`,
`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` and `AZURE_TENANT_ID` from `.env`,
and since K6 a platform admin can set each in Admin → Settings instead
(`auth.google_client_id`, `auth.azure_client_id`, `auth.azure_tenant_id`
and the write-only `auth.azure_client_secret`, which needs a
`LIBRERUN_BACKEND_SECRETS_KEY`); the next sign-in reads them, with nothing
restarted. Google sign-in needs no client secret.

---

## Staging / Production Checklist

Before exposing LibreRun outside localhost:

| Variable | Where | Why |
|----------|-------|-----|
| `APP_SECRET_KEY` | `.env` | The default is insecure |
| `POSTGRES_PASSWORD` + `DATABASE_URL` | `.env` | The default password is public |
| `APP_CORS_ORIGINS` | `.env` | Must match your frontend's real hostname |
| `INITIAL_ADMIN_PASSWORD` | `.env` | Use a strong unique value — then **blank it after the first sign-in** (§4). A blank pair is skipped and the account stays; a filled one is rehashed on every boot, so it both sits on disk for the deployment's lifetime and reverts any password changed in the UI. |
| `LIBRERUN_STUB_LLM` | `.env` | Must be `false`. Stub reports are fixtures. |
| `LIBRERUN_PII_ALLOW_DEGRADED` | `.env` | Leave it `false`. True makes the platform keep redacting with the regex stages alone when Presidio's named-entity stage cannot run — so names, places and organisations are stored and exported unredacted. `.env` reaches **both** the backend and the gateway (`compose.yaml` passes it to each), so one setting is one policy. Check `GET /api/v1/health` and the gateway's `/healthz`: `pii_detector.state` must read `ready`, not `unavailable` or `failed`. |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_AI_API_KEY` | **`gateway.env`** | A key for every provider your steps reference — here, or pasted in Admin → Application Settings over HTTPS ("Model providers", §1). Never in `.env` — see below. |
| `LIBRERUN_GATEWAY_SECRETS_KEY` | **`gateway.env`** | Needed for Admin → Application Settings to keep a provider key; never the backend's key. Back it up with the database — without it the gateway's rows open under nothing, and the gateway will not start ("The gateway's store key", §1). |
| `LIBRERUN_SOURCE_URL` | `.env` | If you changed the source, set it to where your modified source is. The default names the unmodified release and is not an offer of your version; the AGPL-3.0 (section 13) asks you to offer the Corresponding Source of what you run to the people who use it over a network. |
| `LIBRERUN_TLS_DOMAIN`, `LIBRERUN_TLS` and the `tls` profile | `.env` | Serve the UI over HTTPS from the edge ("HTTPS at the edge", §5): the name browsers use, and an ACME address or your own certificate for a public name. `BACKEND_PORT` and `FRONTEND_PORT` stay on loopback — `compose.sh` refuses the profile otherwise — so the edge is the one door. |
| `LIBRERUN_BACKEND_SECRETS_KEY` | `.env` | Needed for Admin → Settings to hold a secret (the Microsoft sign-in secret today); blank, each secret comes from its environment variable. Back it up with the database — a dump restored without it holds ciphertext nothing opens ("The secrets store key", §1). |

**Where each secret lives.** Each process gets the secrets it reads and
no others (decision L28), so where a value lives is part of the posture,
not a preference. The full per-variable list is §1's class-[4] table;
this is the shape of it:

| File | Holds | Read by | Not read by |
|------|-------|---------|-------------|
| `.env` | `APP_SECRET_KEY`, `POSTGRES_PASSWORD`, the `INITIAL_*` pairs, the per-agent gateway keys, the secrets store's key `LIBRERUN_BACKEND_SECRETS_KEY`, the Microsoft client secret's fallback, an in-process agent's tool-key fallbacks (`TAVILY_API_KEY`), the knowledge base's `PINECONE_API_KEY` fallback — and every non-secret setting | compose (substitution), the backend, the frontend build | the gateway in a container, `vector`, `otel-bridge` |
| `gateway.env` | the three provider keys and the gateway's store key `LIBRERUN_GATEWAY_SECRETS_KEY`, and nothing else | the `gateway` service | everything else — which is the point (L23) |
| `observability.env` | the log leg's vendor endpoints and tokens (Datadog, Elastic, Splunk HEC, Cribl HEC) | `vector` | everything else, `otel-bridge` included |
| `observability-traces.env` | the trace leg's vendor endpoints and tokens (Elastic APM, Splunk Observability, Cribl OTLP) | `otel-bridge` | everything else, `vector` included |
| `agent-keys.env` | derived, never edited | the `gateway` service | — |

`chmod 600` every one of them; all five are git-ignored. `agent-keys.env`
is rewritten by `compose.sh` before every command from the
`LIBRERUN_AGENT_KEY_*` variables of the environment and the lines of
`.env` (the environment outranks the file, as it does for compose
itself), because handing the gateway the whole `.env` would hand it
`APP_SECRET_KEY` and the bootstrap credentials too.

The two observability files are two and not one for the same reason
`gateway.env` is separate: Splunk takes a *different* credential per leg,
so one shared file would put the HEC token in the bridge's environment
and the Observability access token in Vector's, where neither is ever
read — visible in `docker inspect`, in a core dump, and to everything
those processes run.

**Copies** of any of them — the ops repository, backups — are
sops-encrypted (`encrypted/.env`, `encrypted/gateway.env`), and a host
that must hold no plaintext at rest runs the stack through
`sops exec-env` and `sops exec-file` with `LIBRERUN_GATEWAY_ENV_FILE`:
§1, "Encrypting `.env` at rest".

Verify it on a running deployment — the backend must show no provider
variable at all:

```bash
docker inspect librerun-backend \
  --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'OPENAI|ANTHROPIC|GOOGLE_AI|GATEWAY_SECRETS'
# expect: nothing (TAVILY_API_KEY, an in-process agent's fallback, and PINECONE_API_KEY are fine)
docker inspect librerun-gateway \
  --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -c OPENAI_API_KEY
# expect: 1
```

`backend/tests/test_secret_partition.py` is the same check as a test: it
derives what every service receives from `compose.yaml` and the examples
beside the `env_file`s it names, derives what each process reads from the
settings models, and fails on the difference.
`backend/tests/test_env_example_classes.py` is its companion in the other
direction: every variable anything reads is *written down* in exactly one
example file, under exactly one class, and §1's tables say the same thing
the files do.

Runtime-tunable settings (CORS origins, session timeout, upload limits,
approval gate, default LLM provider, the knowledge-base embedding model
and the trace-viewer links) change at `/admin/settings` without
redeploying — those are §1's class [3]. Editing CORS origins requires a
backend restart. Class [2], the security posture, is deliberately not on
that page: it changes by reviewed deployment.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Postgres won't start | `./compose.sh logs postgres` — likely a port conflict. Try `POSTGRES_PORT=5433 ./compose.sh up -d` |
| "Connection refused" from backend | Dev mode uses `localhost:5432`; container mode uses `postgres:5432` (compose DNS). Check `DATABASE_URL`. |
| Login fails "Invalid credentials" | Verify `CREDENTIALS_ENABLED=true`, that `INITIAL_ADMIN_*` are set and the bootstrap ran (grep the backend log for `bootstrap_user_`), and that `DATABASE_URL`'s password matches `POSTGRES_PASSWORD`. |
| **You asked for a PDF and got an HTML file** | WeasyPrint's system libraries are missing, so the report service falls back to an HTML download (`weasyprint_failed` in the backend log, `fallback="html"`). Install the host packages under [Ubuntu / Debian host packages](#ubuntu--debian-host-packages). |
| Presidio model download fails | `python -m spacy download en_core_web_lg` — behind a proxy add `--trusted-host pypi.org --trusted-host files.pythonhosted.org` |
| No agents in the picker | Expected with an empty agents directory — that is a valid install. Otherwise check `LIBRERUN_AGENTS_PATH` and the backend log for discovery errors. |
| Page refresh requires re-login | By design. The JWT is held in memory only. |
| Podman "permission denied" | SELinux is blocking bind mounts. Remove `:Z` or `sudo setenforce 0`. |
| Traces not appearing | Check the router first: `./compose.sh logs -f vector` shows every span and log event it receives. Then grep the backend log for `otel_init_`. `otel_init_skipped` means `OTEL_EXPORTER_OTLP_ENDPOINT` didn't reach the process — in dev mode set `http://localhost:4317`; containers default to `http://vector:4317`. `otel_init_failed` prints the exception. If `otel_init_complete` but spans still don't land, set `OTEL_DEBUG=true`. |
| Report text says "fixture" | `LIBRERUN_STUB_LLM=true`. Set it to `false` and provide real provider keys. |
| LLM prompts/responses in logs | Pinned out by default in `backend/app/logging_config.py`. If you re-enable an SDK logger for debugging, don't leave it on — payloads contain user input. |

### Nuclear reset

```bash
./compose.sh down -v --remove-orphans
rm -rf backend/.venv frontend/node_modules
./compose.sh up -d
# Accounts come back from INITIAL_* on backend start
```
