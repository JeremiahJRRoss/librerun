"use client";

import { useEffect, useState } from "react";
import NavBar from "../../../components/NavBar";
import { apiFetch } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";

interface AggSection {
  section_type: string;
  positive: number;
  negative: number;
  positive_rate: number;
}
interface Agg {
  total: number;
  positive: number;
  negative: number;
  positive_rate: number;
  per_section: AggSection[];
  recent_negatives: any[];
}

export default function FeedbackDashboardPage() {
  const { token } = useAuth();
  const [agg, setAgg] = useState<Agg | null>(null);
  const [days, setDays] = useState(90);

  useEffect(() => {
    apiFetch<Agg>(`/admin/feedback?days=${days}`, token).then(setAgg).catch(() => {});
  }, [token, days]);

  if (!agg) return null;
  return (
    <div>
      <NavBar />
      <main className="mx-auto max-w-5xl p-6">
        <h1 className="mb-4 text-2xl font-bold">Feedback Dashboard</h1>
        <div className="mb-4 flex gap-4">
          <div className="rounded border bg-white p-4">
            <div className="text-3xl font-bold">{Math.round(agg.positive_rate * 100)}%</div>
            <div className="text-xs text-slate-600">Positive rate</div>
          </div>
          <div className="rounded border bg-white p-4">
            <div className="text-3xl font-bold">{agg.total}</div>
            <div className="text-xs text-slate-600">Total feedback</div>
          </div>
          <div className="ml-auto">
            <label className="text-xs text-slate-600">Days</label>
            <input type="number" className="ml-2 w-20 rounded border px-2 py-1" value={days} onChange={(e) => setDays(parseInt(e.target.value) || 90)} />
          </div>
        </div>
        <h2 className="mb-2 text-lg font-semibold">Per section</h2>
        <div className="space-y-1 rounded border bg-white p-4">
          {agg.per_section.map((s) => (
            <div key={s.section_type} className="flex items-center gap-2 text-sm">
              <div className="w-40">{s.section_type}</div>
              <div className="h-3 flex-1 rounded bg-slate-200">
                <div className="h-3 rounded bg-green-500" style={{ width: `${Math.round(s.positive_rate * 100)}%` }} />
              </div>
              <div className="w-16 text-right text-xs">
                {s.positive}↑ {s.negative}↓
              </div>
            </div>
          ))}
        </div>
        <h2 className="mb-2 mt-4 text-lg font-semibold">Recent negative feedback</h2>
        <div className="rounded border bg-white">
          <table className="w-full text-sm">
            <thead className="bg-slate-100 text-left">
              <tr><th className="px-2 py-1">Run</th><th className="px-2 py-1">Section</th><th className="px-2 py-1">Comment</th><th className="px-2 py-1">Date</th></tr>
            </thead>
            <tbody>
              {agg.recent_negatives.map((n: any) => (
                <tr key={n.id} className="border-t">
                  <td className="px-2 py-1 font-mono text-xs">{n.run_id.slice(0, 8)}</td>
                  <td className="px-2 py-1">{n.section_type}</td>
                  <td className="px-2 py-1 text-xs">{n.comment}</td>
                  <td className="px-2 py-1 text-xs">{new Date(n.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </main>
    </div>
  );
}
