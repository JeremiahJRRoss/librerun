"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "../lib/api";
import { humanize } from "../lib/runPage";
import { useToast } from "../lib/toast";
import type { ApprovalResponse } from "../types";

interface Props {
  runId: string;
  token: string;
  // Called after an approval or an edit was accepted, so the page can
  // poll at once instead of on its next tick.
  onChanged?: () => void;
}

/**
 * The approval panel, generic for every agent (blueprint S7, gap E4).
 * One fetch of `GET /runs/{id}/approval` — the parked phase, its whole
 * output and the `summary` the manifest's `ui.approval.summary_path`
 * names inside it — then: the summary, editable; the raw payload,
 * collapsed; Approve, or Save & re-run. Nothing here sniffs the
 * payload's shape: what the summary IS is the agent manifest's business.
 */
export default function ApprovalPanel({ runId, token, onChanged }: Props) {
  const { toast } = useToast();
  const [approval, setApproval] = useState<ApprovalResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const [busy, setBusy] = useState(false);
  const [decided, setDecided] = useState(false);

  useEffect(() => {
    let alive = true;
    apiFetch<ApprovalResponse>(`/runs/${runId}/approval`, token)
      .then((r) => {
        if (!alive) return;
        setApproval(r);
        setEditText(r.summary ?? (r.payload ? JSON.stringify(r.payload, null, 2) : ""));
      })
      .catch((e) => {
        if (alive) setLoadError(e.message ?? "Failed to load the parked output");
      });
    return () => {
      alive = false;
    };
  }, [runId, token]);

  async function approve() {
    setBusy(true);
    try {
      await apiFetch(`/runs/${runId}/approve`, token, { method: "POST" });
      setDecided(true);
      toast("Approved — the next phase is starting", "success");
      onChanged?.();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function saveEdit() {
    setBusy(true);
    try {
      await apiFetch(`/runs/${runId}/edit-statement`, token, {
        method: "POST",
        body: JSON.stringify({ edited_statement: editText }),
      });
      setEditing(false);
      setDecided(true);
      toast("Re-running the phase with your edit…", "info");
      onChanged?.();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function copyPayload() {
    try {
      await navigator.clipboard.writeText(JSON.stringify(approval?.payload ?? null, null, 2));
      toast("Payload copied", "success");
    } catch {
      toast("Could not copy — select the text instead", "error");
    }
  }

  if (loadError) {
    return (
      <div className="rounded border border-red-300 bg-red-50 p-4 text-sm text-red-800">
        Could not load the parked output: {loadError}
      </div>
    );
  }
  if (!approval) return <div className="rounded border bg-white p-6">Loading the parked output…</div>;

  const summary = approval.summary ?? null;
  const payloadJson = JSON.stringify(approval.payload ?? null, null, 2);

  return (
    <div className="space-y-4" data-testid="approval-panel">
      <div className="rounded border bg-white p-6">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="font-semibold">
            Waiting for your approval
            {approval.phase ? (
              <span className="ml-2 text-sm font-normal text-slate-500">
                — output of {humanize(approval.phase)}
              </span>
            ) : null}
          </h2>
        </div>
        {editing ? (
          <textarea
            rows={8}
            value={editText}
            onChange={(e) => setEditText(e.target.value)}
            data-testid="approval-edit"
            className="w-full rounded border px-2 py-1 text-sm"
          />
        ) : summary ? (
          <p className="whitespace-pre-wrap text-sm" data-testid="approval-summary">
            {summary}
          </p>
        ) : (
          <p className="text-sm text-slate-500" data-testid="approval-summary">
            The agent parked its output without a summary; the raw payload is below.
          </p>
        )}
        <details className="mt-4" data-testid="approval-raw">
          <summary className="cursor-pointer text-sm text-slate-600">Raw payload</summary>
          <div className="mt-2 flex justify-end">
            <button type="button" onClick={copyPayload} className="rounded border px-3 py-1 text-xs hover:bg-slate-50">
              Copy JSON
            </button>
          </div>
          <pre className="mt-2 max-h-96 overflow-auto rounded bg-slate-50 p-3 text-xs">{payloadJson}</pre>
        </details>
      </div>

      <div className="flex gap-3">
        {editing ? (
          <>
            <button
              onClick={saveEdit}
              disabled={busy}
              className="rounded bg-blue-600 px-4 py-2 text-white disabled:opacity-50"
            >
              Save &amp; re-run
            </button>
            <button onClick={() => setEditing(false)} className="rounded border px-4 py-2">
              Cancel
            </button>
          </>
        ) : (
          <>
            <button
              onClick={approve}
              disabled={busy || decided}
              data-testid="approve-button"
              className="rounded bg-green-600 px-4 py-2 text-white hover:bg-green-700 disabled:opacity-50"
            >
              {decided ? "Approved" : "Approve & continue"}
            </button>
            <button
              onClick={() => setEditing(true)}
              disabled={busy || decided}
              className="rounded border px-4 py-2 hover:bg-slate-50 disabled:opacity-50"
            >
              Edit
            </button>
          </>
        )}
      </div>
    </div>
  );
}
