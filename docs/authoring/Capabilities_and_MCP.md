# Capabilities and the MCP surface

The six services an agent may ask the platform for, the grant that opens
each, and the two surfaces they arrive through — attributes on `ctx.caps`
for an in-process agent, MCP tools at `run.mcp.url` for a container.

Nothing here is optional reading for a container author: the MCP endpoint
*is* the platform, and the error codes below are the whole vocabulary of
refusal.

## The grant

An agent declares what it wants in its manifest, and gets nothing it did
not declare:

```yaml
capabilities: [llm, kb, run_store, progress, audit, pii]
```

Six names, `backend/app/capabilities/__init__.py` is the list of record,
and `case_store` is accepted as an alias of `run_store` for one release
(blueprint S1, L18). An ungranted capability is not a silent no-op — it
raises `CapabilityNotGranted` in-process and answers `-32002` over MCP.

**Grant the minimum.** The grant is an audit boundary, not a security
boundary — see
[`../platform/Security.md`](../platform/Security.md) — but it is the
boundary a reviewer reads, and the one the admin UI shows.

## The six, on both surfaces

| Capability | In-process | MCP tool | What it does |
|---|---|---|---|
| `llm` | `ctx.caps.llm.complete(step, messages, **kwargs)`, `.stub_mode()` | *(model calls go to the gateway, not MCP)* | A model call through the gateway. `step` is an id from `llm.steps[]`; the gateway resolves provider, model and key from this tenant's live configuration. `ctx.caps.config.steps()` — `config_get` over MCP — reads that resolution back without making a call, and needs no grant (below). |
| `kb` | `ctx.caps.kb.search(queries, top_k=10)`, `.available()`, `.stamp(results)` | `kb_search` | Tenant-scoped knowledge-base search. Returns `{title, url, snippet, relevance_score}`. |
| `run_store` | `ctx.caps.run_store.get/set/all` | `run_store_get`, `run_store_set` | A run-scoped JSON key-value store (a Redis hash that expires with the run). |
| `progress` | `ctx.caps.progress.update(step_id, status, detail=None)` | *(the Run Contract's `progress` SSE event)* | The rows the run page draws. A container reports progress on its event stream, not through MCP. |
| `audit` | `ctx.caps.audit.log(action_type, detail)` | `audit_log` | A tenant-scoped audit row, attributed to the run's owner by the chassis — never by the agent. |
| `pii` | `ctx.caps.pii.redact(text)` | `redact` | The intake redaction pipeline on demand: the same recognizers and placeholders intake applies. |

`ctx.caps.seconds_left()` and `ctx.caps.granted(name)` need no grant, and
neither does the run's own configuration (K5a): `ctx.caps.config.steps()`
and `ctx.caps.config.settings()` in-process, the `config_get` tool over
MCP. `settings()` is the `settings[]` the manifest declares, with this
tenant's values — `{key: value}`, a default where the tenant chose none.
`config` is no capability and goes in no grant list: it reads nothing but
the run's own agent's configuration, in the run's own tenant.

### Tool secrets

An agent's own third-party keys — a search key, a vector-store key — are
declared by name in its manifest's `secrets[]` and read with no grant:
`await ctx.caps.secrets.get(name)` in-process, the `secret_get` tool over
MCP (K8a). Each answers this tenant's value: the tenant's own, else every
tenant's default, else — in-process alone — the upper-cased name in the
backend's environment (`search_key` reads `SEARCH_KEY`), the fallback the
process an in-process agent runs in has always given it. A container is
answered from the store's rows alone: its environment is its own. A name
the manifest does not declare raises `SecretNotDeclared` (`-32005`), a
declared name with no value `SecretNotSet` (`-32006`); both are
`KeyError`s, and an unset key usually means "skip this step", not a failed
run. A tenant's admin sets this tenant's value, a platform admin every
tenant's default (`PUT /api/v1/agents/{id}/secrets/{tenant|agent}/{name}`).
A name the platform reserves — a setting of its own, a model provider's
key, anything under `LIBRERUN_` or `OTEL_` — fails the manifest.

`secret_get`'s result is the one response the platform sends with a
secret in it, and only to the run that declared it, in its tenant. The
runner scrubs every value it delivered from what the run persists — the
output, the report, progress, the error text, audit and run-store writes —
replacing each with `[REDACTED_SECRET]`, and refuses a key or a name
holding one (`secret_in_output`). A value stays in the run's scrub after
an admin replaces the row, and an exception the agent raises is scrubbed
before the chassis traces or logs it. An agent's own log lines, a model
prompt and a value it transformed are its own to keep clean.

Two things are *not* capabilities and need no entry in the list: writing
to the trace (the SDK and the in-process tracer do it for you) and
`print()` / `logging`, which become that invocation's log records.

## The MCP endpoint

One JSON-RPC 2.0 endpoint, protocol version `2025-03-26`, at the
`run.mcp.url` the chassis puts in the `POST /v1/runs` body. Authenticate
with the same bearer the chassis sent — the **run token** — on every
call. It expires with the phase.

```http
POST {run.mcp.url}
Authorization: Bearer {the run token}
Content-Type: application/json

{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "kb_search", "arguments": {"queries": ["timeout"], "top_k": 5}}}
```

`initialize` and `tools/list` work as you would expect; `tools/list`
advertises only the current names. `case_store_get` and `case_store_set`
are still accepted on `tools/call` for one release and go at v1.1.

### Arguments are a closed set

Each tool declares its arguments and anything else is refused with
`-32602`. That is deliberate: what an agent can write into a stored row
is a walked argument or nothing.

| Tool | Arguments |
|---|---|
| `kb_search` | `queries` (1–20 strings, required), `top_k` (1–50) |
| `run_store_get` | `key` (required) |
| `run_store_set` | `key`, `value` — any JSON value (both required) |
| `audit_log` | `action_type` (≤30 chars), `detail` (object) — both required |
| `redact` | `text` (≤100 000 chars, required) |
| `config_get` | none |
| `secret_get` | `name` (≤64 chars, required) |

### The refusals

| Code | Means | Do |
|---|---|---|
| `401` | no bearer, a bearer that is not this run's, or one that has expired with the phase | send the run token from *this* invocation's POST body |
| `-32002` | the manifest does not grant the capability this tool needs | add the grant, or stop calling the tool |
| `-32003` | the PII walk refused what you passed (`pii_in_audit`, `pii_in_run_store`, …) — or a tool secret it holds (`secret_in_output`) | redact before you store; a phone number as a JSON *number* is refused wherever it sits |
| `-32004` | `redact` could not run: the PII detector is not ready, and the chassis refused rather than hand back half-redacted text | stop: do not store or send the text, and do not retry with different text (the SDK's `PiiUnavailable`) |
| `-32005` | `secret_get` for a name the manifest does not declare in `secrets[]` | declare it, or stop asking |
| `-32006` | `secret_get` for a declared name with no value in this tenant | treat it as unset; an admin sets it for the tenant, or a platform admin for every tenant |
| `-32602` | an argument the tool does not declare | see the table above |

`-32003` is the one people meet by accident, and it is the platform
working: the walk runs on arguments, not on your good intentions.

## Which surface you are on

| | in-process (`python-package`, `langgraph`) | container (Run Contract) |
|---|---|---|
| services | `ctx.caps.*` | MCP `tools/call` at `run.mcp.url` |
| model calls | `ctx.caps.llm.complete(...)` | the gateway's OpenAI-compatible ingress ([`LLM_Gateway.md`](LLM_Gateway.md)) |
| progress | `ctx.caps.progress.update(...)` | a `progress` SSE event |
| credential | none — you are inside the process | the run token, per invocation |

The Python SDK wraps the MCP surface so a container author writes
`ctx.kb.search(...)` and not JSON-RPC by hand
([`SDK.md`](SDK.md)). The endpoint is documented here because an agent in
a language the SDK does not cover talks to it directly, and because
knowing what the SDK is doing is how you debug it.

## See also

- [`Agents_Design.md`](Agents_Design.md) — the contract and the lifecycle
- [`Run_Contract_v1.md`](Run_Contract_v1.md) — the normative HTTP+SSE spec
- [`Container_Agents.md`](Container_Agents.md) — the walkthrough, with the reach table
- [`Manifest.md`](Manifest.md) — every `agent.yaml` field, generated from the models
- [`../platform/Security.md`](../platform/Security.md) — what a grant is and is not
