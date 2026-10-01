/**
 * Facade behavior: route commits, surface hygiene across navigations, and
 * session rotation at auth boundaries.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  _isNavigationIntentClick,
  _resetFacadeForTests,
  _sessionIdForTests,
  deidentify,
  flushBeforeLogout,
  identify,
  recordNavigationIntent,
  routeCommitted,
  tagSurface,
} from "../facade";
import {
  _queueForTests,
  _resetTransportForTests,
} from "../transport";
import { _classifyForTests, _resetErrorBudgetsForTests } from "../errors";

beforeEach(() => {
  _resetTransportForTests();
  _resetFacadeForTests();
  _resetErrorBudgetsForTests();
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 202, headers: { get: () => null } })));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("routeCommitted", () => {
  it("emits a hard page view for the initial commit", () => {
    identify("jwt"); // records only flow with a token present
    routeCommitted("/dashboard", true);
    const queue = _queueForTests();
    expect(queue).toHaveLength(1);
    expect(queue[0]).toMatchObject({
      type: "page_view",
      route: "dashboard",
      navigation_kind: "hard",
    });
  });

  it("emits a route change with a fresh page id on soft commits", () => {
    identify("jwt");
    routeCommitted("/dashboard", true);
    const firstPageId = (_queueForTests()[0] as any).page_id;
    routeCommitted("/runs/abc-123", false);
    const change = _queueForTests()[1] as any;
    expect(change.type).toBe("route_change");
    expect(change.from_route).toBe("dashboard");
    expect(change.route).toBe("run.detail");
    expect(change.trigger).toBe("unknown"); // no observed intent in this test
    expect(change.duration_ms).toBeUndefined();
    expect(change.page_id).not.toBe(firstPageId);
  });

  it("programmatic navigations report the push trigger with a duration", () => {
    identify("jwt");
    routeCommitted("/dashboard", true);
    recordNavigationIntent("push"); // e.g. a dashboard row click handler
    routeCommitted("/runs/abc-123", false);
    const change = _queueForTests()[1] as any;
    expect(change.trigger).toBe("push");
    expect(change.duration_ms).toBeGreaterThanOrEqual(0);
  });

  it("clears an agent surface on soft navigation — surfaces never leak across views", () => {
    identify("jwt");
    routeCommitted("/runs/abc", true);
    tagSurface({
      owner: "agent",
      agentId: "demo-agent",
      runId: "5f3a1c2e-1111-4222-8333-444455557777",
    });
    routeCommitted("/dashboard", false);
    const change = _queueForTests()[1] as any;
    expect(change.ui_owner).toBe("platform");
    expect(change.agent_id).toBeUndefined();
    expect(change.run_id).toBeUndefined();
  });
});

describe("navigation intent classification", () => {
  function click(
    overrides: Partial<MouseEvent> = {},
    anchorAttrs: Record<string, string> = { href: "/runs/abc" }
  ): MouseEvent {
    const anchor = document.createElement("a");
    for (const [name, value] of Object.entries(anchorAttrs)) {
      anchor.setAttribute(name, value);
    }
    return {
      button: 0,
      metaKey: false,
      ctrlKey: false,
      shiftKey: false,
      altKey: false,
      target: anchor,
      ...overrides,
    } as unknown as MouseEvent;
  }

  it("accepts an unmodified primary click on a same-origin self-target link", () => {
    expect(_isNavigationIntentClick(click())).toBe(true);
  });

  it("rejects clicks that will not navigate this tab", () => {
    expect(_isNavigationIntentClick(click({ metaKey: true }))).toBe(false);
    expect(_isNavigationIntentClick(click({ ctrlKey: true }))).toBe(false);
    expect(_isNavigationIntentClick(click({ shiftKey: true }))).toBe(false);
    expect(_isNavigationIntentClick(click({ altKey: true }))).toBe(false);
    expect(_isNavigationIntentClick(click({ button: 1 }))).toBe(false); // middle
    expect(
      _isNavigationIntentClick(click({}, { href: "/x", target: "_blank" }))
    ).toBe(false);
    expect(
      _isNavigationIntentClick(click({}, { href: "/x", download: "" }))
    ).toBe(false);
    expect(
      _isNavigationIntentClick(click({}, { href: "https://evil.example/x" }))
    ).toBe(false);
    expect(
      _isNavigationIntentClick(click({}, { href: "//evil.example/x" }))
    ).toBe(false);
  });

  it("accepts an explicit target=_self", () => {
    expect(
      _isNavigationIntentClick(click({}, { href: "/x", target: "_self" }))
    ).toBe(true);
  });
});

describe("auth boundaries", () => {
  it("identify and deidentify both rotate the RUM session id", () => {
    const s0 = _sessionIdForTests();
    identify("jwt");
    const s1 = _sessionIdForTests();
    deidentify();
    const s2 = _sessionIdForTests();
    expect(s1).not.toBe(s0);
    expect(s2).not.toBe(s1);
  });

  it("flushBeforeLogout is bounded — a hung relay cannot make logout unavailable", async () => {
    vi.useFakeTimers();
    // A fetch that never settles: without the bound, logout would await
    // this forever and revocation/navigation would never run.
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    identify("jwt");
    routeCommitted("/dashboard", true); // something queued to flush
    let resolved = false;
    const pending = flushBeforeLogout(1500).then(() => {
      resolved = true;
    });
    await vi.advanceTimersByTimeAsync(1600);
    await pending;
    expect(resolved).toBe(true);
  });

  it("the logout transition is suppressed and never crosses into the next login", () => {
    // User A logs out on run.detail; the resulting navigation to /login
    // commits AFTER the identity boundary. Without suppression it would
    // sit queued token-less and ship under user B's identity at next
    // login — carrying A's from_route.
    identify("jwt-user-a");
    routeCommitted("/runs/abc", true);
    deidentify(); // clears the queue and arms suppression
    routeCommitted("/login", false); // the logout navigation commits
    expect(_queueForTests()).toHaveLength(0); // nothing recorded for it

    identify("jwt-user-b");
    routeCommitted("/dashboard", false); // B's first real navigation
    const change = _queueForTests()[0] as any;
    expect(change.type).toBe("route_change");
    expect(change.from_route).toBe("login"); // NOT user A's run.detail
    expect(change.route).toBe("dashboard");
  });

  it("session rotation resets error budgets — a capped session cannot blind the next", () => {
    const stack = "Error\n    at /_next/static/chunks/a.js:1:1";
    const boom = () => {
      const err = new Error("same");
      err.stack = stack;
      return _classifyForTests(err, "window.error");
    };
    for (let i = 0; i < 10; i++) boom();
    expect(boom()).toBeNull(); // per-fingerprint cap reached
    identify("jwt-next-user"); // rotates the RUM session
    expect(boom()).not.toBeNull(); // budgets start fresh
  });
});
