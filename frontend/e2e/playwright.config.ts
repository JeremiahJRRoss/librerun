import { defineConfig } from "@playwright/test";

/**
 * The delight gate's D3–D7, driven in a real browser against a booted
 * stack (blueprint S7). Nothing is started here: the stack is whatever
 * `LR_BASE_URL` points at — the compose stack in `librerun-smoke.yml`,
 * or local processes on a laptop. Configuration is by environment:
 *
 *   LR_BASE_URL          the web UI (default http://localhost:3000)
 *   LR_API_URL           the API (default http://localhost:8000/api/v1)
 *   LR_EMAIL / LR_PASSWORD   an account that may submit runs (the demo's admin)
 *   LR_EXPECT_AGENT_COUNT    how many agent cards the demo promises (default 5)
 *   LR_TRACE_VIEWER_URL  Jaeger, when the trace itself should be read back
 *   LR_RESULTS_DIR       where timings.json lands (default e2e/results)
 *
 * One worker, no retries: the walk is timed, and a retry would hide the
 * time a step really took.
 */
export default defineConfig({
  testDir: ".",
  testMatch: /.*\.spec\.ts$/,
  outputDir: "results/test-results",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  // One agent's whole walk (D3 through D6) has to fit; the budgets inside
  // are asserted by the spec itself, this is only the hard stop.
  timeout: 8 * 60 * 1000,
  expect: { timeout: 30_000 },
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: "results/report" }],
    ["json", { outputFile: "results/playwright.json" }],
  ],
  use: {
    baseURL: process.env.LR_BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
  },
});
