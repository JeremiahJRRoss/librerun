"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "../lib/api";
import { useAuth } from "../lib/auth";
import { stepUpdates } from "../lib/stepPayload";
import { useToast } from "../lib/toast";
import type { AgentConfigResponse, AgentStepConfig } from "../types";
import type { AgentPageTabProps } from "./agentPage/tabs";
import ScopeChip from "./ScopeChip";

// The agent page's Steps panel, as its tab's component (K4b): the shell,
// agentPage/AgentPageTabs.tsx, loads the config once and hands it to every
// tab, and each keeps its own edits until they are saved. The Settings
// panel lives in agentPage/SettingsPanel.tsx since K5a.

export function StepsTab({ data, reload }: AgentPageTabProps) {
  const { token } = useAuth();
  const { toast } = useToast();
  const config = data.config;
  const [steps, setSteps] = useState<AgentStepConfig[]>(config?.steps ?? []);
  const [saving, setSaving] = useState(false);

  // Reading the config back is not a nicety after a save: the server
  // decides what counts as an override (a value equal to the manifest's
  // default is not one), so the ``overridden here`` markers and the
  // values themselves are ITS answer, not something this component can
  // work out from what it just posted.
  useEffect(() => {
    setSteps(config?.steps ?? []);
  }, [config]);

  if (!config) return null;
  return (
    <StepsTable
      agentId={data.agentId}
      steps={steps}
      setSteps={setSteps}
      onSaved={reload}
      meta={config.meta}
      token={token!}
      saving={saving}
      setSaving={setSaving}
      toast={toast}
    />
  );
}

interface StepsTableProps {
  agentId: string;
  steps: AgentStepConfig[];
  setSteps: (next: AgentStepConfig[]) => void;
  onSaved: () => Promise<void>;
  meta: AgentConfigResponse["meta"];
  token: string;
  saving: boolean;
  setSaving: (b: boolean) => void;
  toast: (msg: string, kind?: "success" | "error" | "info") => void;
}

function StepsTable({
  agentId,
  steps,
  setSteps,
  onSaved,
  meta,
  token,
  saving,
  setSaving,
  toast,
}: StepsTableProps) {
  const editable = meta.step_editable_fields;

  function update(idx: number, patch: Partial<AgentStepConfig>) {
    const copy = [...steps];
    copy[idx] = { ...copy[idx], ...patch };
    setSteps(copy);
  }

  async function save() {
    setSaving(true);
    try {
      await apiFetch(`/agents/${agentId}/config/steps`, token, {
        method: "PUT",
        body: JSON.stringify(stepUpdates(steps, editable)),
      });
      await onSaved();
      toast("Steps saved", "success");
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : "Save failed", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section data-scope-region="steps">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="text-lg font-semibold">Pipeline steps</h2>
        <ScopeChip scope="agent_tenant" />
      </div>
      <div className="overflow-x-auto rounded border bg-white">
        <table className="w-full text-sm">
          <thead className="bg-slate-100 text-left">
            <tr>
              <th className="px-2 py-1">Step</th>
              <th className="px-2 py-1">Provider</th>
              <th className="px-2 py-1">Model</th>
              {editable.map((f) => (
                <th key={f} className="px-2 py-1 capitalize">
                  {f.replace(/_/g, " ")}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {steps.map((s, i) => {
              // Every entry in `llm.steps[]` is an LLM step — non-LLM
              // pipeline stages are not in that list at all. So a step
              // with no provider is not "No LLM", it is a step whose
              // manifest leaves the choice to the admin, and this page
              // is the place the platform's own `step_not_configured`
              // error sends them. It used to render such a row greyed
              // out with its inputs disabled, which made that agent
              // impossible to configure anywhere (Codex P1).
              const unconfigured = !s.provider || !s.model;
              return (
                <tr key={s.step_id} className="border-t">
                  <td className="px-2 py-1">
                    <div className="font-mono text-xs">{s.step_id}</div>
                    <div className="text-xs text-slate-500">
                      {s.label || s.description}
                    </div>
                    {/* Blueprint S4a: which values are this tenant's
                        choice and which are the agent's default. */}
                    {s.overridden?.length > 0 && (
                      <div className="text-xs text-blue-700">
                        {`overridden here: ${s.overridden.join(", ")}`}
                      </div>
                    )}
                    {unconfigured && (
                      <div className="text-xs text-amber-700">
                        needs a provider and a model before this step can run
                      </div>
                    )}
                  </td>
                  <td className="px-2 py-1">
                    <select
                      value={s.provider ?? ""}
                      onChange={(e) => update(i, { provider: e.target.value })}
                      className="rounded border px-1 py-0.5 text-xs"
                    >
                      {/* Present only while nothing is chosen, so an
                          unset provider reads as unset instead of
                          silently showing the first option. */}
                      {!s.provider && <option value="">choose a provider…</option>}
                      {meta.supported_providers.map((p) => (
                        <option key={p} value={p}>
                          {p}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="px-2 py-1">
                    <input
                      value={s.model ?? ""}
                      placeholder="model"
                      onChange={(e) => update(i, { model: e.target.value })}
                      className="w-40 rounded border px-1 py-0.5 text-xs"
                    />
                  </td>
                  {editable.map((field) => {
                    const raw = (s as unknown as Record<string, unknown>)[field];
                    const value =
                      raw === null || raw === undefined ? "" : (raw as number | string);
                    return (
                      <td key={field} className="px-2 py-1">
                        <input
                          type="number"
                          step={field === "temperature" ? 0.1 : 1}
                          value={value}
                          onChange={(e) => {
                            const next = e.target.value;
                            const parsed =
                              next === ""
                                ? null
                                : field === "temperature"
                                ? parseFloat(next)
                                : parseInt(next, 10);
                            update(i, { [field]: parsed } as Partial<AgentStepConfig>);
                          }}
                          className="w-20 rounded border px-1 py-0.5 text-xs"
                        />
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <button
        onClick={save}
        disabled={saving}
        className="mt-3 rounded bg-blue-600 px-4 py-2 text-white disabled:opacity-50"
      >
        {saving ? "Saving…" : "Save steps"}
      </button>
    </section>
  );
}
