/**
 * The agent page's Settings tab (K5a, L32).
 *
 * It renders a field per declared setting by its type, marks the values
 * that are this tenant's choice, saves every key as `[{key, value}]` and
 * reads the config back, resets one field to the agent's default, and on
 * the deprecated path says that its values are one for every tenant. The
 * agent here is a synthetic id: the panel names no agent (L13), and
 * neither does this file.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentSettingSpec, AgentSettingValue } from "../../types";

const state = vi.hoisted(() => ({ apiFetch: vi.fn() }));

vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  apiFetch: state.apiFetch,
}));

import { SettingsPanel } from "../agentPage/SettingsPanel";

const AGENT = "probe-agent";

const SPECS: AgentSettingSpec[] = [
  { key: "note", label: "Note", type: "string", default: "hello", options: null, description: "A note." },
  { key: "limit", label: "Limit", type: "int", default: 3, options: null, description: "" },
  { key: "ratio", label: "Ratio", type: "float", default: 0.5, options: null, description: "" },
  { key: "strict", label: "Strict", type: "bool", default: false, options: null, description: "" },
  {
    key: "depth",
    label: "Depth",
    type: "enum",
    default: "advanced",
    options: ["basic", "advanced"],
    description: "",
  },
  { key: "tags", label: "Tags", type: "string_list", default: [], options: null, description: "" },
];

function values(overrides: Record<string, unknown> = {}): AgentSettingValue[] {
  return SPECS.map((spec) => {
    const chosen = spec.key in overrides;
    return {
      key: spec.key,
      label: spec.label,
      type: spec.type,
      value: chosen ? overrides[spec.key] : spec.default,
      default: spec.default,
      overridden: chosen,
    };
  });
}

const field = (key: string) => document.getElementById(`setting-${key}`) as HTMLInputElement;

let reload: ReturnType<typeof vi.fn>;
let toast: ReturnType<typeof vi.fn>;

function panel(settings: AgentSettingValue[], { deprecated = false, specs = SPECS } = {}) {
  return (
    <SettingsPanel
      agentId={AGENT}
      specs={specs}
      settings={settings}
      deprecated={deprecated}
      token="tok"
      reload={reload}
      toast={toast}
    />
  );
}

beforeEach(() => {
  state.apiFetch.mockReset();
  state.apiFetch.mockResolvedValue(undefined);
  reload = vi.fn().mockResolvedValue(undefined);
  toast = vi.fn();
});
afterEach(cleanup);

describe("SettingsPanel", () => {
  it("renders a field per declared setting, by type, with this tenant's values", () => {
    render(panel(values({ limit: 7, strict: true, depth: "basic", tags: ["a", "b"] })));

    expect(field("note").value).toBe("hello");
    expect(field("note").type).toBe("text");
    expect(field("limit").type).toBe("number");
    expect(field("limit").value).toBe("7");
    expect(field("ratio").value).toBe("0.5");
    expect(field("strict").type).toBe("checkbox");
    expect(field("strict").checked).toBe(true);
    expect((document.getElementById("setting-depth") as HTMLSelectElement).value).toBe("basic");
    expect(field("tags").value).toBe("a, b");
    expect(screen.getByText("A note.")).toBeTruthy();
  });

  it("marks this tenant's choices and scopes the heading to this agent and tenant", () => {
    render(panel(values({ limit: 7 })));

    const chip = document.querySelector("[data-scope]");
    expect(chip?.getAttribute("data-scope")).toBe("agent_tenant");
    expect(chip?.textContent).toBe("this agent · this tenant");
    // One overridden value, one marker, one Reset.
    expect(screen.getAllByText(/overridden here/)).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Reset" })).toHaveLength(1);
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("saves every key as a list, blanks as null, then reads the config back", async () => {
    render(panel(values()));

    fireEvent.change(field("note"), { target: { value: "" } });
    fireEvent.change(field("limit"), { target: { value: "0" } });
    fireEvent.click(field("strict"));
    fireEvent.click(field("strict"));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    });

    expect(state.apiFetch).toHaveBeenCalledTimes(1);
    const [path, token, init] = state.apiFetch.mock.calls[0];
    expect(path).toBe(`/agents/${AGENT}/config/settings`);
    expect(token).toBe("tok");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body)).toEqual([
      { key: "note", value: null },
      { key: "limit", value: 0 },
      { key: "ratio", value: 0.5 },
      { key: "strict", value: false },
      { key: "depth", value: "advanced" },
      { key: "tags", value: [] },
    ]);
    // The server decides what is an override, so the page shows ITS answer.
    expect(reload).toHaveBeenCalledTimes(1);
    expect(toast).toHaveBeenCalledWith("Settings saved", "success");
  });

  it("resets one field to the agent's default, and keeps an unsaved edit elsewhere", async () => {
    const view = render(panel(values({ limit: 7 })));

    fireEvent.change(field("note"), { target: { value: "not saved yet" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    });

    expect(state.apiFetch).toHaveBeenCalledTimes(1);
    expect(JSON.parse(state.apiFetch.mock.calls[0][2].body)).toEqual([{ key: "limit", value: null }]);
    expect(reload).toHaveBeenCalledTimes(1);

    // The re-read answers: limit is the default again. The note the admin
    // was typing is theirs until they save it or leave.
    view.rerender(panel(values()));
    expect(field("limit").value).toBe("3");
    expect(screen.queryByText(/overridden here/)).toBeNull();
    expect(field("note").value).toBe("not saved yet");
  });

  it("keeps the edits and says why when the server refuses a save", async () => {
    state.apiFetch.mockRejectedValue(
      new Error("API 400: {\"detail\":\"setting 'limit': must be an integer, not a number\"}"),
    );
    render(panel(values()));

    fireEvent.change(field("limit"), { target: { value: "9" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    });

    expect(reload).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith(expect.stringContaining("setting 'limit'"), "error");
    expect(field("limit").value).toBe("9");
  });

  it("says one value for every tenant on the deprecated path, instead of a scope", () => {
    render(panel(values({ depth: "basic" }), { deprecated: true }));

    expect(document.querySelector("[data-scope]")).toBeNull();
    const notice = screen.getByRole("note");
    expect(notice.textContent).toContain("One value for every tenant");
    expect(notice.textContent).toContain("settings[]");
    // The values are still served and editable.
    expect((document.getElementById("setting-depth") as HTMLSelectElement).value).toBe("basic");
  });

  it("renders nothing for an agent that declares no settings", () => {
    const { container } = render(panel([], { specs: [] }));
    expect(container.textContent).toBe("");
  });
});
