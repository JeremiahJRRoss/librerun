"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";
import NavBar from "../../components/NavBar";
import ScopeChip, { type Scope } from "../../components/ScopeChip";
import { apiFetch } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import type { AgentInfo, AgentKeyRow, DriftSummary } from "../../types";

// K9: each card says what its page edits, or shows, under the chip of the
// scope it acts on; the platform's two say so to a tenant admin, whose
// account the server refuses there.
const CARDS: {
  href: string;
  label: string;
  desc: string;
  scope: Scope;
  platformOnly?: boolean;
  driftBadge?: boolean;
}[] = [
  { href: "/admin/users", label: "Users & Access", desc: "Edits this tenant's users, their roles and sessions", scope: "tenant" },
  { href: "/admin/auth-config", label: "Auth Configuration", desc: "Edits this tenant's sign-in methods and allowlists", scope: "tenant" },
  { href: "/admin/feedback", label: "Feedback Dashboard", desc: "Shows this tenant's per-section feedback", scope: "tenant" },
  {
    href: "/admin/audit-log?action_type=llm_schema_drift",
    label: "Activity Audit Log",
    desc: "Shows this tenant's admin and user actions",
    scope: "tenant",
    driftBadge: true,
  },
  {
    href: "/admin/settings",
    label: "Application Settings",
    desc: "Edits every tenant's settings, model providers and certificates; shows the deployment",
    scope: "platform",
    platformOnly: true,
  },
  {
    href: "/admin/observability",
    label: "Observability",
    desc: "Shows where telemetry goes: the vendor overlay, its sinks, the router",
    scope: "deployment",
    platformOnly: true,
  },
];

// The manifest's rule for an agent id (manifest.py): what the field below
// accepts, so it opens only a page an agent could have.
const AGENT_ID = /^[a-z0-9][a-z0-9-]*$/;
const AGENT_ID_MAX = 50;

