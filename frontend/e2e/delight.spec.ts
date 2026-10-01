/**
 * The delight gate, D3–D7, in a browser (blueprint §3.2, batch S7).
 *
 * For EVERY agent card the demo shows: open a new run, press its "Try a
 * sample" chip, submit from the Review step (D3); watch the run page —
 * labelled progress, the phase timeline, the gate if the agent has one,
 * approve (D4); read the result — the agent's report with a PDF export
 * and a saved thumbs-up, or the structured sections with "Copy JSON"
 * (D5); and the "View trace" button pointing at this run's trace (D6).
 * D7 is the same walk over every other agent. Each step is timed and
 * held to its budget, and the times are written to `results/timings.json`
 * as the delight gate's record.
 *
 * Nothing here names an agent. The list of cards comes from the API, the
 * gated `html_report` agent is found by its manifest, and what each run
 * page must show is decided from the agent's declared phases, steps and
 * LLM steps — so a sixth agent is walked the day it lands, and a card
 * that goes missing fails the count.
 */
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const API = (process.env.LR_API_URL ?? "http://localhost:8000/api/v1").replace(/\/$/, "");
const EMAIL = process.env.LR_EMAIL ?? "";
const PASSWORD = process.env.LR_PASSWORD ?? "";
const EXPECT_AGENTS = Number(process.env.LR_EXPECT_AGENT_COUNT ?? "5");
const TRACE_VIEWER = (process.env.LR_TRACE_VIEWER_URL ?? "").replace(/\/$/, "");
const RESULTS_DIR = process.env.LR_RESULTS_DIR ?? join(__dirname, "results");

// The gate's budgets (blueprint §3.2), in milliseconds.
const BUDGET = {
  D2: 30_000,
  D3: 60_000,
  D4: 180_000,
  D5: 60_000,
  D6: 30_000,
  D7: 300_000,
};

interface AgentRow {
  agent_id: string;
  display_name: string;
  description: string;
  phases: Array<{ name: string; approval: boolean; steps: Array<{ id: string; label: string }> }>;
  output: { mode: "html_report" | "structured" };
  ui: { intake: { steps: Array<{ title: string }> } };
  llm?: { steps: Array<{ id: string; label: string }> };
  has_scenarios: boolean;
  framework?: string;
  runtime?: string;
}

interface AgentTiming {
  agent_id: string;
  display_name: string;
  gated: boolean;
  output_mode: string;
  d3_ms: number;
  d4_ms: number;
  d5_ms: number;
  d6_ms: number;
  run_id: string;
  run_number: string;
  trace_id: string | null;
  model_column: "model" | "trace" | "hidden";
}

const timings: { d2_ms: number | null; agents: AgentTiming[] } = { d2_ms: null, agents: [] };

test.beforeAll(() => {
  if (!EMAIL || !PASSWORD) throw new Error("LR_EMAIL and LR_PASSWORD are required");
});

// ---------------------------------------------------------------- helpers --

async function apiLogin(request: APIRequestContext): Promise<string> {
  const r = await request.post(`${API}/auth/login`, { data: { email: EMAIL, password: PASSWORD } });
  expect(r.ok(), `login via the API: ${r.status()}`).toBeTruthy();
  return (await r.json()).access_token as string;
}

async function listAgents(request: APIRequestContext): Promise<AgentRow[]> {
  const token = await apiLogin(request);
  const r = await request.get(`${API}/agents`, { headers: { Authorization: `Bearer ${token}` } });
  expect(r.ok()).toBeTruthy();
  return (await r.json()) as AgentRow[];
}

/** The demo's primary walk (D3–D6) is the gated `html_report` agent; every other card is D7. */
function orderForTheGate(agents: AgentRow[]): { primary: AgentRow; rest: AgentRow[] } {
  const primary =
    agents.find((a) => a.output.mode === "html_report" && a.phases.some((p) => p.approval)) ??
    agents.find((a) => a.phases.some((p) => p.approval)) ??
    agents[0];
  return { primary, rest: agents.filter((a) => a.agent_id !== primary.agent_id) };
}

/** What the model column must show for this agent, by gap E5's rule. */
function expectedModelColumn(agent: AgentRow): "model" | "trace" | "hidden" {
  const llm = new Set((agent.llm?.steps ?? []).map((s) => s.id));
  if (llm.size === 0) return "hidden";
  const declared = agent.phases.flatMap((p) => p.steps.map((s) => s.id));
  return declared.some((id) => llm.has(id)) ? "model" : "trace";
}

