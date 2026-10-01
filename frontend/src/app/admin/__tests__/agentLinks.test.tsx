/**
 * The admin hub links every agent to its page (K4b, D30).
 *
 * The agent page is a tab shell for every registered agent, so a card
 * links whether or not the agent has anything to configure; `has_config`
 * still decides the "No admin config" note (its meaning is K5a's). The
 * agents are synthetic: the hub names none.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentInfo } from "../../../types";

function agent(agentId: string, displayName: string, hasConfig: boolean): AgentInfo {
  return {
    agent_id: agentId,
    display_name: displayName,
    description: "",
    has_config: hasConfig,
    phases: [],
    ui: { intake: { steps: [] } },
    output: { mode: "structured" },
    capabilities: [],
    feedback_sections: [],
    has_scenarios: false,
  };
}

const AGENTS = vi.hoisted(() => [] as AgentInfo[]);

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: () => undefined, replace: () => undefined }),
}));
vi.mock("../../../components/NavBar", () => ({ default: () => null }));
vi.mock("../../../lib/auth", () => ({
  useAuth: () => ({
    token: "tok",
    user: { id: "u-1", email: "a@example.com", role: "admin", tenant_id: "t-1", is_platform_admin: true },
  }),
}));
vi.mock("../../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/api")>()),
  apiFetch: async (path: string) => {
    if (path === "/agents") return AGENTS;
    if (path === "/admin/drift-summary") return { last_24h: 0, recent: [] };
    throw new Error(`unexpected ${path}`);
  },
}));

import AdminHomePage from "../page";

afterEach(cleanup);

describe("the admin hub's agent cards", () => {
  it("links an agent with has_config: false to its page, and still says it has no admin config", async () => {
    AGENTS.splice(0, AGENTS.length, agent("probe-configured", "Configured Probe", true), agent("probe-bare", "Bare Probe", false));
    render(<AdminHomePage />);

    const bare = await screen.findByRole("link", { name: /Bare Probe/ });
    expect(bare.getAttribute("href")).toBe("/admin/agents/probe-bare/config");
    expect(bare.textContent).toContain("No admin config");

    const configured = screen.getByRole("link", { name: /Configured Probe/ });
    expect(configured.getAttribute("href")).toBe("/admin/agents/probe-configured/config");
    expect(configured.textContent).not.toContain("No admin config");
  });
});
