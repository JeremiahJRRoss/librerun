/**
 * Every field says where its value lives (K9; D30, the tiers of the
 * configuration blueprint's §1.2).
 *
 * On each admin page that edits something, every editable region carries
 * `data-scope-region` and exactly one scope chip of its own — or, for an
 * agent still on the deprecated settings surface, the note that stands in
 * for one — and every input, select and textarea sits inside a region. The
 * rule keys on the attribute's presence, never its value: two regions may
 * share a value (a tool secret's two rows per name do). The mock answers
 * `/admin/tls` with the edge on and `/admin/providers` with a key to seal
 * to, since both panels render their inputs only then, and an answer that
 * hid them would let the rule pass without looking. The agent id is a
 * synthetic one: this file names no agent (L13).
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentConfigResponse, UserProfile } from "../../../types";

const AGENT = "probe-agent";
const PEM = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n";

const state = vi.hoisted(() => ({
  user: null as UserProfile | null,
  deprecated: false,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: () => undefined, replace: () => undefined }),
}));
vi.mock("../../../components/NavBar", () => ({ default: () => null }));
vi.mock("../../../lib/auth", () => ({ useAuth: () => ({ token: "tok", user: state.user }) }));
vi.mock("../../../lib/toast", () => ({ useToast: () => ({ toast: () => undefined }) }));
vi.mock("../../../lib/sealing", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/sealing")>()),
  canSeal: () => true,
  sealingFingerprint: async () => "SHA256:probe",
}));
vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    apiFetch: async (path: string) => {
      if (path === "/health") return { status: "ok", env: "test" };
      if (path === "/admin/settings") return settingRows();
      if (path === "/admin/providers") {
        return {
          reported: true, stub: false, gateway_version: "0", updated_at: null, public_key_pem: PEM,
          providers: [{ name: "openai", aliases: ["openai"], source: "env", fingerprint: null, set_by: null, set_at: null, row: null, reason: null }],
        };
      }
      if (path === "/admin/tls") return tlsOn();
      if (path === "/admin/deployment") return deploymentView();
      if (path === "/admin/users") {
        return [{ id: "u-2", email: "b@example.com", display_name: null, role: "customer", auth_provider: "local", last_sign_in: null, is_active: true }];
      }
      if (path === "/admin/auth-config") {
        return { google_enabled: false, google_allowed_domains: [], google_allowed_emails: [], microsoft_enabled: false, credentials_enabled: true };
      }
      if (path === `/agents/${AGENT}/config`) return agentConfig(state.deprecated);
      if (path === `/agents/${AGENT}/secrets`) {
        const row = { set: false, fingerprint: null, updated_at: null, updated_by: null, last_used_at: null };
        return { agent_id: AGENT, runtime: "python-package", secrets: [{ name: "search_key", effective: "unset", tenant: row, agent: row, environment: { set: false } }] };
      }
      if (path.startsWith("/admin/agent-keys")) {
        return [{ agent_id: AGENT, key_prefix: "probepro", source: "admin", role: "current", issued_at: "2026-09-30T12:00:00Z", issued_by: null, previous_since: null, previous_until: null, last_used_at: null, rotatable: true, registered: true }];
      }
      throw new Error(`unexpected ${path}`);
    },
  };
});

function settingRows() {
  return [
    { key: "trace_viewer_url_template", value: "", default_value: "", value_type: "string", description: "", is_default: true, updated_at: null, updated_by: null, secret: null },
    { key: "cors_origins", value: ["http://localhost:3000"], default_value: ["http://localhost:3000"], value_type: "string_list", description: "", is_default: true, updated_at: null, updated_by: null, secret: null },
    { key: "auth.azure_client_secret", value: null, default_value: null, value_type: "secret", description: "", is_default: true, updated_at: null, updated_by: null, secret: { set: false, source: "unset", fingerprint: null, updated_at: null, updated_by: null } },
  ];
}

function tlsOn() {
  return {
    edge: "on", site: ["librerun.example.lan"], issuer: { kind: "internal", ca: "local", email: null }, root: null,
    leaf: null, source: "environment", choice: null, environment: { variable: "LIBRERUN_TLS", ca: null },
    why: null, needs: [], acknowledged_root: null,
  };
}

function deploymentView() {
  return {
    version: "0", license: "AGPL-3.0-only", source_url: null, demo: true, stub: true,
    gateway: { reachable: true, reported: true, version: "0", updated_at: null, providers: [] },
    settings: [{ name: "LOG_LEVEL", env_class: 2, value: "INFO", source: "default", hint: "Change it in .env, then restart the backend." }],
    otlp_headers: [{ name: "OTEL_EXPORTER_OTLP_HEADERS", set: false }],
    transport: { scheme: "https", host: "librerun.example.lan" },
  };
}

function agentConfig(deprecated: boolean): AgentConfigResponse {
  return {
    meta: {
      supported_providers: ["openai"],
      step_editable_fields: ["temperature"],
      settings: [{ key: "depth", label: "Search depth", type: "enum", description: "", default: "basic", options: ["basic", "advanced"] }],
      deprecated,
      secrets: ["search_key"],
    },
    steps: [{ step_id: "analyze", label: "Analyze", description: "", provider: "openai", model: "a-model", temperature: 0, max_tokens: null, timeout_seconds: null, overridden: [] }],
    settings: [{ key: "depth", label: "Search depth", type: "enum", value: "basic", default: "basic", overridden: false }],
  };
}

function user(isPlatformAdmin: boolean): UserProfile {
  return { id: "u-1", email: "a@example.com", role: "admin", tenant_id: "t-1", is_platform_admin: isPlatformAdmin };
}

import AgentPageTabs from "../../../components/agentPage/AgentPageTabs";
import AuthConfigPage from "../auth-config/page";
import AdminSettingsPage from "../settings/page";
import UsersPage from "../users/page";

/** The rule, over the whole page as rendered. Returns what broke it. */
function unchipped(): string[] {
  const problems: string[] = [];
  const regions = Array.from(document.querySelectorAll<HTMLElement>("[data-scope-region]"));
  if (regions.length === 0) problems.push("no region at all");
  for (const region of regions) {
    const own = Array.from(region.querySelectorAll("[data-scope]")).filter(
      (chip) => chip.closest("[data-scope-region]") === region,
    );
    const note = Array.from(region.querySelectorAll('[role="note"]')).some(
      (n) => n.closest("[data-scope-region]") === region,
    );
    if (own.length !== 1 && !(own.length === 0 && note)) {
      problems.push(`region ${region.getAttribute("data-scope-region")} has ${own.length} chip(s) of its own`);
    }
  }
  for (const field of Array.from(document.querySelectorAll("input, select, textarea"))) {
    if (!field.closest("[data-scope-region]")) {
      problems.push(`a ${field.tagName.toLowerCase()} (${field.getAttribute("aria-label") ?? field.getAttribute("type") ?? ""}) outside any region`);
    }
  }
  return problems;
}

