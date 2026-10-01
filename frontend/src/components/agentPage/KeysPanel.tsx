"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiFetch } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useToast } from "../../lib/toast";
import type { AgentKeyRow, IssuedAgentKey, RotatedAgentKey } from "../../types";
import PlatformOnly from "../PlatformOnly";
import ScopeChip from "../ScopeChip";
import type { AgentPageTabProps } from "./tabs";

/**
 * The agent page's Keys tab (K9-01, K9-02; D10, D29): this agent's gateway
 * keys, for a platform admin.
 *
 * A key names an agent, not a tenant, so it is the platform's (the routes
 * answer 403 to anyone else). An admin key is issued here when the agent
 * has none, rotated with a grace window the old key keeps working for, or
 * revoked; a key from the environment is rotated in `.env`, so its row
 * offers neither. A new value is shown once, read-only, with Copy, and is
 * held only until Done, the next read or the page going away: it is
 * stored nowhere, and a reload finds only its prefix.
 */
export function KeysTab({ data }: AgentPageTabProps) {
  const { token } = useAuth();
  const { toast } = useToast();
  if (!token) return null;
  return <KeysPanel agentId={data.agentId} token={token} toast={toast} />;
}

interface KeysPanelProps {
  agentId: string;
  token: string;
  toast: (msg: string, kind?: "success" | "error" | "info") => void;
}

const GRACE_DEFAULT = 24;
const GRACE_MAX = 720;

function when(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : "never";
}

function detailOf(e: unknown): string {
  if (!(e instanceof ApiError)) return e instanceof Error ? e.message : String(e);
  try {
    const body = JSON.parse(e.message.replace(/^API \d+: /, "")) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
  } catch {
    // not JSON: the message as it is
  }
  return e.message;
}

