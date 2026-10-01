export type RunStatus =
  | "submitted"
  | "refining"
  | "awaiting_approval"
  | "investigating"
  | "complete"
  | "error";

export type Severity = "critical" | "high" | "medium" | "low";

export interface VendorInput {
  name: string;
  product?: string | null;
  feature?: string | null;
  observation?: string | null;
}

export interface RunSummary {
  id: string;
  run_number: string;
  // The run's title (blueprint S2): the agent manifest's ui.list.title_path
  // resolved at intake — what the dashboard shows and search matches.
  title?: string | null;
  vendor_a_name?: string | null;
  vendor_b_name?: string | null;
  /** @deprecated identical to `title` for one release; removed at v1.1 */
  problem_summary: string;
  severity: Severity | null;
  status: RunStatus;
  created_at: string;
  updated_at: string;
  agent_id?: string | null;
}

export interface RunListResponse {
  runs: RunSummary[];
  total: number;
  page: number;
  per_page: number;
}

export interface RefinedStatement {
  refined_problem_statement: string;
  key_signals: string[];
  suspected_root_causes: string[];
  research_focus_areas: string[];
}

// GET /runs/{id}/approval (blueprint S2): the parked phase, its full
// output, and the summary string the agent manifest names inside it.
export interface ApprovalResponse {
  status: RunStatus;
  phase?: string | null;
  payload?: Record<string, unknown> | null;
  summary?: string | null;
}

export interface RunDetail {
  id: string;
  run_number: string;
  title?: string | null;
  // The string the approval view shows for the parked phase output (the
  // manifest's ui.approval.summary_path, or the output's first string).
  approval_summary?: string | null;
  // Legacy demo-agent-shaped fields — present only when the agent's
  // payload carried the well-known keys (nullable since B8).
  vendor_a?: VendorInput | null;
  vendor_b?: VendorInput | null;
  vendor_a_name?: string | null;
  vendor_b_name?: string | null;
  problem_summary: string;
  severity: Severity | null;
  status: RunStatus;
  created_at: string;
  updated_at: string;
  use_case?: string | null;
  problem_statement?: string | null;
  impact_statement?: string | null;
  refined_problem_statement?: string | null;
  agent_id?: string | null;
  user_inputs?: Record<string, unknown> | null;
  // How the agent's final result renders (from its manifest, blueprint
  // B9): "html_report" ships HTML via the report endpoints; "structured"
  // exposes the payload below for generic rendering.
  output_mode?: "html_report" | "structured";
  structured_output?: Record<string, unknown> | null;
  // Run-scoped feedback vocabulary (from the agent's manifest) — exactly
  // what POST /feedback accepts for this run; empty hides the panel.
  feedback_sections?: Array<{ id: string; label: string }>;
  // Deep link into the configured trace viewer (blueprint B5); null when
  // tracing/viewer is off or no trace exists yet.
  trace_url?: string | null;
  // Why the run ended in error (blueprint S7): a code from the chassis's
  // closed vocabulary and the sentence the chassis wrote for it. Never the
  // agent's own failure text — that is operator-facing by the Run
  // Contract and reaches the admin view only (AdminRunDetail.error_detail).
  error_code?: string | null;
  error_message?: string | null;
}

// Admin-only superset. Mirrors backend AdminRunDetail — the raw
// observability ids are deliberately kept off the customer RunDetail type.
export interface AdminRunDetail extends RunDetail {
  trace_id?: string | null;
  phase2_span_id?: string | null;
  // The operator-facing reason: the agent's failure text, the exception,
  // the phase a restart cut short — redacted before it was stored.
  error_detail?: string | null;
}

export interface UserProfile {
  id: string;
  email: string;
  role: "admin" | "customer";
  tenant_id: string;
  display_name?: string | null;
  /**
   * K4b: an admin of the platform tenant — the deployment operator. A
   * display hint for which pages to offer; every route keeps its own
   * gate, so nothing is authorized on it.
   */
  is_platform_admin: boolean;
}

export interface PiiRedactionEntry {
  original_placeholder: string;
  pii_type: string;
  confidence: number;
}

export interface PiiRedactionPreview {
  redacted_content: string;
  redactions_applied: PiiRedactionEntry[];
  original_size_bytes: number;
}

export interface StepProgress {
  step_id: string;
  status: "pending" | "running" | "complete" | "skipped" | "error";
  duration_ms?: number | null;
  detail?: string | null;
  /**
   * Blueprint S4a (D13): the model that actually answered this step,
   * written by the gateway. Shown so an admin who changes a step's model
   * can see the change take effect on the next run without restarting
   * anything — which is the whole claim of model configuration being
   * data.
   */
  model?: string | null;
}

export interface ProgressResponse {
  phase: number;
  steps: StepProgress[];
  // Manifest phase currently running (blueprint B7); null on legacy runs.
  phase_name?: string | null;
}

