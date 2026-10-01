import { X509Certificate } from "node:crypto";
import { readFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";

/**
 * The admin walk, gate R20 (K blueprint B1b; D29, D42, L31, L43): what a
 * platform admin does with no shell, in a real browser, over HTTPS — and
 * what a tenant admin sees of the platform operator's pages.
 *
 * Off unless `LR_ADMIN_WALK=1`, a guard of its own, never `LR_PROVIDER_WALK`:
 * the providers walk runs before T2's steps, where the CA this walk loads
 * would change the chain T2's first step reads. `librerun-smoke` → `tls-edge`
 * runs it after T2's restore, naming this file, with the CA the edge serves
 * by then (the job's, through `LIBRERUN_TLS_CA`) and the CA this walk loads
 * both in Chromium's NSS store and in `NODE_EXTRA_CA_CERTS` — never
 * `ignoreHTTPSErrors`, which hides the chain a browser checks (D42). The same
 * job runs it first with neither trusted, and it must fail at its first page.
 *
 * Every value it types is a throwaway the job makes at run time and passes
 * here by environment, masked: none is a literal in this file.
 *
 *   LR_ADMIN_WALK            1 to run at all
 *   LR_BASE_URL              the edge (the playwright config's baseURL)
 *   LR_EMAIL / LR_PASSWORD   a platform admin
 *   LR_GATEWAY_FINGERPRINT   SHA256:<hex> from the gateway's boot line (D34)
 *   LR_WALK_PROVIDER         the provider whose key is pasted (default openai)
 *   LR_WALK_PROVIDER_KEY     that key, sealed in the browser (K7)
 *   LR_WALK_AZURE_SECRET     a value for auth.azure_client_secret (K6)
 *   LR_WALK_AGENT            the agent whose setting and tool secret are set (default vita-v1)
 *   LR_WALK_SETTING          a value for its pinecone_top_k, other than its default (K5a)
 *   LR_WALK_TOOL_SECRET      a value for its tavily_api_key, this tenant's (K8a)
 *   LR_WALK_KEY_AGENT        an agent id no agent has registered, for a first key (K9)
 *   LR_WALK_CA_CERT          a path: the CA the walk loads, PEM (T2)
 *   LR_WALK_CA_KEY           a path: its private key, PEM
 *   LR_TENANT_ADMIN_TOKEN    a session of a tenant admin the job made (the last test says why)
 */

const WALK = process.env.LR_ADMIN_WALK === "1";

test.describe.configure({ mode: "serial" });
test.skip(!WALK, "LR_ADMIN_WALK=1 turns this walk on; tls-edge sets it");

function need(name: string): string {
  const value = process.env[name] ?? "";
  if (!value) throw new Error(`${name} is required`);
  return value;
}

// Where a provider's key comes from, in the chip's words (ModelProviders.tsx).
const PROVIDER_CHIP = /^(from gateway\.env|runtime · [0-9a-f]{12}|pending|rejected · \w+|not set)$/;

// The first page, before anything else: with the edge's CA untrusted the walk
// must fail here, on the certificate, and nowhere later.
async function signIn(page: Page, origin: string): Promise<void> {
  await page.goto(`${origin}/login`);
  await page.getByLabel("Email").fill(need("LR_EMAIL"));
  await page.getByLabel("Password").fill(need("LR_PASSWORD"));
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 60_000 });
}

// The session lives in the page's memory and never in storage, so a load
// (page.goto) after sign-in is a signed-out page: every move from here on is
// a click, as a person makes it.
async function openHub(page: Page): Promise<void> {
  await page.getByRole("link", { name: "Admin", exact: true }).click();
  await page.waitForURL((url) => url.pathname === "/admin");
}

async function openSettings(page: Page): Promise<void> {
  await openHub(page);
  await page.getByRole("link", { name: /Application Settings/ }).click();
  await page.waitForURL((url) => url.pathname === "/admin/settings");
}

async function openAgent(page: Page, agent: string): Promise<void> {
  await openHub(page);
  await page.locator(`a[href="/admin/agents/${agent}/config"]`).first().click();
  await page.waitForURL((url) => url.pathname === `/admin/agents/${agent}/config`);
}

