# Security notes

What LibreRun guarantees, what it does not, and which credential governs
each flow that leaves the box. This is the operator's page; the author's
side of the same boundary is
[`docs/authoring/Agents_Design.md`](../authoring/Agents_Design.md) and
[`docs/authoring/Container_Agents.md`](../authoring/Container_Agents.md).

Nothing here is aspirational. Every claim names the gate that holds it,
because a security property with no test is a sentence in a document.

## The runtime trust model, in three lines

1. **An in-process agent is trusted code inside the backend's process.**
   It can read anything that process can read, whatever its manifest
   grants. A capability grant is an *audit* boundary — it says what the
   agent asked for and what it used — not a security boundary.
2. **A container agent is network-isolated on the internal `agents`
   network, and that is not a sandbox against hostile code.** It stops an
   agent from reaching Postgres, Vector or the Internet. It does not stop
   an agent that runs code you did not read from doing whatever it likes
   inside its own container with its own secrets.
3. **Therefore: install agents the way you install software.** Read the
   manifest — `capabilities`, `network.egress`, `secrets` — the way you
   read a package's permissions, and run an agent you do not trust in a
   container, never in-process.

That is the whole model. It is deliberately small, and stating it is
better than implying a sandbox that does not exist (L20).

## Tenancy

Every request carries a JWT; the middleware resolves it to a user and
pins `request.state.tenant_id` from the **user record**, never from a
claim the client can choose (`backend/app/middleware.py`). Every query
filters on it. A run, its files, its feedback, its audit rows, its
per-step model configuration and its agent settings are all
tenant-scoped, and one container
agent serving two tenants is the case the container battery exercises
directly — the two-tenant test drives all three example containers and
asserts neither sees the other's data. An agent's settings are each
tenant's values for the `settings[]` its manifest declares, in rows of
that tenant's own, and so, since K8a, are its tool secrets — each tenant's
value of the `secrets[]` it declares, beside every tenant's default a
platform admin sets. Gate T (below) holds both apart wherever a run or an
admin reads them.

The one credential that is *not* tenant-scoped is an agent's gateway key:
it names the agent, not a tenant, which is exactly why it cannot buy a
model call on its own (see **Model calls** below).

What is the deployment's, not a tenant's, is the platform operator's
alone (K9, L31): Application Settings, the deployment view, the
observability pipeline and an agent's Keys tab answer an admin of any
other tenant `403`, whatever the page shows. `test_platform_only_routes.py`
classifies every admin route as platform-only or a tenant admin's, and
drives each platform route as a tenant admin to its refusal.

## PII: redacted before anything is stored

The invariant is in `CLAUDE.md` and it is absolute: **unredacted content
never touches the database.** Redaction happens at every point where
content crosses into storage or leaves the box —

| Point | What is walked |
|---|---|
| intake | every string field the agent's input schema marks `x-pii` (see below) |
| the preview (`POST /files/redact-preview`) | the text, with nothing persisted |
| file upload | the uploaded file's extracted text |
| the run boundary | everything an agent hands back: output, audit details, run-store values, progress labels, event text |
| the MCP `redact` tool | whatever an agent asks to have walked |
| the log and span walkers | log records and span attributes, before export |
| the gateway's outbound leg | the prompt on its way to a provider |

**Intake is schema-driven.** The chassis cannot know which fields of an
agent's input carry personal data, so the agent says: a string property
marked `"x-pii": true` is redacted before the run is stored, and the
wizard offers a redaction preview for it (`docs/authoring/Agents_Design.md`,
input schema). A field **not** marked is stored as submitted, so an agent
must mark every field that may carry personal data. VITA marks its two
log fields; its problem statement and observations are not marked, so
what a user types there is stored as typed. Whether intake should also
run the structural recognizers — addresses, numbers, keys — over unmarked
fields is a v1.1 decision. Everything an agent hands **back** is walked
whole at the run boundary, marked or not.

