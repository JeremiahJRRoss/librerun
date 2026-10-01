/**
 * Transport tests: batching bounds, server-verdict handling (410 kill
 * switch, 429 backoff, 5xx retry-once), and the flush lifecycle.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  _queueForTests,
  _resetTransportForTests,
  configureTransport,
  enqueue,
  flush,
  markIdentityBoundary,
  setTransportToken,
  startTransport,
  transportDisabled,
  RELAY_PATH,
} from "../transport";
import { buildPageView } from "../schema";

const SESSION_ID = "5f3a1c2e-1111-4222-8333-444455556666";
const CTX = {
  pageId: "5f3a1c2e-1111-4222-8333-444455559999",
  route: "dashboard",
  surface: { owner: "platform" as const },
};

function record() {
  return buildPageView(CTX, { navigationKind: "hard" });
}

function mockFetch(responses: Array<Partial<Response> & { status: number }>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  let i = 0;
  const impl = vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    const spec = responses[Math.min(i++, responses.length - 1)];
    return {
      ok: spec.status >= 200 && spec.status < 300,
      status: spec.status,
      headers: {
        get: (name: string) =>
          name === "Retry-After" ? ((spec as any).retryAfter ?? null) : null,
      },
    } as unknown as Response;
  });
  vi.stubGlobal("fetch", impl);
  return { calls, impl };
}

beforeEach(() => {
  _resetTransportForTests();
  configureTransport({ sessionId: SESSION_ID });
  setTransportToken("test-jwt");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("flush", () => {
  it("posts a versioned envelope with the bearer token to the neutral path", async () => {
    const { calls } = mockFetch([{ status: 202 }]);
    enqueue(record());
    enqueue(record());
    await flush("timer");
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain(RELAY_PATH);
    const headers = calls[0].init.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer test-jwt");
    const body = JSON.parse(calls[0].init.body as string);
    expect(body.schema_version).toBe(1);
    expect(body.session_id).toBe(SESSION_ID);
    expect(body.records).toHaveLength(2);
    expect(calls[0].init.keepalive).toBe(true);
    // Requests carry an abort timeout so a hung relay cannot wedge the
    // transport behind a permanently-true inFlight flag.
    expect(calls[0].init.signal).toBeDefined();
    expect(_queueForTests()).toHaveLength(0);
  });

  it("sends nothing without a token — unauthenticated telemetry does not exist", async () => {
    const { calls } = mockFetch([{ status: 202 }]);
    setTransportToken(null);
    enqueue(record());
    await flush("timer");
    expect(calls).toHaveLength(0);
  });

  it("410 disables the transport for the rest of the session", async () => {
    const { calls } = mockFetch([{ status: 410 }]);
    enqueue(record());
    await flush("timer");
    expect(transportDisabled()).toBe(true);
    enqueue(record());
    await flush("hidden");
    expect(calls).toHaveLength(1); // no second attempt, queue stays empty
    expect(_queueForTests()).toHaveLength(0);
  });

  it("a 429 backoff suppresses EVERY flush path until Retry-After passes", async () => {
    vi.useFakeTimers();
    const { calls } = mockFetch([
      { status: 429, retryAfter: "30" } as any,
      { status: 202 },
    ]);
    enqueue(record());
    await flush("timer");
    expect(_queueForTests()).toHaveLength(1); // requeued
    // The server said stop: size, visibility, and pagehide triggers must
    // all hold too — flushing from another path is the storm the 429
    // exists to prevent.
    await flush("size");
    await flush("hidden");
    await flush("pagehide");
    expect(calls).toHaveLength(1);
    vi.setSystemTime(Date.now() + 31_000);
    await flush("hidden"); // window passed — delivery resumes
    expect(calls).toHaveLength(2);
    expect(_queueForTests()).toHaveLength(0);
  });

  it("retries a 5xx once after the pause, then accepts the loss", async () => {
    vi.useFakeTimers();
    const { calls } = mockFetch([{ status: 503 }, { status: 503 }]);
    enqueue(record());
    await flush("timer");
    expect(_queueForTests()).toHaveLength(1); // first failure requeues
    vi.setSystemTime(Date.now() + 15_000); // past the post-5xx pause
    await flush("hidden");
    expect(_queueForTests()).toHaveLength(0); // second failure drops
    expect(calls).toHaveLength(2);
  });

  it("drops the batch on a 4xx that can never succeed", async () => {
    mockFetch([{ status: 400 }]);
    enqueue(record());
    await flush("timer");
    expect(_queueForTests()).toHaveLength(0);
  });
});

describe("batch bounds", () => {
  it("auto-flushes when the record cap is reached", async () => {
    const { calls } = mockFetch([{ status: 202 }]);
    for (let i = 0; i < 50; i++) enqueue(record());
    await Promise.resolve(); // let the size-triggered flush settle
    await vi.waitFor(() => expect(calls.length).toBe(1));
    const body = JSON.parse(calls[0].init.body as string);
    expect(body.records.length).toBeLessThanOrEqual(50);
  });

  it("bounds the in-memory queue by dropping the oldest", () => {
    mockFetch([{ status: 202 }]);
    setTransportToken(null); // prevent auto-flush so the queue actually fills
    for (let i = 0; i < 260; i++) enqueue(record());
    expect(_queueForTests().length).toBeLessThanOrEqual(200);
  });
});

describe("request timeout compatibility", () => {
  it("falls back to AbortController when AbortSignal.timeout is missing", async () => {
    // Round-5 Codex case: in browsers without the static, the timeout
    // must not silently disappear — a hung request would wedge inFlight
    // for the rest of the tab.
    const RealAbortSignal = globalThis.AbortSignal;
    vi.stubGlobal(
      "AbortSignal",
      Object.assign(Object.create(RealAbortSignal), { timeout: undefined })
    );
    const { calls } = mockFetch([{ status: 202 }]);
    enqueue(record());
    await flush("timer");
    expect(calls[0].init.signal).toBeDefined();
  });
});

describe("identity boundaries", () => {
  it("a clearing boundary discards every queued record", () => {
    mockFetch([{ status: 202 }]);
    setTransportToken(null); // keep records queued
    enqueue(record());
    enqueue(record());
    expect(_queueForTests()).toHaveLength(2);
    markIdentityBoundary(true);
    expect(_queueForTests()).toHaveLength(0);
  });

  it("a late failure cannot requeue records across the boundary", async () => {
    // The cross-user attribution bug: user A's batch is in flight when
    // they log out; the request later fails. Without the generation
    // fence it would be requeued and shipped under user B's JWT.
    let resolveFetch!: (r: unknown) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise((res) => (resolveFetch = res)))
    );
    enqueue(record());
    const inflight = flush("timer"); // dispatched with user A's token
    markIdentityBoundary(true); // logout while the request hangs
    resolveFetch({
      ok: false,
      status: 503,
      headers: { get: () => null },
    });
    await inflight;
    expect(_queueForTests()).toHaveLength(0); // dropped, never requeued
  });
});

describe("lifecycle", () => {
  it("flushes when the document becomes hidden", async () => {
    const { calls } = mockFetch([{ status: 202 }]);
    startTransport();
    enqueue(record());
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.waitFor(() => expect(calls.length).toBe(1));
  });
});
