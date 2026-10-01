"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { apiFetch } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useMeta } from "../../lib/meta";
import SourceLink from "../../components/SourceLink";
import { recordNavigationIntent } from "../../lib/telemetry/facade";
import { useToast } from "../../lib/toast";
import type { UserProfile } from "../../types";

export default function LoginPage() {
  // useSearchParams opts a route into client rendering, and Next requires the
  // boundary to be explicit or `next build` fails on this page.
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  // Set by the app-wide 401 handler, so an interrupted session says why it
  // ended instead of dropping the user on a bare sign-in form.
  const sessionExpired = searchParams.get("reason") === "session-expired";
  const { setAuth } = useAuth();
  const { toast } = useToast();
  const meta = useMeta();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    try {
      const res = await apiFetch<{ access_token: string }>("/auth/login", null, {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      const me = await apiFetch<UserProfile>("/auth/me", res.access_token);
      setAuth(res.access_token, me);
      toast("Signed in", "success");
      recordNavigationIntent("push");
      router.push("/dashboard");
    } catch (e) {
      toast("Login failed — check credentials", "error");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="w-full max-w-md rounded-lg border bg-white p-8 shadow">
        <h1 className="mb-6 text-2xl font-bold">LibreRun Sign in</h1>
        {/* Two independent notices: why you landed back here, and how to
            sign in when this is a demo deployment. Both can be true. */}
        {sessionExpired && (
          <div
            role="status"
            className="mb-4 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900"
          >
            Your session ended. Please sign in again.
          </div>
        )}
        {meta?.demo && (
          <p
            data-testid="demo-hint"
            className="mb-4 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900"
          >
            <span className="font-semibold">Demo mode.</span> If <code>./scripts/demo.sh</code>{" "}
            started this, the admin email and password were printed in your terminal and are
            in the <code>.env</code> it wrote; otherwise they are the <code>INITIAL_ADMIN_*</code>{" "}
            pair in your <code>.env</code>.
          </p>
        )}
        <form className="space-y-4" onSubmit={handleLogin}>
          <label className="block">
            <span className="text-sm">Email</span>
            <input
              className="mt-1 w-full rounded border px-3 py-2"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label className="block">
            <span className="text-sm">Password</span>
            <input
              className="mt-1 w-full rounded border px-3 py-2"
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded bg-blue-600 py-2 text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {loading ? "Signing in..." : "Sign in"}
          </button>
        </form>
        <div className="mt-6 space-y-2 border-t pt-4">
          <button
            type="button"
            className="w-full rounded border py-2 text-sm opacity-60"
            disabled
            title="Configure GOOGLE_CLIENT_ID to enable"
          >
            Continue with Google
          </button>
          <button
            type="button"
            className="w-full rounded border py-2 text-sm opacity-60"
            disabled
            title="Configure AZURE_CLIENT_ID to enable"
          >
            Continue with Microsoft
          </button>
        </div>
        <SourceLink className="mt-6 block text-center text-xs text-slate-500 hover:text-blue-600 hover:underline" />
      </div>
    </div>
  );
}