**It fails closed.** The detector has a readiness state (`ready` /
`unavailable` / `failed`), warmed at startup and reported on `GET
/api/v1/health` and the gateway's `/healthz`. When it is not `ready`,
intake, the preview and upload answer `503`; the run boundary and the
MCP tool refuse; telemetry is stripped to its identity. The named-entity
stage silently skipping was the pre-1.0 behaviour and it is gone
(blueprint S4c, gap H15).

`LIBRERUN_PII_ALLOW_DEGRADED=true` restores regex-only redaction as an
**explicit operator opt-out**. It stamps every span and record it
touched and writes a `pii_detector_degraded` audit row, so a deployment
running degraded says so in its own telemetry rather than looking
healthy. Leave it `false`; the production checklist in
[`Install.md`](Install.md) says why in one line.

## Where each secret lives

No process receives a secret it does not read (decision L28). Where a
value lives is part of the posture, not a preference:

| Secret | File | Read by | Never |
|---|---|---|---|
| provider keys — `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_AI_API_KEY` | `gateway.env` | the `gateway` service alone | the backend, an agent, `.env` |
| a provider key pasted in Admin → Application Settings (K7) | sealed in the browser to the gateway's public key; then the database's `secrets` table as a `gateway` row, MultiFernet ciphertext under the gateway's store key | the gateway alone, which opens the blob and serves the key | the backend (which stores and relays what it cannot open), a response body, a log line, a trace, an audit detail |
| the gateway's store key `LIBRERUN_GATEWAY_SECRETS_KEY`, and the sealing keypair's private half it seals | `gateway.env`; the keypair in the `secrets` table. The key is backed up with the database it opens ([Install.md](Install.md#backup-and-restore), "Backup and restore") | the gateway | the backend, `.env`, an `environment:` line, an agent |
| the session secret, the fallbacks of the OAuth client secret and of the knowledge base's vector-store key, the bootstrap credentials, the secrets store's key `LIBRERUN_BACKEND_SECRETS_KEY` | `.env`. The store's key is backed up with the database it opens ([Install.md](Install.md#backup-and-restore), "Backup and restore") | the backend | the gateway, an agent container |
| a secret set in Admin → Settings (K6) — the Microsoft sign-in secret and, since K8a, the knowledge base's vector-store key | the database's `secrets` table, as MultiFernet ciphertext under the backend's store key | the backend, which decrypts it where it is used | a response body, a log line, a trace, an audit detail, Redis |
| an agent's gateway key `LIBRERUN_AGENT_KEY_<ID>` | `.env`, delivered through the derived `agent-keys.env`; or issued on the agent page's Keys tab (K9), shown once and stored as a sha256 and an eight-character prefix | the gateway and that one agent | any other agent, and the database, which keeps no value |
| an agent's own **tool** secrets — a search key — declared in its `secrets[]` (K8a) | the `secrets` table per tenant, with every tenant's default beside it, as ciphertext under the backend's store key; `.env` as an in-process agent's fallback; a container's own environment for a container | the backend, which delivers each value to the declaring run in its tenant alone — in-process, or over MCP `secret_get` | the gateway, and every other tenant's runs |
| observability vendor tokens | `observability.env` | Vector and the OTLP bridge | the backend, the gateway |

Two rules follow from the table and are worth saying plainly:

- **A provider key is the platform's, and the backend never holds one.**
  The gateway is the only process with one. This is CLAUDE.md's rule and
  it has teeth: Gate P derives each service's *received* set from
  `compose.yaml` and each process's *read* set from its settings model
  and fails on a surplus.
- **A tool secret is the agent's, and since K8a LibreRun brokers it per
  tenant** (D20, D32; gap D3, closed). An agent declares the names it
  reads in `secrets[]`; a tenant's admin sets the tenant's value and a
  platform admin every tenant's default, each sealed in the `secrets`
  table; a run reads its tenant's value through `caps.secrets.get` or the
  MCP `secret_get` — the backend's environment serving as an in-process
  agent's fallback, never a container's. A value's delivery to the
  declaring run is the one exception to write-only (L31), and the runner
  scrubs every value it delivered from what the run persists. It is
  still one process: an in-process agent's tool secret, once delivered,
  is readable by any other in-process agent, because they share one
  Python process. That is the first line of the trust model, restated
  where it costs something.

Every secret is **write-only** at the API (L31): never returned, never
logged, never a span attribute, never in an error — the one exception
being a tool secret delivered to the run that declared it, in its tenant
(`secret_get`'s result, D20), which the runner then scrubs, replacing each
value it delivered with `[REDACTED_SECRET]` in the run's output, report,
error text, progress, audit details and run-store values, and in an
exception the agent raises before the phase span or the run's log
records it — a value the run was handed stays scrubbed after an admin
replaces the row. What that scrub cannot cover is what never crosses
the chassis: an agent's own log lines and OTLP export, a model prompt, a
value the agent transformed. Gate S
injects a canary and sweeps eighteen sinks for it — fifteen from K2, the
`secrets` table (K6), the gateway's status row (K7) and what a run
persists (K8a). A secret set in the admin UI
shows only where its value comes from and a fingerprint — a keyed,
12-character digest under the store key — and its row is readable only
while a configured key opens it: after a lost or rotated key the page
says `unreadable` and the environment serves in its place
([`Install.md`](Install.md), "The secrets store key").

A provider key pasted in the admin UI (K7, L33) is sealed in the browser
to the gateway's public key — RSA-OAEP with SHA-256 over a 3072-bit key,
under a label naming its provider, so a blob sealed for one cannot be
replayed as another's — and the backend stores and relays a blob it
cannot open: it holds no RSA code and no private key, which
`test_no_provider_key_in_backend.py` holds. What that protects is the
key from the backend: its process, its logs and its database role never
see it. What it cannot protect is a page sealing to the wrong key. The
browser trusts the public key the backend serves it, so a backend that
served a key of its own could read the next key pasted. The check is out
of band (D34): the page shows `Seals to SHA256:<hex>`, the digest of the
key it imports, and the gateway logs `gateway_sealing_key
fingerprint=SHA256:<hex>` whenever it loads its keypair — compare them
once, and again after a `--rotate-keypair`. D19's limit: WebCrypto exists
only in a secure context, so over plain HTTP from anywhere but
`localhost` the page seals nothing, says so, and sends nothing, and
`gateway.env` stays the way in.

`<NAME>_FILE` works for every secret in both settings models, for
container secret stores; `sops` + `age` encrypt `.env` and `gateway.env`
at rest. Both are in [`Install.md`](Install.md).

## What a container agent can reach

Three doors, all opened by the **run token** the chassis sends on `POST
/v1/runs`, all expiring with the phase:

| Door | Address | Credential |
|---|---|---|
| run-scoped MCP server | `run.mcp.url`, in the POST body | the run token |
| OTLP relay | `OTEL_EXPORTER_OTLP_ENDPOINT` | the run token |
| LLM gateway | `LIBRERUN_GATEWAY_URL` / `OPENAI_BASE_URL` | the run token in `X-LibreRun-Run-Token`, plus the agent key as bearer for clients that insist on an API key |

The `agents` network is internal: Postgres, Valkey, Vector and the
Internet are unreachable from it. `network.egress: true` in the manifest
plus `egress` in the compose fragment is the documented, visible opt-out
— an agent that needs the Internet has to say so in a file you can read.

The container battery asserts the boundary from *inside* each example
container rather than from the host, because a probe that runs outside
the network it is testing proves nothing about it.

## Model calls

An agent never names a model and never holds a provider key (L23, L25).
It calls the gateway with `model: "librerun/<step id>"`; the gateway
resolves the tenant's configuration for that step to a provider, a model
and its own key at request time.

The agent key alone is refused — `401 run_token_required`, in every mode,
single-tenant demo included. One container serves every tenant, and a
credential that names the agent cannot say whose data a call is about or
which run pays for it. A *provider* key presented in an agent's fragment
is refused outright (`401 agent_key_invalid`).

Rotation is `librerun key rotate <id>`: the old value moves to
`LIBRERUN_AGENT_KEY_<ID>_PREVIOUS` and is accepted until `--finish`
retires it.

## Every flow that leaves the box

Nothing below happens unless you configure it, but the last row. Each
row names the credential that governs it and the file that credential
lives in.

| Flow | Goes to | Credential | Off by default? |
|---|---|---|---|
| model calls | your LLM provider | the provider key in `gateway.env`, or the one pasted in Admin → Application Settings, which wins for its provider — the gateway's either way | yes — `LIBRERUN_STUB_LLM=true` keeps every run inside the box |
| web search | the search provider the agent uses (the demo agent: Tavily) | that agent's tool secret, this tenant's (K8a) | yes — absent key, the step degrades |
| knowledge-base search | your vector store (the demo agent: Pinecone) | the platform's `kb.pinecone_api_key`, set in Admin → Settings, or `PINECONE_API_KEY` (K8a) | yes |
| traces and logs | your observability vendor (Datadog, Elastic, Splunk) | the vendor token in `observability.env` | yes — the default sink is the local console |
| browser telemetry | your own backend only | the user's session | the browser never holds a telemetry credential and never speaks OTLP ([Browser_Observability.md](Browser_Observability.md)) |
| an agent's own egress | wherever that agent chooses | the agent's own | yes — `network.egress` is off unless the manifest asks |
| certificates for the HTTPS edge | Let's Encrypt, then ZeroSSL (ACME), from the `edge` container | the ACME account Caddy creates in the edge's volume, under the e-mail in `LIBRERUN_TLS` or the one a platform admin chose on the Certificates panel (T2) | yes — only with the `tls` profile and ACME chosen, by an e-mail in `LIBRERUN_TLS` or on the Certificates panel; the default local CA, a CA of your own and your own files send nothing |
| the Public Suffix List | publicsuffix.org, then its mirror on GitHub, from the backend and the gateway | none | no — `tldextract`, which presidio's e-mail recogniser calls, fetches the list the first time a process meets an address, and uses the snapshot its package bundles when that fails (tldextract 5.3.2 under presidio-analyzer 2.2.364: read in the pinned code, not run) |

A keyless demo run touches none of them but the last: the Public Suffix
List's refresh asks for no credential, sends nothing of yours, and fails
over to the bundled list. That is what "no keys, no config" means, and
`librerun_smoke.py` runs the whole loop with `LIBRERUN_STUB_LLM=true` to
keep it true.

The Source link on the login page and in the navigation bar, which opens
the `source_url` that `/api/v1/meta` reports beside `license` and sends
nothing anywhere itself, is engineering in support of the AGPL's network
clause (section 13), not a substitute for its terms: an operator who runs
modified source sets `LIBRERUN_SOURCE_URL` to where that source is
([Install.md](Install.md), the production checklist).

Building is a flow of its own. A source build fetches packages, the spaCy
model and the upstream images from their indexes and registries, and
nothing of LibreRun's: a release is source only (L37), and every
first-party image is built from the checkout and never pulled (#133).
Publishing source only changes what the recipient fetches to build, and
nothing about what a running container may reach.
[The distribution surface matrix](../release/Distribution_Surface_Matrix.md)
lists each source of those bytes, how it is pinned, and who moves it next;
every image is pinned by tag and digest (R14).

## Agent-side telemetry files

An agent that writes its own `telemetry.jsonl` has **no ingestion path**
into LibreRun, by design (gap G3). Use the SDK instead: `print()`,
`logging` and the SDK's span helpers inside an invocation arrive as that
invocation's log records and spans, already walked for PII and already
parented to the run's trace. A file on a container's disk is none of
those things, and `docker logs` is empty by construction.

If an agent genuinely must write files — a vendored library that only
knows how to log to disk — mount the directory and point a Vector
`file` source at it in your own overlay, as
[`Observability.md`](Observability.md) shows for the vendor overlays.
That path is yours to operate: it does not pass through the PII walkers,
so redact before you write.

## Transport

**Browser to LibreRun: encrypted, when you turn it on.** The opt-in `tls`
profile runs the `edge` service, Caddy pinned by version and digest, which
terminates TLS on one published port and forwards `/api/v1/*` to the
backend and everything else to the web UI
([Install.md](Install.md), "HTTPS at the edge"). **Edge to services:
plain HTTP** on the compose network, as between every service today; TLS
inside the box is on the roadmap, v1.2 and later. The keyless demo is plain HTTP on
`localhost`, which browsers already treat as a secure context, and stays
so (L35).

**Nothing else answers off the host.** The backend and the web UI publish
on `127.0.0.1` by default, and `compose.sh` refuses the `tls` profile —
exit 4, nothing started — unless both still do, the web UI calls the
edge's own origin (`NEXT_PUBLIC_API_URL=/api/v1`) and its server-side
rewrite reaches the backend on the compose network. That binding is the
guard because nothing else can be: Docker publishes a port with its own
iptables rules, ahead of ufw's and firewalld's, and no override file can
remove a publish. Postgres, Valkey, the gateway, Vector and Jaeger were
loopback-only already.

**Whose address is recorded.** Every audit and session row records the
client address the backend sees. Behind the edge that would be the
edge's own, so uvicorn runs with `--proxy-headers` and believes
`X-Forwarded-For` and `X-Forwarded-Proto` from one address alone:
`FORWARDED_ALLOW_IPS` is the edge's fixed address on its own `edge`
network, which only the edge, `edge-control`, the backend and the web UI
join. Never `*`:
agent containers share the backend's `agents` network and could forge the
headers, and so could anything that reaches the published port. The edge
replaces any forwarding header a client sends. If an engine cannot pin the
edge's address (podman-compose, R21), the fallback trusts the edge
network's subnet, which trusts the web UI's server too. What rootless
Podman's port forwarder (rootlessport) shows the edge of a client's
address is not yet measured: R21 records it (K blueprint §3.6).

**Headers.** The edge sends `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy:
strict-origin-when-cross-origin`, a `Content-Security-Policy` that
confines every load and every send to the one origin (K7: `default-src
'self'`, `connect-src 'self'`, `form-action 'self'`, images and fonts
also from `data:` and `blob:`, `frame-ancestors 'none'`, `base-uri
'self'`, `object-src 'none'`) and, for any site name but `localhost`,
`Strict-Transport-Security: max-age=31536000` without
`includeSubDomains` or `preload`. The policy still allows inline script,
for Next 14's bootstrap, and inline style, for the report's own
`<style>`: it stops a page loading or sending anywhere else, not a
script injected into it. A nonce-based `script-src` is a v1.2 issue, and
needs the edge's line removed, since the edge's header replaces whatever
policy an upstream sends. A start without the `tls` profile sends no
policy. HSTS binds a host on every port,
so a site named `localhost` sends none, and the plain loopback URLs keep
working.

**Key material.** The local CA's root and its private key, and ACME's
account key and certificates, live in the edge's volume,
`librerun-edge-data`, the edge's alone; whoever holds the root's key can
mint a certificate your browsers accept for any name, so it never leaves
the volume. Certificates you bring yourself sit in `LIBRERUN_TLS_CERT_DIR`
(default `./tls`, git-ignored), owner-only, mounted read-only into the edge
alone. A CA, or a certificate and key, loaded on Application Settings (T2)
is written once into the edge's control volume, `librerun-edge-control`, by
`edge-control` — the key mode 0600 — never read back or returned, and
deleted once the choice that replaces it is serving. Neither volume is in
any backup (L42): a restore starts a new root unless the CA is loaded
again, by upload or `LIBRERUN_TLS_CA` ([Install.md](Install.md), "HTTPS at
the edge"). Caddy's admin API is a Unix socket on that control volume and
never TCP, and only the edge and `edge-control` mount it. `edge-control` is
the backend's image in a container of its own, with the control volume
and the Caddyfile and no database, secret or agent; the edge sends it a
certificate change only after asking the backend whether the caller is a
platform admin, with the request's headers and never its body. So the
backend — and an in-process agent, which runs in its process as its user —
reaches neither the socket nor a key file, and receives no key (L42, D44
refined). What the backend shows of the edge is public: the site's names,
the issuer, and each certificate's dates and SHA-256.

**What the edge does not cover.** The bundled Jaeger is unauthenticated
and is never routed through the edge: its UI stays on `127.0.0.1:16686`,
so a "View trace" link opens on the host itself or through an SSH tunnel
(`ssh -L 16686:127.0.0.1:16686 <host>`), never from another machine.

## The gates that hold all of this

| Gate | Holds |
|---|---|
| `chassis-purity` | the chassis names no agent (L13) |
| `chassis-zero-agents` | the chassis boots and serves honestly with no agents installed |
| `unit-suites` → Gate P | no process receives a secret it does not read (L28) |
| `librerun-smoke` → Gate S | no secret reaches any of eighteen sinks, a provider key sealed as the browser seals it and a tool secret set for a tenant included |
| `librerun-smoke` → `secrets-as-files`, `encrypted-at-rest` | `_FILE` and sops recipes run as written, from the document itself |
| `librerun-smoke` → `provider-keys` | a key pasted on Application Settings is sealed in the browser to the key the gateway logged, adopted, and carried by the next model call to a mock provider, with the gateway's `StartedAt` and `RestartCount` unchanged; before the paste and after its DELETE the call fails, and a raw key posted in place of the blob is refused `400 plaintext_refused` and stores nothing (K7) |
| `container-battery` | the run-token binding, the reach boundary from inside the container, two tenants kept apart |
| `adapter-battery` | every framework adapter satisfies the same contract |
| `gateway` | the run token is required; a provider key in an agent's slot is refused |
| `docs` → `link-check`, `openapi-drift`, `readme-walk` | the documentation above still describes this tree |
| `release-readiness` → `spdx`, `publish-purity` | every file's licence is what the licence scope map says, `NOTICE` and `THIRD_PARTY.md` are complete, and nothing names the development repository (L36, L39) |
| `unit-suites` → `test_source_access.py` and the Source link test | a running LibreRun says which licence it is under and where the source of the version it runs is: `/api/v1/meta` carries exactly its keys, `license` is the licence scope map's default, and `source_url` is the operator's or else the running version's tag; the login page and the navigation bar link to it in a new tab (R17) |
| `unit-suites` → Gate T | one agent's settings for two tenants never cross in the admin API, the façade or `config_get` (L32), nor its tool secrets in the façade or `secret_get` (K8a) |
| `librerun-smoke` → `tls-edge` | the TLS gate (L35): with `BACKEND_PORT=0.0.0.0:8000` the `tls` profile is refused with exit 4 and nothing starts; with the edge up, the UI and `/api/v1/meta` answer over HTTPS verified against the root copied out of `librerun-edge`, the plain loopback UI still reaches `/api/v1` through the web UI's rewrite, nothing but the edge publishes off loopback, the four headers and the policy under "Headers" above are each sent exactly once, a sign-in's audit row carries neither the edge's address nor a forged `X-Forwarded-For`, `librerun doctor` signs in through this checkout's edge, G0 passes through the edge, the page refuses to seal a provider key on the plain `http://librerun.test:3000` and sends nothing (D19), Chromium — trusting the edge's root through its own store, never told to ignore certificate errors (D42) — seals one through the edge and walks settings, the dashboard and a report without one violation of the edge's policy (K7), the certificates as a platform admin sees them through the edge (T2) — the status naming the generated root, whose SHA-256 is the copied root's, acknowledged; a certificate that is not a CA refused `422` naming `basicConstraints`, and a body sent into `/api/v1/admin/tls` by a mistyped path or the wrong method the edge's own `404`, never the backend's; a CA the job makes loaded and served within 30 s with the edge's `RestartCount` unchanged, and a second over it the same way; no line of a key in any API answer, in the backend's, the edge's or `edge-control`'s log, or in the database's dump, and no `/control` in the backend's container; back to the environment, the copied root verifying again; and the restore, the edge's two volumes removed and `LIBRERUN_TLS_CA` naming the job's CA, whose leaf verifies first, with the status saying `root_changed` — then R20, the admin walk (B1b): with the CA the edge serves untrusted it fails at its first page; trusted as a browser trusts a root, a platform admin with no shell reads which providers have keys and from where, seals one to the key the running gateway logged, sets `auth.azure_client_secret`, a per-tenant agent setting and this tenant's tool secret, issues a first key for an agent id that has none and rotates it, sees the certificate the edge serves and what it needs, and loads a CA the edge then serves with its `RestartCount` unchanged, no value typed and no line of that CA's key in a page, a container's log or the dump; a tenant admin sees each platform-only page explained, never an error (L31, L43) — and a migration that raises stops the backend rather than booting it on a stale schema |
| `librerun-smoke` → `backup-restore` | "Backup and restore" in `docs/platform/Install.md`, walked as written under a database and a role named other than the defaults: a secret setting and a provider key sealed to the gateway, the database dumped inside `librerun-postgres`, `down -v`, Postgres alone on an empty volume, the dump restored into it and only then the rest, with the same two keys: both fingerprints, and the key the gateway seals to, read back unchanged, and neither value in the dump as text; the same dump under a fresh backend store key leaves the secret `unreadable` and `rewrap_secrets --dry-run` exits 3 naming it, while the gateway still serves its own row (L42) |
| `release-readiness` → `no-publication` | R11: no workflow or local action can publish — no registry login, push, cache export, package publish or release asset, no permission that could publish or sign (`write-all`, or `packages`, `attestations`, `id-token` or `pages` write; `pages` and `id-token` in `docs-site.yml` alone), no workflow without top-level `permissions:`, no secret but `GITHUB_TOKEN`, and a step behind `if: false` counts; `scripts/publication_probes.sh` plants each one and the guard must name it (L37) |
| `librerun-smoke` → `source-build` | R12: every service that builds carries `pull_policy: build` in compose's resolved model, and the stack reaches `/api/v1/health` from an `up` with no `--build`, six images built under a loopback registry's prefix and nothing asked of that registry (#133) |
| `release-readiness` → `public-commit` | R18's probes: the public repository's commits are made as `docs/platform/Releasing.md` says — the first parentless, each after it a fast-forward of the public `main` — with the candidate's tree, the public identity, no branch, and one printed push of `main` and no tag, published in a throwaway clone to a bare repository; a public commit the candidate lacks (the revert guard), a `--base` the public `main` never had, a placeholder, the purity canary in the tree and in the identity, a flagged or malformed handle and a rehearsal that prints a push are each planted and must go red (L34, L39) |
| `unit-suites` → the deployment view and the platform gate | an allowlist of names and non-secret values, never the environment; no platform-only route for a tenant admin (L31) |
| `release-readiness` → `dependency-identity` | R14: every image the tree builds from, runs or tests on is fully qualified and pinned by a tag, never `latest`, and the digest of its multi-arch index, and a workflow's `docker run` names its image by a pinned variable; the distribution surface matrix has a row for each image and lock, and its LiteLLM row holds the gateway lock's version and licence; `scripts/dependency_identity_probes.sh` plants each violation and the guard must name it; and Valkey refuses a volume Redis 7.4 wrote and boots on a fresh one (C-18) |
| `release-readiness` → `tree-review` | R16's scan: the tree `git archive` extracts, the one a public repository would carry, holds no secret gitleaks finds (pinned by the SHA-256 its release lists; rule, file and line printed, never a value), and no gitlink, `.gitmodules`, LFS pointer, file with a NUL byte, cache or build output, archive, compiled object or licence file outside the root and `LICENSES/`; a fixture is allowlisted by its file and rule, never a directory; `--probe` plants a token made at run time and each violation, and each must go red with the token in no output |

Each of them is negative-tested: the violation it exists to catch is
injected and the gate must go red. On a clean tree a broken checker and a
working one look identical, which is why "it passed" is not evidence
until it has also been made to fail.

## Reporting a vulnerability

See `SECURITY.md` at the repository root.
