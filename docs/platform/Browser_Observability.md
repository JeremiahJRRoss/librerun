# Browser Observability (the UX plane)

LibreRun instruments its own frontend — web vitals, JavaScript errors,
page and route views — without any vendor browser SDK, without a public
ingestion key, and without the browser ever speaking OTLP. This document
is the contract: the wire schema, the relay's behavior, the OpenTelemetry
mapping, and the privacy model. It joins the two backend planes
(`platform` | `run`, see `Agents_Design.md` "Observability contract") as
the third plane, `ux`.

```
Browser (Next.js)                         FastAPI backend                    Operator pipeline
┌─────────────────────────┐   LibreRun    ┌──────────────────────────┐  OTLP  ┌──────────────┐
│ telemetry facade        │   RUM v1      │ relay  POST /api/v1/_o/e │ ─────► │ Vector / OTel│
│  web-vitals · errors    │ ────────────► │  auth → validate → quota │        │ Collector →  │
│  route commits · ids    │  (closed JSON │  → authorize surface     │        │ any backend  │
│ NO replay · NO bodies   │   envelope,   │  → stamp identity        │        └──────────────┘
│ NO urls · NO free text  │   bearer JWT) │ translator → OTel        │
└─────────────────────────┘               │  service.name=librerun-web│
                                          └──────────────────────────┘
```

Design verdicts this implements (from the nine-report research round,
decided 2026-08-24): emit vendor-neutral telemetry only; ingest through
an authenticated first-party relay, never a public collector; keep the
browser's wire format a small closed schema and construct OTLP
server-side; represent point-in-time facts as OTel log-record events and
operations with duration as spans; treat every browser value as an
untrusted claim with bounded blast radius. Session replay is permanently
out of scope: users paste unredacted vendor logs into LibreRun, and
replay would bypass server-side redaction.

## Why the browser does not speak OTLP

OTLP is the right protocol *between trusted telemetry infrastructure*,
and exactly the wrong shape at a hostile input: a conforming OTLP/JSON
receiver must ignore unknown fields and accept the full extensible data
model, while this boundary wants the opposite — a closed, versioned
schema where unknown fields are rejected and every value has a bounded
domain. A user holding a valid session JWT can bypass the frontend
entirely and script the endpoint; the schema, not the JavaScript, is the
control. Everything downstream of the relay is pure standard OTel.

## LibreRun RUM v1 — the envelope

`POST /api/v1/_o/e`, `Content-Type: application/json`, authenticated by
the app's ordinary bearer JWT. The path is deliberately neutral (common
filter lists carry generic first-party `/collect`-style rules); the name
is an availability optimization, never a security control.

Source of truth: `backend/app/observability/rum_envelope.py` (server)
and `frontend/src/lib/telemetry/schema.ts` (client) — keep them in
lockstep and bump `schema_version` together on any wire change.

```json
{
  "schema_version": 1,
  "session_id": "<uuid — client-random RUM session, rotated at login/logout>",
  "app_version": "<optional, ^[A-Za-z0-9._-]{1,32}$>",
  "records": [ /* 1..50 typed records */ ]
}
```

Every record carries: `occurred_at_ms` (epoch ms, must lie within
−60 min/+2 min of server time), `page_id` (uuid — one hard/soft view
instance), `route` (a **route template**, e.g. `run.detail` — never a
URL; membership in the closed template set is enforced **server-side**
against `ROUTE_TEMPLATES`, since an authenticated caller can bypass the
frontend resolver), `ui_owner` (`platform`|`agent`), and for agent
surfaces `agent_id` (+ optional `run_id`). Record types:

| `type` | Fields (beyond the base) | Bounds |
|---|---|---|
| `web_vital` | `name`, `value`, `rating`, `metric_id`, `navigation_type?` | name ∈ lcp/cls/inp/ttfb/fcp; 0 ≤ value ≤ 1e7 finite; rating ∈ good/needs-improvement/poor; metric_id `^v[0-9]{1,2}-[0-9]{13}-[0-9]{13}$` (exactly the web-vitals generated shape — digits-only because the value is exported verbatim as `browser.web_vital.id`); navigation_type ∈ web-vitals library values |
| `js_exception` | `error_type`, `mechanism`, `fingerprint`, `bundle_module?` | error_type `^[A-Za-z0-9_.$]{1,64}$` (a class name — free text unrepresentable); mechanism ∈ window.error/unhandledrejection; fingerprint `^[a-f0-9]{8,64}$` (client-local hash); bundle_module must match `^/_next/static/…` (server-enforced prefix; charset excludes `?`/`#`; client-side it is additionally extracted only from same-origin frame URLs) |
| `route_change` | `from_route`, `trigger`, `duration_ms?`, `navigation_id?` | trigger ∈ link/push/replace/traverse/initial/unknown; 0 ≤ duration ≤ 120000, present only when navigation *intent* was observed |
| `page_view` | `navigation_kind`, `referrer_route?` | kind ∈ hard/bfcache_restore |

