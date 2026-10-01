export const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

type UnauthorizedHandler = (sentToken: string | null) => void;

let unauthorizedHandler: UnauthorizedHandler | null = null;

/**
 * Register the app-wide reaction to a 401. `AuthProvider` supplies it.
 *
 * A 401 on a request that CARRIED a bearer token means that token was
 * rejected — expired, revoked by a logout in another tab, an admin revoke,
 * or a deactivated user. It never means "this run is not yours": the API
 * answers 404 for another tenant's run and 403 for another user's. So there
 * is exactly one correct response to it, and it belongs in one place rather
 * than in each of the callers that poll.
 *
 * A 401 on a request that carried NO token is an ordinary credential
 * rejection — a failed sign-in — and must not sign anybody out. Hence the
 * `sentToken` argument: the handler cannot infer this from the provider's
 * own token, because a still-signed-in user can reach `/login` through
 * browser history and submit bad credentials while holding a valid session.
 *
 * The argument is the token ITSELF, not a boolean, because a boolean cannot
 * survive a token changing mid-flight: a request sent under an old token can
 * land after a fresh login, and only comparing identities tells that apart
 * from the new token being rejected.
 */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler;
}

/**
 * Build the 401 error, notifying the handler first.
 *
 * `sentToken` is the bearer token THIS request sent, or null if it sent
 * none. That is what decides whether the 401 is a dead session, a rejected
 * password, or a straggler for a token already replaced. It is a required
 * argument on purpose: defaulting it either way would let a new call site
 * silently pick the wrong story.
 *
 * Exported because a couple of call sites use `fetch` directly — a blob
 * download cannot go through `apiFetch`'s JSON parsing — and they must take
 * the same path rather than inventing a second 401 story.
 */
export function unauthorizedError(sentToken: string | null): ApiError {
  // Fire before throwing, so a caller that swallows the error — a poller with
  // an empty catch, say — still triggers the sign-out rather than looping
  // against a dead session.
  unauthorizedHandler?.(sentToken);
  return new ApiError("Unauthorized", 401);
}

export async function apiFetch<T>(
  path: string,
  token: string | null,
  options?: RequestInit
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((options?.headers as Record<string, string>) ?? {}),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (res.status === 401) {
    throw unauthorizedError(token);
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(`API ${res.status}: ${text}`, res.status);
  }
  if (res.status === 204) return undefined as unknown as T;
  return (await res.json()) as T;
}

export async function apiUpload<T>(
  path: string,
  token: string | null,
  form: FormData
): Promise<T> {
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}${path}`, { method: "POST", body: form, headers });
  if (res.status === 401) throw unauthorizedError(token);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(`API ${res.status}: ${text}`, res.status);
  }
  return (await res.json()) as T;
}
