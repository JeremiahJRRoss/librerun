"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import ApprovalPanel from "../../../components/ApprovalPanel";
import NavBar from "../../../components/NavBar";
import ProgressList from "../../../components/ProgressList";
import ResultsView from "../../../components/ResultsView";
import RunTimeline from "../../../components/RunTimeline";
import { ApiError, apiFetch } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import {
  buildTimeline,
  errorMessage,
  pollDelayMs,
  runAgainHref,
  TERMINAL_STATUSES,
} from "../../../lib/runPage";
import { tagSurface, clearSurface } from "../../../lib/telemetry/facade";
import type {
  AdminRunDetail,
  AgentInfo,
  ProgressResponse,
  RunDetail,
} from "../../../types";

const STATUS_LABEL: Record<string, string> = {
  submitted: "Queued",
  refining: "Running",
  awaiting_approval: "Waiting for approval",
  investigating: "Running",
  complete: "Complete",
  error: "Error",
};

/**
 * The run page, generic for every agent (blueprint S7): title, agent,
 * status and the phase timeline; progress with the manifest's labels and
 * the model column under gap E5's rule; the approval panel; the result
 * (the agent's own report for `html_report`, collapsible sections for
 * `structured`); a sanitized error with "Run again"; "View trace" as a
 * button. It polls the detail and the progress together — 2 s while the
 * run is unfinished, and not at all once it is (D8).
 */
