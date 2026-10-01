"use client";

import type { TimelineNode } from "../lib/runPage";

const STATE_CLASS: Record<TimelineNode["state"], string> = {
  done: "border-green-600 bg-green-50 text-green-800",
  current: "border-blue-600 bg-blue-50 text-blue-800 animate-pulse",
  pending: "border-slate-300 bg-white text-slate-500",
  error: "border-red-600 bg-red-50 text-red-800",
};

const STATE_MARK: Record<TimelineNode["state"], string> = {
  done: "●",
  current: "◐",
  pending: "○",
  error: "✗",
};

/**
 * The phase timeline (blueprint S7): queued → the manifest's phases, a
 * gate before each that declares one → complete or error. Generic for
 * every agent — the nodes are built from the agent's manifest by
 * `buildTimeline`, which decides the states; this only draws them.
 */
export default function RunTimeline({ nodes }: { nodes: TimelineNode[] }) {
  return (
    <ol className="flex flex-wrap items-center gap-2 text-sm" data-testid="timeline" aria-label="Run timeline">
      {nodes.map((node, i) => (
        <li key={node.id} className="flex items-center gap-2">
          <span
            data-testid="timeline-node"
            data-kind={node.kind}
            data-state={node.state}
            className={`inline-flex items-center gap-1 rounded-full border px-3 py-1 ${STATE_CLASS[node.state]}`}
          >
            <span aria-hidden="true">{STATE_MARK[node.state]}</span>
            {node.label}
          </span>
          {i < nodes.length - 1 && <span className="text-slate-300" aria-hidden="true">→</span>}
        </li>
      ))}
    </ol>
  );
}
