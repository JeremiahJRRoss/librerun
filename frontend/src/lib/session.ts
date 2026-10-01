/**
 * When a 401 means "your session is gone" rather than "wrong password".
 *
 * The request that was rejected decides this, not the app's current state,
 * and conflating the two has now been wrong twice in two different ways:
 *
 *   - Reading only the provider's token logged out a signed-in user who
 *     walked back to `/login` through browser history and mistyped a
 *     password: that POST carries no token, so its 401 said nothing about
 *     the session it destroyed.
 *   - Reading a mere "did it carry a token" boolean is wrong the moment two
 *     tokens exist in one page's lifetime. A request made with token A that
 *     is still in flight across a logout and a fresh login with token B
 *     comes back 401 — for A, correctly — and a boolean cannot tell that
 *     from B being rejected, so the new, valid session is cleared.
 *
 * So the comparison is between IDENTITIES: end the session only when the
 * token the server rejected is the token we are still holding.
 *
 * Kept as a pure function, in a plain module, so the truth table is testable
 * without rendering a provider.
 */
export function shouldEndSession(req: {
  /** The token this request actually sent, or null if it sent none. */
  sentToken: string | null;
  /** The token the app holds right now, or null if signed out. */
  heldToken: string | null;
}): boolean {
  // No token on the request: nothing of ours was rejected. A failed sign-in,
  // or any other anonymous call. Never ends a session.
  if (!req.sentToken) return false;
  // Signed out already — a burst of pollers all 401 at once and the first one
  // did the work.
  if (!req.heldToken) return false;
  // A late 401 for a token we have already replaced. The session it refers to
  // is gone; the one we hold was never rejected.
  return req.sentToken === req.heldToken;
}

/**
 * The app-wide 401 reaction, built around a GETTER for the held token.
 *
 * The getter is the whole point, and it is why this takes a function where
 * a value would read more naturally. The handler is registered once and
 * fires much later, so any token captured at registration is a snapshot —
 * and in React the snapshot is stale exactly when it matters. `setAuth(B)`
 * queues a state update; the effect that re-registers the handler does not
 * run until the render commits and passive effects flush. A 401 for the old
 * token A arriving inside that window would find A still in the closure,
 * match it against itself, and clear the freshly established B session —
 * the same race the identity comparison exists to prevent, walking back in
 * through the registration path (Codex round 4).
 *
 * So the caller supplies a way to read the CURRENT token at fire time — a
 * ref updated synchronously in `setAuth`/`clearAuth`, not a state value.
 * Taking a getter makes that a requirement of the signature rather than a
 * convention someone has to remember.
 */
export function createUnauthorizedResponder(deps: {
  heldToken: () => string | null;
  onSessionEnd: () => void;
}): (sentToken: string | null) => void {
  return (sentToken: string | null) => {
    if (!shouldEndSession({ sentToken, heldToken: deps.heldToken() })) return;
    deps.onSessionEnd();
  };
}
