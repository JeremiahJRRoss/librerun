"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import NavBar from "../../../../components/NavBar";
import { apiFetch } from "../../../../lib/api";
import { useAuth } from "../../../../lib/auth";
import type { AdminRunDetail } from "../../../../types";

export default function AdminRunDetailPage() {
  const { token, user } = useAuth();
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const id = params.id;
  const [detail, setDetail] = useState<AdminRunDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!token) {
      router.push("/login");
      return;
    }
    if (user && user.role !== "admin") {
      router.push("/dashboard");
    }
  }, [token, user, router]);

  useEffect(() => {
    if (!token || !user || user.role !== "admin") return;
    let alive = true;
    apiFetch<AdminRunDetail>(`/admin/runs/${id}`, token)
      .then((d) => {
        if (alive) setDetail(d);
      })
      .catch((e) => {
        if (alive) setErr(e.message);
      });
    return () => {
      alive = false;
    };
  }, [id, token, user]);

  if (!token || !user) return null;
  if (err) return <div className="p-6 text-red-600">{err}</div>;
  if (!detail) return <div className="p-6">Loading…</div>;

  return (
    <div>
      <NavBar />
      <main className="mx-auto max-w-5xl p-6">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">{detail.run_number}</h1>
            <p className="text-sm text-slate-600">{detail.title || detail.agent_id}</p>
          </div>
          <span className="rounded bg-slate-200 px-3 py-1 text-sm">
            {detail.status}
          </span>
        </div>

        <section className="mb-4 rounded border bg-white p-4">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
            Observability
          </h2>
          <dl className="space-y-2 text-sm">
            <div>
              <dt className="text-xs text-slate-500">Trace ID</dt>
              <dd className="font-mono text-xs break-all">
                {detail.trace_id || "—"}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Phase 2 span ID</dt>
              <dd className="font-mono text-xs break-all">
                {detail.phase2_span_id || "—"}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Trace viewer</dt>
              <dd className="text-xs">
                {detail.trace_url ? (
                  <a
                    href={detail.trace_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    data-testid="view-trace"
                    className="text-blue-600 underline hover:text-blue-800"
                  >
                    View trace ↗
                  </a>
                ) : (
                  <span className="text-slate-500">
                    no viewer configured (TRACE_VIEWER) or no trace yet
                  </span>
                )}
              </dd>
            </div>
          </dl>
        </section>

        <section className="mb-4 rounded border bg-white p-4">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
            Run metadata
          </h2>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            <div>
              <dt className="text-xs text-slate-500">Severity</dt>
              <dd>{detail.severity ?? "—"}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Status</dt>
              <dd>{detail.status}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Created</dt>
              <dd>{new Date(detail.created_at).toLocaleString()}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Updated</dt>
              <dd>{new Date(detail.updated_at).toLocaleString()}</dd>
            </div>
          </dl>
        </section>

        {detail.status === "error" && (
          // The operator-facing reason (blueprint S7): the agent's own
          // failure text, the exception, the phase a restart cut short —
          // served here and nowhere else, redacted before it was stored.
          <section className="mb-4 rounded border border-red-200 bg-white p-4" data-testid="admin-error">
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
              Why it ended in error
            </h2>
            <dl className="space-y-2 text-sm">
              <div>
                <dt className="text-xs text-slate-500">Code</dt>
                <dd className="font-mono text-xs">{detail.error_code || "—"}</dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">What the customer sees</dt>
                <dd>{detail.error_message || "—"}</dd>
              </div>
              <div>
                <dt className="text-xs text-slate-500">Operator detail</dt>
                <dd className="whitespace-pre-wrap break-words font-mono text-xs" data-testid="error-detail">
                  {detail.error_detail || "—"}
                </dd>
              </div>
            </dl>
          </section>
        )}

        <section className="mb-4 rounded border bg-white p-4">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-500">
            Problem statement
          </h2>
          <p className="whitespace-pre-wrap text-sm text-slate-800">
            {detail.problem_statement}
          </p>
        </section>

        <div className="text-sm">
          <Link
            href={`/runs/${detail.id}`}
            className="text-blue-600 underline hover:text-blue-800"
          >
            ← Customer view
          </Link>
        </div>
      </main>
    </div>
  );
}
