"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { settingReset, settingUpdates } from "../../lib/settingsPayload";
import { useToast } from "../../lib/toast";
import type { AgentSettingSpec, AgentSettingValue } from "../../types";
import ScopeChip from "../ScopeChip";
import type { AgentPageTabProps } from "./tabs";

/**
 * The agent page's Settings tab (K5a, L32): the settings an agent's
 * manifest declares (`settings[]`), each with THIS tenant's value.
 *
 * Any agent that declares settings gets it — a container included, with
 * no code of its own. The server is the authority on what a value is: it
 * holds each one to its declared type, stores nothing for a value equal
 * to the default (D17) and says which values are this tenant's choice
 * (`overridden`), so the tab re-reads the config after every write rather
 * than trusting what it posted.
 *
 * An agent still on the deprecated `config_meta()` path (`meta.deprecated`)
 * is served here too, but its values are the agent's own — one for every
 * tenant — so the tab says that instead of showing a tenant scope.
 */
export function SettingsTab({ data, reload }: AgentPageTabProps) {
  const { token } = useAuth();
  const { toast } = useToast();
  const config = data.config;
  if (!config || !token) return null;
  return (
    <SettingsPanel
      agentId={data.agentId}
      specs={config.meta.settings}
      settings={config.settings}
      deprecated={config.meta.deprecated}
      token={token}
      reload={reload}
      toast={toast}
    />
  );
}

interface SettingsPanelProps {
  agentId: string;
  specs: AgentSettingSpec[];
  settings: AgentSettingValue[];
  deprecated: boolean;
  token: string;
  reload: () => Promise<void>;
  toast: (msg: string, kind?: "success" | "error" | "info") => void;
}

function valuesOf(settings: AgentSettingValue[]): Record<string, unknown> {
  return Object.fromEntries(settings.map((s) => [s.key, s.value]));
}

export function SettingsPanel({
  agentId,
  specs,
  settings,
  deprecated,
  token,
  reload,
  toast,
}: SettingsPanelProps) {
  const [values, setValues] = useState<Record<string, unknown>>(() => valuesOf(settings));
  const [saving, setSaving] = useState(false);
  // The fields edited since the server last answered. A re-read after a
  // one-field Reset refreshes every other field from the server, and must
  // not throw away an edit that has not been saved yet.
  const edited = useRef<Set<string>>(new Set());

  useEffect(() => {
    const fresh = valuesOf(settings);
    setValues((previous) => {
      const next = { ...fresh };
      for (const key of edited.current) {
        if (key in previous) next[key] = previous[key];
      }
      return next;
    });
  }, [settings]);

  if (specs.length === 0) return null;
  const byKey = new Map(settings.map((s) => [s.key, s]));

  function update(key: string, value: unknown) {
    edited.current.add(key);
    setValues((previous) => ({ ...previous, [key]: value }));
  }

  async function put(body: unknown, done: string, clear: string[]) {
    setSaving(true);
    try {
      await apiFetch(`/agents/${agentId}/config/settings`, token, {
        method: "PUT",
        body: JSON.stringify(body),
      });
      for (const key of clear) edited.current.delete(key);
      await reload();
      toast(done, "success");
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : "Save failed", "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section data-scope-region="agent-settings">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="text-lg font-semibold">Agent settings</h2>
        {!deprecated && <ScopeChip scope="agent_tenant" />}
      </div>
      {deprecated && (
        <p
          role="note"
          className="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900"
        >
          One value for every tenant: these settings are kept by the agent
          itself, through a surface that is deprecated and goes at v1.2, until
          this agent declares <code>settings[]</code> in its manifest. A change
          saved here changes it for every tenant.
        </p>
      )}
      <div className="space-y-4 rounded border bg-white p-4">
        {specs.map((spec) => {
          const current = byKey.get(spec.key);
          return (
            <div key={spec.key} className="space-y-1">
              <SettingField
                spec={spec}
                value={values[spec.key]}
                onChange={(next) => update(spec.key, next)}
              />
              {current?.overridden && (
                <div className="flex items-center gap-3 text-xs">
                  <span className="text-blue-700">
                    overridden here (the agent&apos;s default is{" "}
                    <code>{JSON.stringify(spec.default)}</code>)
                  </span>
                  <button
                    type="button"
                    disabled={saving}
                    onClick={() =>
                      void put(
                        settingReset(spec.key),
                        `${spec.label} is back to the agent's default`,
                        [spec.key],
                      )
                    }
                    className="rounded border px-2 py-0.5 text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                  >
                    Reset
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>
      <button
        type="button"
        onClick={() =>
          void put(
            settingUpdates(specs, values),
            "Settings saved",
            specs.map((s) => s.key),
          )
        }
        disabled={saving}
        className="mt-3 rounded bg-blue-600 px-4 py-2 text-white disabled:opacity-50"
      >
        {saving ? "Saving…" : "Save settings"}
      </button>
    </section>
  );
}

function SettingField({
  spec,
  value,
  onChange,
}: {
  spec: AgentSettingSpec;
  value: unknown;
  onChange: (next: unknown) => void;
}) {
  const id = `setting-${spec.key}`;
  const label = (
    <div>
      <div className="text-sm font-medium">{spec.label}</div>
      {spec.description && <div className="text-xs text-slate-500">{spec.description}</div>}
    </div>
  );

  if (spec.type === "bool") {
    return (
      <label htmlFor={id} className="flex items-start gap-3">
        <input
          id={id}
          type="checkbox"
          className="mt-0.5 h-4 w-4"
          checked={value === true}
          onChange={(e) => onChange(e.target.checked)}
        />
        {label}
      </label>
    );
  }

  if (spec.type === "enum") {
    const options = spec.options ?? [];
    return (
      <label htmlFor={id} className="block">
        {label}
        <select
          id={id}
          className="mt-1 rounded border px-2 py-1 text-sm"
          value={typeof value === "string" ? value : ""}
          onChange={(e) => onChange(e.target.value)}
        >
          {options.map((opt) => (
            <option key={opt} value={opt}>
              {opt}
            </option>
          ))}
        </select>
      </label>
    );
  }

  if (spec.type === "int" || spec.type === "float") {
    return (
      <label htmlFor={id} className="block">
        {label}
        <input
          id={id}
          type="number"
          step={spec.type === "float" ? "any" : 1}
          className="mt-1 w-32 rounded border px-2 py-1 text-sm"
          value={typeof value === "number" ? value : ""}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") {
              onChange(null);
              return;
            }
            const n = spec.type === "float" ? parseFloat(raw) : parseInt(raw, 10);
            onChange(Number.isFinite(n) ? n : null);
          }}
        />
      </label>
    );
  }

  if (spec.type === "string_list") {
    const list = Array.isArray(value) ? (value as string[]) : [];
    return (
      <label htmlFor={id} className="block">
        {label}
        <input
          id={id}
          type="text"
          className="mt-1 w-full rounded border px-2 py-1 text-sm"
          value={list.join(", ")}
          onChange={(e) =>
            onChange(
              e.target.value
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            )
          }
          placeholder="comma, separated, values"
        />
      </label>
    );
  }

  return (
    <label htmlFor={id} className="block">
      {label}
      <input
        id={id}
        type="text"
        className="mt-1 w-full rounded border px-2 py-1 text-sm"
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}
