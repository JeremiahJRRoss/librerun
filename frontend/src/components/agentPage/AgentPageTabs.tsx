"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiFetch } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import type { AgentConfigResponse } from "../../types";
import { AGENT_PAGE_TABS, type AgentPageData, type AgentPageTab } from "./tabs";

/**
 * The config read's two 404s (K9): an id no agent in this backend is
 * registered under ("Unknown agent: …"), and a registered agent that
 * declares nothing to configure. The first is not "nothing to configure":
 * a platform admin may be here to issue its key ahead of it.
 */
function unregistered(e: ApiError): boolean {
  try {
    const body = JSON.parse(e.message.replace(/^API \d+: /, "")) as { detail?: unknown };
    return typeof body.detail === "string" && body.detail.startsWith("Unknown agent");
  } catch {
    return false;
  }
}

interface Props {
  agentId: string;
  /** The tab registry; the page passes AGENT_PAGE_TABS. A prop, so a test
   * or a later batch can hand the shell tabs of its own. */
  tabs?: AgentPageTab[];
}

/**
 * The agent page (K4b, D30): a tab shell for every registered agent,
 * whether or not it has anything to configure yet.
 *
 * The config is loaded once, here, and handed to every tab. Every
 * available panel stays mounted and only the chosen one is shown, so
 * moving between tabs never discards an edit that was not saved.
 */
export default function AgentPageTabs({ agentId, tabs = AGENT_PAGE_TABS }: Props) {
  const { token, user } = useAuth();
  const [config, setConfig] = useState<AgentConfigResponse | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "missing" | "unregistered" | "error">(
    "loading",
  );
  const [err, setErr] = useState<string | null>(null);
  const [chosen, setChosen] = useState<string | null>(null);
  // The agent and sign-in the page shows now. A panel's reload answers for
  // the agent it was made for, and is dropped if the page has moved on
  // since: that answer would reach the next agent's panels, which would
  // save the first agent's values under the second one's id.
  const showing = useRef({ agentId, token });

  useEffect(() => {
    showing.current = { agentId, token };
    if (!token) return;
    let alive = true;
    // Another agent, or another sign-in: nothing of the previous one may
    // stay on the page, or its panels would save their values under this
    // agent's id while its own config loads (or after it answers 404).
    setConfig(null);
    setErr(null);
    setChosen(null);
    setState("loading");
    apiFetch<AgentConfigResponse>(`/agents/${agentId}/config`, token)
      .then((cfg) => {
        if (!alive) return;
        setConfig(cfg);
        setState("ready");
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setConfig(null);
        if (e instanceof ApiError && e.status === 404) {
          setState(unregistered(e) ? "unregistered" : "missing");
        } else {
          setErr(e instanceof Error ? e.message : String(e));
          setState("error");
        }
      });
    return () => {
      alive = false;
    };
  }, [agentId, token]);

  const reload = useCallback(async () => {
    if (!token) return;
    const cfg = await apiFetch<AgentConfigResponse>(`/agents/${agentId}/config`, token);
    const now = showing.current;
    if (now.agentId === agentId && now.token === token) setConfig(cfg);
  }, [agentId, token]);

  if (!user) return null;
  if (state === "loading") {
    return <div className="p-6 text-sm text-slate-500">Loading…</div>;
  }
  if (state === "error") {
    return (
      <div className="rounded border border-red-300 bg-red-50 p-6 text-sm text-red-800">{err}</div>
    );
  }

  const data: AgentPageData = { agentId, user, config };
  const shown = tabs.filter((tab) => tab.available(data));
  const current = shown.find((tab) => tab.id === chosen) ?? shown[0];

  return (
    <div>
      {state === "unregistered" && (
        <div
          data-testid="agent-unregistered"
          className="mb-6 rounded border border-amber-300 bg-amber-50 p-6 text-sm text-amber-900"
        >
          No agent is registered under this id in this backend.
          {shown.length > 0 && " A key can still be issued for it here, ahead of its agent."}
        </div>
      )}
      {state === "missing" && (
        // What the agent declares is the config GET's to say, whichever
        // tabs show: a platform admin also has the Keys tab, which is the
        // platform's, so "nothing to configure" is said only when no tab is.
        <div
          data-testid="agent-declares-nothing"
          className="mb-6 rounded border bg-slate-50 p-6 text-sm text-slate-700"
        >
          This agent declares no LLM steps and no settings
          {shown.length === 0
            ? ", so there is nothing to configure for it here."
            : "; the tab below is the platform's, not a configuration it declares."}
        </div>
      )}
      {current && (
        <>
          <div role="tablist" aria-label="Agent configuration" className="mb-6 flex gap-1 border-b">
            {shown.map((tab) => {
              const selected = tab.id === current.id;
              return (
                <button
                  key={tab.id}
                  id={`agent-tab-${tab.id}`}
                  role="tab"
                  type="button"
                  aria-selected={selected}
                  aria-controls={`agent-panel-${tab.id}`}
                  onClick={() => setChosen(tab.id)}
                  className={`-mb-px border-b-2 px-4 py-2 text-sm ${
                    selected
                      ? "border-blue-600 font-semibold text-blue-700"
                      : "border-transparent text-slate-600 hover:text-slate-900"
                  }`}
                >
                  {tab.label}
                </button>
              );
            })}
          </div>
          {shown.map((tab) => {
            const Panel = tab.component;
            return (
              <div
                key={tab.id}
                id={`agent-panel-${tab.id}`}
                role="tabpanel"
                aria-labelledby={`agent-tab-${tab.id}`}
                hidden={tab.id !== current.id}
              >
                <Panel data={data} reload={reload} />
              </div>
            );
          })}
        </>
      )}
    </div>
  );
}