async function signIn(page: Page): Promise<number> {
  const started = Date.now();
  await page.goto("/login");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/dashboard/);
  // D2: the banner says this is demo mode.
  await expect(page.getByTestId("demo-banner")).toBeVisible();
  return Date.now() - started;
}

/**
 * The session is held in memory (the app persists no token by design), so
 * the walk moves through the app's own links exactly as a person does:
 * the brand link back to the dashboard, then "+ New Run". A hard
 * navigation would land on the sign-in page.
 */
async function openNewRun(page: Page): Promise<void> {
  if (!/\/dashboard/.test(page.url())) {
    await page.getByRole("link", { name: "LibreRun" }).click();
    await page.waitForURL(/\/dashboard/);
  }
  await page.getByRole("link", { name: "+ New Run" }).click();
  await page.waitForURL(/\/runs\/new/);
}

async function waitForStatus(page: Page, wanted: string[], timeoutMs: number): Promise<string> {
  const badge = page.getByTestId("run-status");
  await expect
    .poll(async () => (await badge.getAttribute("data-status")) ?? "", { timeout: timeoutMs, intervals: [500] })
    .toMatch(new RegExp(`^(${wanted.join("|")})$`));
  return (await badge.getAttribute("data-status")) ?? "";
}

async function walkOneAgent(page: Page, agent: AgentRow, all: AgentRow[]): Promise<AgentTiming> {
  const gated = agent.phases.some((p) => p.approval);

  // ---- D3: cards → the agent → "Try a sample" → submit ----------------
  let t = Date.now();
  await openNewRun(page);
  const cards = page.getByTestId("agent-card");
  await expect(cards).toHaveCount(all.length);
  const card = page.locator(`[data-testid="agent-card"][data-agent-id="${agent.agent_id}"]`);
  await expect(card).toBeVisible();
  await expect(card.getByRole("heading", { name: agent.display_name })).toBeVisible();
  await expect(card.getByText(agent.description.slice(0, 40))).toBeVisible();
  await expect(card.getByTestId("framework-badge")).toBeVisible();
  const chip = card.getByTestId("sample-chip").first();
  await expect(chip, `${agent.agent_id} offers a sample`).toBeVisible();
  await chip.click();
  // The sample fills the form; a stepped intake opens on its Review
  // step, a single-page one is the whole form — either way the next
  // click is Submit.
  if (agent.ui.intake.steps.length > 0) {
    await expect(page.getByRole("heading", { name: "Review & Submit" })).toBeVisible();
  }
  await page.getByRole("button", { name: "Submit Run" }).click();
  await page.waitForURL(/\/runs\/[0-9a-f-]{36}$/);
  const runId = page.url().split("/").pop() ?? "";
  const d3 = Date.now() - t;

  // ---- D4: progress with labels, the gate, approve ---------------------
  t = Date.now();
  await expect(page.getByTestId("timeline")).toBeVisible();
  if (gated) {
    const status = await waitForStatus(page, ["awaiting_approval", "complete", "error"], BUDGET.D4);
    expect(status, `${agent.agent_id} reached the gate`).toBe("awaiting_approval");
    await expect(page.getByTestId("approval-panel")).toBeVisible();
    await expect(page.getByTestId("approval-summary")).not.toBeEmpty();
    // The timeline parks on the gate.
    await expect(page.locator('[data-testid="timeline-node"][data-kind="gate"][data-state="current"]')).toHaveCount(1);
    await page.getByTestId("approve-button").click();
  }
  const final = await waitForStatus(page, ["complete", "error"], BUDGET.D4);
  expect(final, `${agent.agent_id} completed`).toBe("complete");
  // Every timeline node is done; the progress list shows labelled rows.
  await expect(page.locator('[data-testid="timeline-node"][data-state="done"]')).toHaveCount(
    await page.getByTestId("timeline-node").count(),
  );
  const rows = page.getByTestId("progress-row");
  await expect(rows.first()).toBeVisible();
  const rowCount = await rows.count();
  expect(rowCount).toBeGreaterThan(0);
  for (let i = 0; i < rowCount; i++) {
    const row = rows.nth(i);
    const id = (await row.getAttribute("data-step-id")) ?? "";
    const label = (await row.getByTestId("step-label").textContent())?.trim() ?? "";
    expect(label, `row ${id} is labelled from the manifest`).not.toBe(id);
    expect(label.length).toBeGreaterThan(0);
  }
  // The model column, by the agent's own manifest (gap E5): models where
  // the rows ARE the LLM steps, "on the trace" where they are named
  // otherwise, no column where no model is called.
  const column = expectedModelColumn(agent);
  await expect(page.getByTestId("progress-list")).toHaveAttribute("data-model-column", column);
  if (column === "model") {
    await expect(page.locator('[data-testid="step-model"][data-model-kind="model"]').first()).toBeVisible();
    await expect(page.locator('[data-testid="step-model"][data-model-kind="trace"]')).toHaveCount(0);
  } else if (column === "trace") {
    await expect(page.locator('[data-testid="step-model"][data-model-kind="trace"]').first()).toBeVisible();
    await expect(page.locator('[data-testid="step-model"][data-model-kind="model"]')).toHaveCount(0);
    await expect(page.getByTestId("model-column-help")).toBeVisible();
  }
  const d4 = Date.now() - t;

  // ---- D5: the result; PDF; feedback --------------------------------------
  t = Date.now();
  if (agent.output.mode === "html_report") {
    await expect(page.locator(".report-container")).toBeVisible();
    expect((await page.locator(".report-container").innerText()).length).toBeGreaterThan(200);
    await page.getByRole("button", { name: "Export ▾" }).click();
    // The export opens the rendered PDF in a new tab from a blob the
    // page holds. A blob PDF never fires `load` in headless Chromium, so
    // the tab is not what is checked: the URL handed to `window.open` is
    // captured, fetched inside the page (same object URL), and its bytes
    // must be a PDF. The report renders in a background task the page
    // polls, so this waits as long as the page does.
    await page.evaluate(() => {
      const w = window as unknown as { __opened: string[]; open: typeof window.open };
      w.__opened = [];
      const original = w.open.bind(window);
      w.open = ((url?: string | URL, ...rest: unknown[]) => {
        w.__opened.push(String(url));
        return (original as (...a: unknown[]) => Window | null)(url, ...rest);
      }) as typeof window.open;
    });
    await page.getByRole("button", { name: "PDF" }).click();
    await expect
      .poll(async () => page.evaluate(() => (window as unknown as { __opened: string[] }).__opened.length), {
        timeout: 130_000,
        intervals: [1000],
      })
      .toBeGreaterThan(0);
    const blobUrl = await page.evaluate(() => (window as unknown as { __opened: string[] }).__opened[0]);
    expect(blobUrl, "the PDF opened from a blob the browser holds").toMatch(/^blob:/);
    const head = await page.evaluate(async (url) => {
      const bytes = new Uint8Array(await (await fetch(url)).arrayBuffer());
      return { size: bytes.length, magic: String.fromCharCode(...bytes.slice(0, 5)) };
    }, blobUrl);
    expect(head.magic, "a real PDF").toBe("%PDF-");
    expect(head.size).toBeGreaterThan(1000);
    for (const extra of page.context().pages()) {
      if (extra !== page) await extra.close();
    }
  } else {
    const output = page.getByTestId("structured-output");
    await expect(output).toBeVisible();
    expect(await page.getByTestId("output-section").count()).toBeGreaterThan(0);
    await page.getByTestId("copy-json").click();
    await expect(page.getByText("Result copied as JSON")).toBeVisible();
  }
  // Feedback saves (every bundled manifest declares at least one section).
  await page.getByRole("button", { name: "👍" }).first().click();
  await expect(page.getByText("Feedback saved")).toBeVisible();
  const d5 = Date.now() - t;

  // ---- D6: "View trace" opens the viewer on THIS run -------------------
  t = Date.now();
  const trace = page.getByTestId("view-trace");
  await expect(trace).toBeVisible();
  const href = (await trace.getAttribute("href")) ?? "";
  expect(href).toMatch(/^https?:\/\//);
  await expect(trace).toHaveAttribute("target", "_blank");
  await expect(trace).toHaveAttribute("rel", /noopener/);
  const traceId = href.match(/([0-9a-f]{32})/)?.[1] ?? null;
  expect(traceId, "the link carries a trace id").not.toBeNull();
  if (TRACE_VIEWER) {
    // The trace really is in the viewer, and it is one tree rooted at
    // `run` — read the way the API smoke reads it, with the same
    // patience for two processes' batch exporters.
    await expect
      .poll(
        async () => {
          const r = await page.request.get(`${TRACE_VIEWER}/api/traces/${traceId}`);
          if (!r.ok()) return "no trace yet";
          const spans = ((await r.json()).data?.[0]?.spans ?? []) as Array<{
            spanID: string;
            operationName: string;
            references?: Array<{ spanID: string }>;
          }>;
          if (spans.length === 0) return "no spans yet";
          const ids = new Set(spans.map((s) => s.spanID));
          const roots = spans.filter((s) => !(s.references ?? []).some((ref) => ids.has(ref.spanID)));
          return roots.length === 1 && roots[0].operationName === "run" ? "one tree" : `${roots.length} roots`;
        },
        { timeout: 90_000, intervals: [3000] },
      )
      .toBe("one tree");
  }
  const d6 = Date.now() - t;

  const runNumber = (await page.getByTestId("run-number").textContent())?.trim() ?? "";
  return {
    agent_id: agent.agent_id,
    display_name: agent.display_name,
    gated,
    output_mode: agent.output.mode,
    d3_ms: d3,
    d4_ms: d4,
    d5_ms: d5,
    d6_ms: d6,
    run_id: runId,
    run_number: runNumber,
    trace_id: traceId,
    model_column: column,
  };
}

// ------------------------------------------------------------------ tests --

test.describe.serial("the delight gate, D3–D7, over every agent card", () => {
  let agents: AgentRow[] = [];

  test("D2/D3 — the demo's cards are all there, and signing in shows the banner", async ({ page, request }) => {
    agents = await listAgents(request);
    expect(agents.length, `the demo promises ${EXPECT_AGENTS} agent cards`).toBe(EXPECT_AGENTS);
    for (const a of agents) {
      expect(a.display_name, a.agent_id).not.toBe(a.agent_id);
      expect(a.description.length, `${a.agent_id} describes itself`).toBeGreaterThan(10);
      expect(a.has_scenarios, `${a.agent_id} ships a sample`).toBe(true);
    }
    timings.d2_ms = await signIn(page);
    expect(timings.d2_ms).toBeLessThan(BUDGET.D2);
    await openNewRun(page);
    await expect(page.getByTestId("agent-card")).toHaveCount(EXPECT_AGENTS);
  });

  test("D3–D6 — the gated agent: sample, gate, report, PDF, feedback, trace", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await signIn(page);
    const { primary } = orderForTheGate(agents);
    const timing = await walkOneAgent(page, primary, agents);
    timings.agents.push(timing);
    expect(timing.d3_ms, "D3 under budget").toBeLessThan(BUDGET.D3);
    expect(timing.d4_ms, "D4 under budget").toBeLessThan(BUDGET.D4);
    expect(timing.d5_ms, "D5 under budget").toBeLessThan(BUDGET.D5);
    expect(timing.d6_ms, "D6 under budget").toBeLessThan(BUDGET.D6);
  });

  test("D7 — every other agent card completes its own sample", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await signIn(page);
    const { rest } = orderForTheGate(agents);
    expect(rest.length).toBe(EXPECT_AGENTS - 1);
    const started = Date.now();
    for (const agent of rest) {
      timings.agents.push(await walkOneAgent(page, agent, agents));
    }
    const d7 = Date.now() - started;
    expect(d7, "D7 under budget").toBeLessThan(BUDGET.D7);
    // The dashboard lists every run of this walk with a duration.
    await page.getByRole("link", { name: "LibreRun" }).click();
    await page.waitForURL(/\/dashboard/);
    for (const t of timings.agents) {
      await expect(page.getByText(t.run_number, { exact: true })).toBeVisible();
    }
    await expect(page.getByTestId("run-duration").first()).not.toBeEmpty();
  });

  test.afterAll(() => {
    mkdirSync(RESULTS_DIR, { recursive: true });
    const d7_total_ms = timings.agents.slice(1).reduce((n, t) => n + t.d3_ms + t.d4_ms + t.d5_ms + t.d6_ms, 0);
    writeFileSync(
      join(RESULTS_DIR, "timings.json"),
      JSON.stringify({ recorded_at: new Date().toISOString(), budgets_ms: BUDGET, d7_total_ms, ...timings }, null, 2),
    );
  });
});
