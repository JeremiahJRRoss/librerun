"use client";

import { useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useRef, useState, ReactNode } from "react";
import type { UserProfile } from "../types";
import { setUnauthorizedHandler } from "./api";
import { createUnauthorizedResponder } from "./session";
import { identify as telemetryIdentify, deidentify as telemetryDeidentify } from "./telemetry/facade";

interface AuthState {
  token: string | null;
  user: UserProfile | null;
  setAuth: (token: string, user: UserProfile) => void;
  clearAuth: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<UserProfile | null>(null);

  // The token as of RIGHT NOW, for readers that cannot wait for a render.
  //
  // `token` above is what the app renders; this ref is what the 401 handler
  // asks. They differ for one render plus the passive-effect flush after
  // every sign-in and sign-out, and a 401 landing in that window is not
  // hypothetical — it is an in-flight request from the session that just
  // ended. The ref is written synchronously in both mutators below so the
  // two can never be more than that window apart.
  const tokenRef = useRef<string | null>(null);

  const setAuth = useCallback((t: string, u: UserProfile) => {
    tokenRef.current = t;
    setToken(t);
    setUser(u);
    // Telemetry gets the live JWT (it never reads storage) and a fresh
    // RUM session id — the auth session itself never appears in telemetry.
    telemetryIdentify(t);
  }, []);
  const clearAuth = useCallback(() => {
    // Flush the authenticated tail, then rotate the RUM session so the
    // next login's telemetry cannot correlate with this one's.
    telemetryDeidentify();
    tokenRef.current = null;
    setToken(null);
    setUser(null);
  }, []);

  // One reaction to a rejected token, for the whole app.
  //
  // Without this a 401 mid-session left each caller to cope alone: the run
  // page replaced itself with the bare word "Unauthorized" and kept polling
  // every three seconds against a session that was already gone, with no
  // NavBar left to log out from.
  //
  // Registered once, not once per token. The responder reads `tokenRef` when
  // it fires rather than closing over a value, so re-registering on every
  // token change would buy nothing — and depending on `token` here is what
  // opened the stale-closure window in the first place.
  useEffect(() => {
    setUnauthorizedHandler(
      createUnauthorizedResponder({
        heldToken: () => tokenRef.current,
        onSessionEnd: () => {
          clearAuth();
          router.replace("/login?reason=session-expired");
        },
      })
    );
    return () => setUnauthorizedHandler(null);
  }, [clearAuth, router]);

  return (
    <AuthContext.Provider value={{ token, user, setAuth, clearAuth }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
