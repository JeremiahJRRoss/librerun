"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import NavBar from "../../../components/NavBar";
import { apiFetch } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";

interface Entry {
  id: string;
  action_type: string;
  user_email: string | null;
  detail: any;
  ip_address: string | null;
  created_at: string;
}
interface Page { entries: Entry[]; total: number; }

const QUICK_FILTERS = [
  { label: "All", value: "" },
  { label: "Schema drift", value: "llm_schema_drift" },
  { label: "Config change", value: "config_change" },
  { label: "Blocked request", value: "blocked_request" },
];

function AuditLogContent() {
  const { token } = useAuth();
  const searchParams = useSearchParams();
  const [data, setData] = useState<Page | null>(null);
  const [page, setPage] = useState(1);
  const [action, setAction] = useState(searchParams?.get("action_type") ?? "");

  useEffect(() => {
    const qs = new URLSearchParams({ page: String(page), per_page: "50" });
    if (action) qs.set("action_type", action);
    apiFetch<Page>(`/admin/audit-log?${qs}`, token).then(setData).catch(() => {});
  }, [token, page, action]);

  if (!data) return null;
  return (
    <main className="mx-auto max-w-5xl p-6">
      <h1 className="mb-4 text-2xl font-bold">Activity Audit Log</h1>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <input placeholder="Filter by action_type..." value={action} onChange={(e) => { setPage(1); setAction(e.target.value); }} className="rounded border px-2 py-1 text-sm" />
        {QUICK_FILTERS.map((f) => (
          <button
            key={f.value}
            type="button"
            onClick={() => { setPage(1); setAction(f.value); }}
            className={`rounded border px-2 py-1 text-xs ${action === f.value ? "bg-blue-600 text-white" : "bg-white hover:bg-slate-50"}`}
          >
            {f.label}
          </button>
        ))}
      </div>
      <div className="overflow-x-auto rounded border bg-white">
        <table className="w-full text-sm">
          <thead className="bg-slate-100 text-left">
            <tr><th className="px-2 py-1">Time</th><th className="px-2 py-1">User</th><th className="px-2 py-1">Action</th><th className="px-2 py-1">Detail</th><th className="px-2 py-1">IP</th></tr>
          </thead>
          <tbody>
            {data.entries.map((e) => (
              <tr key={e.id} className="border-t">
                <td className="px-2 py-1 text-xs">{new Date(e.created_at).toLocaleString()}</td>
                <td className="px-2 py-1 text-xs">{e.user_email ?? ""}</td>
                <td className="px-2 py-1"><span className="rounded bg-slate-200 px-1 py-0.5 text-xs">{e.action_type}</span></td>
                <td className="px-2 py-1 text-xs">
                  <details><summary>view</summary><pre>{JSON.stringify(e.detail, null, 2)}</pre></details>
                </td>
                <td className="px-2 py-1 text-xs">{e.ip_address ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-3 flex gap-2">
        <button disabled={page <= 1} onClick={() => setPage(page - 1)} className="rounded border px-3 py-1 text-sm disabled:opacity-50">Prev</button>
        <span className="text-sm">Page {page}</span>
        <button disabled={page * 50 >= data.total} onClick={() => setPage(page + 1)} className="rounded border px-3 py-1 text-sm disabled:opacity-50">Next</button>
      </div>
    </main>
  );
}

export default function AuditLogPage() {
  return (
    <div>
      <NavBar />
      <Suspense fallback={<main className="mx-auto max-w-5xl p-6">Loading…</main>}>
        <AuditLogContent />
      </Suspense>
    </div>
  );
}
