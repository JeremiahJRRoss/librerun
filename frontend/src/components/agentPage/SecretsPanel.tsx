"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { ApiError, apiFetch } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useToast } from "../../lib/toast";
import type { AgentSecretRow, AgentSecretState, AgentSecretsList, UserProfile } from "../../types";
import ScopeChip, { type Scope } from "../ScopeChip";
import type { AgentPageTabProps } from "./tabs";

/**
 * The agent page's Secrets tab (K8b; D29, D32, L31): the tool secrets an
 * agent's manifest declares in `secrets[]` — a search key, a vector-store
 * key — each with two rows, this tenant's value and every tenant's
 * default, and never a value.
 *
 * The server holds every answer: whether a row is set, its fingerprint,
 * who set it, when a run last read it, and where a run of this tenant
 * reads it now (`effective`). A value goes in by `PUT` and never comes
 * back out, so the field is never pre-filled, and it is emptied as its
 * request is sent: the page keeps no value after that. The default is
 * every tenant's, so its row is a platform admin's to write (D32); a
 * tenant admin sees it, with its controls closed.
 */
export function SecretsTab({ data }: AgentPageTabProps) {
  const { token } = useAuth();
  const { toast } = useToast();
  if (!token) return null;
  return <SecretsPanel agentId={data.agentId} user={data.user} token={token} toast={toast} />;
}

interface SecretsPanelProps {
  agentId: string;
  user: UserProfile;
  token: string;
  toast: (msg: string, kind?: "success" | "error" | "info") => void;
}

/** The API's two scopes (`…/secrets/{scope}/{name}`); a chip's scope is its label alone. */
type RowScope = "tenant" | "agent";

const ROWS: { scope: RowScope; chip: Scope; label: string }[] = [
  { scope: "tenant", chip: "agent_tenant", label: "this tenant" },
  { scope: "agent", chip: "agent", label: "the default" },
];

/** Whose value a row holds, in the words a toast and a confirmation use. */
const WHOSE: Record<RowScope, string> = { tenant: "this tenant's", agent: "the default" };

/** What a refusal said: the API answers `{detail, code}`. */
interface Refusal {
  status: number | null;
  code: string | null;
  detail: string;
}

function refusalOf(e: unknown): Refusal {
  if (!(e instanceof ApiError)) {
    return { status: null, code: null, detail: e instanceof Error ? e.message : String(e) };
  }
  try {
    const body = JSON.parse(e.message.replace(/^API \d+: /, "")) as { detail?: unknown; code?: unknown };
    return {
      status: e.status,
      code: typeof body.code === "string" ? body.code : null,
      detail: typeof body.detail === "string" ? body.detail : e.message,
    };
  } catch {
    return { status: e.status, code: null, detail: e.message };
  }
}

function when(iso: string | null | undefined): string | null {
  return iso ? new Date(iso).toLocaleString() : null;
}

/** The environment variable an in-process agent falls back to (K8a). */
function variableOf(name: string): string {
  return name.toUpperCase();
}

function effectiveText(state: AgentSecretState): string {
  switch (state.effective) {
    case "tenant":
      return "A run in this tenant reads this tenant's value.";
    case "agent":
      return "A run in this tenant reads the default.";
    case "environment":
      return `A run in this tenant reads it from the environment (${variableOf(state.name)}).`;
    default:
      return "Not set: a run in this tenant is told secret_not_set.";
  }
}

