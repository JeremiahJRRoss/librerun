/**
 * The LibreRun browser telemetry facade — the only API application code
 * touches. Domain-stable on purpose: OpenTelemetry's browser vocabulary is
 * still Development-status and churning, so nothing OTel-shaped appears
 * here. The server-side translator owns that mapping
 * (`backend/app/observability/web_telemetry.py`).
 *
 * Identity model (three separate concepts, never conflated):
 *  - the AUTH session lives in the JWT and never appears in telemetry;
 *  - the RUM session id is a client-random correlation id, rotated at
 *    login/logout boundaries via `rotateSession()`;
 *  - page ids identify one hard/soft view instance and stamp every record.
 */

import {
  buildJsException,
  buildPageView,
  buildRouteChange,
  buildWebVital,
  resolveRoute,
  Surface,
  Trigger,
  uuid,
} from "./schema";
import {
  configureTransport,
  enqueue,
  flush,
  markIdentityBoundary,
  setTransportSession,
  setTransportToken,
  startTransport,
} from "./transport";
import { registerErrorHandlers, resetErrorBudgets } from "./errors";
import { registerVitals } from "./vitals";

interface FacadeState {
  initialized: boolean;
  sessionId: string;
  pageId: string;
  route: string;
  surface: Surface;
  /** The hard-load view: web vitals in P1 always describe this page. */
  hardPage: { pageId: string; route: string } | null;
  pendingIntent: { at: number; trigger: Trigger } | null;
  /** Set at logout: the resulting navigation commit belongs to the user
   * who just left and must not be recorded — see deidentify(). */
  suppressNextCommit: boolean;
}

const state: FacadeState = {
  initialized: false,
  sessionId: uuid(),
  pageId: uuid(),
  route: "other",
  surface: { owner: "platform" },
  hardPage: null,
  pendingIntent: null,
  suppressNextCommit: false,
};

function ctx() {
  return { pageId: state.pageId, route: state.route, surface: state.surface };
}

function hardCtx() {
  const page = state.hardPage ?? { pageId: state.pageId, route: state.route };
  // Vitals belong to the hard-loaded document; surface at fire time may
  // have moved on, so vitals stay platform-owned in P1.
  return { pageId: page.pageId, route: page.route, surface: { owner: "platform" as const } };
}

export function initTelemetry(): void {
  if (state.initialized || typeof window === "undefined") return;
  state.initialized = true;
  state.route = resolveRoute(window.location.pathname);
  state.hardPage = { pageId: state.pageId, route: state.route };
  configureTransport({
    sessionId: state.sessionId,
    appVersion: process.env.NEXT_PUBLIC_APP_VERSION || undefined,
  });
  startTransport();
  registerVitals((metric) => enqueue(buildWebVital(hardCtx(), metric)));
  registerErrorHandlers((input) => enqueue(buildJsException(ctx(), input)));

  // Navigation intent capture: a same-origin link click or a history
  // traversal marks the start of a route commit. The commit observer
  // (TelemetryProvider) turns a fresh intent into a real duration.
  document.addEventListener(
    "click",
    (e) => {
      if (_isNavigationIntentClick(e as MouseEvent)) {
        state.pendingIntent = { at: Date.now(), trigger: "link" };
      }
    },
    { capture: true, passive: true }
  );
  window.addEventListener("popstate", () => {
    state.pendingIntent = { at: Date.now(), trigger: "traverse" };
  });
  // Back/forward-cache restores are a distinct kind of page view.
  window.addEventListener("pageshow", (e) => {
    if ((e as PageTransitionEvent).persisted) {
      enqueue(buildPageView(ctx(), { navigationKind: "bfcache_restore" }));
    }
  });
}

/**
 * Record navigation intent for a PROGRAMMATIC navigation, called
 * immediately before `router.push`/`router.replace` at user-flow call
 * sites (a dashboard row click, a post-submit redirect). Without this
 * the schema's `push`/`replace` triggers would never occur and those
 * primary navigations would commit as `unknown` with no duration.
 * Auth-guard redirects inside effects deliberately do NOT record intent:
 * their "intent moment" is an effect firing, not a user action, so a
 * duration from it would measure render scheduling rather than
 * navigation UX.
 */
export function recordNavigationIntent(
  trigger: "push" | "replace" = "push"
): void {
  state.pendingIntent = { at: Date.now(), trigger };
}

/**
 * True only for a click that plausibly navigates THIS browsing context:
 * unmodified primary-button activation of a same-origin, non-download,
 * self-target link. Modifier clicks (new tab/window), middle clicks,
 * `target="_blank"`, and `download` links never navigate this tab, so
 * recording intent for them would start a later unrelated navigation's
 * duration at the wrong click. `defaultPrevented` is deliberately NOT
 * filtered: this runs at capture phase (before app handlers), and
 * Next.js `<Link>` prevents default as part of performing the client
 * navigation itself — filtering it would drop intent for every Link
 * click. The residual (a prevented, non-navigating click followed by an
 * unrelated navigation) is bounded by the 30 s freshness window and
 * cleared on the first commit.
 */
