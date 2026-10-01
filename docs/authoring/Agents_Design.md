# Designing an Agent

The behavioural contract every pluggable agent must satisfy — the
anatomy every agent has. How an agent is conceived and planned — the
problem, the users, the phases to cut and where to gate — is taught in
person. This doc is the companion to
[`docs/authoring/Agents_Install.md`](./Agents_Install.md), which covers
loading and directory layout. Read that first if you have not registered
an agent before.

The base class, dataclasses, and runner referenced here are the
authoritative source — if anything in this doc contradicts
[`backend/app/agents/protocol.py`](../../backend/app/agents/protocol.py)
or
[`backend/app/services/agent_runner.py`](../../backend/app/services/agent_runner.py),
the code wins.

---

## Mental model

The chassis drives every agent through the **phase list its
`agent.yaml` manifest declares** (blueprint B7). For VITA's manifest —
`analyze`, then `investigate` gated on approval — the lifecycle is:

```
POST /runs ──► start_run ──► analyze ──► awaiting_approval
                                              │
                                       (customer edits?)
                                              │
                               rerun_current_phase ─► awaiting_approval
                                              │
                                      (customer approves)
                                              │
                                              ▼
                              resume_run ──► investigate ──► complete
```

- `start_run` / `resume_run` / `rerun_current_phase` are background
  tasks in
  [`app/services/agent_runner.py`](../../backend/app/services/agent_runner.py).
  They walk the manifest: consecutive phases **without** an
  `approval` gate auto-advance inside one task; a gated phase parks
  the run in `awaiting_approval` until the approve endpoint calls
  `resume_run`. A single-phase agent goes submit → complete with no
  gate at all.
- The runner **owns all persistence.** It sets `Run.status`, tracks
  `Run.current_phase`, writes `RunSnapshot.analysis` (non-final
  phases) / `structured_data` + `report_html` (final phase), and
  opens the OTEL root span for each phase.
- The agent is **pure** — it receives an `AgentInput`, returns a
  result object, and never touches `Run` / `RunSnapshot` columns
  directly. (It may write ancillary rows like audit entries in its
  own session.)

---

## The `agent.yaml` manifest

Source of truth:
[`backend/app/agents/manifest.py`](../../backend/app/agents/manifest.py);
worked examples:
[`backend/agents/vita_v1/agent.yaml`](../../backend/agents/vita_v1/agent.yaml)
and the commented manifests `librerun init` renders from its three
templates (`cli/src/librerun/templates/`); the generated field reference
is [`docs/authoring/Manifest.md`](./Manifest.md).

Every agent directory ships one — discovery refuses to register an
agent without it. Fields:

| Field | Required | Meaning |
|-------|----------|---------|
| `manifest_version` | no (default `1`) | Contract version; only `1` exists. |
| `id` | yes | Registry key. Must equal the class `agent_id` (mismatch → skipped with `agent_manifest_id_mismatch`). Lowercase/digits/hyphens. |
| `name` | yes | Display name; should equal the class `display_name`. |
| `description` | no | One-liner for pickers/cards. |
| `runtime` | yes | `python-package` (imported in-process, this doc's main subject) or `container` (blueprint B12a — a running container speaking [Run Contract v1](./Run_Contract_v1.md), addressed by `container.url`; requires `input_schema` since there is no code to serve it). Container agents get the whole generic lifecycle — wizard, gates, structured results, trace links — without any Python. |
| `container` | with `runtime: container` | `{url}` — where the chassis reaches the agent's contract endpoints. `${VAR}` expands from the chassis environment at discovery; unresolved → registration skipped (logged). |
| `network.egress` | no (default `false`) | S4. Agent containers join only the internal `agents` network (`compose.yaml`): the platform — the Run Contract, the run-scoped MCP server, the OTLP relay, and since S4a the LLM gateway — is all they can reach; no path to Vector, Postgres, Redis or the Internet exists. `true` declares that the agent's compose fragment (`agents.compose.yaml`) also joins the non-internal `egress` network — an opt-out from "no path off-box but through the chassis" that the admin page shows beside the grants; the fragment must match. |
| `phases` | yes | Ordered list of `{name, approval, deadline_seconds, steps}`. `approval: true` = a human must approve the previous phase's output before this one runs. The first phase can't be gated. Phase names dispatch to agent methods of the same name via `AgentProtocol.run_phase`. `deadline_seconds` (S4, optional) is one invocation's wall-clock budget under the platform ceiling `LIBRERUN_MAX_PHASE_SECONDS` (default 3600; unset = the ceiling, above it = clamped): the runner fails the phase when it passes, for every runtime; a container receives it in the `POST /v1/runs` body (`AgentInput.deadline_seconds` in-process) and its run token expires a minute after it. `steps` (S7, optional) is `[{id, label}]`: the progress rows the phase reports — `id` is whatever the agent passes as `step_id`, free text — with the label the run page shows for each; an undeclared row renders under its raw id. Declaring a row says nothing about models: the page shows a model only on a row whose id **equals** an `llm.steps[]` id (the gateway records the model by LLM step id); a row named otherwise — a LangGraph adapter's `phase:node` — says "on the trace" and links there (gap E5). |
| `framework` | no | S7. Free text naming the framework the agent is built on (`langgraph`, `llamaindex`, `vercel-ai-sdk`), shown as the badge on the agent card; empty shows the runtime instead. A label and nothing else — the chassis dispatches on `runtime`, never on this (L7: no framework-specific UI). |
| `input_schema` | no | Path (inside the agent dir) to a JSON Schema file for intake. Until blueprint B8, agents may keep serving the schema from `input_schema()` in code instead. |
| `output.mode` | yes | `html_report` (agent renders HTML, the demo agent's mode — served via the report endpoints) or `structured` (the final phase's structured result is exposed on the run detail as `structured_output` and rendered generically by the results view; downloads fall back to a generic document). Both modes are live since B9. |
| `feedback_sections` | no | `{id, label}` list (plain strings coerced); thumbs targets the results view renders, and the vocabulary `POST /feedback` accepts for this agent's runs (undeclared sections 422 — enforced since B9). Default: one `overall`. |
| `scenarios` | no | Directory (inside the agent dir) of demo scenario JSON files; default `scenarios`. See below. |
| `capabilities` | no | The agent's **grant** since blueprint B13. The runner builds a per-run façade from this list (`llm` — `complete(step, messages)` through the gateway since S4a, never a provider key and never a model name; `kb`, `run_store` — run-scoped scratch that expires seven days after its last write (`docs/authoring/Run_Contract_v1.md`), never a record of anything — `progress`, `audit`, and since S4 `pii` — `redact(text)`, the intake PII pipeline on demand, the MCP `redact` tool for containers); reaching for an ungranted one raises `CapabilityNotGranted`, in-process and over the run-scoped MCP server alike. Unrecognized names remain legal and grant nothing (descriptive slugs like VITA's `web-search`), but are logged so a typo of a real capability is visible. The run's own configuration needs no grant: `caps.config.steps()` and `caps.config.settings()` in-process, the MCP `config_get` tool for a container (K5a). |
| `ui.intake` | no | Intake layout (B8): `{steps: [{title, description?, fields}]}` renders the stepped wizard (fields reference top-level input-schema properties; empty `fields` = informational step; unclaimed properties get an automatic "Details" step; the chassis appends the final Review step). Omit for a single-page form. The legacy string `generic` still parses as "no steps"; `bespoke` was removed when VITA moved onto the generic wizard. |
| `ui.list.title_path` | no | Dotted path into `user_inputs` naming the run's **title** (blueprint S2): resolved at intake into the run row and shown/searched on the dashboard for every agent. Omitted: the first string input not marked `x-pii`. |
| `ui.approval.summary_path` | no | Dotted path into a parked phase output naming the **summary** string the approval view shows and offers for editing (S2). Served by `GET /runs/{id}/approval` as `summary` next to the full `payload` and the parked `phase`. Omitted: the first non-empty string in the output. |
| `settings` | no | K5a (L32). `[{key, label, type, default, options, description}]`: the settings the agent reads at run time, each with its type (`string`, `int`, `float`, `bool`, `enum` with its `options`, `string_list`) and its default. Each tenant's admin edits the effective values on the agent page's **Settings** tab, a container's included; a run reads them with `caps.config.settings()` in-process or `ctx.config.settings()` (the MCP `config_get` tool) in a container, and needs no grant. A value is held to its type strictly (a boolean is never an integer), and a value equal to the default is stored as nothing, so the agent's next release can move a default and reach every tenant that never chose one (D17). Declaring any turns the deprecated `config_meta()` path off for the agent. Field reference: [`Manifest.md`](./Manifest.md#agentsettingspec). |
| `secrets` | no | K8a (L31, D20, D32). The names of the third-party keys the agent's own tools need — `[search_api_key]` — never a model provider's key (the gateway alone holds those, L23) and never a platform setting. Each name is valued on the agent page's **Secrets** tab (K8b): this tenant's value, set by its admin, and every tenant's default, set by a platform admin; neither is ever shown again. A run reads one with `caps.secrets.get(name)` in-process, which falls back to the upper-cased name in the backend's environment, or `ctx.secrets.get(name)` in a container, the MCP `secret_get` tool, which answers from the two rows alone; neither needs a grant. A declared name nobody set is `SecretNotSet` (`-32006` over MCP), an undeclared one `SecretNotDeclared` (`-32005`), and every value a run was delivered is scrubbed from what it persists (below). |

**Scenarios.** A scenario file is
`{"name": str, "description"?: str, "user_inputs": dict}` where
`user_inputs` is a valid `POST /runs` body. They are served at
`GET /agents/{id}/scenarios`, power the intake page's "Load
scenario" prefill, and the demo scenario doubles as the CI smoke
run (blueprint B14). The submittability promise is enforced at
serve time — since B8 each agent owns its request shape, so
`user_inputs` is validated against the agent's own
`input_schema()`. Malformed or non-submittable files are logged
(`agent_scenario_invalid`) and skipped.

---

## Packaging & distribution (blueprint B12)

An agent is **one package + one manifest**, and it reaches the chassis
two ways:

- **Directory install** — the package sits under the agents directory
  (`LIBRERUN_AGENTS_PATH`, default `backend/agents/`) and is imported
  as `agents.<name>`.
- **Pip install** — the package ships as a distribution declaring a
  `librerun.agents` entry point that resolves to the package itself
  (VITA: distribution `vita-agent`, entry `vita-v1 = "vita_v1"`).
  Discovery resolves the entry point, finds `agent.yaml` in the
  package directory, and applies the identical contract.

Discovery imports directory agents under a parent package **bound to
the scanned root** — the in-tree root keeps its natural `agents` name,
and a custom `LIBRERUN_AGENTS_PATH` whose basename would resolve
elsewhere (or isn't a valid identifier) gets a synthetic parent bound
directly to the directory, so a custom root always serves its own
packages even when a same-named regular package exists on `sys.path`.

Design rules that make one codebase serve both modes:

1. **Relative imports internally** — the same module tree imports as
   `agents.vita_v1` (in-tree) and `vita_v1` (installed). Never
   hardcode the in-tree absolute name in the package's own code.
2. **Assets travel as package-data** — `agent.yaml`, the report
   template, `scenarios/`. Discovery and the scenario
   endpoint read them from the package directory on disk, wherever
   that is (checkout or site-packages).
3. **The chassis is a host requirement** — agent code may import
   `app.*` (protocol, config, models, logging). Declare only your
   direct third-party deps in `pyproject.toml` and install the agent
   into the chassis environment; a standalone venv without the chassis
   cannot run it. (True SDK-grade decoupling is later blueprint work.)
4. **Editable state never assumes a writable package** — a wheel can
   land in read-only `site-packages`, so anything the agent writes at
   runtime goes under the state directory (`LIBRERUN_STATE_DIR`, default
   `$XDG_STATE_HOME/librerun` → `~/.local/state/librerun`), never beside
   its package. Configuration is not such state: an agent's settings
   are declared in `settings[]` and valued per tenant by the platform
   (L32), which is how the demo agent keeps its own since K5b.
5. **Same id in both modes → the directory copy wins** — entry points
   register first, the directory scan second, and the registry's
   last-wins overwrite makes the local checkout beat the installed
   snapshot. Iterate in-tree, ship as a wheel.

---

## The `AgentProtocol` contract

Source:
[`backend/app/agents/protocol.py`](../../backend/app/agents/protocol.py).

| Member | Required | Used by |
|--------|----------|---------|
| `agent_id` / `display_name` / `description` (class attrs) | yes | Registry (cross-checked against `agent.yaml`), `GET /agents`, `AgentPicker`, admin cards |
| One `async` method **per manifest phase** (VITA: `analyze`, `investigate`) | yes | The runner dispatches each declared phase to the method of the same name via `run_phase` |
| `input_schema() -> dict` | yes | `DynamicForm` on `/runs/new`, `GET /agents/{id}/input-schema` |
| `async run_phase(phase_name, inp, on_progress)` | optional override | Default `getattr` dispatch covers one-method-per-phase; override for exotic routing |
| `config_meta() -> AgentConfigMeta \| None` | deprecated since K5a, removed at v1.2 | For an agent that declares no `settings[]`, its `settings` are still served on the Settings tab in the manifest's shapes — one value for every tenant, marked `meta.deprecated`, with `agent_settings_protocol_deprecated` in the log on every request. Declare `settings[]` in `agent.yaml` instead (L32) |
| `llm.steps[]` in `agent.yaml` (not a method) | optional | The Steps tab of `/admin/agents/{id}/config`. Since S4a a step's effective model is the tenant's admin configuration over the manifest's default, resolved by the gateway at request time — the two protocol methods that used to serve this are gone, because the one implementation of them wrote one file shared by every tenant |
| `settings[]` in `agent.yaml` (not a method) | optional | The Settings tab, for any runtime: each tenant's values, stored as divergences from the declared defaults and read by a run through `caps.config.settings()` (K5a). With `llm.steps[]`, what `has_config` means — the agent page has a Steps or a Settings tab |
| `get_settings() / update_settings()` | deprecated since K5a, removed at v1.2 | The deprecated path's values and its Save: one `update_settings({key: value})` per Save, a `null` sent as the declared default. A run reads its tenant's settings through `caps.config.settings()` instead |
| `review_schema()` | optional | Structured approval UI (fallback: free-text edit) |
| `feedback_sections()` | optional | Legacy in-code feedback keys; superseded by the manifest's `feedback_sections` |
| `report_template_path()` | optional | Point at a Jinja template for PDF render |

Non-final phases return an `AnalysisResult` (`status`
`"awaiting_approval"` = phase succeeded — whether a review actually
happens is the manifest's call); the final phase returns an
`InvestigationResult` (`status` `"complete"` = run done).

The base class provides sensible no-op defaults for every optional
method, so an agent only opts into config / review / feedback /
report surface as needed.

---

## `input_schema()` — the intake contract

Return a JSON Schema object (a Python `dict`). It is the **request
contract** for `POST /runs?agent_id=<id>` (blueprint B8): the wizard
renders from it, the server validates submissions against it (422 on
violation), and scenarios are checked against it before being served.
Supported conventions:

- Standard JSON Schema keywords: `type`, `required`, `properties`,
  `title`, `description`, `enum`, `minLength`/`maxLength` (the wizard
  shows char counters and gates Next on them), nested `object`
  properties with their own `required`, `additionalProperties`, etc.
  Validation is Draft 2020-12 via `jsonschema`.
- `"x-ui-widget": "textarea"` on a string property opts into a
  multi-line input.
- `"x-pii": true` on a string property marks content that may contain
  PII: the wizard offers a redaction-preview file upload
  (`POST /files/redact-preview`) for it, and the server **redacts the
  field before persisting** — schema-driven enforcement of the
  "unredacted content never touches the database" invariant.
- `"x-upload-kind": "log" | "config"` picks the upload extension/size
  policy for that preview (default `log`).

Minimum viable schema (the shape the old scaffold carried; the
templates `librerun init` renders ship a `question` field of the same
kind in `input_schema.json`):

```python
def input_schema(self) -> dict:
    return {
        "type": "object",
        "required": ["description"],
        "properties": {
            "description": {
                "type": "string",
                "title": "Describe your problem",
                "x-ui-widget": "textarea",
            },
        },
    }
```

The form values land in `AgentInput.user_inputs` (persisted to
`Run.user_inputs`, a JSONB column).

> **VITA note:** since B8 VITA is just another schema-driven agent —
> its seven-step wizard is its manifest's `ui.intake.steps` over its
> `input_schema()`, rendered by the same `DynamicForm` every agent
> uses. There is no bespoke intake code path anymore.

---

## `analyze(inp, on_progress)` — phase 1

Runs when the run is created or the customer edits the refined
statement. Return an `AnalysisResult`:

```python
@dataclass
class AnalysisResult:
    display: dict       # shown on the approval screen
    structured: dict    # persisted to RunSnapshot.analysis, replayed into investigate
    status: str = "awaiting_approval"
```

Status semantics for a **non-final** phase (see `_run_phases` in
[`agent_runner.py`](../../backend/app/services/agent_runner.py)):

| `result.status` | `Run.status` becomes |
|-----------------|----------------------|
| `"awaiting_approval"` | `awaiting_approval` if the next phase is gated, else the next phase starts immediately |
| `"blocked"` | `error` |
| anything else | `error` |

`structured` is handed back verbatim to `investigate` as
`AgentInput.prior_analysis`. Keep it JSON-serialisable. The runner
strips a reserved `_drifts` key before persisting if you use one for
schema-drift reporting.

---

## `investigate(inp, on_progress)` — phase 2

Runs after the customer approves the analysis. Return an
`InvestigationResult`:

```python
@dataclass
class InvestigationResult:
    status: str
    report_html: str | None = None
    structured: dict | None = None
    error: str | None = None
```

- `report_html` is persisted to `RunSnapshot.report_html` and served
  verbatim by `GET /runs/{id}/report/embedded`. The run page
  injects the HTML directly — do not add React-side citation or
  section rendering (see `CLAUDE.md`).
- `structured` is persisted to `RunSnapshot.structured_data`.
- `status="complete"` moves the run to `complete`. Anything else
  moves it to `error`.

---

## `AgentInput`

```python
@dataclass
class AgentInput:
    run_id: UUID
    tenant_id: UUID
    user_inputs: dict
    prior_analysis: dict | None = None   # every phase after the first, and reruns
    user_edits: str | None = None        # reruns (customer edited the parked output) only
```

The runner populates `prior_analysis` from the snapshot the previous
phase wrote. Your agent must be **idempotent across re-runs** — a
customer may edit the refined statement several times before
approving.

---

## Progress callbacks

The runner passes an `on_progress` callable:

```python
OnProgress = Callable[[StepProgress], Awaitable[None]]

@dataclass
class StepProgress:
    step_id: str
    status: str   # pending | running | complete | error | skipped
    detail: str | None = None
```

Two supported patterns:

1. **Call it directly** — writes a Redis hash at
   `run:{run_id}:progress`, which the frontend polls to light up
   step badges.

   ```python
   await on_progress(StepProgress("my_step", "running"))
   ```

2. **Use `PipelineOrchestrator`** — wrap each step in
   `orch.run_step(run_id, step_id, coro_factory)` (see
   [`backend/app/services/orchestrator.py`](../../backend/app/services/orchestrator.py))
   to get timing, OTEL child spans, error logging, and progress
   writes for free. This is what `vita_v1` does. The orchestrator
   is duck-typed on its `llm` argument — it only calls
   `llm.get_step_config(step_id)` (for span attributes) and
   tolerates a `KeyError` for non-LLM steps.

The two patterns mix cleanly — you can orchestrator-run the heavy
steps and call `on_progress` directly for lightweight ones.

---

## Optional config surface

Every registered agent has a page at `/admin/agents/{id}/config`, linked
from its admin card: since K4b a tab shell, whose tabs appear when the
agent has something for them — **Steps** for the LLM steps its manifest
declares, **Settings** for the settings it declares, **Secrets** for the
tool secrets it declares (K8b) — and which says so plainly when it has
none; a platform admin also sees **Keys**, the agent's gateway keys, on
every agent's page (K9). `has_config` on `GET /agents` is the same
predicate: it counts `llm.steps[]`, `settings[]` and, since K8a,
`secrets[]`.

All three are declared in `agent.yaml` and valued per tenant by the
platform (L25, L32, L31). None is a method to implement, so a container
gets every tab exactly as an in-process agent does:

```yaml
settings:
  - key: search_depth           # what a run reads the value by
    label: Search depth         # what the Settings tab shows; the key if omitted
    type: enum                  # string, int, float, bool, enum or string_list
    options: [basic, advanced]  # an enum's choices, and no other type's
    default: advanced           # every tenant's value until its admin picks another
    description: How far the web search looks.
```

- **Reading.** A run reads its tenant's values when it asks:
  `await inp.capabilities.config.settings()` in-process,
  `await ctx.config.settings()` in a container (the MCP `config_get`
  tool). Either is a `{key: value}` mapping with every declared key, and
  a tenant that never changed a setting reads its default. Neither needs
  a grant: a setting is the agent's own configuration, not a platform
  capability.
- **Storing.** Each tenant's values live in `agent_settings`, keyed by
  tenant, agent and key; the Settings tab saves them with `PUT
  /agents/{id}/config/settings` and a body of `[{key, value}]`, `null`
  for the default. A value is held to its type strictly — a boolean is
  never an integer, a float takes an integer and keeps a float — and one
  equal to the default is stored as nothing (D17), so your next release
  can move a default and reach every tenant that never chose one. A row
  whose key you remove, or whose type you change, is ignored and the
  default served.
- **Steps.** The steps table is declared the same way: your LLM steps in
  `agent.yaml` (`llm.steps[]`, blueprint S4a), their effective values per
  tenant in `agent_step_configs`, resolved by the gateway at request time.
- **Secrets.** A tool secret is declared by name alone, in `secrets[]`,
  and valued on the Secrets tab: each name shows this tenant's row and
  every tenant's default — set or not, its fingerprint, who set it and
  when a run last read it, never a value — and a platform admin alone
  writes the default (D32). A run reads one with `caps.secrets.get(name)`
  or `ctx.secrets.get(name)`, as the manifest table says.

**Deprecated: `config_meta()`, `get_settings()` and `update_settings()`.**
Before K5a an in-process agent returned `AgentConfigMeta.settings` from
`config_meta()` and kept the values itself, one value for every tenant.
For one release an agent that declares no `settings[]` is still served
that way, in the shapes above, with `meta.deprecated` set — the Settings
tab then says that its values are every tenant's — and
`agent_settings_protocol_deprecated` in the log on every request; a Save
is one `update_settings({key: value})`, `null` sent as the declared
default. The three methods are removed at v1.2. Declaring `settings[]`
turns the deprecated path off for the agent; the demo agent declares
`settings[]` since K5b and ships no `config.json`.

Both config `PUT`s funnel through
[`backend/app/routers/agents.py`](../../backend/app/routers/agents.py),
which writes a `config_change` audit row with
`surface="agent_config"` — the settings row naming the keys whose value
changed — so you do not need to audit writes yourself.

---

## Report rendering

If your agent produces an HTML report:

1. Build the HTML inside `investigate` and return it as
   `result.report_html`. It is shown inside the run page as-is.
2. For richer rendering, ship a Jinja template in your agent
   directory (e.g. `report.html`), render it in a helper, and return
   the string. See
   `render_embedded` in
   [`backend/agents/vita_v1/report.py`](../../backend/agents/vita_v1/report.py)
   and
   [`backend/agents/vita_v1/report.html`](../../backend/agents/vita_v1/report.html).
3. To control the downloadable HTML/PDF document, override
   `render_report_document(run, structured)` and return a complete
   HTML document string (blueprint B9). VITA renders its own Jinja
   template behind this hook. Agents that skip it fall back to their
   cached embedded fragment; `structured`-mode agents fall back to a
   generic rendering of their structured result — export works out of
   the box either way, and the shell never imports agent modules.

**External links must open in a new tab** (from `CLAUDE.md`): in
Jinja, use the `ext(url, text)` macro defined in
`backend/agents/vita_v1/report.html`. Rolling your own `<a>` requires
`target="_blank" rel="noopener noreferrer"` on any `http://` / `https://`
href.

---

## Platform invariants every agent MUST observe

These are non-negotiable (all come from `CLAUDE.md`):

- **Tenant scoping.** Every DB query filters by
  `inp.tenant_id`. The runner gives you the tenant — use it.
- **PII redaction before persist.** Route user text through
  `app.logging_pii.user_content(...)` before logging; never write raw
  user content into audit rows. The runner handles `analysis` /
  `structured_data` / `report_html` — those flow back through your
  return value.
- **Config-driven pipeline.** Declare your LLM steps in `agent.yaml`
  (`llm.steps[]`) and call them by **step id**, never by model:
  `ctx.llm.complete("analyze", messages)` in a container,
  `ctx.capabilities.llm.complete(...)` in process. Provider, model,
  temperature and limits are the tenant's admin configuration over your
  declared defaults, resolved by the gateway at request time
  (`docs/authoring/LLM_Gateway.md`). Never hard-code a model, and never
  ask for a provider credential — there is none to hand out.
- **Soft delete.** The shell handles run deletion via `deleted_at`.
  Agents never delete `Run` rows.
- **Naming conventions.** If you emit vendor-style citation data,
  match VITA's keys: `works_cited_a` / `works_cited_b` /
  `skills_cited`.
- **Drift audit.** If you validate LLM output shape, log drifts via
  `app.services.audit_service.log_schema_drift` inside a SAVEPOINT so
  a write failure cannot poison the outer transaction. Pattern:
  `_persist_drifts` in
  [`backend/agents/vita_v1/agent.py`](../../backend/agents/vita_v1/agent.py).

---

## What the chassis does with what you hand it (S4)

Every agent-supplied value the chassis persists or forwards is
**walked** at the boundary (`app/services/run_boundary.py`, one walker
in `pii_service`), so "unredacted content never touches the database"
holds by construction rather than by an agent's good manners:

- **Content** — string leaves of your terminal output, `report_html`,
  the analysis text, audit `detail` values, run-store values, progress
  details, container `log` / `failed` text — is redacted in place with
  the intake placeholders (at this boundary dates and place names are
  left alone: a report's timeline is content, not identity).
- **Identifiers** — object keys, a run-store key, a progress `step_id`,
  an audit `action_type` — are checked as their literal text and never
  rewritten. A flagged one **refuses the write**: the run ends `error`
  with reason `pii_in_output` for terminal output; the capability call
  raises `PiiRefused` with `pii_in_audit`, `pii_in_store` or
  `pii_in_progress` for the others (over MCP: `-32003`); a container's
  `progress` event is dropped with a warning, since it has no reply
  channel. The message names the argument and the JSON path, never the
  value.
- **Numbers** — integers and integral doubles — run the walker's number
  rule: Luhn for card numbers, libphonenumber for phone numbers (of
  `PII_PHONE_REGION`, or with a `+`), nine digits under a
  social-security key. Carry an epoch under a time-named key
  (`created_at`, `ts_ns`) or as an ISO string; a phone number as a JSON
  number is refused wherever it sits — that is the documented footgun.
- **Attribution is not an argument.** An audit row your agent emits
  carries the run owner's `user_id` and email, stamped by the chassis;
  `caps.audit.log(action_type, detail)` takes nothing else.
- **Tool secrets are scrubbed first** (K8a, D20). Every value your run
  was delivered — by `caps.secrets.get`, or `secret_get` over MCP — is
  replaced with `[REDACTED_SECRET]` in content before any PII rule runs,
  and an identifier or key holding one refuses the write with
  `secret_in_output`. It is a backstop, not a licence: what never crosses
  the boundary — your own log lines and OTLP export, a model prompt, a
  value you transformed — is not covered, so hand the value to the tool
  that needs it and put it nowhere else.

## Logging & tracing

- Wrap each phase in `with log_context(run_id=..., agent=...,
  phase=...):` so every structured log line carries the context.
  `log_context` lives at
  [`backend/app/logging_context.py`](../../backend/app/logging_context.py).
- The runner has already opened the phase span (named after the
  manifest phase — e.g. `analyze`, `investigate`, or `analyze_edit`
  on a rerun) by the time your method runs, so any
  `_tracer.start_as_current_span(...)` or
  `PipelineOrchestrator.run_step` calls you make nest under it in
  the trace viewer automatically. The phase span is itself a child of
  the run's root span `run`, which the submission request opened and
  whose W3C context the run row carries (`root_traceparent` /
  `root_tracestate`): every phase restores it as its remote parent, so
  a gated run is **one trace** before and after the approval and
  `trace_id` never changes for the run's life (blueprint S4). Nothing
  in your agent has to do anything for this; a container agent gets
  the same context as `traceparent` / `tracestate` headers on every
  Run Contract request.
- **The model call's span is the gateway's, not yours and not the
  backend's.** Since S4a the backend installs no LLM instrumentor: it
  makes no model call, and an instrumentor here would record a provider
  response verbatim the moment some agent's own dependency happened to
  pull `openai` in. The gateway writes exactly one span per call —
  `gen_ai.request.model`, the token counts, `librerun.cost_usd`,
  `librerun.step_id` — inside this run's tree, under
  `service.name=librerun-gateway`, stamped `librerun.scope="run"`, with
  both content sides walked before they are recorded. You write nothing
  for it (`docs/authoring/LLM_Gateway.md`, "What the trace shows").

---

## Observability contract

The chassis emits telemetry on exactly two interfaces, both
vendor-neutral — this is a promise, not an accident of the current
stack:

- **Traces:** standard OTLP to `OTEL_EXPORTER_OTLP_ENDPOINT` (gRPC
  `:4317` or `http/protobuf` `:4318`, per
  `OTEL_EXPORTER_OTLP_PROTOCOL`; blank = tracing off, spans become
  no-ops). One trace per run: the root span `run` at submission, every
  phase span under it across the approval gate, the run plane sampling
  itself — the root is recorded whatever an upstream `traceparent`'s
  flags say, so a caller's `00` never silences a run; a caller's valid
  `traceparent` becomes the root's parent and its `tracestate` (within
  the W3C limits, and only when the identifier check does not flag it)
  survives onto every phase and every container request.
- **Logs:** NDJSON — one self-describing JSON object per line — at
  `LOG_FILE_PATH` (compose mounts `./data/logs/`). The backend's own
  logs do **not** ship over OTLP; the file *is* their contract.

One deliberate exception rides the OTLP interface: **browser (UX-plane)
events** — web vitals, JS errors, page/route views — are translated
server-side from the authenticated relay
(`docs/platform/Browser_Observability.md`) and exported as OTLP **log records
and spans under their own resource**, `service.name=librerun-web`.
The browser itself never speaks OTLP and holds no telemetry
credential; the relay is the trust boundary.

Any collector that speaks OTLP or can tail a file — the bundled
Vector, an OpenTelemetry Collector, Cribl, Elastic — consumes all of
it without the chassis knowing or caring which.

### The three planes

Every span and every log line declares which telemetry plane it
belongs to:

| Plane | Meaning | Span attribute | Log field |
|---|---|---|---|
| `run` | a task **executing** an agent run — runner, orchestrator steps, agent code, a container's own exported spans, and the gateway's one span per model call. **May carry LLM prompt/completion content.** | `librerun.scope="run"` | `librerun_scope: "run"` |
| `platform` | everything else — HTTP serving (including requests *about* runs: progress polls, run reads), auth, startup, health | `librerun.scope="platform"` | `librerun_scope: "platform"` |
| `ux` | what the **browser** experienced — web vitals, JS errors, page/route views. Server-translated from the relay; carries **no free text by construction** (closed schema) and is always additionally marked `librerun.telemetry.source="browser_untrusted"`. Never claims `run` or `platform`. | `librerun.scope="ux"` | *(n/a — UX events are OTLP log records, resource `service.name=librerun-web`)* |

The backend stamps derive from one predicate —
`app/logging_context.current_scope()`, "is `agent_id` bound?" — so
the two backend signals cannot disagree; the `ux` stamp is applied by
the relay's translator, never by the browser. Finer cuts layer on top:

- **Authorship** (who wrote the code): the log `logger` field —
  `agents.<id>.*` is agent-package code; `app.*` is chassis
  machinery. Note the runner and orchestrator log as `app.*` while
  *inside* the run plane: plane and authorship are different axes.
- **Model calls:** the gateway's span, carrying `gen_ai.*`,
  `librerun.step_id` and `librerun.cost_usd`; an orchestrator step
  around one additionally carries `openinference.span.kind`.
- **Identity:** run-plane spans carry `agent.id`, `agent.name`,
  `tenant.id`, `run.id`, `run.number`, `phase`, `session.id`,
  `user.id` (see "Logging & tracing" above); log lines carry the
  matching snake_case fields. `user_email` appears **never** — not on
  spans and, since S4, not on platform log lines either: the request
  context binds opaque ids only (`tenant_id`, `user_id`, `session_id`,
  `request_id`), and binding it is refused.
- **Walked before it leaves (S4):** every span, span event, log record
  and resource attribute the box exports — both planes, an agent's own
  instrumentation included — passes the one walker over the OTLP model
  (`app/observability/otlp_walk.py`): string positions redacted with
  the intake placeholders, attribute keys and numeric values checked
  (a flagged span is stripped to its identity — ids, timing, the
  `librerun.*` / `agent.*` stamps, the name `redacted` — so the tree
  keeps its shape; a flagged log record is dropped for one warning
  naming the logger and the count), `bytes` values dropped with a
  `librerun.bytes_dropped` count, and the protocol's own scalars —
  timestamps, ids, kind, flags, status code — never checked. The
  backend's log stream is walked the same way on the queue-only
  pipeline (`LOG_QUEUE_ONLY`), `print()` and raw descriptor writes
  included.
- **Correlation:** log lines emitted inside an active span carry
  `trace_id` / `span_id`, so logs join traces in any backend.

### Routing rules

1. **Route sinks; never split one trace across backends.** A run is
   one trace in which platform spans ("user clicked Approve") parent
   run spans ("step 7 called the model") — the join is the most
   valuable observability artifact this platform produces. Split
   *logs* freely by plane; for traces, filter or duplicate **whole
   traces**, not individual spans.
2. **Treat the run plane as sensitive.** By default it carries LLM
   prompt/completion content. The switch is
   `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`
   (`SPAN_AND_EVENT` default; `NO_CONTENT` opts out; settable in `.env`
   in both run modes). Since S4a it lives on the **gateway**, with the
   provider keys, because that is the process that makes the call and
   writes the one LLM span — the backend installs no LLM instrumentor
   for it to govern. All four values are honoured there, `SPAN_ONLY`
   and `EVENT_ONLY` included: they exist to pick a destination, and a
   resolution that collapsed them to a boolean would write content
   through the one an operator had explicitly turned off. A value
   outside the enum fails closed to `NO_CONTENT`, since the only reason
   to set this variable is to suppress content. What the span keeps is
   walked whatever the manifest's `llm.redact_outbound` says — that
   switch governs what the *model* reads, never what telemetry keeps.
   An agent that installs OpenInference instrumentation of its own is
   the one case where the partial modes are not distinguished (that
   family reads either as capture); those spans are still walked on the
   way out. Give the run plane the shorter retention and the tighter
   ACL.
3. **Vector recipe** — a ready commented `plane_router` block ships
   in `config/vector.yaml`'s sink gallery:

   ```yaml
   plane_router:
     type: route
     inputs: [backend_logs]
     route:
       run: '.librerun_scope == "run"'
       platform: '.librerun_scope != "run"'
   ```

   OpenTelemetry Collector equivalent: route on the log field
   `librerun_scope` / span attribute `librerun.scope` (the `routing`
   connector, or OTTL `where attributes["librerun.scope"] == "run"`).
4. **The UX plane routes by resource.** Browser telemetry arrives on
   the OTLP interface under `service.name=librerun-web` (every record
   also carries `librerun.scope="ux"`), so a resource-level route
   separates it from backend traces without inspecting records. It is
   platform-vs-agent attributable (`librerun.ui.owner`,
   `librerun.agent.id`, `librerun.run.id` — relay-authorized claims)
   and safe to retain longer than the run plane: its closed schema
   cannot carry user content. Details, envelope spec, and privacy
   model: `docs/platform/Browser_Observability.md`.

Two boundaries to keep in view: not everything about a run rides
telemetry — schema-drift reports land in `activity_audit_log`
(`action_type="llm_schema_drift"`) and feedback in its own tables —
and container-hosted agents are a separate process with their own
boundary (`docs/authoring/Run_Contract_v1.md`, "Observability boundary").

---

## Errors & retries

- **Prefer returning `status="error"` / `status="blocked"` with a
  user-facing `display` or `error` string** over raising. The runner
  traps unhandled exceptions and marks the run `error`, but a
  graceful return lets you carry a helpful message to the UI.
- The runner does **not** auto-retry. Per-step retries are an agent
  concern. VITA keeps its retry count in the `max_retries_per_step`
  setting.
- Roll back any DB changes you made inside your own session on
  failure. `vita_v1` does this in the `except Exception` branches of
  its `analyze` / `investigate`.

---

## Re-runs & edits

`rerun_current_phase` replays the phase whose output was under
review (`Run.current_phase`) with:

- `prior_analysis` = the last `RunSnapshot.analysis`.
- `user_edits` = the customer's edited statement.

Treat both fields as advisory input. Later phases may also run twice
if a run is re-opened — every phase method must be safe to
re-execute against the same `RunSnapshot`.

---

## Testing

Follow the `vita_v1` layout:

```
backend/agents/my_agent/tests/
├── __init__.py
├── conftest.py
├── fixtures/
└── test_my_agent.py
```

Useful hooks:

- Add `backend/` to your `PYTHONPATH` (conftest-level) so `import
  agents.my_agent` works the same way discovery does.
- Between tests that register fake agents, call
  `app.agents.registry._clear_registry_for_tests()` — the registry
  is process-global.
- Use the in-memory span exporter from the existing test
  infrastructure if you need to assert on OTEL spans; the shell tests
  in `backend/tests/` are a reference.

Run with:

```bash
cd backend && pytest agents/my_agent/tests
```

### The conformance battery

Your own tests prove your agent does what you meant. The shared battery
in [`backend/adapter_kit/`](../../backend/adapter_kit/) proves it satisfies
the things the *chassis* cannot supply on your behalf, and it is
framework-agnostic — it takes anything implementing `AgentProtocol` plus
a `Scenario`:

```python
from adapter_kit import Scenario, run_battery

result = await run_battery(MyAgent(), Scenario.from_file("scenarios/demo.json"))
assert result.passed, result.failures
```

It checks that the scenario satisfies your agent's **own**
`input_schema()` (so a demo the intake form would reject cannot ship as
passing), that progress is streamed in the chassis' status vocabulary,
that spans are emitted, and that everything you return survives the JSONB
commit the runner performs — including the shapes that fail only at
`COMMIT`, after the work is done.

Adapters for other frameworks **must** pass it; agents written directly
against `AgentProtocol` are strongly encouraged to.

---

## Worked example

The minimum viable shape of a hand-written `AgentProtocol` agent — the
old scaffold's, kept here as the worked example; `librerun init` now
scaffolds an agent from a template instead
([`docs/authoring/Quickstart.md`](./Quickstart.md)):

```python
class MyAgent(AgentProtocol):
    agent_id = "my-agent"
    display_name = "My Agent"
    description = "Describe what it does"

    def input_schema(self) -> dict:
        return {"type": "object", "required": ["description"], "properties": {...}}

    async def analyze(self, inp: AgentInput, on_progress) -> AnalysisResult:
        await on_progress(StepProgress("analyze", "running"))
        # TODO: your Phase 1 logic here
        await on_progress(StepProgress("analyze", "complete"))
        return AnalysisResult(
            display={"summary": "Replace with your analysis"},
            structured={"summary": "Replace with your analysis"},
        )

    async def investigate(self, inp: AgentInput, on_progress) -> InvestigationResult:
        await on_progress(StepProgress("investigate", "running"))
        # TODO: your Phase 2 logic here
        await on_progress(StepProgress("investigate", "complete"))
        return InvestigationResult(
            status="complete",
            report_html="<h2>Replace with your report</h2>",
        )
```

For a non-trivial reference — orchestrated pipeline, drift auditing,
config surface, custom report template — read
[`backend/agents/vita_v1/agent.py`](../../backend/agents/vita_v1/agent.py)
end-to-end.