export type ActionType =
  | "blocked_request"
  // Blueprint S1: new rows carry the run_* spellings; audit rows are
  // history, so the pre-S1 case_* values stay legal and keep rendering.
  | "run_create"
  | "run_delete"
  | "run_update"
  | "case_create"
  | "case_delete"
  | "case_update"
  | "config_change"
  | "vendor_registry_edit"
  | "role_change"
  | "session_revoke"
  | "auth_config_change"
  | "llm_schema_drift";

export interface DriftSummaryRecent {
  id: string;
  run_id: string | null;
  step_id: string | null;
  drift_type: string | null;
  model: string | null;
  report_count: number;
  created_at: string | null;
}

export interface DriftSummary {
  last_24h: number;
  last_7d: number;
  by_step: Record<string, number>;
  by_model: Record<string, number>;
  recent: DriftSummaryRecent[];
}

// ---- Agent framework (Phase 1 backend) ------------------------------------

// One progress row a phase reports, with the label the run page shows
// for it (blueprint S7, manifest phases[].steps[]). Optional: a row the
// manifest does not declare renders under its raw id.
export interface AgentPhaseStep {
  id: string;
  label: string;
}

// Manifest phase entry (blueprint B7): the chassis runs these in order;
// approval: true means a human approves the previous phase's output first.
export interface AgentPhase {
  name: string;
  approval: boolean;
  steps: AgentPhaseStep[];
}

// One step of the schema-driven intake wizard (blueprint B8). fields
// reference top-level input-schema properties; an empty list is an
// informational step. The chassis appends the final Review step itself.
export interface AgentIntakeStep {
  title: string;
  description?: string;
  fields: string[];
}

export interface AgentInfo {
  agent_id: string;
  display_name: string;
  description: string;
  has_config: boolean;
  phases: AgentPhase[];
  // Blueprint S7: the framework the agent is built on, free text for the
  // badge on its card; empty means the card shows the runtime instead.
  framework?: string;
  // Schema-driven intake layout: steps → stepped wizard, none → single-page.
  ui: { intake: { steps: AgentIntakeStep[] } };
  output: { mode: "html_report" | "structured" };
  capabilities: string[];
  // Blueprint S4: the runtime, and whether a container agent's compose
  // fragment also joins the non-internal ``egress`` network — an opt-out
  // from "no path off-box but through the chassis".
  runtime?: "python-package" | "container";
  network?: { egress: boolean };
  // Blueprint S4a: the LLM steps the manifest declares, and whether the
  // gateway redacts PII from what this agent sends to a model.
  llm?: {
    steps: Array<{ id: string; label: string }>;
    redact_outbound: boolean;
  };
  // Feedback targets the results view offers thumbs on (blueprint B9).
  feedback_sections: Array<{ id: string; label: string }>;
  has_scenarios: boolean;
}

// Demo scenario served by GET /agents/{id}/scenarios. ``user_inputs`` is
// a valid POST /runs body for the agent — it prefills the intake form
// and can be submitted verbatim.
export interface AgentScenario {
  id: string;
  name: string;
  description: string;
  user_inputs: Record<string, unknown>;
}

export type AgentSettingType =
  | "string"
  | "int"
  | "float"
  | "bool"
  | "enum"
  | "string_list";

/**
 * One entry of the manifest's `settings[]` (K5a, L32), as the config GET
 * serves it in `meta.settings`: what the Settings tab renders a field for.
 */
export interface AgentSettingSpec {
  key: string;
  label: string;
  type: AgentSettingType;
  default: unknown;
  /** The choices of an `enum` setting; null for every other type. */
  options: string[] | null;
  description: string;
}

/**
 * A setting's effective value for THIS tenant (K5a). `overridden` is the
 * server's answer to "is this the tenant's choice or the agent's
 * default?" — a value equal to the default is never stored (D17).
 */
export interface AgentSettingValue {
  key: string;
  label: string;
  type: AgentSettingType;
  value: unknown;
  default: unknown;
  overridden: boolean;
}

export interface AgentConfigMeta {
  supported_providers: string[];
  step_editable_fields: string[];
  settings: AgentSettingSpec[];
  /**
   * True when the settings come from the agent's deprecated
   * `config_meta()` surface: one value for every tenant, until the agent
   * declares `settings[]` (removed at v1.2).
   */
  deprecated: boolean;
  /**
   * K8a: the tool secrets the manifest declares in `secrets[]`, by name
   * alone. The Secrets tab reads their state from `GET …/secrets`.
   */
  secrets: string[];
}

export interface AgentStepConfig {
  step_id: string;
  /** The manifest's label for the step; what the admin page shows. */
  label: string;
  description: string;
  provider: string | null;
  model: string | null;
  temperature: number | null;
  max_tokens: number | null;
  timeout_seconds: number | null;
  /**
   * Blueprint S4a: the fields THIS TENANT has overridden. Everything
   * else is the agent's declared default, so the page can say which is
   * which — a value that looks edited but is not would send an admin
   * hunting for a change nobody made.
   */
  overridden: string[];
}

export interface AgentConfigResponse {
  meta: AgentConfigMeta;
  steps: AgentStepConfig[];
  settings: AgentSettingValue[];
}

