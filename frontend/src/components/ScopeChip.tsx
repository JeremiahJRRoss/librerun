/**
 * Where a value lives, on the field that shows it (K4b, D30; the tiers of
 * the configuration blueprint's §1.2). The words are the ones K9 uses when
 * it places a chip on every field; K4b builds the chip and places none.
 * K8b adds `agent` — one agent's value for every tenant, the default of a
 * tool secret that a platform admin sets — for the Secrets tab's rows.
 */
export type Scope = "deployment" | "platform" | "tenant" | "agent" | "agent_tenant";

export const SCOPE_LABELS: Record<Scope, string> = {
  deployment: "deployment",
  platform: "platform",
  tenant: "this tenant",
  agent: "this agent",
  agent_tenant: "this agent · this tenant",
};

const SCOPE_TITLES: Record<Scope, string> = {
  deployment: "Set in the deployment's environment; read-only here, and a change needs a restart",
  platform: "Set by a platform admin; every tenant reads it",
  tenant: "Set by an admin of this tenant, for this tenant alone",
  agent: "Set by a platform admin for this agent; every tenant without a value of its own reads it",
  agent_tenant: "Set for this agent, in this tenant alone",
};

export default function ScopeChip({ scope }: { scope: Scope }) {
  return (
    <span
      data-scope={scope}
      title={SCOPE_TITLES[scope]}
      className="inline-flex items-center rounded-full border border-slate-300 bg-slate-50 px-2 py-0.5 text-xs text-slate-600"
    >
      {SCOPE_LABELS[scope]}
    </span>
  );
}
