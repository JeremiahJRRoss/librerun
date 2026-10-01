"use client";

import {
  MODEL_COLUMN_HELP,
  humanize,
  modelCell,
  modelColumnMode,
  stepLabel,
} from "../lib/runPage";
import type { AgentInfo, StepProgress } from "../types";

const STATUS_ICON: Record<string, string> = {
  pending: "○",
  running: "◐",
  complete: "●",
  skipped: "⊘",
  error: "✗",
};

interface Props {
  steps: StepProgress[];
  // The run's agent, from `GET /agents` — its manifest carries the labels
  // (`phases[].steps[]`) and the LLM step ids the model column is keyed by.
  agent: AgentInfo | null;
  phaseName?: string | null;
  // Where "on the trace" points when a model cannot be attributed to a row.
  traceUrl?: string | null;
  title?: string;
}

/**
 * The labelled progress list (blueprint S7). Every row is a progress
 * record the agent reported; its label comes from the manifest, and the
 * model column follows gap E5's rule exactly (`modelColumnMode`): shown
 * where the row's id IS the LLM step, "on the trace" where the agent's
 * ids differ, hidden where the agent calls no model. The page polls and
 * hands the rows in; this component fetches nothing.
 */
export default function ProgressList({ steps, agent, phaseName, traceUrl, title }: Props) {
  const mode = modelColumnMode(steps, agent);
  return (
    <div className="rounded border bg-white p-6" data-testid="progress-list" data-model-column={mode}>
      <h2 className="mb-4 text-lg font-semibold">
        {title ?? "Progress"}
        {phaseName ? <span className="ml-2 text-sm font-normal text-slate-500">— {humanize(phaseName)}</span> : null}
      </h2>
      {steps.length === 0 ? (
        <p className="text-sm text-slate-500">No steps reported yet.</p>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="w-6 py-1" aria-label="Status" />
              <th className="py-1">Step</th>
              {mode !== "hidden" && <th className="py-1">Model</th>}
              <th className="py-1 text-right">Time</th>
            </tr>
          </thead>
          <tbody>
            {steps.map((s) => {
              const cell = modelCell(s, mode);
              return (
                <tr key={s.step_id} data-testid="progress-row" data-step-id={s.step_id} className="border-t">
                  <td
                    className={`py-1.5 text-center ${
                      s.status === "complete"
                        ? "text-green-600"
                        : s.status === "error"
                        ? "text-red-600"
                        : s.status === "running"
                        ? "text-blue-600"
                        : "text-slate-400"
                    }`}
                    title={s.status}
                  >
                    {STATUS_ICON[s.status] ?? "○"}
                  </td>
                  <td className="py-1.5">
                    <span data-testid="step-label">{stepLabel(s.step_id, agent)}</span>
                    {s.detail && s.status === "error" && (
                      <span className="ml-2 text-xs text-red-500">{s.detail}</span>
                    )}
                  </td>
                  {mode !== "hidden" && (
                    <td className="py-1.5" data-testid="step-model" data-model-kind={cell.kind}>
                      {cell.kind === "model" ? (
                        <span className="font-mono text-xs text-slate-700">{cell.model}</span>
                      ) : cell.kind === "trace" ? (
                        traceUrl ? (
                          <a
                            href={traceUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-xs text-blue-600 underline hover:text-blue-800"
                          >
                            on the trace ↗
                          </a>
                        ) : (
                          <span className="text-xs text-slate-500">on the trace</span>
                        )
                      ) : (
                        <span className="text-xs text-slate-400">—</span>
                      )}
                    </td>
                  )}
                  <td className="py-1.5 text-right text-xs text-slate-500">
                    {s.duration_ms != null ? `${Math.round(s.duration_ms)} ms` : ""}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {mode !== "hidden" && (
        <p className="mt-3 text-xs text-slate-500" data-testid="model-column-help">
          {MODEL_COLUMN_HELP}
        </p>
      )}
    </div>
  );
}