beforeEach(() => {
  state.user = user(true);
  state.deprecated = false;
});
afterEach(cleanup);

describe("every editable region carries its scope", () => {
  it("on Application Settings: the settings rows, Model providers, Deployment and Certificates", async () => {
    render(<AdminSettingsPage />);
    // Each panel has rendered what it edits.
    await screen.findByLabelText("CA certificate");
    await screen.findByTestId("deployment-settings");
    await waitFor(() => expect(document.querySelector('[data-scope-region="model-providers"] input')).toBeTruthy());
    await screen.findByText("trace_viewer_url_template");

    const scopes = Array.from(document.querySelectorAll("[data-scope-region]")).map((r) => r.getAttribute("data-scope-region"));
    expect(scopes).toEqual(expect.arrayContaining(["model-providers", "deployment", "certificates", "settings"]));
    expect(document.querySelector('[data-scope-region="deployment"] [data-scope="deployment"]')).toBeTruthy();
    expect(document.querySelector('[data-scope-region="settings"] [data-scope="platform"]')).toBeTruthy();
    expect(unchipped()).toEqual([]);
  });

  it("on Users & Access and Auth Configuration: this tenant", async () => {
    state.user = user(false);
    render(<UsersPage />);
    await screen.findByText("b@example.com");
    expect(document.querySelector('[data-scope-region] [data-scope="tenant"]')).toBeTruthy();
    expect(unchipped()).toEqual([]);
    cleanup();

    render(<AuthConfigPage />);
    await screen.findByText("Google SSO enabled");
    expect(document.querySelector('[data-scope-region] [data-scope="tenant"]')).toBeTruthy();
    expect(unchipped()).toEqual([]);
  });

  it("on the agent page: Steps, Settings, Secrets and Keys", async () => {
    render(<AgentPageTabs agentId={AGENT} />);
    await screen.findByRole("tab", { name: "Keys" });
    await waitFor(() => expect(document.querySelector('[data-scope-region="keys"] input')).toBeTruthy());
    await waitFor(() => expect(document.querySelectorAll("[data-row-set]").length).toBe(2));

    for (const chip of ["agent_tenant", "agent", "platform"]) {
      expect(document.querySelector(`[data-scope="${chip}"]`), chip).toBeTruthy();
    }
    expect(unchipped()).toEqual([]);
  });

  it("on an agent still on the deprecated settings surface: the note stands in for the chip", async () => {
    state.deprecated = true;
    render(<AgentPageTabs agentId={AGENT} />);
    await screen.findByRole("tab", { name: "Settings" });
    await waitFor(() => expect(document.querySelector('[data-scope-region="agent-settings"] [role="note"]')).toBeTruthy());
    expect(unchipped()).toEqual([]);
  });

  it("the rule bites: a field outside a region, or a region with no chip", async () => {
    render(
      <div>
        <input aria-label="stray" />
        <section data-scope-region="bare">
          <input aria-label="inside" />
        </section>
      </div>,
    );
    expect(unchipped()).toEqual([
      "region bare has 0 chip(s) of its own",
      "a input (stray) outside any region",
    ]);
  });
});