/**
 * One place a tool secret's value may come from (K8a, L31): set or not,
 * its fingerprint (a keyed 12-character digest; null while unset and for
 * a row no configured key opens), who set it and when, and when a run
 * last read it. Never a value. The default row's `updated_by` is a
 * platform admin's to see; the environment row carries `set` alone.
 */
export interface AgentSecretRow {
  set: boolean;
  fingerprint?: string | null;
  updated_at?: string | null;
  updated_by?: string | null;
  last_used_at?: string | null;
}

/**
 * One declared tool secret in this tenant (K8a, D32): this tenant's row,
 * every tenant's default and, for an in-process agent, the backend's
 * environment (null for a container, whose own environment the platform
 * cannot see). `effective` is where a run of this tenant reads it now.
 */
export interface AgentSecretState {
  name: string;
  effective: "tenant" | "agent" | "environment" | "unset";
  tenant: AgentSecretRow;
  agent: AgentSecretRow;
  environment: AgentSecretRow | null;
}

/** `GET /agents/{id}/secrets`: every declared name, in the manifest's order. */
export interface AgentSecretsList {
  agent_id: string;
  runtime: string;
  secrets: AgentSecretState[];
}

// JSON Schema subset relevant to DynamicForm. Follows the conventions
// the agent input_schema() returns: type, properties, required, plus the
// custom ``x-ui-widget`` hint for textarea selection.
export interface JsonSchemaField {
  type?: "string" | "integer" | "number" | "boolean" | "array" | "object";
  title?: string;
  description?: string;
  enum?: string[];
  items?: JsonSchemaField;
  properties?: Record<string, JsonSchemaField>;
  required?: string[];
  minLength?: number;
  maxLength?: number;
  "x-ui-widget"?: "textarea" | "tag-input";
  // Marks a field whose content may contain PII: the wizard offers a
  // redaction-preview file upload and the backend redacts before persist.
  "x-pii"?: boolean;
  // Upload policy for the redaction preview ("log" | "config"), log default.
  "x-upload-kind"?: string;
}

export interface JsonSchema extends JsonSchemaField {
  type: "object";
  properties: Record<string, JsonSchemaField>;
  required?: string[];
}

// Certificates at the edge (T2; L42, L43): what GET /admin/tls answers.
// Public material alone — a key is never in it (L42).
export type TlsNeed =
  | "edge_off"
  | "edge_restart"
  | "trust_root"
  | "root_changed"
  | "files_ending"
  | "ca_ending"
  | "acme_requirements";

export interface TlsCertificate {
  subject: string | null;
  issuer: string | null;
  names: string[];
  not_before: string | null;
  not_after: string | null;
  sha256: string;
}

export interface TlsRoot extends TlsCertificate {
  // local (the edge's own), env-<12 hex> (LIBRERUN_TLS_CA's) or
  // loaded-<12 hex> (loaded on Application Settings).
  ca: string;
  name: string | null;
}

export interface TlsStatus {
  edge: "on" | "off";
  site: string[];
  issuer: { kind: "internal" | "acme" | "files"; ca: string | null; email: string | null } | null;
  root: TlsRoot | null;
  leaf: TlsCertificate | null;
  source: "environment" | "ui" | null;
  choice: { kind: "ca" | "files" | "acme"; by: string | null; by_email: string | null; at: string | null } | null;
  environment: { variable: "LIBRERUN_TLS" | "LIBRERUN_TLS_CA"; ca: string | null } | null;
  why: string | null;
  needs: TlsNeed[];
  acknowledged_root: string | null;
}

// The deployment as the backend reads it (K9-04; D16, D43): what
// GET /admin/deployment answers a platform admin. An allowlist of names
// and non-secret values: never a secret, the environment, a header's
// value or a URL's userinfo or query.
export interface DeploymentSetting {
  name: string;
  env_class: 1 | 2;
  value: string | number | boolean | null;
  // env: set in the process environment or .env, compose's defaults
  // included; default: the settings model's own.
  source: "env" | "default";
  hint: string;
}

export interface DeploymentView {
  version: string;
  license: string;
  source_url: string | null;
  demo: boolean;
  stub: boolean | null;
  gateway: {
    reachable: boolean;
    reported: boolean;
    version: string | null;
    updated_at: string | null;
    providers: { name: string; source: "runtime" | "env" | "unset" | null; fingerprint: string | null }[];
  };
  settings: DeploymentSetting[];
  otlp_headers: { name: string; set: boolean }[];
  transport: { scheme: string; host: string | null };
}

// An installed agent key (K9-08, D10): never its value.
export interface AgentKeyRow {
  agent_id: string;
  key_prefix: string;
  // env: from the gateway's environment, rotated in .env; admin: issued here.
  source: "env" | "admin";
  role: "current" | "previous";
  issued_at: string;
  issued_by: string | null;
  previous_since: string | null;
  previous_until: string | null;
  last_used_at: string | null;
  rotatable: boolean;
  registered: boolean;
}

// The one response that carries a key's value: shown once, stored nowhere.
export interface IssuedAgentKey {
  agent_id: string;
  key: string;
  key_prefix: string;
}

export interface RotatedAgentKey extends IssuedAgentKey {
  grace_hours: number;
}