Unknown fields are rejected (`extra="forbid"`) — at the envelope level
that fails the request (400); at the record level the record drops alone
and the batch survives. There are **no identity fields**: tenant and
user are underivable from the payload by construction.

### Relay responses

| Status | Meaning | Client behavior |
|---|---|---|
| `202 {"accepted", "dropped", "reasons"}` | batch processed; per-record drops counted by reason (`schema`/`skew`/`surface`) | forget the batch |
| `400` | malformed JSON (NaN/Infinity included), bad envelope, >50 records | drop the batch |
| `401` | no/invalid session | drop; do not retry |
| `410` | `UX_TELEMETRY_ENABLED=false` — the runtime kill switch | stop sending for the rest of the browser session |
| `413` | body > 128 KiB | drop |
| `429` + `Retry-After` | quota exceeded | back off, requeue |
| `5xx` | transient | one retry while the page lives |

### Quotas and hardening

Cost per request = `max(records, ⌈bytes/1024⌉)`, charged per minute
against three fixed-window buckets keyed on the **verified** principal:
per user (`UX_TELEMETRY_USER_UNITS_PER_MINUTE`, default 600), per tenant
(`…TENANT…`, 6000), and process-global (`…GLOBAL…`, 60000). Rejects are
never free: bytes are charged before parsing (a malformed 400 still
costs its size), and a request that trips the body cap is charged at the
cap cost (128 units) before the 413 leaves — on the streamed path the
server has already buffered the full cap by then, and either 413 route
would otherwise loop past the quota untouched. Once a bucket is spent,
429 supersedes the 400/413 answer. Redis
outages fail open (quota is an abuse bound, not a correctness gate — the
hard body/record caps and authentication hold regardless). The relay
never logs request bodies, and its own path must stay excluded from any
frontend network instrumentation (telemetry about telemetry recurses).

### Surface authorization

`ui_owner="agent"` is a *claim*. The relay drops the record unless the
named `agent_id` exists in the registry, and — when a `run_id` is
claimed — that run is the authenticated tenant's own live (not
soft-deleted) run **of that agent**. A tenant-A user cannot attribute
telemetry to tenant B's runs, or to run A while browsing run B with a
mismatched agent. Platform records must carry no agent fields at all.

## Identity: three concepts, never conflated

- **Auth session** — the JWT. Authenticates the principal; never appears
  in telemetry.
- **RUM `session.id`** — client-random uuid, format-validated but
  untrusted; rotated by the facade at login and logout so sessions never
  correlate across auth boundaries.
- **Page/navigation ids** — `librerun.page.id` (one per hard/soft view)
  and `librerun.navigation.id`, the cross-signal join keys.

The relay stamps what the server knows: `librerun.tenant.id` (from the
JWT), `librerun.user.pseudonym` — a **tenant-scoped keyed HMAC** of the
user id (never a bare hash: user ids are enumerable, and the per-tenant
key means the same user maps to unrelated pseudonyms under different
tenants) — and `librerun.telemetry.source="browser_untrusted"` so
downstream policy can always distinguish browser claims from backend
facts.

## OpenTelemetry mapping (the translator)

`backend/app/observability/web_telemetry.py` is the only place OTel
objects are built for browser data. Dedicated providers carry the
resource `service.name=librerun-web`, `service.namespace=librerun`, and
the pinned browser-vocabulary version
`librerun.semconv.version="1.44.0"` — the `browser.web_vital.*`
convention is Development-status and has already taken one breaking
rework, so the pin names exactly what this translator implements and a
future rename lands here, in one file. Emission is endpoint-gated by
`OTEL_EXPORTER_OTLP_ENDPOINT` exactly like backend tracing (blank =
records validate, count, and drop cleanly).

