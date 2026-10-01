/**
 * The agent page's tab shell (K4b, D30).
 *
 * The shell loads the config once, shows the tabs whose `available`
 * accepts the page's data, and says plainly when there is nothing to
 * configure. The agent here is a synthetic id, and the registry under
 * test is the real one, or a probe tab where a case needs one: the shell
 * names no agent (L13), and neither does this file.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentConfigResponse, UserProfile } from "../../types";

const state = vi.hoisted(() => ({
  user: null as UserProfile | null,
  apiFetch: vi.fn(),
}));

vi.mock("../../lib/auth", () => ({
  useAuth: () => ({ token: "tok", user: state.user }),
}));
vi.mock("../../lib/toast", () => ({
  useToast: () => ({ toast: () => undefined }),
}));
vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  apiFetch: state.apiFetch,
}));

import { ApiError } from "../../lib/api";
import AgentPageTabs from "../agentPage/AgentPageTabs";
import { AGENT_PAGE_TABS, type AgentPageTab } from "../agentPage/tabs";

const AGENT = "probe-agent";

function user(isPlatformAdmin: boolean): UserProfile {
  return {
    id: "u-1",
    email: "admin@example.com",
    role: "admin",
    tenant_id: "t-1",
    is_platform_admin: isPlatformAdmin,
  };
}

function config({ settings = true, secrets = false } = {}): AgentConfigResponse {
  return {
    meta: {
      supported_providers: ["openai"],
      step_editable_fields: ["temperature"],
      settings: settings
        ? [
            {
              key: "depth",
              label: "Search depth",
              type: "enum",
              description: "",
              default: "basic",
              options: ["basic", "advanced"],
            },
          ]
        : [],
      deprecated: false,
      secrets: secrets ? ["search_key"] : [],
    },
    steps: [
      {
        step_id: "analyze",
        label: "Analyze",
        description: "",
        provider: "openai",
        model: "a-model",
        temperature: 0,
        max_tokens: null,
        timeout_seconds: null,
        overridden: [],
      },
    ],
    settings: settings
      ? [
          {
            key: "depth",
            label: "Search depth",
            type: "enum",
            value: "basic",
            default: "basic",
            overridden: false,
          },
        ]
      : [],
  };
}

const tabNames = () => screen.getAllByRole("tab").map((tab) => tab.textContent);

beforeEach(() => {
  state.user = user(false);
  state.apiFetch.mockReset();
});
afterEach(cleanup);

describe("AgentPageTabs", () => {
  it("shows Steps and Settings when the agent declares both, one panel at a time", async () => {
    state.apiFetch.mockResolvedValue(config());
    render(<AgentPageTabs agentId={AGENT} />);

    expect(await screen.findByRole("tab", { name: "Steps" })).toBeTruthy();
    expect(tabNames()).toEqual(["Steps", "Settings"]);
    // Loaded once, for this agent, and handed to every tab.
    expect(state.apiFetch).toHaveBeenCalledTimes(1);
    expect(state.apiFetch).toHaveBeenCalledWith(`/agents/${AGENT}/config`, "tok");

    expect(screen.getByRole("tabpanel").textContent).toContain("Pipeline steps");
    fireEvent.click(screen.getByRole("tab", { name: "Settings" }));
    expect(screen.getByRole("tabpanel").textContent).toContain("Search depth");
    expect(screen.getByRole("tab", { name: "Settings" }).getAttribute("aria-selected")).toBe("true");
  });

  it("offers no Settings tab to an agent that declares no settings", async () => {
    state.apiFetch.mockResolvedValue(config({ settings: false }));
    render(<AgentPageTabs agentId={AGENT} />);

    expect(await screen.findByRole("tab", { name: "Steps" })).toBeTruthy();
    expect(tabNames()).toEqual(["Steps"]);
  });

  it("offers the Secrets tab to an agent that declares a tool secret, and to no other (K8b)", async () => {
    // The panel reads its own list, so the config is still loaded once.
    state.apiFetch.mockImplementation(async (path: string) =>
      path.endsWith("/secrets")
        ? { agent_id: AGENT, runtime: "python-package", secrets: [] }
        : config({ secrets: true }),
    );
    render(<AgentPageTabs agentId={AGENT} />);

    expect(await screen.findByRole("tab", { name: "Secrets" })).toBeTruthy();
    expect(tabNames()).toEqual(["Steps", "Settings", "Secrets"]);
    expect(state.apiFetch.mock.calls.filter(([path]) => path === `/agents/${AGENT}/config`)).toHaveLength(1);
    fireEvent.click(screen.getByRole("tab", { name: "Secrets" }));
    expect(await screen.findByText("Tool secrets")).toBeTruthy();
    expect(state.apiFetch).toHaveBeenCalledWith(`/agents/${AGENT}/secrets`, "tok");
    cleanup();

    state.apiFetch.mockReset();
    state.apiFetch.mockResolvedValue(config({ secrets: false }));
    render(<AgentPageTabs agentId={AGENT} />);
    expect(await screen.findByRole("tab", { name: "Steps" })).toBeTruthy();
    expect(tabNames()).toEqual(["Steps", "Settings"]);
    expect(state.apiFetch).toHaveBeenCalledTimes(1);
  });

  it("says plainly that an agent declares nothing when the config GET answers 404", async () => {
    state.apiFetch.mockRejectedValue(new ApiError("not found", 404));
    render(<AgentPageTabs agentId={AGENT} />);

    expect(await screen.findByText(/declares no LLM steps and no settings/)).toBeTruthy();
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(document.body.textContent).not.toContain("manages its own configuration");
  });

  it("drops the previous agent's panels while the next agent's config loads, and on its 404", async () => {
    // Codex, PR #158: a shell that kept the old config would hand the old
    // panels the new agent's id, so a Save could write one agent's values
    // to another; and a 404 would leave them there for good.
    state.apiFetch.mockResolvedValueOnce(config());
    const view = render(<AgentPageTabs agentId="probe-first" />);
    await screen.findByRole("tab", { name: "Steps" });

    let refuse: (reason: unknown) => void = () => undefined;
    state.apiFetch.mockReturnValueOnce(
      new Promise((_, reject) => {
        refuse = reject;
      }),
    );
    view.rerender(<AgentPageTabs agentId="probe-second" />);
    expect(state.apiFetch).toHaveBeenLastCalledWith("/agents/probe-second/config", "tok");
    expect(screen.queryByRole("tab")).toBeNull();
    expect(screen.queryByRole("tabpanel", { hidden: true })).toBeNull();
    expect(screen.getByText("Loading…")).toBeTruthy();

    refuse(new ApiError("not found", 404));
    expect(await screen.findByText(/declares no LLM steps and no settings/)).toBeTruthy();
    expect(screen.queryByRole("tab")).toBeNull();
    expect(screen.queryByRole("tabpanel", { hidden: true })).toBeNull();
  });

  it("drops a panel's reload that answers after the page has moved to another agent", async () => {
    // A Save reloads the config; if the page moves on before that answer
    // lands, the answer is the old agent's and must not reach the new
    // agent's panels, which would save it under the new agent's id.
    const probe: AgentPageTab = {
      id: "probe",
      label: "Probe",
      available: () => true,
      component: ({ data, reload }) => (
        <div>
          <p>{`step: ${data.config?.steps[0]?.label}`}</p>
          <button type="button" onClick={() => void reload()}>
            reload
          </button>
        </div>
      ),
    };
    const labelled = (label: string): AgentConfigResponse => {
      const cfg = config();
      return { ...cfg, steps: [{ ...cfg.steps[0], label }] };
    };

    state.apiFetch.mockResolvedValueOnce(labelled("first agent's"));
    const view = render(<AgentPageTabs agentId="probe-first" tabs={[probe]} />);
    await screen.findByText("step: first agent's");

    let answer: (cfg: AgentConfigResponse) => void = () => undefined;
    state.apiFetch.mockReturnValueOnce(
      new Promise<AgentConfigResponse>((resolve) => {
        answer = resolve;
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "reload" }));
    expect(state.apiFetch).toHaveBeenLastCalledWith("/agents/probe-first/config", "tok");

    state.apiFetch.mockResolvedValueOnce(labelled("second agent's"));
    view.rerender(<AgentPageTabs agentId="probe-second" tabs={[probe]} />);
    await screen.findByText("step: second agent's");

    answer(labelled("first agent's, late"));
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText("step: second agent's")).toBeTruthy();
    expect(screen.queryByText(/first agent's/)).toBeNull();
  });

  it("still loads the next agent when the old agent's panel reloads after the page moved on", async () => {
    // A Save that finishes after the page has moved on calls the old
    // agent's reload while the next agent's config is loading. Its answer
    // is dropped, and the next agent's own answer still lands: the page
    // neither shows the old config nor waits on it for good.
    let heldReload: () => Promise<void> = async () => undefined;
    const probe: AgentPageTab = {
      id: "probe",
      label: "Probe",
      available: () => true,
      component: ({ data, reload }) => {
        heldReload = reload;
        return <p>{`step: ${data.config?.steps[0]?.label}`}</p>;
      },
    };
    const labelled = (label: string): AgentConfigResponse => {
      const cfg = config();
      return { ...cfg, steps: [{ ...cfg.steps[0], label }] };
    };

    state.apiFetch.mockResolvedValueOnce(labelled("first agent's"));
    const view = render(<AgentPageTabs agentId="probe-first" tabs={[probe]} />);
    await screen.findByText("step: first agent's");
    const reloadOfFirst = heldReload;

    let answerSecond: (cfg: AgentConfigResponse) => void = () => undefined;
    state.apiFetch.mockReturnValueOnce(
      new Promise<AgentConfigResponse>((resolve) => {
        answerSecond = resolve;
      }),
    );
    view.rerender(<AgentPageTabs agentId="probe-second" tabs={[probe]} />);
    expect(screen.getByText("Loading…")).toBeTruthy();

    state.apiFetch.mockResolvedValueOnce(labelled("first agent's, late"));
    await act(async () => {
      await reloadOfFirst();
    });
    expect(state.apiFetch).toHaveBeenLastCalledWith("/agents/probe-first/config", "tok");

    answerSecond(labelled("second agent's"));
    expect(await screen.findByText("step: second agent's")).toBeTruthy();
    expect(screen.queryByText(/first agent's/)).toBeNull();
  });

  it("lets a tab read the platform-admin flag, and shows it to a platform admin alone", async () => {
    const probe: AgentPageTab = {
      id: "operator",
      label: "Operator",
      available: (data) => data.user.is_platform_admin,
      component: () => <p>for the operator</p>,
    };
    const tabs = [...AGENT_PAGE_TABS, probe];
    // K9's Keys tab reads its own list, by path; the config is the rest.
    state.apiFetch.mockImplementation(async (path: string) =>
      path.startsWith("/admin/agent-keys") ? [] : config(),
    );

    render(<AgentPageTabs agentId={AGENT} tabs={tabs} />);
    await screen.findByRole("tab", { name: "Steps" });
    expect(tabNames()).toEqual(["Steps", "Settings"]);
    cleanup();

    state.user = user(true);
    render(<AgentPageTabs agentId={AGENT} tabs={tabs} />);
    await screen.findByRole("tab", { name: "Steps" });
    expect(tabNames()).toEqual(["Steps", "Settings", "Keys", "Operator"]);
  });

  it("offers the Keys tab to a platform admin alone, and tells an unregistered id from one with nothing declared (K9)", async () => {
    const notFound = (detail: string) => new ApiError(`API 404: ${JSON.stringify({ detail })}`, 404);
    state.user = user(true);
    state.apiFetch.mockImplementation(async (path: string) => {
      if (path.startsWith("/admin/agent-keys")) return [];
      throw notFound(`Unknown agent: ${AGENT}`);
    });
    render(<AgentPageTabs agentId={AGENT} />);

    expect(await screen.findByTestId("agent-unregistered")).toBeTruthy();
    expect(tabNames()).toEqual(["Keys"]);
    expect(document.body.textContent).not.toContain("declares no LLM steps and no settings");
    expect(state.apiFetch).toHaveBeenCalledWith(`/admin/agent-keys?agent_id=${AGENT}`, "tok");
    cleanup();

    // A tenant admin: no Keys tab, and the unregistered text alone.
    state.user = user(false);
    render(<AgentPageTabs agentId={AGENT} />);
    expect(await screen.findByTestId("agent-unregistered")).toBeTruthy();
    expect(screen.queryByRole("tablist")).toBeNull();
    cleanup();

    // Registered, with nothing declared: that is "nothing to configure".
    state.apiFetch.mockImplementation(async () => {
      throw notFound(`Agent ${AGENT} does not expose a config surface`);
    });
    render(<AgentPageTabs agentId={AGENT} />);
    expect(await screen.findByText(/declares no LLM steps and no settings/)).toBeTruthy();
    expect(screen.queryByTestId("agent-unregistered")).toBeNull();
    cleanup();

    // …and a platform admin is told the same, beside the Keys tab, which is
    // the platform's and not a configuration the agent declares (Codex's P2
    // on 6e7cdba): only the "nothing to configure" clause waits for no tab.
    state.user = user(true);
    state.apiFetch.mockImplementation(async (path: string) => {
      if (path.startsWith("/admin/agent-keys")) return [];
      throw notFound(`Agent ${AGENT} does not expose a config surface`);
    });
    render(<AgentPageTabs agentId={AGENT} />);
    const notice = await screen.findByTestId("agent-declares-nothing");
    expect(notice.textContent).toMatch(/declares no LLM steps and no settings/);
    expect(notice.textContent).not.toContain("nothing to configure");
    expect(tabNames()).toEqual(["Keys"]);
    expect(screen.queryByTestId("agent-unregistered")).toBeNull();
  });
});
