/**
 * LibreRun RUM v1 — the browser side of the closed telemetry schema.
 *
 * This module is the POSITIVE SCHEMA boundary: every record is built by a
 * typed builder that accepts only enumerated categories, bounded numbers,
 * and pattern-checked identifiers. Anything out of domain returns null and
 * is never enqueued — arbitrary user text (pasted logs, error messages,
 * URLs, query strings) is unrepresentable here by construction, not
 * scrubbed after the fact. The backend relay re-validates everything with
 * the same rules (`backend/app/observability/rum_envelope.py`); keep the
 * two in lockstep and bump SCHEMA_VERSION together on any wire change.
 */

export const SCHEMA_VERSION = 1;

export type UiOwner = "platform" | "agent";
export interface Surface {
  owner: UiOwner;
  agentId?: string;
  runId?: string;
}

export const VITAL_NAMES = ["lcp", "cls", "inp", "ttfb", "fcp"] as const;
export type VitalName = (typeof VITAL_NAMES)[number];
export const RATINGS = ["good", "needs-improvement", "poor"] as const;
export type Rating = (typeof RATINGS)[number];
export const NAVIGATION_TYPES = [
  "navigate",
  "reload",
  "back-forward",
  "back-forward-cache",
  "prerender",
  "restore",
  "soft-navigation",
] as const;
export type NavigationType = (typeof NAVIGATION_TYPES)[number];
export const TRIGGERS = [
  "link",
  "push",
  "replace",
  "traverse",
  "initial",
  "unknown",
] as const;
export type Trigger = (typeof TRIGGERS)[number];
export const MECHANISMS = ["window.error", "unhandledrejection"] as const;
export type Mechanism = (typeof MECHANISMS)[number];

const ROUTE_RE = /^[a-z0-9_.-]{1,64}$/;
const AGENT_ID_RE = /^[a-z0-9][a-z0-9_-]{0,49}$/;
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// EXACT mirror of the relay's `_METRIC_ID`: web-vitals generateUniqueID()
// emits `v{major}-{Date.now()}-{13-digit random}` and nothing else, and
// the relay drops the whole record on any other shape. Digits-only on
// purpose — the id is exported verbatim server-side, so a bare identifier
// charset would be a free-text channel for callers that skip this
// frontend. A web-vitals major bump that changes the shape fails the
// generated-id test in __tests__/schema.test.ts at upgrade time.
const METRIC_ID_RE = /^v[0-9]{1,2}-[0-9]{13}-[0-9]{13}$/;
const ERROR_TYPE_RE = /^[A-Za-z0-9_.$]{1,64}$/;
const FINGERPRINT_RE = /^[a-f0-9]{8,64}$/;
// EXACT mirror of the relay's bound (rum_envelope._BUNDLE_MODULE_PATTERN:
// prefix + ≤110 chars = 124 total). A looser client bound would not be
// harmless: a path passing here but failing there makes the relay drop
// the ENTIRE exception record as a schema failure, not just this field —
// so overlong paths must be omitted client-side instead.
const BUNDLE_MODULE_RE = /^\/_next\/static\/[A-Za-z0-9_@\[\]./-]{1,110}$/;

interface RecordBase {
  occurred_at_ms: number;
  page_id: string;
  route: string;
  ui_owner: UiOwner;
  agent_id?: string;
  run_id?: string;
}

export interface WebVitalRecord extends RecordBase {
  type: "web_vital";
  name: VitalName;
  value: number;
  rating: Rating;
  metric_id: string;
  navigation_type?: NavigationType;
}

export interface JsExceptionRecord extends RecordBase {
  type: "js_exception";
  error_type: string;
  mechanism: Mechanism;
  fingerprint: string;
  bundle_module?: string;
}

export interface RouteChangeRecord extends RecordBase {
  type: "route_change";
  from_route: string;
  trigger: Trigger;
  duration_ms?: number;
  navigation_id?: string;
}

export interface PageViewRecord extends RecordBase {
  type: "page_view";
  navigation_kind: "hard" | "bfcache_restore";
  referrer_route?: string;
}

export type RumRecord =
  | WebVitalRecord
  | JsExceptionRecord
  | RouteChangeRecord
  | PageViewRecord;

export interface RumEnvelope {
  schema_version: typeof SCHEMA_VERSION;
  session_id: string;
  app_version?: string;
  records: RumRecord[];
}

/**
 * The closed route map. Route identity comes from matching the App Router
 * pathname against these templates — never from exporting the pathname
 * itself — so concrete ids, tenant slugs, query strings, and fragments
 * cannot appear in telemetry. Unknown paths collapse to "other".
 *
 * LOCKSTEP: the relay enforces the same closed set server-side
 * (`backend/app/observability/rum_envelope.py` ROUTE_TEMPLATES) — a new
 * page route added here without its template there is dropped with the
 * `schema` reason in the relay response. Update both together.
 */
