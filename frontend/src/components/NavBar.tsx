"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { apiFetch } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useMeta } from "../lib/meta";
import { flushBeforeLogout } from "../lib/telemetry/facade";
import SourceLink from "./SourceLink";

/** The demo-mode banner (blueprint S3): shown on every signed-in page while
 * the backend reports ``demo: true``. It states only what /meta reports —
 * a stub LLM and the shipped default secret are named when they are in
 * use, and nothing is assumed about how the mode was switched on (a
 * hand-written .env can set LIBRERUN_DEMO too). */
export function DemoBanner({
  stubLlm,
  defaultSecret,
}: {
  // null when the gateway could not be reached (blueprint S4a): keyless
  // mode is its fact, and saying nothing beats guessing "not stubbed".
  stubLlm: boolean | null;
  defaultSecret: boolean;
}) {
  const facts: string[] = [];
  if (stubLlm) facts.push("the LLM is a stub answering from canned fixtures");
  if (stubLlm === null) facts.push("the LLM gateway is unreachable");
  if (defaultSecret) facts.push("the shipped default secret is in use");
  const list = facts.length > 1 ? `${facts.slice(0, -1).join(", ")} and ${facts[facts.length - 1]}` : facts[0];
  return (
    <div
      role="status"
      data-testid="demo-banner"
      className="border-b border-amber-300 bg-amber-50 px-6 py-2 text-sm text-amber-900"
    >
      <span className="font-semibold">Demo mode</span> — not for production
      {list ? `: ${list}` : ""}. Leave it by unsetting <code>LIBRERUN_DEMO</code>
      {defaultSecret ? " and setting APP_SECRET_KEY" : ""}.
    </div>
  );
}

export default function NavBar() {
  const { user, token, clearAuth } = useAuth();
  const router = useRouter();
  const meta = useMeta();

  async function logout() {
    // Telemetry tail first: /auth/logout revokes the session server-side,
    // so anything flushed after it would be 401-dropped at the relay.
    try {
      await flushBeforeLogout();
    } catch {}
    try {
      if (token) await apiFetch("/auth/logout", token, { method: "POST" });
    } catch {}
    clearAuth();
    // No navigation intent here: the logout transition belongs to the
    // user who just left and is deliberately never recorded (see
    // deidentify's suppressNextCommit).
    router.push("/login");
  }

  return (
    <>
    {meta?.demo && <DemoBanner stubLlm={meta.stub_llm} defaultSecret={meta.default_secret} />}
    <nav className="flex items-center justify-between border-b bg-white px-6 py-3">
      <div className="flex items-center gap-6">
        <Link href="/dashboard" className="text-xl font-bold text-blue-600">
          LibreRun
        </Link>
        {user?.role === "admin" && (
          <Link href="/admin" className="text-sm text-slate-600 hover:text-blue-600">
            Admin
          </Link>
        )}
      </div>
      <div className="flex items-center gap-3 text-sm">
        <SourceLink className="text-xs text-slate-500 hover:text-blue-600 hover:underline" />
        {user && (
          <>
            <span>{user.email}</span>
            <span className="rounded bg-slate-200 px-2 py-0.5 text-xs">{user.role}</span>
            <button onClick={logout} className="rounded border px-3 py-1 hover:bg-slate-50">
              Log out
            </button>
          </>
        )}
      </div>
    </nav>
    </>
  );
}
