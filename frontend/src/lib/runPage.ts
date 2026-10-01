/**
 * The run page's logic, apart from React (blueprint S7): the phase
 * timeline, progress labels, the model column's rule (gap E5), the
 * sanitized error line, the polling policy (D8) and the dashboard's
 * duration. Pure functions over the API's shapes, so every decision the
 * page makes is unit-tested here, and the page itself only renders.
 *
 * Nothing in this file names an agent (L13): every label, phase and step
 * comes from the agent's manifest through the API.
 */
import type {
  AgentInfo,
  AgentPhase,
  RunDetail,
  RunStatus,
  StepProgress,
} from "../types";

export const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>([
  "complete",
  "error",
]);

/** The statuses in which a phase is (supposedly) running. */
export const RUNNING_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>([
  "submitted",
  "refining",
  "investigating",
]);

/**
 * D8: 2 s while the run is not finished, and no timer at all once it is.
 * A parked run polls too — the approval may come from another tab — and
 * stops the moment the run reaches a terminal state.
 */
export const POLL_INTERVAL_MS = 2000;

export function pollDelayMs(status: RunStatus | null | undefined): number | null {
  if (!status) return POLL_INTERVAL_MS;
  return TERMINAL_STATUSES.has(status) ? null : POLL_INTERVAL_MS;
}

// ---------------------------------------------------------------- timeline --

export type TimelineKind = "submitted" | "phase" | "gate" | "terminal";
export type TimelineState = "done" | "current" | "pending" | "error";

export interface TimelineNode {
  id: string;
  label: string;
  kind: TimelineKind;
  state: TimelineState;
}

/** `refine_problem` → `Refine problem`; a phase name is a manifest identifier. */
export function humanize(name: string): string {
  const spaced = name.replace(/[_:]+/g, " ").trim();
  return spaced ? spaced[0].toUpperCase() + spaced.slice(1) : name;
}

/**
 * The generic phase timeline: queued → each manifest phase, with an
 * approval gate drawn before every phase that declares one → complete or
 * error. The current position is the run's status plus the phase the
 * progress endpoint names (`phase_name`); with no phase list (the agent
 * is not installed any more) the timeline is submitted → terminal.
 *
 * `error` marks the node the run died on: the phase named by the cursor,
 * or the terminal node when nothing had started.
 */
export function buildTimeline(
  phases: AgentPhase[] | null | undefined,
  status: RunStatus,
  currentPhase: string | null | undefined,
): TimelineNode[] {
  const list = phases ?? [];
  const nodes: TimelineNode[] = [{ id: "submitted", label: "Submitted", kind: "submitted", state: "pending" }];
  for (const phase of list) {
    if (phase.approval) {
      nodes.push({
        id: `gate:${phase.name}`,
        label: "Approval",
        kind: "gate",
        state: "pending",
      });
    }
    nodes.push({ id: `phase:${phase.name}`, label: humanize(phase.name), kind: "phase", state: "pending" });
  }
  const terminal: TimelineNode = {
    id: "terminal",
    label: status === "error" ? "Error" : "Complete",
    kind: "terminal",
    state: "pending",
  };
  nodes.push(terminal);

  const phaseIndex = (name: string | null | undefined) =>
    name ? nodes.findIndex((n) => n.id === `phase:${name}`) : -1;

  const markUpTo = (upTo: number, state: TimelineState) => {
    for (let i = 0; i < nodes.length; i++) {
      if (i < upTo) nodes[i].state = "done";
      else if (i === upTo) nodes[i].state = state;
    }
  };

  const cursor = phaseIndex(currentPhase);
  switch (status) {
    case "submitted":
      markUpTo(0, "current");
      break;
    case "refining":
    case "investigating": {
      // The phase the progress endpoint names is running; before it the
      // cursor is unknown, so only "submitted" is done and the first
      // phase is current.
      const at = cursor >= 0 ? cursor : nodes.findIndex((n) => n.kind === "phase" || n.kind === "gate");
      markUpTo(at >= 0 ? at : 0, "current");
      break;
    }
    case "awaiting_approval": {
      // The phase that parked is done; the gate right after it waits.
      const gate = cursor >= 0 ? cursor + 1 : nodes.findIndex((n) => n.kind === "gate");
      markUpTo(gate >= 0 ? gate : 0, "current");
      break;
    }
    case "complete":
      markUpTo(nodes.length - 1, "done");
      break;
    case "error": {
      const at = cursor >= 0 ? cursor : nodes.length - 1;
      markUpTo(at, "error");
      terminal.state = "error";
      break;
    }
  }
  return nodes;
}

// ---------------------------------------------------------------- progress --

