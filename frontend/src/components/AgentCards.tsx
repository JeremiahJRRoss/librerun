"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "../lib/api";
import { frameworkBadge } from "../lib/runPage";
import type { AgentInfo, AgentScenario } from "../types";

interface Props {
  token: string;
  selected: string | null;
  // Receives the full agent row — callers need its manifest fields
  // (ui.intake, phases) to decide what to render next.
  onSelect: (agent: AgentInfo) => void;
  // "Try a sample": the scenario's inputs go straight into the form.
  onSample: (agent: AgentInfo, scenario: AgentScenario) => void;
  // A card to select as soon as the list arrives (the `?agent=` query,
  // which is how "Run again" lands here).
  preselect?: string | null;
}

/**
 * The agent cards (blueprint S7, D3): every installed agent as a card —
 * its name, its description, a badge naming the framework its manifest
 * declares (or its runtime), and a chip per sample scenario under "Try a
 * sample". Everything on a card is manifest data through `GET /agents`;
 * the chassis knows no agent by name (L13).
 */
export default function AgentCards({ token, selected, onSelect, onSample, preselect }: Props) {
  const [agents, setAgents] = useState<AgentInfo[] | null>(null);
  const [samples, setSamples] = useState<Record<string, AgentScenario[]>>({});
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    apiFetch<AgentInfo[]>("/agents", token)
      .then(async (rows) => {
        if (!alive) return;
        setAgents(rows);
        // Auto-select when there is only one agent, or when the caller
        // named one — no point asking the user to "pick" in either case.
        const wanted = preselect ? rows.find((a) => a.agent_id === preselect) : null;
        const only = rows.length === 1 ? rows[0] : null;
        const pick = wanted ?? only;
        if (pick && pick.agent_id !== selected) onSelect(pick);
        // The chips: one request per agent that declares scenarios, in
        // parallel; an agent whose request fails simply has no chips.
        const loaded = await Promise.all(
          rows
            .filter((a) => a.has_scenarios)
            .map(async (a): Promise<[string, AgentScenario[]]> => {
              try {
                return [a.agent_id, await apiFetch<AgentScenario[]>(`/agents/${a.agent_id}/scenarios`, token)];
              } catch {
                return [a.agent_id, []];
              }
            }),
        );
        if (!alive) return;
        const next: Record<string, AgentScenario[]> = {};
        for (const [id, list] of loaded) next[id] = list;
        setSamples(next);
      })
      .catch((e) => {
        if (alive) setErr(e.message ?? "Failed to load agents");
      });
    return () => {
      alive = false;
    };
    // `selected` is deliberately not a dependency: the list is fetched
    // once, and re-fetching on every selection would re-run the
    // auto-select against the user's own choice.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, preselect]);

  if (err) {
    return (
      <div className="rounded border border-red-300 bg-red-50 p-4 text-sm text-red-800">
        Failed to load agents: {err}
      </div>
    );
  }
  if (!agents) {
    return <div className="p-4 text-sm text-slate-500">Loading agents…</div>;
  }
  if (agents.length === 0) {
    return (
      <div className="rounded border bg-amber-50 p-4 text-sm text-amber-900">
        No agents are registered. Check that at least one agent package
        loaded at server startup.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2" data-testid="agent-cards">
      {agents.map((a) => {
        const isSelected = a.agent_id === selected;
        const chips = samples[a.agent_id] ?? [];
        return (
          <div
            key={a.agent_id}
            data-testid="agent-card"
            data-agent-id={a.agent_id}
            data-selected={isSelected ? "true" : "false"}
            className={`flex flex-col rounded border transition ${
              isSelected
                ? "border-blue-600 bg-blue-50 ring-2 ring-blue-600"
                : "border-slate-300 bg-white hover:bg-slate-50"
            }`}
          >
            <button
              type="button"
              onClick={() => onSelect(a)}
              aria-pressed={isSelected}
              className="flex-1 p-4 text-left"
            >
              <div className="flex items-start justify-between gap-2">
                <h3 className="font-semibold">{a.display_name}</h3>
                <span
                  data-testid="framework-badge"
                  className="shrink-0 rounded-full border border-slate-300 bg-slate-100 px-2 py-0.5 font-mono text-[11px] text-slate-700"
                  title={a.framework ? "Framework, from the agent's manifest" : "Runtime"}
                >
                  {frameworkBadge(a)}
                </span>
              </div>
              <p className="mt-1 text-sm text-slate-600">{a.description}</p>
              <p className="mt-2 font-mono text-xs text-slate-400">{a.agent_id}</p>
            </button>
            {chips.length > 0 && (
              <div className="flex flex-wrap items-center gap-2 border-t border-slate-200 px-4 py-3">
                <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                  Try a sample
                </span>
                {chips.map((sc) => (
                  <button
                    key={sc.id}
                    type="button"
                    data-testid="sample-chip"
                    title={sc.description}
                    onClick={() => onSample(a, sc)}
                    className="rounded-full border border-blue-300 bg-white px-3 py-1 text-xs text-blue-700 hover:bg-blue-100"
                  >
                    {sc.name}
                  </button>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
