"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import NavBar from "../../components/NavBar";
import { apiFetch } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { durationLabel, RUNNING_STATUSES } from "../../lib/runPage";
import { recordNavigationIntent } from "../../lib/telemetry/facade";
import type { AgentInfo, RunListResponse, RunStatus } from "../../types";

const STATUSES: Array<RunStatus | "all"> = [
  "all",
  "refining",
  "awaiting_approval",
  "investigating",
  "complete",
  "error",
];

export default function DashboardPage() {
  const { token, user } = useAuth();
  const router = useRouter();
  const [data, setData] = useState<RunListResponse | null>(null);
  const [agents, setAgents] = useState<Record<string, string>>({});
  const [statusFilter, setStatusFilter] = useState<RunStatus | "all">("all");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  // The duration of a run still running is measured against now; a
  // ticker keeps it moving while any listed run is unfinished.
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!token) {
      router.push("/login");
      return;
    }
    const params = new URLSearchParams();
    if (statusFilter !== "all") params.set("status", statusFilter);
    if (search) params.set("search", search);
    params.set("page", String(page));
    apiFetch<RunListResponse>(`/runs?${params}`, token).then(setData).catch(() => setData(null));
  }, [token, statusFilter, search, page, router]);

  useEffect(() => {
    if (!data?.runs.some((r) => RUNNING_STATUSES.has(r.status))) return;
    const t = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(t);
  }, [data]);

  // Build agent_id → display_name map once so the table can show friendly
  // labels without per-row lookups.
  useEffect(() => {
    if (!token) return;
    apiFetch<AgentInfo[]>("/agents", token)
      .then((rows) => {
        const m: Record<string, string> = {};
        for (const a of rows) m[a.agent_id] = a.display_name;
        setAgents(m);
      })
      .catch(() => setAgents({}));
  }, [token]);

  if (!token) return null;

  return (
    <div>
      <NavBar />
      <main className="mx-auto max-w-6xl p-6">
        <div className="mb-4 flex items-center justify-between">
          <h1 className="text-2xl font-bold">Runs</h1>
          <Link
            href="/runs/new"
            className="rounded bg-blue-600 px-4 py-2 text-white hover:bg-blue-700"
          >
            + New Run
          </Link>
        </div>
        <div className="mb-4 flex gap-2">
          {STATUSES.map((s) => (
            <button
              key={s}
              onClick={() => {
                setStatusFilter(s);
                setPage(1);
              }}
              className={`rounded-full border px-3 py-1 text-xs ${
                statusFilter === s ? "bg-blue-600 text-white" : "bg-white"
              }`}
            >
              {s}
            </button>
          ))}
          <input
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
            placeholder="Search..."
            className="ml-auto rounded border px-3 py-1 text-sm"
          />
        </div>
        <div className="overflow-x-auto rounded border bg-white">
          <table className="w-full text-sm">
            <thead className="bg-slate-100 text-left">
              {/* Title, agent, status, duration (blueprint S7) — every
                  column is something every agent's run has. */}
              <tr>
                <th className="px-3 py-2">Run #</th>
                <th className="px-3 py-2">Title</th>
                <th className="px-3 py-2">Agent</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Duration</th>
                <th className="px-3 py-2">Created</th>
              </tr>
            </thead>
            <tbody>
              {data?.runs.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-3 py-8 text-center text-slate-500">
                    No runs yet
                  </td>
                </tr>
              )}
              {data?.runs.map((c) => (
                <tr
                  key={c.id}
                  className="cursor-pointer border-t hover:bg-slate-50"
                  onClick={() => {
                    recordNavigationIntent("push");
                    router.push(`/runs/${c.id}`);
                  }}
                >
                  <td className="px-3 py-2 font-mono">{c.run_number}</td>
                  <td className="px-3 py-2">{c.title || "—"}</td>
                  <td className="px-3 py-2 text-xs">
                    {c.agent_id ? agents[c.agent_id] ?? c.agent_id : "—"}
                  </td>
                  <td className="px-3 py-2">
                    <span className="rounded bg-slate-200 px-2 py-0.5 text-xs">{c.status}</span>
                  </td>
                  <td className="px-3 py-2 text-xs text-slate-600" data-testid="run-duration">
                    {durationLabel(c.created_at, c.updated_at, c.status, now)}
                  </td>
                  <td className="px-3 py-2">{new Date(c.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {data && data.total > data.per_page && (
          <div className="mt-4 flex gap-2">
            <button
              disabled={page <= 1}
              onClick={() => setPage(page - 1)}
              className="rounded border px-3 py-1 text-sm disabled:opacity-50"
            >
              Prev
            </button>
            <span className="px-2 py-1 text-sm">
              Page {page} of {Math.ceil(data.total / data.per_page)}
            </span>
            <button
              disabled={page * data.per_page >= data.total}
              onClick={() => setPage(page + 1)}
              className="rounded border px-3 py-1 text-sm disabled:opacity-50"
            >
              Next
            </button>
          </div>
        )}
      </main>
    </div>
  );
}