/** `{step_id: label}` from the manifest's `phases[].steps[]`; first declaration wins. */
export function stepLabels(agent: AgentInfo | null | undefined): Record<string, string> {
  const labels: Record<string, string> = {};
  for (const phase of agent?.phases ?? []) {
    for (const step of phase.steps ?? []) {
      if (!(step.id in labels)) labels[step.id] = step.label;
    }
  }
  return labels;
}

/**
 * The label for a progress row: the manifest's `phases[].steps[]` label,
 * else the LLM step's label when the row's id IS an LLM step, else the
 * raw id — an undeclared row is shown, never hidden.
 */
export function stepLabel(stepId: string, agent: AgentInfo | null | undefined): string {
  const declared = stepLabels(agent)[stepId];
  if (declared) return declared;
  const llm = agent?.llm?.steps.find((s) => s.id === stepId);
  if (llm?.label) return llm.label;
  return stepId;
}

export type ModelColumnMode = "hidden" | "model" | "trace";

/**
 * Gap E5, and the S5 revert it records. The gateway writes the model
 * that answered a call under the LLM STEP id; a progress row's id is
 * free text the agent chooses. A model may be shown on a row ONLY when
 * the row's id equals an LLM step id — never inferred from a name,
 * because manifest membership proves the step exists, not that this row
 * made the call, and a confidently wrong attribution is worse than a
 * blank cell.
 *
 * So the column has three modes, decided for the agent as a whole:
 * - `hidden`: the agent declares no LLM steps — there is nothing to show;
 * - `model`: at least one reported row IS an LLM step, so the rows carry
 *   the model where the gateway recorded one and "—" where it did not
 *   (yet);
 * - `trace`: the agent declares LLM steps but names its rows otherwise
 *   (a LangGraph adapter emits `phase:node`), so no row can be shown a
 *   model — the column says "on the trace" and links there, and the
 *   page's help text says why, until v1.1's explicit association.
 */
export function modelColumnMode(
  steps: StepProgress[],
  agent: AgentInfo | null | undefined,
): ModelColumnMode {
  const llmIds = new Set((agent?.llm?.steps ?? []).map((s) => s.id));
  if (llmIds.size === 0) return "hidden";
  return steps.some((s) => llmIds.has(s.step_id)) ? "model" : "trace";
}

export type ModelCell =
  | { kind: "model"; model: string }
  | { kind: "trace" }
  | { kind: "none" };

/** What one row's model cell shows under the column's mode. */
export function modelCell(step: StepProgress, mode: ModelColumnMode): ModelCell {
  // A recorded model is the gateway's own record, keyed by the same id
  // this row carries: exact, never inferred.
  if (step.model) return { kind: "model", model: step.model };
  if (mode === "trace") return { kind: "trace" };
  return { kind: "none" };
}

export const MODEL_COLUMN_HELP =
  "A model is shown only where a progress row's id is the LLM step that made " +
  "the call — the gateway records the model by step id, and the page never " +
  "guesses from a name. An agent whose rows are named differently records " +
  "the model on the trace instead.";

// ------------------------------------------------------------------- errors --

/** The customer-safe line for a run in `error`: the chassis's sentence
 * for its code, or a generic one. Never the agent's own failure text. */
export function errorMessage(detail: Pick<RunDetail, "error_message">): string {
  return detail.error_message?.trim() || "This run ended in error.";
}

/** "Run again": the new-run page, this run's agent selected and its
 * inputs loaded (they are the redacted, stored inputs). */
export function runAgainHref(detail: Pick<RunDetail, "id" | "agent_id">): string {
  const params = new URLSearchParams();
  if (detail.agent_id) params.set("agent", detail.agent_id);
  params.set("from", detail.id);
  return `/runs/new?${params.toString()}`;
}

// ----------------------------------------------------------------- duration --

/**
 * The dashboard's duration column: created → updated for a finished or
 * parked run (the row's `updated_at` moves on every status write), and
 * created → now for one still running. Whole seconds, then minutes and
 * hours, because a run is minutes long, not milliseconds.
 */
export function durationLabel(
  createdAt: string,
  updatedAt: string,
  status: RunStatus,
  now: number = Date.now(),
): string {
  const start = Date.parse(createdAt);
  const end = RUNNING_STATUSES.has(status) ? now : Date.parse(updatedAt);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return "—";
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ${seconds % 60} s`;
  const hours = Math.floor(minutes / 60);
  return `${hours} h ${minutes % 60} min`;
}

/** The badge on an agent card: the framework the manifest names, else the runtime. */
export function frameworkBadge(agent: Pick<AgentInfo, "framework" | "runtime">): string {
  const framework = agent.framework?.trim();
  if (framework) return framework;
  return agent.runtime === "container" ? "container" : "in-process";
}
