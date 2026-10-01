"use client";

import { useEffect, useState } from "react";
import { apiFetch, ApiError, unauthorizedError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useToast } from "../lib/toast";
import type { RunDetail } from "../types";
import FeedbackControls from "./FeedbackControls";
import StructuredOutput from "./StructuredOutput";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

interface ReportTaskCreated {
  task_id: string;
}

interface TaskStatus {
  status: string;
  result_url: string | null;
  error: string | null;
}

interface EmbeddedReport {
  html: string;
}

export default function ResultsView({ detail }: { detail: RunDetail }) {
  const structuredMode = detail.output_mode === "structured";
  // Run-scoped feedback vocabulary, resolved server-side from the
  // agent's manifest — exactly what POST /feedback will accept, so no
  // control we render can 422 by construction. Empty (agent unresolvable
  // or nothing declared) hides the panel rather than guessing.
  const sections = detail.feedback_sections ?? [];
  const [html, setHtml] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [exportOpen, setExportOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const { token } = useAuth();
  const { toast } = useToast();

  useEffect(() => {
    let cancelled = false;
    setHtml(null);
    setLoadError(null);
    // structured-mode agents ship no rendered HTML — the payload arrives
    // on the detail itself (structured_output).
    if (structuredMode) return;
    apiFetch<EmbeddedReport>(`/runs/${detail.id}/report/embedded`, token)
      .then((r) => {
        if (!cancelled) setHtml(r.html);
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : "Failed to load report");
      });
    return () => {
      cancelled = true;
    };
  }, [detail.id, token, structuredMode]);

  async function exportReport(format: "html" | "pdf") {
    if (exporting) return;
    setExporting(true);
    try {
      const { task_id } = await apiFetch<ReportTaskCreated>(
        `/runs/${detail.id}/report`,
        token,
        { method: "POST", body: JSON.stringify({ format }) }
      );
      const deadline = Date.now() + 2 * 60 * 1000;
      while (Date.now() < deadline) {
        await new Promise((res) => setTimeout(res, 2000));
        const status = await apiFetch<TaskStatus>(`/tasks/${task_id}`, token);
        if (status.status === "complete" && status.result_url) {
          const origin = API_BASE.replace(/\/api\/v1\/?$/, "");
          const res = await fetch(`${origin}${status.result_url}`, {
            headers: token ? { Authorization: `Bearer ${token}` } : {},
          });
          // Same 401 path as every other call: a rejected token signs out
          // once, app-wide, instead of surfacing as a download failure.
          if (res.status === 401) throw unauthorizedError(token);
          if (!res.ok) throw new ApiError(`Download failed: ${res.status}`, res.status);
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          window.open(url, "_blank");
          setTimeout(() => URL.revokeObjectURL(url), 60_000);
          toast(`Report ready (${format.toUpperCase()})`, "success");
          return;
        }
        if (status.status === "error") {
          throw new Error(status.error || "Report generation failed");
        }
      }
      toast("Report generation timed out — please retry", "error");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Export failed";
      toast(msg, "error");
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="space-y-6">
      {structuredMode ? (
        // Collapsible sections with "Copy JSON" (blueprint S7); the
        // html_report path below is unchanged (G0).
        <StructuredOutput data={detail.structured_output ?? {}} />
      ) : (
        <>
          {loadError && (
            <div className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-800">
              Failed to load report: {loadError}
            </div>
          )}

          {html === null && !loadError && (
            <div className="rounded border bg-white p-6 text-sm text-slate-500">
              Loading report…
            </div>
          )}

          {html !== null && (
            <div className="report-container">
              <div dangerouslySetInnerHTML={{ __html: html }} />
            </div>
          )}
        </>
      )}

      {sections.length > 0 && (
        <div className="rounded border bg-white p-4">
          <h3 className="mb-3 text-sm font-semibold text-slate-700">
            Feedback on this investigation
          </h3>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {sections.map((s) => (
              <div key={s.id} className="flex items-center justify-between rounded border p-3">
                <span className="text-sm">{s.label}</span>
                <FeedbackControls runId={detail.id} sectionType={s.id} />
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="flex gap-2">
        <button
          onClick={() => setExportOpen(!exportOpen)}
          className="rounded border px-4 py-2 hover:bg-slate-50"
        >
          Export ▾
        </button>
        {exportOpen && (
          <>
            <button
              onClick={() => exportReport("html")}
              disabled={exporting}
              className="rounded border px-3 py-2 disabled:opacity-50"
            >
              HTML
            </button>
            <button
              onClick={() => exportReport("pdf")}
              disabled={exporting}
              className="rounded border px-3 py-2 disabled:opacity-50"
            >
              PDF
            </button>
          </>
        )}
      </div>
    </div>
  );
}