export function SecretsPanel({ agentId, user, token, toast }: SecretsPanelProps) {
  const [list, setList] = useState<AgentSecretsList | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  // What the admin is typing, per row: never pre-filled — the server has
  // nothing to give — and emptied the moment its request is sent.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  // A default row a write was refused on (403): closed, as for a tenant admin.
  const [refused, setRefused] = useState<Set<string>>(() => new Set());
  // The store's refusal (503): no value can be kept until an operator acts.
  const [store, setStore] = useState<Refusal | null>(null);
  // The agent and sign-in the panel shows now: a list read for another is dropped.
  const showing = useRef({ agentId, token });

  const read = useCallback(async () => {
    const answer = await apiFetch<AgentSecretsList>(`/agents/${agentId}/secrets`, token);
    const now = showing.current;
    if (now.agentId === agentId && now.token === token) {
      setList(answer);
      setFailed(null);
    }
  }, [agentId, token]);

  useEffect(() => {
    showing.current = { agentId, token };
    setList(null);
    setFailed(null);
    setDrafts({});
    setRefused(new Set());
    setStore(null);
    read().catch((e: unknown) => {
      const now = showing.current;
      if (now.agentId === agentId && now.token === token) setFailed(refusalOf(e).detail);
    });
  }, [agentId, token, read]);

  async function write(name: string, scope: RowScope, clear: boolean) {
    const key = `${scope}:${name}`;
    const value = drafts[key] ?? "";
    const whose = `${WHOSE[scope]} ${name}`;
    if (clear && !window.confirm(`Clear ${whose}? A run then reads the next source in the order.`)) {
      return;
    }
    // The value leaves the page with its request: the field is emptied
    // before it is sent (flushSync commits that first), whatever the
    // answer, and nothing here keeps it after.
    flushSync(() => {
      setDrafts((previous) => ({ ...previous, [key]: "" }));
      setBusy(key);
    });
    try {
      await apiFetch(
        `/agents/${agentId}/secrets/${scope}/${name}`,
        token,
        clear ? { method: "DELETE" } : { method: "PUT", body: JSON.stringify({ value }) },
      );
      setStore(null);
      toast(clear ? `Cleared ${whose}` : `Saved ${whose}`, "success");
    } catch (e: unknown) {
      const refusal = refusalOf(e);
      if (refusal.status === 403 && scope === "agent") {
        setRefused((previous) => new Set(previous).add(key));
      } else if (refusal.status === 503) {
        setStore(refusal);
      } else {
        toast(refusal.detail, "error");
      }
    } finally {
      setBusy(null);
    }
    await read().catch((e: unknown) => toast(refusalOf(e).detail, "error"));
  }

  if (failed) {
    return (
      <div className="rounded border border-red-300 bg-red-50 p-4 text-sm text-red-800">
        The secrets could not be read: {failed}
      </div>
    );
  }
  if (!list) return <div className="p-4 text-sm text-slate-500">Loading…</div>;
  const container = list.runtime === "container";

  return (
    <section>
      <h2 className="mb-1 text-lg font-semibold">Tool secrets</h2>
      <p className="mb-3 text-sm text-slate-600">
        The keys this agent&apos;s manifest names for the services it calls. A value is never shown
        again once set: each row says whether it is set, its fingerprint, who set it and when a run
        last read it. A run reads this tenant&apos;s value, else the default
        {container ? "." : ", else the upper-cased name in the backend's environment."}
      </p>
      {store && <StoreNote refusal={store} />}
      {container && (
        <p role="note" className="mb-3 text-xs text-slate-600">
          This agent runs in a container: its own environment is invisible to the platform, so a
          value it keeps there is neither read nor shown here.
        </p>
      )}
      <ul className="space-y-4">
        {list.secrets.map((state) => (
          <li key={state.name} data-secret={state.name} className="rounded border bg-white p-4">
            <h3 className="font-mono text-sm font-semibold">{state.name}</h3>
            <p data-effective={state.effective} className="mb-3 text-xs text-slate-600">
              {effectiveText(state)}
            </p>
            <div className="space-y-3">
              {ROWS.map(({ scope, chip, label }) => {
                const key = `${scope}:${state.name}`;
                const closed = scope === "agent" && (!user.is_platform_admin || refused.has(key));
                return (
                  <SecretRowView
                    key={scope}
                    name={state.name}
                    row={state[scope]}
                    chip={chip}
                    label={label}
                    me={user.id}
                    draft={drafts[key] ?? ""}
                    busy={busy === key}
                    closed={closed}
                    onDraft={(next) => setDrafts((previous) => ({ ...previous, [key]: next }))}
                    onSet={() => void write(state.name, scope, false)}
                    onClear={() => void write(state.name, scope, true)}
                  />
                );
              })}
              {state.environment && (
                <p data-environment={state.environment.set ? "set" : "unset"} className="text-xs text-slate-600">
                  <code>{variableOf(state.name)}</code> in the backend&apos;s environment:{" "}
                  {state.environment.set
                    ? "set — from the environment, when neither row is set."
                    : "not set."}
                </p>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function SecretRowView({
  name,
  row,
  chip,
  label,
  me,
  draft,
  busy,
  closed,
  onDraft,
  onSet,
  onClear,
}: {
  name: string;
  row: AgentSecretRow;
  chip: Scope;
  label: string;
  me: string;
  draft: string;
  busy: boolean;
  closed: boolean;
  onDraft: (next: string) => void;
  onSet: () => void;
  onClear: () => void;
}) {
  const by = row.updated_by ? (row.updated_by === me ? "you" : `user ${row.updated_by}`) : null;
  const setAt = when(row.updated_at);
  const usedAt = when(row.last_used_at);
  return (
    <div data-scope-region={chip} data-row-set={row.set ? "set" : "unset"} className="rounded border p-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <ScopeChip scope={chip} />
        <span className="font-medium">{row.set ? "set" : "not set"}</span>
        {row.set && row.fingerprint && (
          <span data-fingerprint className="font-mono text-xs text-slate-600">
            {row.fingerprint}
          </span>
        )}
        {row.set && !row.fingerprint && (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-900">
            unreadable — replace or clear
          </span>
        )}
      </div>
      {row.set && (
        <p className="mt-1 text-xs text-slate-500">
          {`set${by ? ` by ${by}` : ""}${setAt ? `, ${setAt}` : ""}`} · last used by a run:{" "}
          {usedAt ?? "never"}
        </p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <input
          type="password"
          autoComplete="new-password"
          aria-label={`New value for ${name} (${label})`}
          value={draft}
          disabled={closed || busy}
          onChange={(e) => onDraft(e.target.value)}
          placeholder={row.set ? "A new value, to replace it" : "The value, to set it"}
          className="w-64 rounded border px-2 py-1 text-sm disabled:bg-slate-100"
        />
        <button
          type="button"
          onClick={onSet}
          disabled={closed || busy || !draft.trim()}
          className="rounded bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-50"
        >
          {busy ? "Saving…" : row.set ? "Replace" : "Set"}
        </button>
        {row.set && (
          <button
            type="button"
            onClick={onClear}
            disabled={closed || busy}
            className="rounded border px-3 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
          >
            Clear
          </button>
        )}
      </div>
      {closed && <p className="mt-1 text-xs text-slate-600">Platform operators only.</p>}
    </div>
  );
}

function StoreNote({ refusal }: { refusal: Refusal }) {
  const shared = refusal.code === "secrets_store_key_shared";
  return (
    <div
      role="alert"
      data-store-refusal={refusal.code ?? ""}
      className="mb-3 rounded border border-amber-400 bg-amber-50 p-3 text-sm text-amber-900"
    >
      <strong>No value can be kept here yet</strong> ({refusal.code ?? `HTTP ${refusal.status}`}).{" "}
      {shared
        ? "The backend's store key is also the gateway's, and a value is sealed only once they differ: docs/platform/Install.md, “The gateway's store key”, says how to give the gateway a key of its own."
        : "The backend has no store key, LIBRERUN_BACKEND_SECRETS_KEY, to seal a value with: docs/platform/Install.md, “The secrets store key”, says how to add one."}
    </div>
  );
}
