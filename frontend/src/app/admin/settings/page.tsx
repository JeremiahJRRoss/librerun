"use client";

import { useCallback, useEffect, useState } from "react";
import CertificatesPanel from "../../../components/CertificatesPanel";
import DeploymentPanel from "../../../components/DeploymentPanel";
import ModelProviders from "../../../components/ModelProviders";
import NavBar from "../../../components/NavBar";
import PlatformOnly from "../../../components/PlatformOnly";
import ScopeChip from "../../../components/ScopeChip";
import { ApiError, apiFetch } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { useToast } from "../../../lib/toast";

type SettingValueType = "string_list" | "int" | "bool" | "string" | "secret";

// What the API shows of a secret setting (K6, L31): where its value comes
// from and a fingerprint. Never the value — the row's `value` and
// `default_value` are null for a secret, and this page never asks.
interface SecretState {
  set: boolean;
  source: "runtime" | "env" | "unset" | "unreadable";
  fingerprint: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

interface SettingRow {
  key: string;
  value: unknown;
  default_value: unknown;
  value_type: SettingValueType;
  description: string;
  is_default: boolean;
  updated_at: string | null;
  updated_by: string | null;
  secret?: SecretState | null;
}

function secretChip(secret: SecretState | null | undefined): { text: string; tone: string } {
  switch (secret?.source) {
    case "runtime":
      return { text: `runtime · ${secret.fingerprint ?? ""}`, tone: "bg-emerald-100 text-emerald-800" };
    case "env":
      return { text: "from the environment", tone: "bg-slate-200 text-slate-700" };
    case "unreadable":
      return { text: "unreadable — replace or clear", tone: "bg-amber-100 text-amber-900" };
    default:
      return { text: "not set", tone: "bg-slate-200 text-slate-700" };
  }
}

interface HealthResponse {
  status: string;
  env: string;
  cors_restart_required?: boolean;
}

function TagListInput({
  value,
  onChange,
  placeholder,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const trimmed = draft.trim();
    if (!trimmed || value.includes(trimmed)) {
      setDraft("");
      return;
    }
    onChange([...value, trimmed]);
    setDraft("");
  };
  return (
    <div>
      <div className="flex flex-wrap gap-1">
        {value.map((v, i) => (
          <span
            key={`${v}-${i}`}
            className="inline-flex items-center gap-1 rounded bg-slate-200 px-2 py-0.5 text-xs"
          >
            {v}
            <button
              type="button"
              onClick={() => onChange(value.filter((_, idx) => idx !== i))}
              className="text-slate-500 hover:text-red-600"
              aria-label={`Remove ${v}`}
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <div className="mt-1 flex gap-1">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              add();
            }
          }}
          placeholder={placeholder}
          className="flex-1 rounded border px-2 py-1 text-xs"
        />
        <button
          type="button"
          onClick={add}
          className="rounded border px-2 py-1 text-xs hover:bg-slate-50"
        >
          Add
        </button>
      </div>
    </div>
  );
}

function formatValue(v: unknown): string {
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "boolean") return v ? "true" : "false";
  return v == null ? "" : String(v);
}