export default function AdminHomePage() {
  const { user, token } = useAuth();
  const router = useRouter();
  const [drift, setDrift] = useState<DriftSummary | null>(null);
  const [agents, setAgents] = useState<AgentInfo[] | null>(null);
  // K9: the ids a key is installed for with no agent registered under them.
  const [unregistered, setUnregistered] = useState<string[]>([]);
  const [agentIdDraft, setAgentIdDraft] = useState("");

  useEffect(() => {
    if (!token) router.push("/login");
    else if (user && user.role !== "admin") router.push("/dashboard");
  }, [token, user, router]);

  useEffect(() => {
    if (!token || !user || user.role !== "admin") return;
    // A single failed fetch should not break the admin home — swallow errors.
    apiFetch<DriftSummary>("/admin/drift-summary", token)
      .then(setDrift)
      .catch(() => setDrift(null));
    apiFetch<AgentInfo[]>("/agents", token)
      .then(setAgents)
      .catch(() => setAgents([]));
    if (user.is_platform_admin) {
      apiFetch<AgentKeyRow[]>("/admin/agent-keys", token)
        .then((rows) =>
          setUnregistered(
            Array.from(new Set(rows.filter((row) => !row.registered).map((row) => row.agent_id))),
          ),
        )
        .catch(() => setUnregistered([]));
    }
  }, [token, user]);

  const agentIdValid = AGENT_ID.test(agentIdDraft) && agentIdDraft.length <= AGENT_ID_MAX;

  function openAgent(e: FormEvent) {
    e.preventDefault();
    if (agentIdValid) router.push(`/admin/agents/${agentIdDraft}/config`);
  }

  if (!token || !user) return null;
  return (
    <div>
      <NavBar />
      <main className="mx-auto max-w-5xl p-6">
        <h1 className="mb-6 text-2xl font-bold">Admin</h1>

        <section className="mb-8">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-600">
            Agents
          </h2>
          {agents === null && (
            <p className="text-sm text-slate-500">Loading agents…</p>
          )}
          {agents && agents.length === 0 && (
            <p className="text-sm text-slate-500">No agents registered.</p>
          )}
          {agents && agents.length > 0 && (
            <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
              {agents.map((a) => {
                const card = (
                  <div className="rounded border bg-white p-4 hover:bg-slate-50">
                    <h3 className="font-semibold">{a.display_name}</h3>
                    <p className="mt-1 text-xs text-slate-600">{a.description}</p>
                    <p className="mt-2 font-mono text-xs text-slate-400">{a.agent_id}</p>
                    {/* Blueprint S4: the grants, and for a container agent
                        whether it opted out of "no path off-box but through
                        the chassis" (manifest network.egress). */}
                    <p className="mt-2 text-xs text-slate-500">
                      {a.runtime === "container" ? "container" : "in-process"}
                      {" · grants: "}
                      {a.capabilities.length > 0 ? a.capabilities.join(", ") : "none"}
                      {a.runtime === "container" && (
                        <>
                          {" · egress: "}
                          <span
                            className={
                              a.network?.egress
                                ? "font-medium text-amber-700"
                                : "text-slate-500"
                            }
                          >
                            {a.network?.egress
                              ? "allowed (network.egress: true)"
                              : "none — chassis only"}
                          </span>
                        </>
                      )}
                    </p>
                    {/* Blueprint S4a: the declared LLM steps, and the
                        opt-out from outbound redaction — an operator must
                        be able to see that an agent sends unredacted text
                        to a model without reading its manifest. */}
                    {a.llm && a.llm.steps.length > 0 && (
                      <p className="mt-1 text-xs text-slate-500">
                        {`llm steps: ${a.llm.steps.length}`}
                        {" · outbound PII: "}
                        <span
                          className={
                            a.llm.redact_outbound
                              ? "text-slate-500"
                              : "font-medium text-amber-700"
                          }
                        >
                          {a.llm.redact_outbound
                            ? "redacted"
                            : "NOT redacted (llm.redact_outbound: false)"}
                        </span>
                      </p>
                    )}
                    {!a.has_config && (
                      <p className="mt-2 text-xs italic text-slate-500">
                        No admin config
                      </p>
                    )}
                  </div>
                );
                // K4b: every agent's page is a tab shell, so every card
                // links to it; the note above still says whether the
                // agent has anything to configure (has_config, K5a's).
                return (
                  <Link
                    key={a.agent_id}
                    href={`/admin/agents/${a.agent_id}/config`}
                    className="block"
                  >
                    {card}
                  </Link>
                );
              })}
            </div>
          )}
        </section>

        {user.is_platform_admin && (
          <section data-testid="agent-keys-hub" className="mb-8">
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-600">
              Agent keys
            </h2>
            {unregistered.length > 0 && (
              <p data-testid="unregistered-keys" className="mb-2 text-sm text-amber-900">
                Keys installed for ids no agent is registered under:{" "}
                {unregistered.map((id, i) => (
                  <span key={id}>
                    {i > 0 && ", "}
                    <Link href={`/admin/agents/${id}/config`} className="font-mono underline">
                      {id}
                    </Link>
                  </span>
                ))}
              </p>
            )}
            <form onSubmit={openAgent} className="flex items-end gap-2 text-sm">
              <label className="text-xs text-slate-600">
                Agent id, for a first key (its page&apos;s Keys tab)
                <input
                  value={agentIdDraft}
                  onChange={(e) => setAgentIdDraft(e.target.value.trim())}
                  maxLength={AGENT_ID_MAX}
                  className="mt-1 block w-64 rounded border px-2 py-1 font-mono text-sm"
                />
              </label>
              <button
                type="submit"
                disabled={!agentIdValid}
                className="rounded border px-3 py-1 text-sm disabled:opacity-50"
              >
                Open
              </button>
            </form>
          </section>
        )}

        <section>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-600">
            System
          </h2>
          <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
            {CARDS.map((c) => (
              <Link
                key={c.href}
                href={c.href}
                className="relative rounded border bg-white p-4 hover:bg-slate-50"
              >
                <div className="flex items-center gap-2">
                  <h3 className="font-semibold">{c.label}</h3>
                  <ScopeChip scope={c.scope} />
                </div>
                <p className="mt-1 text-xs text-slate-600">{c.desc}</p>
                {c.platformOnly && !user.is_platform_admin && (
                  <p className="mt-1 text-xs font-medium text-slate-500">platform operators only</p>
                )}
                {c.driftBadge && drift && drift.last_24h > 0 && (
                  <span
                    className="absolute right-3 top-3 rounded-full bg-red-600 px-2 py-0.5 text-xs font-semibold text-white"
                    title={`${drift.last_24h} drift events in the last 24h`}
                  >
                    {drift.last_24h} drift
                  </span>
                )}
              </Link>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}
