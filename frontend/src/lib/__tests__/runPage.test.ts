import { describe, expect, it } from "vitest";
import {
  buildTimeline,
  durationLabel,
  errorMessage,
  frameworkBadge,
  humanize,
  modelCell,
  modelColumnMode,
  pollDelayMs,
  runAgainHref,
  stepLabel,
  stepLabels,
} from "../runPage";
import type { AgentInfo, StepProgress } from "../../types";

function agent(overrides: Partial<AgentInfo> = {}): AgentInfo {
  return {
    agent_id: "toy-v1",
    display_name: "Toy",
    description: "A toy",
    has_config: false,
    phases: [
      { name: "analyze", approval: false, steps: [{ id: "classify", label: "Classify the request" }] },
      { name: "investigate", approval: true, steps: [{ id: "draft", label: "Draft the plan" }] },
    ],
    ui: { intake: { steps: [] } },
    output: { mode: "structured" },
    capabilities: ["llm"],
    llm: {
      steps: [
        { id: "classify", label: "Classification (admin label)" },
        { id: "draft", label: "Drafting (admin label)" },
      ],
      redact_outbound: true,
    },
    feedback_sections: [],
    has_scenarios: true,
    ...overrides,
  };
}

const states = (nodes: ReturnType<typeof buildTimeline>) => nodes.map((n) => `${n.id}=${n.state}`);

describe("the phase timeline (blueprint S7)", () => {
  it("draws submitted → phases with a gate before each approval → complete, from the manifest", () => {
    const nodes = buildTimeline(agent().phases, "submitted", null);
    expect(nodes.map((n) => n.id)).toEqual([
      "submitted",
      "phase:analyze",
      "gate:investigate",
      "phase:investigate",
      "terminal",
    ]);
    expect(nodes.map((n) => n.label)).toEqual(["Submitted", "Analyze", "Approval", "Investigate", "Complete"]);
    expect(states(nodes)).toEqual([
      "submitted=current",
      "phase:analyze=pending",
      "gate:investigate=pending",
      "phase:investigate=pending",
      "terminal=pending",
    ]);
  });

  it("marks the phase the progress endpoint names as current while running", () => {
    expect(states(buildTimeline(agent().phases, "refining", "analyze"))).toEqual([
      "submitted=done",
      "phase:analyze=current",
      "gate:investigate=pending",
      "phase:investigate=pending",
      "terminal=pending",
    ]);
    expect(states(buildTimeline(agent().phases, "investigating", "investigate"))).toEqual([
      "submitted=done",
      "phase:analyze=done",
      "gate:investigate=done",
      "phase:investigate=current",
      "terminal=pending",
    ]);
  });

  it("falls back to the first phase while the cursor is still unknown", () => {
    expect(states(buildTimeline(agent().phases, "refining", null))[1]).toBe("phase:analyze=current");
  });

  it("parks on the gate after the phase that produced the output", () => {
    expect(states(buildTimeline(agent().phases, "awaiting_approval", "analyze"))).toEqual([
      "submitted=done",
      "phase:analyze=done",
      "gate:investigate=current",
      "phase:investigate=pending",
      "terminal=pending",
    ]);
  });

  it("is all done at complete, and names the failing phase at error", () => {
    expect(new Set(buildTimeline(agent().phases, "complete", "investigate").map((n) => n.state))).toEqual(
      new Set(["done"]),
    );
    const failed = buildTimeline(agent().phases, "error", "investigate");
    expect(states(failed)).toEqual([
      "submitted=done",
      "phase:analyze=done",
      "gate:investigate=done",
      "phase:investigate=error",
      "terminal=error",
    ]);
    expect(failed[failed.length - 1].label).toBe("Error");
  });

  it("degrades to submitted → terminal when the agent is gone", () => {
    expect(buildTimeline(null, "error", null).map((n) => `${n.id}=${n.state}`)).toEqual([
      "submitted=done",
      "terminal=error",
    ]);
    expect(buildTimeline([], "complete", null).map((n) => n.state)).toEqual(["done", "done"]);
  });

  it("humanizes manifest identifiers without inventing words", () => {
    expect(humanize("refine_problem")).toBe("Refine problem");
    expect(humanize("analyze:classify")).toBe("Analyze classify");
    expect(humanize("")).toBe("");
  });
});

describe("progress labels come from the manifest", () => {
  it("prefers phases[].steps[], then the LLM step's label, then the raw id", () => {
    const a = agent();
    expect(stepLabels(a)).toEqual({ classify: "Classify the request", draft: "Draft the plan" });
    expect(stepLabel("classify", a)).toBe("Classify the request");
    const llmOnly = agent({ phases: [{ name: "analyze", approval: false, steps: [] }] });
    expect(stepLabel("classify", llmOnly)).toBe("Classification (admin label)");
    expect(stepLabel("search_the_kb", a)).toBe("search_the_kb");
    expect(stepLabel("anything", null)).toBe("anything");
  });

  it("keeps the first declaration when two phases declare one id", () => {
    const a = agent({
      phases: [
        { name: "one", approval: false, steps: [{ id: "x", label: "First" }] },
        { name: "two", approval: false, steps: [{ id: "x", label: "Second" }] },
      ],
    });
    expect(stepLabel("x", a)).toBe("First");
  });
});