export function _isNavigationIntentClick(e: MouseEvent): boolean {
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) {
    return false;
  }
  const target = e.target as Element | null;
  const anchor = target?.closest?.("a[href]");
  if (!anchor) return false;
  if (anchor.hasAttribute("download")) return false;
  const targetAttr = (anchor.getAttribute("target") || "").toLowerCase();
  if (targetAttr && targetAttr !== "_self") return false;
  const href = anchor.getAttribute("href") || "";
  return href.startsWith("/") && !href.startsWith("//");
}

/** Auth pushes the live JWT in; telemetry never reads storage for it. */
export function identify(token: string): void {
  setTransportToken(token);
  // Boundary WITHOUT clearing: pre-login records (the login page's own
  // vitals) belong to the person now identified. The generation bump
  // still fences any straggling in-flight request from a previous
  // identity out of the requeue path.
  markIdentityBoundary(false);
  rotateSession();
}

/**
 * Flush the queued tail while the session JWT is still VALID. The normal
 * logout path revokes the session server-side before `clearAuth()` runs,
 * so a flush from `deidentify()` alone would arrive with a dead token
 * and be 401-dropped. Call (and await) this BEFORE the revocation
 * request; `deidentify()` then only has the identity boundary left to do.
 *
 * The wait is BOUNDED: telemetry is best-effort and must never make
 * logout unavailable, so a stalled relay costs at most `timeoutMs` — the
 * dispatched request itself keeps running in the background (and the
 * transport additionally aborts hung requests on its own timer).
 */
export function flushBeforeLogout(timeoutMs = 1500): Promise<void> {
  return Promise.race([
    flush("logout"),
    new Promise<void>((resolve) => setTimeout(resolve, timeoutMs)),
  ]);
}

export function deidentify(): void {
  // Best-effort tail flush first — it dispatches synchronously with the
  // OLD token — then a hard identity boundary: the queue is discarded
  // and any late failure of that flush cannot requeue records across the
  // boundary. Leftovers are accepted loss; delivering them later under
  // the NEXT user's JWT would be misattribution, which is worse.
  void flush("logout");
  markIdentityBoundary(true);
  setTransportToken(null);
  rotateSession();
  // The navigation this logout triggers (e.g. run.detail → login)
  // commits AFTER this boundary and would sit queued token-less; the
  // next login's identify() deliberately keeps the pre-login queue, so
  // without suppression user A's final transition — including A's
  // from_route — would be delivered stamped with user B's identity.
  // The transition is A's action: drop it, like the rest of A's tail.
  state.suppressNextCommit = true;
}

export function rotateSession(): void {
  state.sessionId = uuid();
  setTransportSession(state.sessionId);
  // Error-storm budgets are per RUM session by contract: a previous
  // session exhausting its caps must not blind this one's errors.
  resetErrorBudgets();
}

/**
 * Called by views that render agent-owned content (the run detail page).
 * These are CLAIMS — the relay authorizes them against the registry and
 * the tenant's own runs before anything is emitted.
 */
export function tagSurface(surface: Surface): void {
  state.surface = surface;
}

export function clearSurface(): void {
  state.surface = { owner: "platform" };
}

/**
 * Route-commit notification from the navigation observer. Returns records
 * to the queue; computes a duration only when a navigation intent was
 * actually observed (link click / traversal) and is fresh.
 */
export function routeCommitted(pathname: string, isInitial: boolean): void {
  const toRoute = resolveRoute(pathname);
  if (isInitial) {
    state.route = toRoute;
    state.hardPage = { pageId: state.pageId, route: toRoute };
    enqueue(buildPageView(ctx(), { navigationKind: "hard" }));
    return;
  }
  const fromRoute = state.route;
  state.pageId = uuid();
  state.route = toRoute;
  // A soft navigation leaves the previous view's agent surface behind.
  clearSurface();
  const intent = state.pendingIntent;
  state.pendingIntent = null;
  if (state.suppressNextCommit) {
    // Logout transition: state is advanced (so the NEXT navigation's
    // from_route is this destination, not the previous user's page)
    // but nothing is recorded.
    state.suppressNextCommit = false;
    return;
  }
  const fresh = intent !== null && Date.now() - intent.at <= 30_000;
  enqueue(
    buildRouteChange(ctx(), {
      fromRoute,
      trigger: fresh ? intent!.trigger : "unknown",
      durationMs: fresh ? Date.now() - intent!.at : undefined,
      navigationId: uuid(),
    })
  );
}

/** Test hook: reset facade state. */
export function _resetFacadeForTests(): void {
  state.initialized = false;
  state.sessionId = uuid();
  state.pageId = uuid();
  state.route = "other";
  state.surface = { owner: "platform" };
  state.hardPage = null;
  state.pendingIntent = null;
  state.suppressNextCommit = false;
}

export function _sessionIdForTests(): string {
  return state.sessionId;
}