export default function RunDetailPage() {
  const { token, user } = useAuth();
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const id = params.id;
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [progress, setProgress] = useState<ProgressResponse | null>(null);
  const [agents, setAgents] = useState<AgentInfo[] | null>(null);
  const [admin, setAdmin] = useState<AdminRunDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Bumped to poll right now (after an approval), instead of on the tick.
  const [nudge, setNudge] = useState(0);

  const status = detail?.status ?? null;
  const terminal = status ? TERMINAL_STATUSES.has(status) : false;

  const fetchOnce = useCallback(async (): Promise<RunDetail | null> => {
    if (!token) return null;
    try {
      const [d, p] = await Promise.all([
        apiFetch<RunDetail>(`/runs/${id}`, token),
        apiFetch<ProgressResponse>(`/runs/${id}/progress`, token).catch(() => null),
      ]);
      setDetail(d);
      if (p) setProgress(p);
      return d;
    } catch (e: any) {
      // The session is gone and AuthProvider is already routing to the
      // login page; leave the error unset so the page does not flash
      // the bare word "Unauthorized" on the way out.
      if (!(e instanceof ApiError && e.status === 401)) setErr(e.message);
      return null;
    }
  }, [id, token]);

  // The polling loop (D8): one timer, re-armed from the answer's own
  // status, so the interval is a property of the run's state and the
  // loop ends itself at a terminal state.
  useEffect(() => {
    if (!token) {
      router.push("/login");
      return;
    }
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function tick() {
      const d = await fetchOnce();
      if (!alive) return;
      const delay = pollDelayMs(d?.status ?? (d === null ? "error" : undefined));
      if (delay !== null && d !== null) timer = setTimeout(tick, delay);
    }
    tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [fetchOnce, token, router, nudge]);

  // The agent's manifest, for labels, phases, LLM steps and the badge —
  // read once through the listing every card is built from.
  useEffect(() => {
    if (!token) return;
    let alive = true;
    apiFetch<AgentInfo[]>("/agents", token)
      .then((rows) => {
        if (alive) setAgents(rows);
      })
      .catch(() => {
        if (alive) setAgents([]);
      });
    return () => {
      alive = false;
    };
  }, [token]);

  // Admins see the raw trace id beside the button and, on an error, the
  // operator-facing detail — both admin-only by contract, so they come
  // from the admin endpoint, once per status change.
  useEffect(() => {
    if (!token || user?.role !== "admin" || !status) return;
    let alive = true;
    apiFetch<AdminRunDetail>(`/admin/runs/${id}`, token)
      .then((d) => {
        if (alive) setAdmin(d);
      })
      .catch(() => {
        if (alive) setAdmin(null);
      });
    return () => {
      alive = false;
    };
  }, [id, token, user?.role, status]);

  // This view renders agent-owned content: telemetry from it is
  // attributed to the agent's surface. The values are claims — the
  // backend relay authorizes them against the registry and the tenant's
  // own runs before anything is emitted.
  useEffect(() => {
    if (detail?.agent_id) {
      tagSurface({ owner: "agent", agentId: detail.agent_id, runId: id });
      return () => clearSurface();
    }
  }, [detail?.agent_id, id]);

  const agent = useMemo(
    () => (agents && detail?.agent_id ? agents.find((a) => a.agent_id === detail.agent_id) ?? null : null),
    [agents, detail?.agent_id],
  );
  const timeline = useMemo(
    () => (detail ? buildTimeline(agent?.phases, detail.status, progress?.phase_name) : []),
    [agent?.phases, detail, progress?.phase_name],
  );

  if (!token) return null;
  if (err) return <div className="p-6 text-red-600">{err}</div>;
  if (!detail) return <div className="p-6">Loading…</div>;

  const steps = progress?.steps ?? [];
  const showProgress = steps.length > 0 || (status !== null && !terminal && status !== "awaiting_approval");

  return (
    <div>
      <NavBar />
      <main className="mx-auto max-w-5xl p-6">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold" data-testid="run-title">
              {detail.title || detail.run_number}
            </h1>
            <p className="text-sm text-slate-600">
              <span className="font-mono" data-testid="run-number">{detail.run_number}</span>
              {" · "}
              <span data-testid="run-agent">{agent?.display_name ?? detail.agent_id ?? "unknown agent"}</span>
            </p>
          </div>
          <div className="flex items-center gap-3">
            <span
              data-testid="run-status"
              data-status={detail.status}
              className={`rounded-full px-3 py-1 text-sm ${
                detail.status === "complete"
                  ? "bg-green-100 text-green-800"
                  : detail.status === "error"
                  ? "bg-red-100 text-red-800"
                  : detail.status === "awaiting_approval"
                  ? "bg-amber-100 text-amber-900"
                  : "bg-blue-100 text-blue-800"
              }`}
            >
              {STATUS_LABEL[detail.status] ?? detail.status}
            </span>
            {detail.trace_url && (
              // The deep link the backend built for the configured viewer
              // (blueprint B5/L8; D6 of the delight gate). External, so a
              // new tab (CLAUDE.md: every http(s) link opens in a new tab).
              <a
                href={detail.trace_url}
                target="_blank"
                rel="noopener noreferrer"
                data-testid="view-trace"
                className="rounded bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
              >
                View trace ↗
              </a>
            )}
          </div>
        </div>

        {(admin?.trace_id || user?.role === "admin") && (
          <div className="mb-4 flex flex-wrap items-center gap-4 text-xs text-slate-500">
            {admin?.trace_id && (
              <span>
                trace <span className="font-mono" data-testid="trace-id">{admin.trace_id}</span>
              </span>
            )}
            {user?.role === "admin" && (
              <Link href={`/admin/runs/${detail.id}`} className="text-blue-600 underline hover:text-blue-800">
                Admin view →
              </Link>
            )}
          </div>
        )}

        <div className="mb-6 rounded border bg-white p-4">
          <RunTimeline nodes={timeline} />
        </div>

        <div className="space-y-6">
          {detail.status === "awaiting_approval" && (
            <ApprovalPanel runId={detail.id} token={token} onChanged={() => setNudge((n) => n + 1)} />
          )}

          {detail.status === "error" && (
            <div
              className="rounded border border-red-300 bg-red-50 p-6 text-red-900"
              data-testid="run-error"
              data-error-code={detail.error_code ?? ""}
            >
              <h2 className="mb-1 font-semibold">This run did not finish</h2>
              <p className="text-sm" data-testid="error-message">
                {errorMessage(detail)}
              </p>
              {user?.role === "admin" && (
                <p className="mt-2 text-xs text-red-800">
                  The operator detail is on the admin view.
                </p>
              )}
              <Link
                href={runAgainHref(detail)}
                data-testid="run-again"
                className="mt-4 inline-block rounded bg-blue-600 px-4 py-2 text-sm text-white hover:bg-blue-700"
              >
                Run again
              </Link>
            </div>
          )}

          {detail.status === "complete" && <ResultsView detail={detail} />}

          {showProgress && (
            <ProgressList
              steps={steps}
              agent={agent}
              phaseName={progress?.phase_name ?? null}
              traceUrl={detail.trace_url}
              title={terminal ? "Steps" : detail.status === "awaiting_approval" ? "Steps so far" : "Progress"}
            />
          )}
          {!showProgress && !terminal && detail.status !== "awaiting_approval" && (
            <div className="rounded border bg-white p-6">
              <p className="animate-pulse text-sm text-slate-600">Working…</p>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