export default function AdminSettingsPage() {
  const { token } = useAuth();
  const { toast } = useToast();
  const [rows, setRows] = useState<SettingRow[]>([]);
  // "forbidden" is a first-class state, not an error: these settings are
  // application-global and gated to platform operators (admins of the
  // platform tenant), so an ordinary tenant admin lands here legitimately
  // and must see an explanation — not an eternal "Loading settings…".
  const [access, setAccess] = useState<"loading" | "ready" | "forbidden" | "error">(
    "loading"
  );
  const [drafts, setDrafts] = useState<Record<string, unknown>>({});
  // A secret's input: what the admin is typing, never pre-filled from the
  // server (which has nothing to give) and emptied after every Set,
  // Replace or Clear, so a value does not linger in the page.
  const [secretDrafts, setSecretDrafts] = useState<Record<string, string>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [restartRequired, setRestartRequired] = useState(false);

  const load = useCallback(async () => {
    try {
      const list = await apiFetch<SettingRow[]>("/admin/settings", token);
      setRows(list);
      const nextDrafts: Record<string, unknown> = {};
      for (const r of list) {
        if (r.value_type !== "secret") nextDrafts[r.key] = r.value;
      }
      setDrafts(nextDrafts);
      setAccess("ready");
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) {
        setAccess("forbidden");
      } else {
        setAccess("error");
        toast(e instanceof Error ? e.message : "Failed to load settings", "error");
      }
    }
    try {
      const h = await apiFetch<HealthResponse>("/health", token);
      setRestartRequired(!!h.cors_restart_required);
    } catch {
      // health endpoint failure is non-fatal
    }
  }, [token, toast]);

  useEffect(() => {
    load();
  }, [load]);

  async function save(row: SettingRow) {
    setSavingKey(row.key);
    try {
      const updated = await apiFetch<SettingRow>(`/admin/settings/${row.key}`, token, {
        method: "PUT",
        body: JSON.stringify({ value: drafts[row.key] }),
      });
      setRows((prev) => prev.map((r) => (r.key === row.key ? updated : r)));
      toast(`Saved ${row.key}`, "success");
      if (row.key === "cors_origins") setRestartRequired(true);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Save failed", "error");
    } finally {
      setSavingKey(null);
    }
  }

  async function reset(row: SettingRow) {
    if (!confirm(`Reset ${row.key} to its default?`)) return;
    setSavingKey(row.key);
    try {
      const updated = await apiFetch<SettingRow>(
        `/admin/settings/reset/${row.key}`,
        token,
        { method: "POST" }
      );
      setRows((prev) => prev.map((r) => (r.key === row.key ? updated : r)));
      setDrafts((prev) => ({ ...prev, [row.key]: updated.value }));
      toast(`Reset ${row.key}`, "success");
      if (row.key === "cors_origins") setRestartRequired(true);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Reset failed", "error");
    } finally {
      setSavingKey(null);
    }
  }

  async function saveSecret(row: SettingRow) {
    const value = secretDrafts[row.key] ?? "";
    setSavingKey(row.key);
    try {
      const updated = await apiFetch<SettingRow>(`/admin/settings/${row.key}`, token, {
        method: "PUT",
        body: JSON.stringify({ value }),
      });
      setRows((prev) => prev.map((r) => (r.key === row.key ? updated : r)));
      toast(`${row.secret?.set ? "Replaced" : "Set"} ${row.key}`, "success");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Save failed", "error");
    } finally {
      setSecretDrafts((prev) => ({ ...prev, [row.key]: "" }));
      setSavingKey(null);
    }
  }

  async function clearSecret(row: SettingRow) {
    if (!confirm(`Clear ${row.key}? The environment's value applies again.`)) return;
    setSavingKey(row.key);
    try {
      const updated = await apiFetch<SettingRow>(
        `/admin/settings/reset/${row.key}`,
        token,
        { method: "POST" }
      );
      setRows((prev) => prev.map((r) => (r.key === row.key ? updated : r)));
      toast(`Cleared ${row.key}`, "success");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Clear failed", "error");
    } finally {
      setSecretDrafts((prev) => ({ ...prev, [row.key]: "" }));
      setSavingKey(null);
    }
  }

  function renderSecretEditor(row: SettingRow) {
    const draft = secretDrafts[row.key] ?? "";
    const busy = savingKey === row.key;
    return (
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="password"
          autoComplete="new-password"
          aria-label={`New value for ${row.key}`}
          value={draft}
          onChange={(e) => setSecretDrafts((prev) => ({ ...prev, [row.key]: e.target.value }))}
          placeholder={row.secret?.set ? "A new value, to replace it" : "The value, to set it"}
          className="w-64 rounded border px-2 py-1 text-sm"
        />
        <button
          onClick={() => saveSecret(row)}
          disabled={busy || !draft.trim()}
          className="rounded bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-50"
        >
          {busy ? "Saving…" : row.secret?.set ? "Replace" : "Set"}
        </button>
        {row.secret?.set && (
          <button
            onClick={() => clearSecret(row)}
            disabled={busy}
            className="rounded border px-3 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
          >
            Clear
          </button>
        )}
      </div>
    );
  }

  function renderEditor(row: SettingRow) {
    const value = drafts[row.key];
    const setValue = (v: unknown) =>
      setDrafts((prev) => ({ ...prev, [row.key]: v }));
    switch (row.value_type) {
      case "bool":
        return (
          <label className="inline-flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={!!value}
              onChange={(e) => setValue(e.target.checked)}
            />
            <span>{value ? "Enabled" : "Disabled"}</span>
          </label>
        );
      case "int":
        return (
          <input
            type="number"
            value={value == null ? "" : String(value)}
            onChange={(e) =>
              setValue(e.target.value === "" ? null : parseInt(e.target.value, 10))
            }
            className="w-32 rounded border px-2 py-1 text-sm"
          />
        );
      case "string_list":
        return (
          <TagListInput
            value={Array.isArray(value) ? (value as string[]) : []}
            onChange={setValue}
            placeholder="e.g., https://staging.example.com"
          />
        );
      default:
        return (
          <input
            type="text"
            value={value == null ? "" : String(value)}
            onChange={(e) => setValue(e.target.value)}
            className="w-64 rounded border px-2 py-1 text-sm"
          />
        );
    }
  }

  return (
    <div>
      <NavBar />
      <main className="mx-auto max-w-4xl p-6">
        <h1 className="mb-4 text-2xl font-bold">Application Settings</h1>

        {access === "forbidden" && (
          <PlatformOnly reason="These settings are application-global — every tenant reads them — so they are managed by an admin of the platform tenant." />
        )}

        {access === "error" && (
          <div className="rounded border border-red-300 bg-red-50 p-4 text-sm text-red-900">
            Failed to load settings.{" "}
            <button className="underline" onClick={() => load()}>
              Retry
            </button>
          </div>
        )}

        {restartRequired && (
          <div className="mb-4 rounded border border-amber-400 bg-amber-50 p-3 text-sm text-amber-900">
            <strong>Restart required.</strong> CORS origins were changed; a
            rolling restart is needed before the new list takes effect.
          </div>
        )}

        {/* K7: the provider keys, sealed in this browser to the gateway.
            Platform admins only, like everything on this page. */}
        {access === "ready" && <ModelProviders token={token} toast={toast} />}

        {/* K9: the deployment as the backend reads it, read-only; its
            transport row links to the Certificates panel below (D43). */}
        {access === "ready" && <DeploymentPanel token={token} />}

        {/* T2: what the HTTPS edge serves and needs, and the certificate
            choices — a platform admin's, like everything on this page. */}
        {access === "ready" && <CertificatesPanel token={token} toast={toast} />}

        {(access === "loading" || access === "ready") && (
        <div className="space-y-4" data-scope-region="settings">
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-semibold">Settings</h2>
            <ScopeChip scope="platform" />
          </div>
          {rows.map((row) => (
            <div key={row.key} className="rounded border bg-white p-4">
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <h3 className="font-semibold">{row.key}</h3>
                    {row.value_type === "secret" ? (
                      <span
                        data-testid={`secret-source-${row.key}`}
                        className={`rounded px-1.5 py-0.5 text-xs ${secretChip(row.secret).tone}`}
                      >
                        {secretChip(row.secret).text}
                      </span>
                    ) : (
                    <span
                      className={`rounded px-1.5 py-0.5 text-xs ${
                        row.is_default
                          ? "bg-slate-200 text-slate-700"
                          : "bg-blue-100 text-blue-800"
                      }`}
                    >
                      {row.is_default ? "default" : "override"}
                    </span>
                    )}
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-600">
                      {row.value_type}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-slate-600">{row.description}</p>
                  {row.value_type !== "secret" && (
                  <p className="mt-1 text-xs text-slate-400">
                    Default: <span className="font-mono">{formatValue(row.default_value)}</span>
                  </p>
                  )}
                  {!row.is_default && row.updated_at && (
                    <p className="mt-1 text-xs text-slate-400">
                      Updated {new Date(row.updated_at).toLocaleString()}
                      {row.updated_by ? ` by ${row.updated_by}` : ""}
                    </p>
                  )}
                </div>
                {row.value_type !== "secret" && (
                <div className="flex flex-col items-end gap-2">
                  <button
                    onClick={() => save(row)}
                    disabled={savingKey === row.key}
                    className="rounded bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-50"
                  >
                    {savingKey === row.key ? "Saving…" : "Save"}
                  </button>
                  {!row.is_default && (
                    <button
                      onClick={() => reset(row)}
                      disabled={savingKey === row.key}
                      className="rounded border px-3 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                    >
                      Reset to default
                    </button>
                  )}
                </div>
                )}
              </div>
              <div className="mt-3">
                {row.value_type === "secret" ? renderSecretEditor(row) : renderEditor(row)}
              </div>
            </div>
          ))}
          {access === "loading" && rows.length === 0 && (
            <p className="text-sm text-slate-500">Loading settings…</p>
          )}
        </div>
        )}
      </main>
    </div>
  );
}
