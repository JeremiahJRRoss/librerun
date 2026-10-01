/**
 * Positive-schema tests: the builders are the browser-side PII boundary,
 * so these tests prove out-of-domain input is unrepresentable — including
 * a canary sweep over everything a builder will actually emit.
 */
import { describe, expect, it } from "vitest";
import {
  buildJsException,
  buildPageView,
  buildRouteChange,
  buildWebVital,
  resolveRoute,
  RumRecord,
  uuid,
} from "../schema";

const CANARY = "LIBRERUN_SECRET_CANARY_9f31=hunter2 with spaces";
const CTX = {
  pageId: "5f3a1c2e-1111-4222-8333-444455556666",
  route: "run.detail",
  surface: { owner: "platform" as const },
};
const AGENT_CTX = {
  ...CTX,
  surface: {
    owner: "agent" as const,
    agentId: "demo-agent",
    runId: "5f3a1c2e-1111-4222-8333-444455557777",
  },
};

describe("resolveRoute", () => {
  it("maps every known app route to its template", () => {
    expect(resolveRoute("/")).toBe("root");
    expect(resolveRoute("/login")).toBe("login");
    expect(resolveRoute("/dashboard")).toBe("dashboard");
    expect(resolveRoute("/runs/new")).toBe("run.new");
    expect(resolveRoute("/runs/8f14e45f-ce")).toBe("run.detail");
    expect(resolveRoute("/admin")).toBe("admin.home");
    expect(resolveRoute("/admin/agents/demo-agent/config")).toBe("admin.agent_config");
    expect(resolveRoute("/admin/runs/8f14e45f-ce")).toBe("admin.run_detail");
    expect(resolveRoute("/admin/settings")).toBe("admin.settings");
  });

  it("collapses unknown paths — ids, queries, junk — to 'other'", () => {
    expect(resolveRoute("/tenants/acme/secret-page")).toBe("other");
    expect(resolveRoute("/runs/1/extra")).toBe("other");
    expect(resolveRoute(`/${CANARY}`)).toBe("other");
  });
});

describe("buildWebVital", () => {
  const good = {
    name: "LCP",
    value: 1234.5,
    rating: "good",
    id: "v4-1712345678901-1234567890123",
  };

  it("builds a bounded record", () => {
    const rec = buildWebVital(CTX, { ...good, navigationType: "navigate" });
    expect(rec).toMatchObject({
      type: "web_vital",
      name: "lcp",
      value: 1234.5,
      rating: "good",
      ui_owner: "platform",
      navigation_type: "navigate",
    });
  });

  it("rejects out-of-domain values", () => {
    expect(buildWebVital(CTX, { ...good, name: "FID" })).toBeNull();
    expect(buildWebVital(CTX, { ...good, value: NaN })).toBeNull();
    expect(buildWebVital(CTX, { ...good, value: -1 })).toBeNull();
    expect(buildWebVital(CTX, { ...good, value: 1e9 })).toBeNull();
    expect(buildWebVital(CTX, { ...good, rating: "amazing" })).toBeNull();
    expect(buildWebVital(CTX, { ...good, id: CANARY })).toBeNull();
  });

  it("metric ids accept only the web-vitals generated shape", () => {
    // Exactly what generateUniqueID() emits — this line is the upgrade
    // tripwire: a web-vitals major that changes the shape fails here.
    const generated = `v6-${Date.now()}-${
      Math.floor(Math.random() * (9e12 - 1)) + 1e12
    }`;
    expect(buildWebVital(CTX, { ...good, id: generated })).not.toBeNull();
    // Charset-valid free text is out of domain: the relay exports this
    // field verbatim, so the identifier charset alone was a text channel.
    expect(
      buildWebVital(CTX, { ...good, id: "customer-acme-production-secret" })
    ).toBeNull();
    expect(
      buildWebVital(CTX, { ...good, id: "LIBRERUN_SECRET_CANARY_9f31" })
    ).toBeNull();
  });

  it("drops an unknown navigationType instead of passing it through", () => {
    const rec = buildWebVital(CTX, { ...good, navigationType: CANARY });
    expect(rec).not.toBeNull();
    expect(rec!.navigation_type).toBeUndefined();
  });
});

