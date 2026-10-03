# Observability — your telemetry, in the tool you already use

LibreRun's telemetry spine is one Vector router in the box (decision
L3). Everything a run produces — the backend's spans and log lines, a
container agent's own instrumentation through the authenticated relay,
the browser's UX plane through the RUM relay — arrives there already
walked for PII, and Vector forwards it. This page is about the last
hop: getting it into **Datadog, Elastic or Splunk**.

The tracing and observability tools are designed to teach evaluation as
well as to operate: one trace per run, with the model, tokens and cost
of every LLM call on it, is the evidence a course compares two runs by.
The pages that teach evaluation with them are not written yet
([what 1.0 does not do yet](../release/v1.0.0.md#what-10-does-not-do-yet)).

Three overlays ship in 1.0. Each carries **both legs** — logs *and*
traces — for **both planes**, with the resource identity intact, so
`service.name` stays `librerun-backend`, `librerun-web` or the agent's
own. Selecting one adds a destination; it does not replace the bundled
Jaeger viewer or change anything else.

> **What the tests prove.** CI runs a keyless demo run per vendor
> against a mock intake and decodes what arrived: the envelope each
> vendor documents, the resource identity, this run's trace id, and the
> absence of the PII fixture. That proves **the wire shape LibreRun
> emits is the one the vendor documents**. It is *not* that vendor's
> acceptance of your data — only your own account can tell you that.
> Region, quota, index mapping, token scope and retention are yours.

---

## How selection works

Three files, and they are not interchangeable:

| Where | What goes there | Read by |
|---|---|---|
| `.env` | `LIBRERUN_OBS_VENDOR=<vendor>` | Compose itself, while building Vector's and the bridge's command lines — before either container exists. An `env_file` is read *by* the container, which is too late. |
| `observability.env` | the **log** leg: intake URLs, API keys, HEC token, index | `vector` only |
| `observability-traces.env` | the **trace** leg: OTLP URLs and their auth | `otel-bridge` only |

**Three files, not one, and the split is deliberate.** L28 says no
process receives a secret it does not read, and Gate P
(`backend/tests/test_secret_partition.py`) enforces it against
`compose.yaml` and the committed examples. A single shared file would
put your Splunk Observability access token in Vector's environment and
your HEC token in the bridge's, where neither is ever read — visible in
`docker inspect`, in a core dump, and to everything those processes
run. Neither vendor file goes anywhere near the backend, the gateway or
the frontend.

```bash
cp observability.env.example observability.env
cp observability-traces.env.example observability-traces.env
$EDITOR observability.env observability-traces.env   # your vendor's sections
echo 'LIBRERUN_OBS_VENDOR=datadog' >> .env
./compose.sh --profile app --profile obs up -d
```

The `obs` profile starts **otel-bridge**, a stock OpenTelemetry
Collector. Every trace leg goes through it, because it is the one
component in the box that emits spec-compliant OTLP **protobuf** —
Vector's native `opentelemetry` sink serialises Vector's internal event
shape, which OTLP receivers reject, so Vector wire-shapes OTLP/JSON and
the bridge re-exports protobuf with the vendor's auth header.

Changing any of these **recreates** containers; a bare `restart` keeps
the old environment:

```bash
./compose.sh --profile app --profile obs up -d --force-recreate vector otel-bridge
```

**Unset or empty is the shipped default** and changes nothing: Vector
runs its base config alone, the Jaeger viewer behaves exactly as it
does today, and nothing is forwarded to a vendor.

**An unknown value is refused, not guessed at.** It names a config file
that is not mounted, so Vector and the bridge stop with an error saying
which file they wanted, and `/admin/otel-status` reports the value as
unsupported. Groundcover is one such value: it is not a supported
vendor (JR, 2026-09-20), and it is refused on exactly the same grounds
as a typo — there are three overlays and no fourth.

### Check it from inside the chassis

**Admin → Observability** (`GET /api/v1/admin/otel-status`) names the
active overlay, the config files that selection loads, the sinks those
files declare, and whether the backend can still reach the router. It is
the deployment's, so a platform admin's alone since K9 — a tenant admin
is told "Platform operators only." — and every endpoint it shows has lost
its userinfo and query, as the Deployment panel's does.

Two things it deliberately does not claim. The sink list is the
**overlay's declaration**, not a read-back from Vector — Vector's API is
bound to loopback inside its own container and widening it to the
compose network to populate a status page would trade a real boundary
for a nicer field. And "Vector: reachable" is a **TCP connect from the
backend** to the OTLP endpoint it exports to, which is the vantage
point the backend actually has. Neither is a delivery receipt from your
vendor.

### Before you forward run-plane traces off-box

Run-plane spans carry LLM prompt and completion content by default. If
that should not leave your network, set this in `.env` before selecting
an overlay:

```
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT
```

PII is a separate matter and is already handled: everything reaching
Vector has been through the one walker (blueprint S4), including an
agent's own spans, attributes, events and log records. **The overlays
forward and never redact** — a second redactor at the edge would only
be a weaker copy of the first, and CI re-asserts the walk at each
vendor's wire rather than trusting this paragraph.

---

## Datadog

Logs ride Vector's native `datadog_logs` sink to the logs intake.
Traces go OTLP through the bridge to **a Datadog Agent you run** —
Datadog's trace intake is not an OTLP endpoint, and the Agent's own
OTLP receiver is the documented path. The Agent holds `DD_API_KEY` and
`DD_SITE`; the bridge sends it no credential and needs none.

`.env`:

```
LIBRERUN_OBS_VENDOR=datadog
```

`observability.env` (the log leg, read by Vector):

```
# Scheme + host, NO path — the sink appends /api/v2/logs itself. This is
# also where a non-default Datadog site is expressed, because `endpoint`
# overrides the sink's site/DD_SITE:
#   US1  https://http-intake.logs.datadoghq.com
#   EU   https://http-intake.logs.datadoghq.eu
DATADOG_LOGS_ENDPOINT=https://http-intake.logs.datadoghq.com
DATADOG_API_KEY=<your Datadog API key>
```

`observability-traces.env` (the trace leg, read by the bridge):

```
# Your Agent's OTLP/HTTP traces URL, full path. No credential: the
# Agent holds DD_API_KEY and DD_SITE itself.
DD_AGENT_OTLP_URL=http://datadog-agent:4318/v1/traces
```

Your Agent needs its OTLP receiver switched on — `otlp_config.receiver.protocols.http`
in `datadog.yaml`, or `DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_HTTP_ENDPOINT=0.0.0.0:4318`.

```bash
./compose.sh --profile app --profile obs up -d --force-recreate vector otel-bridge
```

Where things land: logs under `service:librerun-backend` (and your
agents' own service names); traces in APM, one trace per run.

---

## Elastic

Logs ride Vector's native `elasticsearch` sink into **data streams**
(`logs-<dataset>-<namespace>`). Traces go OTLP through the bridge to
the **APM OTLP intake** — `/v1/traces` on an APM server, or the Elastic
Cloud managed OTLP endpoint.

`.env`:

```
LIBRERUN_OBS_VENDOR=elastic
```

`observability.env` (the log leg, read by Vector):

```
# Cluster or deployment URL, NO path — the sink appends /_bulk itself.
ELASTIC_URL=https://my-deployment.es.us-east-1.aws.found.io:443
ELASTIC_API_KEY=<your cluster API key>
# `ApiKey` is Elastic's documented scheme for API keys. A cluster on
# basic auth sets Basic here and puts the base64 pair in ELASTIC_API_KEY.
ELASTIC_AUTH_SCHEME=ApiKey
ELASTIC_LOGS_DATASET=librerun
ELASTIC_LOGS_NAMESPACE=default
```

`observability-traces.env` (the trace leg, read by the bridge):

```
# The APM OTLP intake, full path.
ELASTIC_APM_OTLP_URL=https://my-deployment.apm.us-east-1.aws.found.io/v1/traces
# Its OWN key, not the cluster's: Elastic issues them separately, and
# naming them separately is what lets each process hold only the one it
# reads. A self-managed APM server taking a secret token sets Bearer
# below and puts the token here.
ELASTIC_APM_API_KEY=<your APM API key>
ELASTIC_APM_AUTH_SCHEME=ApiKey
```

```bash
./compose.sh --profile app --profile obs up -d --force-recreate vector otel-bridge
```

The sink is pinned to `api_version: v8` rather than probing the cluster
on every start, which would turn a slow or guarded cluster into a
start-up failure in the telemetry router. On a v7 cluster, change that
line in `config/vector-elastic.yaml`.

Where things land: logs in `logs-librerun-default`; traces in APM under
`service.name`.

---

## Splunk

Splunk splits the two legs across two products, so this overlay takes
**two different credentials**. Logs ride Vector's native
`splunk_hec_logs` sink to **HEC**. Traces go OTLP through the bridge to
**Splunk Observability's** ingest — `/v2/trace/otlp`, which documents
`Content-Type: application/x-protobuf`, which is precisely why the
bridge exists.

`.env`:

```
LIBRERUN_OBS_VENDOR=splunk
```

`observability.env` (the log leg, read by Vector):

```
# HEC base URL, NO path — the sink appends /services/collector/event.
SPLUNK_HEC_URL=https://http-inputs-myorg.splunkcloud.com:443
SPLUNK_HEC_TOKEN=<your HEC token>
SPLUNK_INDEX=main
SPLUNK_SOURCETYPE=librerun
```

`observability-traces.env` (the trace leg, read by the bridge):

```
# Splunk Observability's OTLP trace ingest, full path. The access token
# rides the X-SF-Token header Splunk documents — NOT the HEC token, and
# the two files are why neither process holds the other's.
SPLUNK_OTLP_URL=https://ingest.us1.observability.splunkcloud.com/v2/trace/otlp
SPLUNK_ACCESS_TOKEN=<your org access token>
```

```bash
./compose.sh --profile app --profile obs up -d --force-recreate vector otel-bridge
```

Where things land: events in your HEC index; traces in Splunk APM.

---

## Cribl

**Cribl** is a fourth overlay with its own switch: `VECTOR_CRIBL=1` in
`.env`, then `./compose.sh --profile cribl up -d`. Since K4 its values
are split the same way this page's three vendors are —
`CRIBL_HEC_ENDPOINT` and `CRIBL_HEC_TOKEN` in `observability.env`,
`CRIBL_OTLP_ENDPOINT` and `CRIBL_OTLP_TOKEN` in
`observability-traces.env` — so neither process is handed the other's
token. See `config/vector-cribl.yaml`.

## Something else
**Anything else with an OTLP endpoint** — Tempo, Honeycomb, Phoenix, a
collector of your own — needs no overlay: point
`VECTOR_JAEGER_ENDPOINT` at its OTLP/HTTP `/v1/traces` with the viewer
overlay, or copy the `viewer_forward` sink out of
`config/vector-viewer.yaml`. Log warehouses (ClickHouse, S3, Loki) stay
a copy-uncomment gallery in `config/vector.yaml`.

A vendor not listed here is not refused on principle — it simply has no
overlay, no mock and no contract test, and shipping one that nobody has
decoded a payload from would be the untested claim these three exist to
avoid.

---

## When it does not work

| Symptom | Where to look |
|---|---|
| `vector` exits immediately | `./compose.sh logs vector`. An overlay selected with its endpoint unset fails loudly on purpose — that is the empty default doing its job. |
| `otel-bridge` restarts in a loop | `./compose.sh logs otel-bridge`. An empty exporter endpoint is rejected by the collector; check the vendor's OTLP variable in `observability-traces.env` — the bridge's file, not Vector's. |
| Admin → Observability says **unsupported** | `LIBRERUN_OBS_VENDOR` is not one of `datadog`, `elastic`, `splunk`. Nothing is being forwarded. |
| Logs arrive, traces do not | The trace leg is the bridge's. Confirm the `obs` profile is up (`./compose.sh --profile obs ps`) and that the vendor's OTLP URL carries its **full path**. |
| Traces arrive, logs do not | The log leg is Vector's own sink. Check the credential and that the log endpoint has **no path** on it — all three sinks append their own. |
| Nothing at all | `./compose.sh logs vector` should show the debug console sink still printing. If it is silent, the problem is upstream of any vendor: check `OTEL_EXPORTER_OTLP_ENDPOINT` on the backend and Admin → Observability's router reachability. |
| A change had no effect | Environment changes need `--force-recreate`; a bare `restart` keeps the old environment. |

## Files

| File | What it is |
|---|---|
| `config/vector.yaml` | the base router; the sink gallery for warehouses |
| `config/vector-otlp-shape.yaml` | the OTLP/JSON trace re-serializer every overlay's trace leg reads |
| `config/vector-datadog.yaml` · `-elastic` · `-splunk` | one overlay per vendor, both legs |
| `config/otel-bridge-<vendor>.yaml` | the collector config that leg's protobuf export uses |
| `observability.env.example` · `observability-traces.env.example` | the templates, one per process (L28) |
| `.github/workflows/obs-vendors.yml` | validate, the per-vendor contract tests, the negative cases |
| `scripts/obs_mock_intake.py` · `obs_vendor_contract.py` | the mock intake and the decoder that judges what it received |

See also `docs/platform/Browser_Observability.md` for the UX plane and
`docs/authoring/Agents_Design.md` ("Observability contract") for how the three
planes are stamped and routed.