test("a platform admin, with no shell, sets each kind of secret, keys an agent and loads a CA", async ({
  page,
  baseURL,
}) => {
  const origin = baseURL ?? "https://librerun.test:8443";
  await signIn(page, origin);
  expect(await page.evaluate(() => window.isSecureContext)).toBe(true);
  const typed: string[] = [];

  // Which providers have keys, and from where; then a key, sealed to the key
  // the gateway logged at boot (D34) and adopted with nothing restarted (K7).
  await openSettings(page);
  await expect(page.getByRole("heading", { name: "Model providers" })).toBeVisible();
  await expect(page.getByTestId("providers-fingerprint")).toHaveText(need("LR_GATEWAY_FINGERPRINT"));
  for (const name of ["openai", "anthropic", "google"]) {
    await expect(page.getByTestId(`provider-chip-${name}`)).toHaveText(PROVIDER_CHIP);
  }
  const provider = process.env.LR_WALK_PROVIDER ?? "openai";
  const providerKey = need("LR_WALK_PROVIDER_KEY");
  typed.push(providerKey);
  const providerInput = page.getByTestId(`provider-input-${provider}`);
  await providerInput.fill(providerKey);
  await page.getByTestId(`provider-set-${provider}`).click();
  await expect(providerInput).toHaveValue("");
  await expect(page.getByTestId(`provider-chip-${provider}`)).toHaveText(/^runtime · [0-9a-f]{12}$/, {
    timeout: 60_000,
  });
  expect(await page.content()).not.toContain(providerKey);

  // An OAuth secret, into the backend's store (K6): its fingerprint, never its value.
  const azure = need("LR_WALK_AZURE_SECRET");
  typed.push(azure);
  const azureInput = page.getByLabel("New value for auth.azure_client_secret", { exact: true });
  await azureInput.fill(azure);
  await azureInput.locator("xpath=..").getByRole("button", { name: /^(Set|Replace)$/ }).click();
  await expect(page.getByTestId("secret-source-auth.azure_client_secret")).toHaveText(/^runtime · [0-9a-f]{12}$/);
  await expect(azureInput).toHaveValue("");
  expect(await page.content()).not.toContain(azure);

  // The certificate the edge serves and what it needs (T2, L43): after the
  // restore, the job's CA through LIBRERUN_TLS_CA, and a root changed since
  // it was acknowledged.
  const serving = page.getByTestId("tls-serving");
  await expect(serving).toContainText(/the edge's own CA \(env-[0-9a-f]{12}\)/);
  await expect(page.getByTestId("tls-source")).toContainText("From the environment (LIBRERUN_TLS_CA).");
  await expect(page.getByTestId("tls-need-root_changed")).toBeVisible();

  // A first key for an agent id that has none, from the hub's field: shown
  // once, then rotated with a grace (K9). An environment key, as the example
  // containers are keyed, offers neither.
  await openHub(page);
  const keyAgent = need("LR_WALK_KEY_AGENT");
  await page.getByLabel(/Agent id, for a first key/).fill(keyAgent);
  await page.getByRole("button", { name: "Open", exact: true }).click();
  await page.waitForURL((url) => url.pathname === `/admin/agents/${keyAgent}/config`);
  await expect(page.getByTestId("agent-unregistered")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Keys" })).toHaveAttribute("aria-selected", "true");
  const keys = page.locator("#agent-panel-keys");
  await keys.getByRole("button", { name: "Issue a key" }).click();
  const minted = keys.getByTestId("keys-minted");
  await expect(minted).toBeVisible();
  const first = await minted.getByLabel("New key").inputValue();
  expect(first).toMatch(/^lr_agent_[A-Za-z0-9_-]{43}$/);
  await expect(keys.getByTestId("keys-unregistered")).toBeVisible();
  const rows = keys.getByTestId("keys-rows").locator("tbody tr");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText(`lr_agent_${first.slice(9, 17)}…`);
  await expect(rows.first()).toContainText("issued here");
  await minted.getByRole("button", { name: "Done" }).click();
  await expect(minted).toHaveCount(0);
  await keys.getByLabel(/Grace, hours/).fill("1");
  await keys.getByRole("button", { name: "Rotate", exact: true }).click();
  await expect(minted).toBeVisible();
  const second = await minted.getByLabel("New key").inputValue();
  expect(second).toMatch(/^lr_agent_[A-Za-z0-9_-]{43}$/);
  expect(second).not.toBe(first);
  await expect(rows).toHaveCount(2);
  await expect(keys.getByTestId("keys-rows")).toContainText("previous, until");
  await minted.getByRole("button", { name: "Done" }).click();
  await expect(minted).toHaveCount(0);

  // A per-tenant agent setting (K5a) and this tenant's tool secret (K8a).
  const agent = process.env.LR_WALK_AGENT ?? "vita-v1";
  await openAgent(page, agent);
  await page.getByRole("tab", { name: "Settings" }).click();
  const settings = page.locator("#agent-panel-settings");
  const value = need("LR_WALK_SETTING");
  await settings.locator("#setting-pinecone_top_k").fill(value);
  await settings.getByRole("button", { name: "Save settings" }).click();
  await expect(settings).toContainText("overridden here (the agent's default is 10)");
  await expect(settings.locator("#setting-pinecone_top_k")).toHaveValue(value);

  await page.getByRole("tab", { name: "Secrets" }).click();
  const secret = page.locator('#agent-panel-secrets li[data-secret="tavily_api_key"]');
  const tenantRow = secret.locator('[data-scope-region="agent_tenant"]');
  const toolSecret = need("LR_WALK_TOOL_SECRET");
  typed.push(toolSecret);
  await tenantRow.getByLabel("New value for tavily_api_key (this tenant)").fill(toolSecret);
  await tenantRow.getByRole("button", { name: /^(Set|Replace)$/ }).click();
  await expect(tenantRow).toHaveAttribute("data-row-set", "set");
  await expect(tenantRow.locator("[data-fingerprint]")).toHaveText(/^[0-9a-f]{12}$/);
  await expect(secret.locator("p[data-effective]")).toHaveAttribute("data-effective", "tenant");
  expect(await page.content()).not.toContain(toolSecret);

  // A CA loaded without a shell or a restart (T2, L43): the panel names it,
  // and the job reads a leaf it signed from the edge after the walk.
  await openSettings(page);
  const caPath = need("LR_WALK_CA_CERT");
  const loaded = new X509Certificate(readFileSync(caPath, "utf-8")).fingerprint256.replace(/:/g, "").toLowerCase();
  await page.getByLabel("CA certificate", { exact: true }).setInputFiles(caPath);
  await page.getByLabel("CA private key", { exact: true }).setInputFiles(need("LR_WALK_CA_KEY"));
  await page.getByRole("button", { name: "Load the CA", exact: true }).click();
  await expect(page.getByTestId("tls-source")).toContainText("Chosen on this page", { timeout: 60_000 });
  await expect(page.getByTestId("tls-serving")).toContainText(`the edge's own CA (loaded-${loaded.slice(0, 12)})`);

  // No value typed is on the last page either (L31).
  const content = await page.content();
  for (const each of typed) expect(content).not.toContain(each);
});

test("a tenant admin sees each platform-only page explained, not an error", async ({ browser, baseURL }) => {
  const origin = baseURL ?? "https://librerun.test:8443";
  const token = need("LR_TENANT_ADMIN_TOKEN");
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    // The product signs in the platform tenant alone (auth.py's
    // _default_tenant), so no password signs a tenant admin in. The job made a
    // tenant, an admin of it and a session with the backend's own code; the
    // sign-in answer here is that session, and from /auth/me on every answer
    // is the backend's, to a tenant admin.
    await page.route("**/api/v1/auth/login", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ access_token: token, token_type: "bearer" }),
      }),
    );
    await page.goto(`${origin}/login`);
    await page.getByLabel("Email").fill("tenant-admin@example.com");
    await page.getByLabel("Password").fill("a session stands in for it");
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 60_000 });
    await page.unroute("**/api/v1/auth/login");

    await openHub(page);
    await expect(page.getByTestId("agent-keys-hub")).toHaveCount(0);
    for (const card of ["Application Settings", "Observability"]) {
      const link = page.getByRole("link", { name: new RegExp(card) });
      await expect(link).toContainText("platform operators only");
      await link.click();
      await expect(page.getByRole("heading", { name: card, level: 1 })).toBeVisible();
      const explained = page.getByTestId("platform-only");
      await expect(explained).toContainText("Platform operators only.");
      await expect(explained).toContainText("Your admin account administers your tenant, not the deployment.");
      await expect(page.getByText(/Failed to load|Could not read/)).toHaveCount(0);
      await openHub(page);
    }

    // On an agent's page the Keys tab is the operator's, so it is not offered,
    // and the default a tool secret falls back to is closed, and says why.
    const agent = process.env.LR_WALK_AGENT ?? "vita-v1";
    await page.locator(`a[href="/admin/agents/${agent}/config"]`).first().click();
    await page.waitForURL((url) => url.pathname === `/admin/agents/${agent}/config`);
    await expect(page.getByRole("tab", { name: "Settings" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "Keys" })).toHaveCount(0);
    await page.getByRole("tab", { name: "Secrets" }).click();
    const fallback = page.locator('#agent-panel-secrets li[data-secret="tavily_api_key"] [data-scope-region="agent"]');
    await expect(fallback).toContainText("Platform operators only.");
  } finally {
    await context.close();
  }
});
