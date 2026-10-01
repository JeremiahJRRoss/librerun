/**
 * Batching transport for LibreRun RUM v1.
 *
 * Delivery model (decided from the 2026-08 research round):
 *  - `fetch(..., { keepalive: true })` only. The session JWT travels as an
 *    Authorization bearer header, which `sendBeacon()` cannot set — and
 *    unauthenticated telemetry does not exist, so there is nothing useful
 *    for a beacon to carry.
 *  - The 64 KiB keepalive budget is SHARED across in-flight keepalive
 *    requests, so batches stay ≤ 48 KiB serialized and ≤ 50 records.
 *  - Routine delivery happens while the page is healthy (jittered ~15 s
 *    timer, or size threshold); `visibilitychange → hidden` flushes the
 *    small tail; `pagehide` is a fallback only. Tail loss is accepted —
 *    RUM is best-effort by design.
 *  - Server verdicts are respected: 410 disables telemetry for the rest
 *    of the session (the operator kill switch — no frontend rebuild
 *    needed); 429 backs off per Retry-After; 4xx drops the batch (it will
 *    never succeed); 5xx/network retries once while the page lives.
 *  - The relay path itself must never become telemetry: nothing in this
 *    module records its own requests, and any future fetch
 *    instrumentation must exclude RELAY_PATH.
 */

import { RumRecord, RumEnvelope, SCHEMA_VERSION } from "./schema";
import { API_BASE } from "../api";

export const RELAY_PATH = "/_o/e";

const MAX_QUEUE = 200;
const MAX_BATCH_RECORDS = 50;
const MAX_BATCH_BYTES = 48 * 1024;
const FLUSH_INTERVAL_MS = 15_000;
const FLUSH_JITTER_MS = 5_000;
const DEFAULT_BACKOFF_MS = 60_000;
// A hung relay must not wedge the transport: `inFlight` blocks every
// later flush until the request settles, so each request carries an
// abort timeout (where the platform supports it).
const REQUEST_TIMEOUT_MS = 10_000;

function requestTimeoutSignal(): AbortSignal | undefined {
  const ctor = globalThis.AbortSignal as
    | (typeof AbortSignal & { timeout?: (ms: number) => AbortSignal })
    | undefined;
  if (ctor?.timeout) return ctor.timeout(REQUEST_TIMEOUT_MS);
  // Compatibility fallback: without a timeout the protection silently
  // disappears and one hung request wedges `inFlight` for the rest of
  // the tab. The one-shot timer firing after a settled request is a
  // harmless no-op abort.
  if (typeof AbortController !== "undefined") {
    const controller = new AbortController();
    setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    return controller.signal;
  }
  return undefined;
}

interface TransportState {
  queue: RumRecord[];
  token: string | null;
  sessionId: string;
  appVersion?: string;
  disabled: boolean;
  backoffUntil: number;
  retriedOnce: boolean;
  timer: ReturnType<typeof setTimeout> | null;
  inFlight: boolean;
  /** Bumped at every auth identity boundary — see markIdentityBoundary. */
  generation: number;
}

const state: TransportState = {
  queue: [],
  token: null,
  sessionId: "",
  appVersion: undefined,
  disabled: false,
  backoffUntil: 0,
  retriedOnce: false,
  timer: null,
  inFlight: false,
  generation: 0,
};

/**
 * Declare an auth identity boundary (login/logout). Records must never
 * cross one: a batch queued or in flight under user A must not be
 * delivered — or failure-requeued — under user B's JWT, where the relay
 * would stamp B's tenant and pseudonym onto A's records. Bumping the
 * generation makes every in-flight flush's requeue path a no-op, and
 * logout additionally discards the queue (the logout flush already took
 * its best-effort tail; leftovers are accepted loss, never misattribution).
 */
export function markIdentityBoundary(clearQueue: boolean): void {
  state.generation += 1;
  if (clearQueue) state.queue.length = 0;
  state.backoffUntil = 0;
  state.retriedOnce = false;
}

export function configureTransport(opts: {
  sessionId: string;
  appVersion?: string;
}): void {
  state.sessionId = opts.sessionId;
  state.appVersion = opts.appVersion;
}

export function setTransportToken(token: string | null): void {
  state.token = token;
}

export function setTransportSession(sessionId: string): void {
  state.sessionId = sessionId;
}

export function transportDisabled(): boolean {
  return state.disabled;
}