describe("the model column (gap E5: exact id, never inference)", () => {
  const row = (step_id: string, model?: string | null): StepProgress => ({
    step_id,
    status: "complete",
    model: model ?? null,
  });

  it("is hidden for an agent that declares no LLM steps", () => {
    const none = agent({ llm: { steps: [], redact_outbound: true } });
    expect(modelColumnMode([row("classify")], none)).toBe("hidden");
    expect(modelColumnMode([row("classify")], null)).toBe("hidden");
  });

  it("shows models only where a row's id IS an LLM step id", () => {
    const mode = modelColumnMode([row("classify", "gpt-4o-mini"), row("search")], agent());
    expect(mode).toBe("model");
    expect(modelCell(row("classify", "gpt-4o-mini"), mode)).toEqual({ kind: "model", model: "gpt-4o-mini" });
    // An LLM step the gateway has not recorded (yet): blank, not guessed.
    expect(modelCell(row("draft"), mode)).toEqual({ kind: "none" });
    // A row that is no LLM step at all, on an agent whose ids match: blank.
    expect(modelCell(row("search"), mode)).toEqual({ kind: "none" });
  });

  it("says 'on the trace' for an agent whose row ids differ from its step ids", () => {
    // A LangGraph adapter emits phase:node; `analyze:classify` is not
    // the step `classify`, and must not be attributed its model.
    const rows = [row("analyze:classify"), row("investigate:draft")];
    const mode = modelColumnMode(rows, agent());
    expect(mode).toBe("trace");
    expect(modelCell(rows[0], mode)).toEqual({ kind: "trace" });
    // ...unless the gateway did record one under that exact id.
    expect(modelCell(row("analyze:classify", "gpt-4o"), mode)).toEqual({ kind: "model", model: "gpt-4o" });
  });

  it("never matches on a suffix", () => {
    expect(modelColumnMode([row("phase:classify")], agent())).toBe("trace");
  });
});

describe("errors are sanitized and offer a way back", () => {
  it("shows the chassis sentence, never the agent's text, with a generic fallback", () => {
    expect(errorMessage({ error_message: "The platform restarted while this run was in the middle of a phase." })).toContain(
      "restarted",
    );
    expect(errorMessage({ error_message: null })).toBe("This run ended in error.");
    expect(errorMessage({ error_message: "   " })).toBe("This run ended in error.");
  });

  it("links 'Run again' at the new-run page with the agent and the source run", () => {
    expect(runAgainHref({ id: "abc", agent_id: "toy-v1" })).toBe("/runs/new?agent=toy-v1&from=abc");
    expect(runAgainHref({ id: "abc", agent_id: null })).toBe("/runs/new?from=abc");
  });
});

describe("polling (D8): 2 s while unfinished, stopped at terminal states", () => {
  it("polls every two seconds until the run finishes", () => {
    for (const status of ["submitted", "refining", "awaiting_approval", "investigating"] as const) {
      expect(pollDelayMs(status)).toBe(2000);
    }
    expect(pollDelayMs(undefined)).toBe(2000);
  });
  it("stops at complete and error", () => {
    expect(pollDelayMs("complete")).toBeNull();
    expect(pollDelayMs("error")).toBeNull();
  });
});

describe("the dashboard's duration column", () => {
  const created = "2026-09-22T10:00:00Z";
  it("measures created → updated once the run has stopped moving", () => {
    expect(durationLabel(created, "2026-09-22T10:00:42Z", "complete")).toBe("42 s");
    expect(durationLabel(created, "2026-09-22T10:03:05Z", "awaiting_approval")).toBe("3 min 5 s");
    expect(durationLabel(created, "2026-09-22T11:02:00Z", "error")).toBe("1 h 2 min");
  });
  it("measures created → now while the run is still running", () => {
    const now = Date.parse("2026-09-22T10:00:10Z");
    expect(durationLabel(created, created, "investigating", now)).toBe("10 s");
  });
  it("never goes negative and copes with garbage", () => {
    expect(durationLabel(created, "2026-09-22T09:59:00Z", "complete")).toBe("0 s");
    expect(durationLabel("not a date", created, "complete")).toBe("—");
  });
});

describe("the card badge", () => {
  it("shows the framework the manifest names, else the runtime", () => {
    expect(frameworkBadge({ framework: "langgraph", runtime: "python-package" })).toBe("langgraph");
    expect(frameworkBadge({ framework: "", runtime: "container" })).toBe("container");
    expect(frameworkBadge({ framework: undefined, runtime: "python-package" })).toBe("in-process");
  });
});