const ROUTE_PATTERNS: Array<[RegExp, string]> = [
  [/^\/$/, "root"],
  [/^\/login$/, "login"],
  [/^\/dashboard$/, "dashboard"],
  [/^\/runs\/new$/, "run.new"],
  [/^\/runs\/[^/]+$/, "run.detail"],
  [/^\/admin$/, "admin.home"],
  [/^\/admin\/agents\/[^/]+\/config$/, "admin.agent_config"],
  [/^\/admin\/audit-log$/, "admin.audit_log"],
  [/^\/admin\/auth-config$/, "admin.auth_config"],
  [/^\/admin\/runs\/[^/]+$/, "admin.run_detail"],
  [/^\/admin\/feedback$/, "admin.feedback"],
  [/^\/admin\/settings$/, "admin.settings"],
  [/^\/admin\/users$/, "admin.users"],
];

export function resolveRoute(pathname: string): string {
  for (const [re, template] of ROUTE_PATTERNS) {
    if (re.test(pathname)) return template;
  }
  return "other";
}

interface BuildContext {
  pageId: string;
  route: string;
  surface: Surface;
}

function base(ctx: BuildContext): RecordBase | null {
  if (!UUID_RE.test(ctx.pageId) || !ROUTE_RE.test(ctx.route)) return null;
  const rec: RecordBase = {
    occurred_at_ms: Date.now(),
    page_id: ctx.pageId,
    route: ctx.route,
    ui_owner: ctx.surface.owner,
  };
  if (ctx.surface.owner === "agent") {
    if (!ctx.surface.agentId || !AGENT_ID_RE.test(ctx.surface.agentId)) return null;
    rec.agent_id = ctx.surface.agentId;
    if (ctx.surface.runId) {
      if (!UUID_RE.test(ctx.surface.runId)) return null;
      rec.run_id = ctx.surface.runId;
    }
  }
  return rec;
}

export function buildWebVital(
  ctx: BuildContext,
  input: {
    name: string;
    value: number;
    rating: string;
    id: string;
    navigationType?: string;
  }
): WebVitalRecord | null {
  const b = base(ctx);
  const name = input.name.toLowerCase();
  if (
    b === null ||
    !(VITAL_NAMES as readonly string[]).includes(name) ||
    !Number.isFinite(input.value) ||
    input.value < 0 ||
    input.value > 1e7 ||
    !(RATINGS as readonly string[]).includes(input.rating) ||
    !METRIC_ID_RE.test(input.id)
  ) {
    return null;
  }
  const rec: WebVitalRecord = {
    ...b,
    type: "web_vital",
    name: name as VitalName,
    value: input.value,
    rating: input.rating as Rating,
    metric_id: input.id,
  };
  if (
    input.navigationType &&
    (NAVIGATION_TYPES as readonly string[]).includes(input.navigationType)
  ) {
    rec.navigation_type = input.navigationType as NavigationType;
  }
  return rec;
}

export function buildJsException(
  ctx: BuildContext,
  input: {
    errorType: string;
    mechanism: Mechanism;
    fingerprint: string;
    bundleModule?: string;
  }
): JsExceptionRecord | null {
  const b = base(ctx);
  if (
    b === null ||
    !ERROR_TYPE_RE.test(input.errorType) ||
    !(MECHANISMS as readonly string[]).includes(input.mechanism) ||
    !FINGERPRINT_RE.test(input.fingerprint)
  ) {
    return null;
  }
  const rec: JsExceptionRecord = {
    ...b,
    type: "js_exception",
    error_type: input.errorType,
    mechanism: input.mechanism,
    fingerprint: input.fingerprint,
  };
  if (input.bundleModule && BUNDLE_MODULE_RE.test(input.bundleModule)) {
    rec.bundle_module = input.bundleModule;
  }
  return rec;
}

export function buildRouteChange(
  ctx: BuildContext,
  input: {
    fromRoute: string;
    trigger: Trigger;
    durationMs?: number;
    navigationId?: string;
  }
): RouteChangeRecord | null {
  const b = base(ctx);
  if (
    b === null ||
    !ROUTE_RE.test(input.fromRoute) ||
    !(TRIGGERS as readonly string[]).includes(input.trigger)
  ) {
    return null;
  }
  const rec: RouteChangeRecord = {
    ...b,
    type: "route_change",
    from_route: input.fromRoute,
    trigger: input.trigger,
  };
  if (input.durationMs !== undefined) {
    if (
      !Number.isFinite(input.durationMs) ||
      input.durationMs < 0 ||
      input.durationMs > 120_000
    ) {
      return null;
    }
    rec.duration_ms = Math.round(input.durationMs);
  }
  if (input.navigationId && UUID_RE.test(input.navigationId)) {
    rec.navigation_id = input.navigationId;
  }
  return rec;
}

export function buildPageView(
  ctx: BuildContext,
  input: { navigationKind: "hard" | "bfcache_restore"; referrerRoute?: string }
): PageViewRecord | null {
  const b = base(ctx);
  if (b === null) return null;
  const rec: PageViewRecord = {
    ...b,
    type: "page_view",
    navigation_kind: input.navigationKind,
  };
  if (input.referrerRoute && ROUTE_RE.test(input.referrerRoute)) {
    rec.referrer_route = input.referrerRoute;
  }
  return rec;
}

export function uuid(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c?.randomUUID) return c.randomUUID();
  // Non-crypto fallback for ancient environments — correlation ids only.
  let out = "";
  for (const ch of "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx") {
    if (ch === "x") out += Math.floor(Math.random() * 16).toString(16);
    else if (ch === "y") out += (8 + Math.floor(Math.random() * 4)).toString(16);
    else out += ch;
  }
  return out;
}