describe("buildJsException", () => {
  const good = {
    errorType: "SyntaxError",
    mechanism: "window.error" as const,
    fingerprint: "deadbeef",
  };

  it("builds and never carries a message field", () => {
    const rec = buildJsException(CTX, {
      ...good,
      bundleModule: "/_next/static/chunks/app/runs/[id]/page-abc.js",
    });
    expect(rec).not.toBeNull();
    expect(JSON.stringify(rec)).not.toContain("message");
  });

  it("rejects free-text error types and bad fingerprints", () => {
    expect(buildJsException(CTX, { ...good, errorType: `Error: ${CANARY}` })).toBeNull();
    expect(buildJsException(CTX, { ...good, fingerprint: "DEADBEEF" })).toBeNull();
    expect(buildJsException(CTX, { ...good, fingerprint: CANARY })).toBeNull();
  });

  it("mirrors the relay's exact bundle-path bound — omit, never record-fatal", () => {
    // Relay bound: "/_next/static/" (14) + ≤110 chars = 124 total. A
    // looser client bound would make the relay drop the WHOLE record.
    const atBound = "/_next/static/" + "a".repeat(110);
    const overBound = "/_next/static/" + "a".repeat(111);
    const ok = buildJsException(CTX, { ...good, bundleModule: atBound });
    expect(ok!.bundle_module).toBe(atBound);
    const over = buildJsException(CTX, { ...good, bundleModule: overBound });
    expect(over).not.toBeNull(); // the exception itself survives
    expect(over!.bundle_module).toBeUndefined();
    const noPrefix = buildJsException(CTX, {
      ...good,
      bundleModule: "customer-secret",
    });
    expect(noPrefix!.bundle_module).toBeUndefined();
  });

  it("omits a bundle module carrying a query string", () => {
    const rec = buildJsException(CTX, {
      ...good,
      bundleModule: `/_next/x.js?leak=${CANARY}`,
    });
    expect(rec).not.toBeNull();
    expect(rec!.bundle_module).toBeUndefined();
  });
});

describe("surface shape", () => {
  it("agent surface stamps agent_id and run_id", () => {
    const rec = buildPageView(AGENT_CTX, { navigationKind: "hard" });
    expect(rec).toMatchObject({
      ui_owner: "agent",
      agent_id: "demo-agent",
      run_id: AGENT_CTX.surface.runId,
    });
  });

  it("platform surface can never leak agent fields", () => {
    const sneaky = {
      ...CTX,
      surface: { owner: "platform" as const, agentId: "demo-agent", runId: uuid() },
    };
    const rec = buildPageView(sneaky, { navigationKind: "hard" });
    expect(rec).not.toBeNull();
    expect(rec!.agent_id).toBeUndefined();
    expect(rec!.run_id).toBeUndefined();
  });

  it("agent surface without an agent id builds nothing", () => {
    const broken = { ...CTX, surface: { owner: "agent" as const } };
    expect(buildPageView(broken, { navigationKind: "hard" })).toBeNull();
  });
});

describe("route change bounds", () => {
  it("rejects absurd durations and unknown triggers", () => {
    const base = { fromRoute: "dashboard", trigger: "link" as const };
    expect(buildRouteChange(CTX, { ...base, durationMs: -5 })).toBeNull();
    expect(buildRouteChange(CTX, { ...base, durationMs: 999_999 })).toBeNull();
    expect(
      buildRouteChange(CTX, { fromRoute: "dashboard", trigger: CANARY as never })
    ).toBeNull();
    expect(buildRouteChange(CTX, { fromRoute: CANARY, trigger: "link" })).toBeNull();
  });
});

describe("canary sweep over everything the builders emit", () => {
  it("no expressible record can carry a canary or a URL", () => {
    const records: Array<RumRecord | null> = [
      buildWebVital(AGENT_CTX, {
        name: "INP",
        value: 143,
        rating: "good",
        id: "v4-1712345678901-1234567890123",
        navigationType: "navigate",
      }),
      buildJsException(CTX, {
        errorType: "TypeError",
        mechanism: "unhandledrejection",
        fingerprint: "0123abcd",
        bundleModule: "/_next/static/chunks/main-xyz.js",
      }),
      buildRouteChange(CTX, {
        fromRoute: "dashboard",
        trigger: "link",
        durationMs: 420,
        navigationId: uuid(),
      }),
      buildPageView(CTX, { navigationKind: "hard", referrerRoute: "login" }),
    ];
    const body = JSON.stringify(records);
    expect(records.every((r) => r !== null)).toBe(true);
    expect(body).not.toContain("LIBRERUN_SECRET_CANARY");
    expect(body).not.toContain("://");
    expect(body).not.toContain("?");
  });
});
