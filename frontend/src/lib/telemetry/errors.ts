/**
 * Global JS error capture — the PII-hardest channel, handled by NOT
 * exporting free text at all. Users paste raw vendor logs into this app,
 * and application code can interpolate any of it into an Error message
 * (`throw new Error("failed parsing " + line)`), so:
 *
 *  - `error.message` NEVER leaves the browser (in any encoding);
 *  - the raw stack string NEVER leaves the browser;
 *  - what does leave: the error class name (charset-bounded), the
 *    mechanism, an fnv1a grouping fingerprint computed locally from the
 *    class + normalized own-bundle frame paths, and optionally the first
 *    own-bundle static asset path with query/hash stripped.
 *
 * Budgets keep an exception storm from spending the telemetry quota: at
 * most a handful of records per fingerprint, and a session-wide cap.
 */

import type { Mechanism } from "./schema";

export interface ErrorInput {
  errorType: string;
  mechanism: Mechanism;
  fingerprint: string;
  bundleModule?: string;
}

const ERROR_TYPE_RE = /^[A-Za-z0-9_.$]{1,64}$/;
const MAX_PER_FINGERPRINT = 5;
const MAX_PER_SESSION = 30;

const fingerprintCounts = new Map<string, number>();
let sessionCount = 0;

export function fnv1a(input: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < input.length; i++) {
    hash ^= input.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

// A line only counts as a stack FRAME when it is shaped like one: V8's
// "    at fn (url:1:2)" or Gecko/WebKit's "fn@url:1:2". The V8 message
// line ("Error: <text>") — which can interpolate user content, including
// path-like tokens — matches neither and is skipped entirely, so message
// text can never be mined for "bundle paths". Residual risk accepted: a
// message deliberately forged to look like a frame can smuggle at most a
// 128-char token from the bundle-module charset under /_next/static/ ON
// OUR OWN ORIGIN — stack strings are implementation-defined (no perfect
// parse exists), so the containment is shape + origin + prefix + charset
// + length, and under-attribution is always preferred over free text.
const FRAME_SHAPE_RE = /^\s*at\s|^[A-Za-z0-9_$.<>\[\] ]*@\S/;
// ANY URL-shaped token: a scheme ("https://", "chrome-extension://",
// "blob:", …) or a protocol-relative "//host/…". Matching only http(s)
// here would let extension/other-scheme frames fall through to the
// relative branch, whose substring match would then export the foreign
// URL's imitation "/_next/static/…" path.
const URL_TOKEN_RE = /[A-Za-z][A-Za-z0-9+.\-]*:\/\/[^\s)]+|(?:^|[\s(@])(\/\/[^\s)]+)/;
// A scheme WITHOUT "//" ("data:text/javascript,…", "javascript:…"): the
// colon must be followed by a non-digit so frame line/col suffixes
// (":1:1") and host ports stay recognizable as such. Any line carrying
// one is fail-closed — a data:-style frame's "path" is entirely
// foreign/attacker-shaped content and must never feed the relative
// fallback.
const BARE_SCHEME_RE = /[A-Za-z][A-Za-z0-9+.\-]*:(?![0-9])/;
const RELATIVE_BUNDLE_RE = /\/_next\/static\/[^\s):?#]+/;
const BUNDLE_PREFIX = "/_next/static/";

/**
 * Extract the own-bundle path from one frame-shaped line, or null.
 *
 * Any URL-shaped frame — http(s), extension schemes, protocol-relative,
 * anything with a scheme — must PARSE (resolved against the page's own
 * origin, so "//evil/…" becomes concrete) and match
 * `window.location.origin` exactly; foreign schemes can never match an
 * http(s) page origin and fail closed, as do unparseable URLs. A
 * third-party script's path is foreign data even when it imitates
 * `/_next/static/…`. Only a line carrying no URL-shaped token at all may
 * contribute a bare relative `/_next/static/…` token, which is
 * same-origin by construction.
 */
