import { expect, test, type BrowserContext, type Page } from "@playwright/test";

/**
 * Model providers in a real browser (K7; L33, D19, D34, D42).
 *
 * Off unless `LR_PROVIDER_WALK=1`: this one needs a stack set up for it, and
 * every step that runs Playwright names its spec — `scenario-smoke`'s runs
 * `delight.spec.ts` alone. Two jobs set this one up, and name this file on
 * the command line:
 *
 * - `tls-edge` — first `LR_PLAIN_URL` (`http://librerun.test:3000`), a plain
 *   origin that is not a secure context: the section explains and never
 *   posts (D19). That runs in a context that has never opened the HTTPS
 *   origin, because the edge sends HSTS for `librerun.test` and Chromium
 *   would upgrade the plain origin afterwards. Then `LR_BASE_URL`
 *   (`https://librerun.test:8443`), the edge, whose root the job trusts in
 *   Chromium's NSS store and in Node's `NODE_EXTRA_CA_CERTS` — never
 *   `ignoreHTTPSErrors`, which hides the chain a browser checks (D42): a key
 *   is sealed and adopted, and not one page raises a Content-Security-Policy
 *   violation under the edge's policy.
 * - `provider-keys` — `LR_BASE_URL` on `localhost`, a secure context over
 *   plain HTTP: the section shows the fingerprint the gateway logged
 *   (`LR_GATEWAY_FINGERPRINT`) and takes `LR_PROVIDER_KEY`, which the job's
 *   mock provider then sees on the next model call.
 *
 *   LR_PROVIDER_WALK        1 to run at all
 *   LR_BASE_URL             the web UI (the playwright config's baseURL)
 *   LR_API_URL              the API, for the run the edge walk opens (optional)
 *   LR_EMAIL / LR_PASSWORD  a platform admin
 *   LR_PLAIN_URL            a plain-HTTP origin that must refuse (optional)
 *   LR_GATEWAY_FINGERPRINT  SHA256:<hex> from the gateway's boot line (optional)
 *   LR_PROVIDER_KEY         a key to paste (optional)
 *   LR_PROVIDER_NAME        which provider it is for (default openai)
 */

const WALK = process.env.LR_PROVIDER_WALK === "1";
const EMAIL = process.env.LR_EMAIL ?? "";
const PASSWORD = process.env.LR_PASSWORD ?? "";
const PLAIN = process.env.LR_PLAIN_URL ?? "";
const API = process.env.LR_API_URL ?? "";
const FINGERPRINT = process.env.LR_GATEWAY_FINGERPRINT ?? "";
const KEY = process.env.LR_PROVIDER_KEY ?? "";
const PROVIDER = process.env.LR_PROVIDER_NAME ?? "openai";

test.describe.configure({ mode: "serial" });
test.skip(!WALK, "LR_PROVIDER_WALK=1 turns this walk on; the jobs that need it set it");

async function signIn(page: Page, origin: string): Promise<void> {
  if (!EMAIL || !PASSWORD) throw new Error("LR_EMAIL and LR_PASSWORD are required");
  await page.goto(`${origin}/login`);
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 60_000 });
}

// The session lives in the page's memory and never in storage, so a load
// (page.goto) after sign-in is a signed-out page: every move from here on
// is a click, as a person makes it.
async function openSettings(page: Page): Promise<void> {
  await page.getByRole("link", { name: "Admin", exact: true }).click();
  await page.getByRole("link", { name: /Application Settings/ }).click();
  await page.waitForURL((url) => url.pathname === "/admin/settings");
}

/** Every request the page makes that would store a provider key. */
function postsOfKeys(page: Page): string[] {
  const seen: string[] = [];
  page.on("request", (request) => {
    if (/\/admin\/providers\/[^/]+\/key$/.test(new URL(request.url()).pathname) && request.method() !== "GET") {
      seen.push(`${request.method()} ${request.url()}`);
    }
  });
  return seen;
}

/** Every Content-Security-Policy violation any page of the context raises,
 * kept here in the test rather than in a page, whose window each navigation
 * replaces. The listener is installed before any script of a page runs, and
 * reports through a binding, which the policy does not govern; Chromium's
 * own console line for a refused load is kept too. */