export function KeysPanel({ agentId, token, toast }: KeysPanelProps) {
  const [rows, setRows] = useState<AgentKeyRow[] | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "forbidden" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  // A value just minted: shown once, never stored, gone at Done, the next
  // read or unmount.
  const [minted, setMinted] = useState<IssuedAgentKey | null>(null);
  const [grace, setGrace] = useState<string>(String(GRACE_DEFAULT));
  const [busy, setBusy] = useState(false);
  const valueRef = useRef<HTMLInputElement | null>(null);
  const showing = useRef({ agentId, token });
  // Each read's number: only the last one started may set the rows, so an
  // older answer landing late never puts back a key a newer read had gone
  // past (a rotated or revoked one shown as current).
  const reads = useRef(0);

  // A read of this agent's keys. `keep` is for the refresh after an issue
  // or a rotation, which must not take away the one copy of the value just
  // shown; every other read starts without a value on the page (the
  // reset): the one a mint showed is gone, and only its prefix comes back.
  const read = useCallback(async (keep: boolean) => {
    if (!keep) setMinted(null);
    const mine = ++reads.current;
    const current = () => {
      const now = showing.current;
      return now.agentId === agentId && now.token === token && reads.current === mine;
    };
    try {
      const answer = await apiFetch<AgentKeyRow[]>(
        `/admin/agent-keys?agent_id=${encodeURIComponent(agentId)}`,
        token,
      );
      if (!current()) return;
      if (!Array.isArray(answer)) throw new Error("not a list of keys");
      setRows(answer);
      setState("ready");
      setError(null);
    } catch (e) {
      if (!current()) return;
      if (e instanceof ApiError && e.status === 403) {
        setState("forbidden");
      } else {
        setError(detailOf(e));
        setState("error");
      }
    }
  }, [agentId, token]);

  const load = useCallback(() => read(false), [read]);

  // A value is shown the moment its mint answers, before the rows are read
  // again: that read may be slow or never answer, and the value exists
  // nowhere else. It is shown only on the page it was minted for. The
  // actions stay disabled until the rows have caught up (the caller awaits
  // this), so no rotate or revoke starts from rows the mint made stale.
  async function show(answer: IssuedAgentKey, done: string) {
    const now = showing.current;
    if (now.agentId !== agentId || now.token !== token) {
      toast(`A key was minted for ${agentId} after the page moved on; rotate it there to see a value.`, "error");
      return;
    }
    setMinted(answer);
    toast(done, "success");
    await read(true);
  }

  useEffect(() => {
    showing.current = { agentId, token };
    setRows(null);
    setState("loading");
    void load();
    return () => {
      // Unmounting, or another agent: no value outlives the page it was
      // minted on, and an answer still on its way belongs to no page.
      showing.current = { agentId: "", token: "" };
      setMinted(null);
    };
  }, [agentId, token, load]);

  async function issue() {
    setBusy(true);
    try {
      const answer = await apiFetch<IssuedAgentKey>(
        `/admin/agent-keys/${encodeURIComponent(agentId)}`,
        token,
        { method: "POST" },
      );
      await show(answer, "Key issued. Copy it now: it is shown once.");
    } catch (e) {
      toast(detailOf(e), "error");
    } finally {
      setBusy(false);
    }
  }

  async function rotate() {
    // The whole field, digits only: parseInt would read "1.5" or "1e2" as
    // 1, and the previous key would stop working far sooner than typed.
    const typed = grace.trim();
    const hours = /^[0-9]+$/.test(typed) ? Number(typed) : Number.NaN;
    if (!Number.isInteger(hours) || hours < 0 || hours > GRACE_MAX) {
      toast(`The grace window is a whole number of hours, 0 to ${GRACE_MAX}.`, "error");
      return;
    }
    setBusy(true);
    try {
      const answer = await apiFetch<RotatedAgentKey>(
        `/admin/agent-keys/${encodeURIComponent(agentId)}/rotate?grace_hours=${hours}`,
        token,
        { method: "POST" },
      );
      await show(answer, `Key rotated; the previous one works for ${answer.grace_hours} hour(s).`);
    } catch (e) {
      toast(detailOf(e), "error");
    } finally {
      setBusy(false);
    }
  }

  async function revoke() {
    if (!window.confirm(`Revoke every key issued here for ${agentId}? Its container stops authenticating.`)) {
      return;
    }
    setBusy(true);
    try {
      await apiFetch(`/admin/agent-keys/${encodeURIComponent(agentId)}`, token, { method: "DELETE" });
      await load();
      toast("Keys issued here are revoked.", "success");
    } catch (e) {
      toast(detailOf(e), "error");
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    if (!minted) return;
    if (window.isSecureContext && navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(minted.key);
        toast("Copied.", "success");
        return;
      } catch {
        // fall through to selecting it
      }
    }
    valueRef.current?.select();
    toast("Selected: copy it with your keyboard.", "info");
  }

  if (state === "forbidden") {
    return (
      <PlatformOnly reason="An agent key answers for every tenant's runs, so it is managed by an admin of the platform tenant." />
    );
  }

  const current = rows?.find((row) => row.role === "current") ?? null;
  const adminKeys = (rows ?? []).filter((row) => row.rotatable);
  const unregistered = (rows ?? []).some((row) => !row.registered);

  return (
    <section data-scope-region="keys" aria-labelledby="agent-keys" className="rounded border bg-white p-4">
      <div className="flex items-center gap-2">
        <h2 id="agent-keys" className="text-lg font-semibold">
          Gateway keys
        </h2>
        <ScopeChip scope="platform" />
      </div>
      <p className="mt-1 text-xs text-slate-600">
        The key this agent&apos;s container presents to the gateway. It answers for every
        tenant&apos;s runs, so a platform admin manages it. A new value is shown once and stored
        nowhere: only its prefix is kept.
      </p>

      {state === "loading" && <p className="mt-3 text-sm text-slate-500">Reading the keys…</p>}
      {state === "error" && (
        <p className="mt-3 text-sm text-red-800">
          The keys could not be read: {error}{" "}
          <button className="underline" onClick={() => void load()}>
            Retry
          </button>
        </p>
      )}

      {unregistered && (
        <p data-testid="keys-unregistered" className="mt-3 text-sm text-amber-900">
          No agent is registered under {agentId} in this backend: a key here waits for its agent, or was
          left by one uninstalled.
        </p>
      )}

      {minted && (
        <div data-testid="keys-minted" className="mt-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm">
          <p className="font-semibold">Copy this key now: it is shown once and stored nowhere.</p>
          <input
            ref={valueRef}
            readOnly
            aria-label="New key"
            value={minted.key}
            className="mt-2 w-full rounded border px-2 py-1 font-mono text-xs"
          />
          <div className="mt-2 flex gap-2">
            <button type="button" className="rounded border px-3 py-1 text-xs" onClick={() => void copy()}>
              Copy
            </button>
            <button type="button" className="rounded border px-3 py-1 text-xs" onClick={() => setMinted(null)}>
              Done
            </button>
          </div>
        </div>
      )}

      {rows && (
        <>
          <table data-testid="keys-rows" className="mt-3 w-full text-left text-sm">
            <thead>
              <tr className="text-xs text-slate-500">
                <th className="py-1 pr-3 font-normal">Key</th>
                <th className="py-1 pr-3 font-normal">From</th>
                <th className="py-1 pr-3 font-normal">Role</th>
                <th className="py-1 pr-3 font-normal">Issued</th>
                <th className="py-1 font-normal">Last used</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`${row.role}-${row.key_prefix}`} data-key-source={row.source} className="border-t align-top">
                  <td className="py-1 pr-3 font-mono text-xs">lr_agent_{row.key_prefix}…</td>
                  <td className="py-1 pr-3 text-xs">
                    {row.source === "env" ? (
                      <>
                        the environment — rotate it in <code>.env</code>:{" "}
                        <code>librerun key rotate {agentId}</code>
                      </>
                    ) : (
                      "issued here"
                    )}
                  </td>
                  <td className="py-1 pr-3 text-xs">
                    {row.role}
                    {row.role === "previous" && row.previous_until ? `, until ${when(row.previous_until)}` : ""}
                  </td>
                  <td className="py-1 pr-3 text-xs">{when(row.issued_at)}</td>
                  <td className="py-1 text-xs">{when(row.last_used_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <p className="mt-2 text-sm text-slate-600">No key is installed for this agent.</p>}

          <div className="mt-3 flex flex-wrap items-end gap-3">
            {!current && (
              <button
                type="button"
                disabled={busy}
                onClick={() => void issue()}
                className="rounded bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-50"
              >
                Issue a key
              </button>
            )}
            {current?.rotatable && (
              <>
                <label className="text-xs text-slate-600">
                  Grace, hours (0–{GRACE_MAX})
                  <input
                    type="number"
                    min={0}
                    max={GRACE_MAX}
                    value={grace}
                    onChange={(e) => setGrace(e.target.value)}
                    className="ml-2 w-20 rounded border px-2 py-1 text-sm"
                  />
                </label>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void rotate()}
                  className="rounded border px-3 py-1 text-sm disabled:opacity-50"
                >
                  Rotate
                </button>
              </>
            )}
            {adminKeys.length > 0 && (
              <button
                type="button"
                disabled={busy}
                onClick={() => void revoke()}
                className="rounded border border-red-300 px-3 py-1 text-sm text-red-800 disabled:opacity-50"
              >
                Revoke
              </button>
            )}
          </div>
        </>
      )}
    </section>
  );
}