function bundlePathFromFrame(line: string): string | null {
  const urlToken = line.match(URL_TOKEN_RE);
  if (urlToken) {
    const token = urlToken[1] ?? urlToken[0];
    try {
      const ownOrigin =
        typeof window !== "undefined" ? window.location.origin : "";
      if (!ownOrigin) return null;
      // Strip the trailing :line:col before parsing — ":" is legal in
      // URL paths, so it would otherwise survive into pathname.
      const url = new URL(token.replace(/(?::\d+){1,2}$/, ""), ownOrigin);
      if (url.origin !== ownOrigin) return null;
      return url.pathname.startsWith(BUNDLE_PREFIX) ? url.pathname : null;
    } catch {
      return null;
    }
  }
  // No "//"-style URL on the line — but a bare scheme ("data:…") means
  // the line still carries a URL whose content is foreign; reject rather
  // than substring-match an imitation path out of it.
  if (BARE_SCHEME_RE.test(line)) return null;
  const relative = line.match(RELATIVE_BUNDLE_RE);
  return relative ? relative[0] : null;
}

/**
 * Reduce a stack to own-bundle frame *paths* only (no message lines, no
 * foreign origins, no query strings). Used solely as local fingerprint
 * input plus the first path as `bundle_module`.
 */
export function normalizeFrames(stack: string | undefined): string[] {
  if (!stack) return [];
  const frames: string[] = [];
  for (const line of stack.split("\n").slice(0, 12)) {
    if (!FRAME_SHAPE_RE.test(line)) continue;
    const path = bundlePathFromFrame(line);
    if (path) frames.push(path);
    if (frames.length >= 4) break;
  }
  return frames;
}

function classify(raw: unknown, mechanism: Mechanism): ErrorInput | null {
  let errorType = "Error";
  let stack: string | undefined;
  if (raw instanceof Error) {
    errorType = raw.name || "Error";
    stack = raw.stack;
  } else if (mechanism === "unhandledrejection") {
    errorType = "UnhandledRejection";
  }
  if (!ERROR_TYPE_RE.test(errorType)) errorType = "Error";

  const frames = normalizeFrames(stack);
  const fingerprint = fnv1a(`${errorType}|${mechanism}|${frames.join("|")}`);

  if (sessionCount >= MAX_PER_SESSION) return null;
  const seen = fingerprintCounts.get(fingerprint) ?? 0;
  if (seen >= MAX_PER_FINGERPRINT) return null;
  fingerprintCounts.set(fingerprint, seen + 1);
  sessionCount += 1;

  const input: ErrorInput = { errorType, mechanism, fingerprint };
  // Passed intact: the schema builder enforces the relay's exact
  // prefix+length bound and OMITS an overlong path (a truncated path
  // would be a wrong identifier, and a client-side-only looser bound
  // would make the relay drop the whole record).
  if (frames.length > 0) input.bundleModule = frames[0];
  return input;
}

export function registerErrorHandlers(
  report: (input: ErrorInput) => void
): void {
  window.addEventListener("error", (event) => {
    const classified = classify(event.error, "window.error");
    if (classified) report(classified);
  });
  window.addEventListener("unhandledrejection", (event) => {
    const classified = classify(
      (event as PromiseRejectionEvent).reason,
      "unhandledrejection"
    );
    if (classified) report(classified);
  });
}

/**
 * Reset the storm budgets. Called by the facade whenever the RUM session
 * rotates (login/logout): budgets are per RUM session by contract, and a
 * previous session exhausting its caps must not blind the next one's
 * error telemetry in the same tab.
 */
export function resetErrorBudgets(): void {
  fingerprintCounts.clear();
  sessionCount = 0;
}

/** Test hook: same operation, kept under the test-hook naming convention. */
export const _resetErrorBudgetsForTests = resetErrorBudgets;

export function _classifyForTests(
  raw: unknown,
  mechanism: Mechanism
): ErrorInput | null {
  return classify(raw, mechanism);
}
