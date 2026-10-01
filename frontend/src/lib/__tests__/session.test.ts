import { describe, expect, it, afterEach, vi } from "vitest";
import { createUnauthorizedResponder, shouldEndSession } from "../session";
import { apiFetch, apiUpload, setUnauthorizedHandler, unauthorizedError } from "../api";

describe("shouldEndSession", () => {
  const A = "token-a";
  const B = "token-b";

  it("ends the session when the token we hold is the one rejected", () => {
    expect(shouldEndSession({ sentToken: A, heldToken: A })).toBe(true);
  });

  // Regression 1. A signed-in user reaches /login through browser history
  // and mistypes a password: the sign-in POST carries no bearer token, so
  // its 401 says nothing about the session that is still perfectly valid.
  // Reading only the held token logged that user out with "session expired".
  it("leaves a live session alone when a tokenless request is rejected", () => {
    expect(shouldEndSession({ sentToken: null, heldToken: A })).toBe(false);
  });

  // Regression 2. A boolean "did this request carry a token" is not enough
  // once two tokens exist in one page's lifetime: a request sent under A,
  // still in flight across a logout and a fresh login as B, comes back 401
  // for A — and a boolean cannot tell that from B being rejected.
  it("ignores a late 401 for a token that has already been replaced", () => {
    expect(shouldEndSession({ sentToken: A, heldToken: B })).toBe(false);
  });

  it("is a no-op for a failed sign-in while signed out", () => {
    expect(shouldEndSession({ sentToken: null, heldToken: null })).toBe(false);
  });

  it("is a no-op once the token is already cleared", () => {
    expect(shouldEndSession({ sentToken: A, heldToken: null })).toBe(false);
  });
});

describe("401 notification carries the request's own token", () => {
  afterEach(() => {
    setUnauthorizedHandler(null);
    vi.unstubAllGlobals();
  });

  function stub401() {
    const fetchMock = vi.fn(async () =>
      new Response("", { status: 401 })
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("reports a null token for an anonymous request", async () => {
    stub401();
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    await expect(apiFetch("/auth/login", null, { method: "POST" })).rejects.toMatchObject({
      status: 401,
    });
    expect(handler).toHaveBeenCalledWith(null);
  });

  it("reports the exact token an authenticated request sent", async () => {
    stub401();
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    await expect(apiFetch("/runs/abc", "jwt-value")).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledWith("jwt-value");
  });

  it("reports the request's token on uploads too", async () => {
    stub401();
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    await expect(apiUpload("/runs/abc/files", null, new FormData())).rejects.toMatchObject({
      status: 401,
    });
    expect(handler).toHaveBeenCalledWith(null);
  });

  // The direct-`fetch` call sites (the blob download in ResultsView) build
  // the error themselves; they must take the same path, argument included.
  it("notifies from the exported error builder", () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    expect(unauthorizedError("jwt-value").status).toBe(401);
    expect(handler).toHaveBeenCalledWith("jwt-value");
  });

  it("tolerates having no handler registered", () => {
    setUnauthorizedHandler(null);
    expect(() => unauthorizedError(null)).not.toThrow();
  });
});

describe("the responder reads the held token when it FIRES, not when it is built", () => {
  const A = "token-a";
  const B = "token-b";

  /** A token store with the timing a `useRef` has: writes land at once. */
  function syncStore(initial: string | null = null) {
    let current = initial;
    return { get: () => current, set: (t: string | null) => { current = t; } };
  }

  /**
   * A token store with the timing React STATE has: a write is queued and is
   * not visible to an already-registered closure until it is flushed. This
   * is the provider's real behaviour between `setAuth` and the passive
   * effect that would re-register the handler.
   */
  function deferredStore(initial: string | null = null) {
    let visible = initial;
    let pending = initial;
    return {
      get: () => visible,
      set: (t: string | null) => { pending = t; },
      flush: () => { visible = pending; },
    };
  }

  it("ignores a 401 for the old token when a new session is already live", () => {
    const held = syncStore(A);
    const onSessionEnd = vi.fn();
    const respond = createUnauthorizedResponder({ heldToken: held.get, onSessionEnd });

    // A request goes out under A. Then the user signs out and back in as B.
    held.set(null);
    held.set(B);
    // Only now does the in-flight request for A come back 401.
    respond(A);

    expect(onSessionEnd).not.toHaveBeenCalled();
    expect(held.get()).toBe(B);
  });

  it("still ends the session when the live token is the rejected one", () => {
    const held = syncStore(A);
    const onSessionEnd = vi.fn();
    createUnauthorizedResponder({ heldToken: held.get, onSessionEnd })(A);
    expect(onSessionEnd).toHaveBeenCalledTimes(1);
  });

  // The regression itself. Wiring the responder to a value that updates a
  // render late — which is what closing over the `token` STATE did — clears
  // the new session, and the identity comparison cannot save it: both sides
  // read A because the store has not caught up.
  it("would clear the new session if the held token lagged a render", () => {
    const lagging = deferredStore(A);
    const onSessionEnd = vi.fn();
    const respond = createUnauthorizedResponder({ heldToken: lagging.get, onSessionEnd });

    lagging.set(B);        // setAuth(B) — queued, not yet visible
    respond(A);            // the old request's 401 lands inside the window
    expect(onSessionEnd).toHaveBeenCalledTimes(1);   // ← the bug, reproduced

    // And once it has caught up, the same 401 is correctly ignored. That
    // difference is the entire fix: the provider writes its ref
    // synchronously so the window in the first line never exists.
    lagging.flush();
    onSessionEnd.mockClear();
    respond(A);
    expect(onSessionEnd).not.toHaveBeenCalled();
  });

  it("never ends a session for an anonymous request, whatever is held", () => {
    for (const held of [null, A]) {
      const onSessionEnd = vi.fn();
      createUnauthorizedResponder({ heldToken: () => held, onSessionEnd })(null);
      expect(onSessionEnd).not.toHaveBeenCalled();
    }
  });
});