export function enqueue(record: RumRecord | null): void {
  if (record === null || state.disabled) return;
  state.queue.push(record);
  if (state.queue.length > MAX_QUEUE) state.queue.shift();
  if (state.queue.length >= MAX_BATCH_RECORDS) void flush("size");
}

function takeBatch(): { records: RumRecord[]; body: string } | null {
  if (state.queue.length === 0 || !state.sessionId) return null;
  let count = Math.min(state.queue.length, MAX_BATCH_RECORDS);
  while (count > 0) {
    const records = state.queue.slice(0, count);
    const envelope: RumEnvelope = {
      schema_version: SCHEMA_VERSION,
      session_id: state.sessionId,
      records,
    };
    if (state.appVersion) envelope.app_version = state.appVersion;
    const body = JSON.stringify(envelope);
    if (body.length <= MAX_BATCH_BYTES) {
      state.queue.splice(0, count);
      return { records, body };
    }
    count = Math.floor(count / 2);
  }
  // A single record that cannot fit is malformed by our own bounds — drop it.
  state.queue.shift();
  return null;
}

export async function flush(
  _reason: "timer" | "size" | "hidden" | "pagehide" | "logout"
): Promise<void> {
  if (state.disabled || state.inFlight || !state.token) return;
  // A server-imposed backoff (429 Retry-After, or the post-5xx pause)
  // binds EVERY flush path — size-triggered, visibilitychange, pagehide,
  // logout included. Flushing "just once more" from another trigger is
  // exactly the storm the server asked us to stop; the queue holds
  // (bounded, drop-oldest) until the window passes.
  if (Date.now() < state.backoffUntil) return;
  const batch = takeBatch();
  if (batch === null) return;

  const gen = state.generation;
  state.inFlight = true;
  try {
    const res = await fetch(`${API_BASE}${RELAY_PATH}`, {
      method: "POST",
      keepalive: true,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${state.token}`,
      },
      body: batch.body,
      signal: requestTimeoutSignal(),
    });
    if (res.status === 410) {
      state.disabled = true;
      state.queue.length = 0;
      return;
    }
    if (res.status === 429) {
      if (gen === state.generation) {
        const retryAfter = Number(res.headers.get("Retry-After"));
        state.backoffUntil =
          Date.now() +
          (Number.isFinite(retryAfter) && retryAfter > 0
            ? retryAfter * 1000
            : DEFAULT_BACKOFF_MS);
        requeueFront(batch.records);
      }
      return;
    }
    if (!res.ok && res.status >= 500) {
      retryOrDrop(batch.records, gen);
      return;
    }
    // 2xx accepted; other 4xx (400/401/413) would never succeed — drop.
    state.retriedOnce = false;
  } catch {
    // Network failure — retry once while the page is still alive.
    retryOrDrop(batch.records, gen);
  } finally {
    state.inFlight = false;
  }
}

function requeueFront(records: RumRecord[]): void {
  state.queue.unshift(...records);
  if (state.queue.length > MAX_QUEUE) state.queue.length = MAX_QUEUE;
}

function retryOrDrop(records: RumRecord[], gen: number): void {
  if (gen !== state.generation) return; // identity boundary crossed: drop
  if (state.retriedOnce) {
    state.retriedOnce = false;
    return; // second failure: accept the loss
  }
  state.retriedOnce = true;
  requeueFront(records);
  state.backoffUntil = Date.now() + 5_000 + Math.random() * 5_000;
}

function scheduleNext(): void {
  if (state.timer !== null) return;
  const delay = FLUSH_INTERVAL_MS + (Math.random() * 2 - 1) * FLUSH_JITTER_MS;
  state.timer = setTimeout(() => {
    state.timer = null;
    void flush("timer");
    if (!state.disabled) scheduleNext();
  }, delay);
}

export function startTransport(): void {
  if (typeof window === "undefined") return;
  scheduleNext();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") void flush("hidden");
  });
  window.addEventListener("pagehide", () => {
    void flush("pagehide");
  });
}

/** Test hook: reset all transport state. */
export function _resetTransportForTests(): void {
  if (state.timer !== null) clearTimeout(state.timer);
  state.queue = [];
  state.token = null;
  state.sessionId = "";
  state.appVersion = undefined;
  state.disabled = false;
  state.backoffUntil = 0;
  state.retriedOnce = false;
  state.timer = null;
  state.inFlight = false;
  state.generation = 0;
}

/** Test hook: inspect the queue without draining it. */
export function _queueForTests(): readonly RumRecord[] {
  return state.queue;
}