| Record | OTel signal | Names |
|---|---|---|
| `web_vital` | **log-record event** `browser.web_vital` (SemConv 1.44 shape) | `browser.web_vital.name/.value/.id/.rating/.navigation_type` |
| `js_exception` | **log-record event** `exception` | Stable `exception.type`; `librerun.exception.mechanism`, `librerun.error.fingerprint`, `librerun.error.bundle_module`. **No `exception.message`, no `exception.stacktrace` — ever.** |
| `route_change` with `duration_ms` | **span** `browser.route.commit` (explicit client timestamps, fresh root) | `librerun.navigation.from_route/.trigger/.id` |
| `route_change` without duration | log-record event `librerun.route_change` | same |
| `page_view` | log-record event `librerun.page_view` | `librerun.navigation.kind/.referrer_route` |

Every signal carries the common context: `librerun.scope="ux"`,
`librerun.telemetry.source`, `session.id`, `librerun.page.id`,
`librerun.route.template`, `librerun.ui.owner`, tenant id, user
pseudonym, and (agent surfaces) `librerun.agent.id`/`librerun.run.id`.
Log records and spans are emitted with an **empty context** — the
relay's own request span is never their parent, and metrics are the
pipeline's job (derive vitals histograms downstream; per-browser metrics
would be a cardinality trap).

The `browser.route.commit` span is honestly named: it measures
navigation **intent → committed URL state** (Next.js App Router's
`usePathname`/`useSearchParams` boundary), never "route fully rendered"
— Server Components and Suspense stream after commit and no framework
event marks "settled". No network-quiet or DOM-quiet heuristics.

## Privacy model

The design goal is stronger than "redact before storage": browser
telemetry is **structurally unable** to carry user content. Enforced in
four layers, each independently tested:

1. **Positive typed schema in the browser** — builders accept only
   enumerated categories, bounded numbers, and pattern-checked
   identifiers; out-of-domain input builds nothing. URLs never leave the
   browser: route identity comes from the closed route map, and unknown
   paths collapse to `other`. Error *messages* and raw stacks never
   leave either — only a class name, a local fingerprint hash, and an
   own-bundle asset path with query/hash unrepresentable. Error storms
   are budgeted client-side (per-fingerprint and per-session caps).
2. **The relay re-validates everything** with the same closed schema
   (`extra="forbid"`), plus skew, size, and surface authorization.
3. **The translator's attribute-key snapshot test** — a new emitted
   attribute key fails CI before it can widen the surface silently.
4. **Canary suites on both sides** — secret-shaped strings are injected
   into every representable slot and asserted absent from all serialized
   output; a guard-the-guard test plants a deliberate leak and requires
   the detector to catch it (a sweep that cannot catch a planted leak
   proves nothing).

Never collected, by construction: DOM text, input/textarea values,
clipboard, request/response bodies, console output, URLs/query
strings/fragments, error messages, raw stacks, replay of any kind.

Compliance posture: treat UX-plane records as personal data while they
carry the user pseudonym and session id (they do). The recommended
retention split — raw UX events 14 days (30 max for troubleshooting),
derived aggregates 90 days — is an engineering recommendation, not a
statutory number. Client IPs are never written into UX records.

## Operating it

- **Enable:** it is on by default and emits only when
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set (same knob as backend tracing).
  Nothing to configure in the frontend build; the browser knows only the
  relative relay path.
- **Kill switch:** set `UX_TELEMETRY_ENABLED=false` and restart the
  backend — browsers receive 410 and stop for the rest of their
  sessions. No frontend rebuild.
- **Route it:** UX-plane data arrives on the OTLP interface under
  `service.name=librerun-web` (`config/vector.yaml` sink gallery shows
  the three-plane split). Point it at any OTLP backend; Honeycomb reads
  the standard `session.id` today, Elastic's OTel RUM path (preview) and
  SigNoz chart the events generically.
- **Quotas:** tune the three `UX_TELEMETRY_*_UNITS_PER_MINUTE` knobs;
  429s are visible client-side as backoff, server-side in relay logs.

## Boundaries and roadmap

Phase 1 (this document) ships vitals, errors, route commits, page
views, attribution, and the trust boundary. Deliberately not yet built:
the initial document-load trace and its server `traceparent` join,
resource-timing budgets, soft-navigation vitals tagging
(`navigation.kind`), and browser→backend trace continuation. When API
traces arrive, the decided policy is **restart-and-link**: the backend
roots its own trace and records the browser's context as a span link —
an incoming `sampled` flag is never sampling authority (W3C flags are
advisory; OTel's default `ParentBased` sampler would otherwise let any
JWT holder force recording).