async function recordViolations(context: BrowserContext): Promise<string[]> {
  const seen: string[] = [];
  await context.exposeBinding("__lrCspViolation", ({ page }, text: string) => {
    seen.push(`${page.url()}: ${text}`);
  });
  await context.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      const report = (window as unknown as { __lrCspViolation?: (text: string) => void }).__lrCspViolation;
      report?.(`${event.violatedDirective} ${event.blockedURI} ${event.sourceFile}:${event.lineNumber}`);
    });
  });
  context.on("console", (message) => {
    if (/Content Security Policy/i.test(message.text())) seen.push(`console: ${message.text()}`);
  });
  return seen;
}

test("the plain origin explains and never posts (D19)", async ({ browser }) => {
  test.skip(!PLAIN, "no plain origin to walk (LR_PLAIN_URL)");
  // A context that has never opened the HTTPS origin, or HSTS would upgrade
  // this one (K blueprint §11, T1 item 6; D42).
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    const posts = postsOfKeys(page);
    await signIn(page, PLAIN);
    expect(await page.evaluate(() => window.isSecureContext)).toBe(false);
    await openSettings(page);
    const refusal = page.getByTestId("providers-refusal");
    await expect(refusal).toBeVisible();
    await expect(refusal).toContainText("not a secure context");
    await expect(refusal).toContainText("gateway.env");
    await expect(page.getByTestId(`provider-input-${PROVIDER}`)).toHaveCount(0);
    expect(posts).toEqual([]);
  } finally {
    await context.close();
  }
});

test("a key is sealed to the gateway, adopted, and no page breaks the policy", async ({ browser, baseURL }) => {
  const origin = baseURL ?? "http://localhost:3000";
  const context = await browser.newContext();
  const policyViolations = await recordViolations(context);
  try {
    const page = await context.newPage();
    await signIn(page, origin);
    expect(await page.evaluate(() => window.isSecureContext)).toBe(true);

    await openSettings(page);
    await expect(page.getByRole("heading", { name: "Model providers" })).toBeVisible();
    await expect(page.getByTestId("providers-refusal")).toHaveCount(0);
    const shown = page.getByTestId("providers-fingerprint");
    await expect(shown).toHaveText(/^SHA256:[0-9a-f]{64}$/);
    if (FINGERPRINT) {
      // D34: the page seals to the key the gateway logged at boot.
      await expect(shown).toHaveText(FINGERPRINT);
    }

    if (KEY) {
      const input = page.getByTestId(`provider-input-${PROVIDER}`);
      await input.fill(KEY);
      await page.getByTestId(`provider-set-${PROVIDER}`).click();
      await expect(input).toHaveValue("");
      await expect(page.getByTestId(`provider-chip-${PROVIDER}`)).toHaveText(/^runtime · [0-9a-f]{12}$/, {
        timeout: 60_000,
      });
      // The key is on no page, and was never in the field's value after Set.
      expect(await page.content()).not.toContain(KEY);
    }

    // Pages beyond settings, under the same policy: the dashboard and, when
    // the API is named, a finished run's rendered report — which carries its
    // own <style>.
    await page.getByRole("link", { name: "LibreRun", exact: true }).click();
    await page.waitForURL((url) => url.pathname === "/dashboard");
    await page.waitForLoadState("networkidle");
    if (API) {
      const login = await page.request.post(`${API}/auth/login`, { data: { email: EMAIL, password: PASSWORD } });
      expect(login.ok(), `login via the API: ${login.status()}`).toBeTruthy();
      const headers = { Authorization: `Bearer ${(await login.json()).access_token as string}` };
      const runs = await page.request.get(`${API}/runs`, { headers });
      expect(runs.ok(), `GET /runs: ${runs.status()}`).toBeTruthy();
      const list = ((await runs.json()).runs ?? []) as { id: string; run_number: string; status: string }[];
      let report: { id: string; run_number: string } | undefined;
      for (const run of list.filter((each) => each.status === "complete")) {
        const detail = await (await page.request.get(`${API}/runs/${run.id}`, { headers })).json();
        if (detail.output_mode === "html_report") {
          report = run;
          break;
        }
      }
      // The job named the API because it has finished a run: its report is
      // the page with the most to break, so its absence is a failure.
      expect(report, "a finished run with a rendered report").toBeTruthy();
      await page.locator("tr", { hasText: report?.run_number ?? "" }).first().click();
      await page.waitForURL((url) => url.pathname === `/runs/${report?.id}`);
      await expect(page.locator(".report-container")).toBeVisible({ timeout: 60_000 });
      await page.waitForLoadState("networkidle");
    }
    expect(policyViolations).toEqual([]);
  } finally {
    await context.close();
  }
});
