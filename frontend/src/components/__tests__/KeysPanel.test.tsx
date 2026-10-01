/**
 * The agent page's Keys tab (K9-01, K9-02; D10).
 *
 * A new value is shown once, read-only, with Copy, and is gone at the next
 * read; an environment key offers no Rotate or Revoke and says it rotates in
 * `.env`; a rotation sends its grace window; a tenant admin is told the tab
 * is the platform's. The agent id is a synthetic one: the tab names no
 * agent (L13).
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentKeyRow } from "../../types";

const state = vi.hoisted(() => ({ apiFetch: vi.fn() }));

vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  apiFetch: state.apiFetch,
}));

import { ApiError } from "../../lib/api";
import { KeysPanel } from "../agentPage/KeysPanel";

const AGENT = "probe-agent";
const VALUE = "lr_agent_probeprobeprobeprobeprobeprobeprobe";

function row(overrides: Partial<AgentKeyRow> = {}): AgentKeyRow {
  return {
    agent_id: AGENT,
    key_prefix: "probepro",
    source: "admin",
    role: "current",
    issued_at: "2026-09-30T12:00:00Z",
    issued_by: null,
    previous_since: null,
    previous_until: null,
    last_used_at: null,
    rotatable: true,
    registered: true,
    ...overrides,
  };
}

let rows: AgentKeyRow[] = [];
const toast = vi.fn();

beforeEach(() => {
  rows = [];
  toast.mockReset();
  state.apiFetch.mockReset();
  state.apiFetch.mockImplementation(async (path: string, _token: string, init?: { method?: string }) => {
    if (path.startsWith("/admin/agent-keys?agent_id=") && !init?.method) return rows;
    if (path === `/admin/agent-keys/${AGENT}` && init?.method === "POST") {
      rows = [row()];
      return { agent_id: AGENT, key: VALUE, key_prefix: "probepro" };
    }
    if (path.startsWith(`/admin/agent-keys/${AGENT}/rotate`) && init?.method === "POST") {
      return { agent_id: AGENT, key: VALUE, key_prefix: "probepro", grace_hours: 1 };
    }
    if (path === `/admin/agent-keys/${AGENT}` && init?.method === "DELETE") {
      rows = [];
      return null;
    }
    throw new Error(`unexpected ${init?.method ?? "GET"} ${path}`);
  });
  vi.stubGlobal("isSecureContext", true);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("KeysPanel", () => {
  it("shows a new value once, with Copy, and never again after a re-read", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<KeysPanel agentId={AGENT} token="tok" toast={toast} />);

    fireEvent.click(await screen.findByRole("button", { name: "Issue a key" }));
    const shown = (await screen.findByLabelText("New key")) as HTMLInputElement;
    expect(shown.value).toBe(VALUE);
    expect(shown.readOnly).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(VALUE));

    // The next read — here, after Revoke — finds only a prefix, and the
    // value is gone from the page.
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(screen.queryByLabelText("New key")).toBeNull());
    expect(document.body.innerHTML).not.toContain(VALUE);
    expect(screen.getByRole("button", { name: "Issue a key" })).toBeTruthy();
  });

  it("offers no Rotate or Revoke on an environment key, and says it rotates in .env", async () => {
    rows = [row({ source: "env", rotatable: false })];
    render(<KeysPanel agentId={AGENT} token="tok" toast={toast} />);

    const table = await screen.findByTestId("keys-rows");
    expect(table.textContent).toContain(".env");
    expect(table.textContent).toContain(`librerun key rotate ${AGENT}`);
    expect(screen.queryByRole("button", { name: "Rotate" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Revoke" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Issue a key" })).toBeNull();
  });

  it("sends the grace window with a rotation", async () => {
    rows = [row()];
    render(<KeysPanel agentId={AGENT} token="tok" toast={toast} />);

    const grace = (await screen.findByLabelText(/Grace, hours/)) as HTMLInputElement;
    expect(grace.value).toBe("24");
    fireEvent.change(grace, { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Rotate" }));

    await screen.findByLabelText("New key");
    const rotations = state.apiFetch.mock.calls.filter(([path]) => String(path).includes("/rotate"));
    expect(rotations.map(([path]) => path)).toEqual([`/admin/agent-keys/${AGENT}/rotate?grace_hours=1`]);
  });

  it("shows a minted value at once, even when the rows' refresh never answers", async () => {
    // Codex's P2 on 6e7cdba: the value was set only after the list was read
    // again, so a read that hung kept the one copy of the key off the page.
    const never = new Promise<never>(() => undefined);
    const answered = state.apiFetch.getMockImplementation()!;
    let mints = 0;
    state.apiFetch.mockImplementation(async (path: string, token: string, init?: { method?: string }) => {
      if (init?.method === "POST") mints += 1;
      if (mints > 0 && path.startsWith("/admin/agent-keys?agent_id=")) return never;
      return answered(path, token, init);
    });
    render(<KeysPanel agentId={AGENT} token="tok" toast={toast} />);

    fireEvent.click(await screen.findByRole("button", { name: "Issue a key" }));
    expect(((await screen.findByLabelText("New key")) as HTMLInputElement).value).toBe(VALUE);
    cleanup();

    rows = [row()];
    mints = 0;
    render(<KeysPanel agentId={AGENT} token="tok" toast={toast} />);
    fireEvent.click(await screen.findByRole("button", { name: "Rotate" }));
    expect(((await screen.findByLabelText("New key")) as HTMLInputElement).value).toBe(VALUE);
  });

  it("shows no value on a page that moved on before the mint answered", async () => {
    let answer: (value: unknown) => void = () => undefined;
    const answered = state.apiFetch.getMockImplementation()!;
    state.apiFetch.mockImplementation(async (path: string, token: string, init?: { method?: string }) => {
      if (init?.method === "POST") return new Promise((resolve) => { answer = resolve; });
      return answered(path, token, init);
    });
    const { rerender } = render(<KeysPanel agentId={AGENT} token="tok" toast={toast} />);
    fireEvent.click(await screen.findByRole("button", { name: "Issue a key" }));
    rerender(<KeysPanel agentId="probe-other" token="tok" toast={toast} />);
    answer({ agent_id: AGENT, key: VALUE, key_prefix: "probepro" });

    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.stringContaining("after the page moved on"), "error"));
    expect(screen.queryByLabelText("New key")).toBeNull();
    expect(document.body.innerHTML).not.toContain(VALUE);
  });

  it("keeps the actions disabled until the rows have caught up with a mint", async () => {
    // Codex's P2 on f017826: with the refresh still out, the actions came
    // back, and a rotate or revoke could start from rows the mint had made
    // stale.
    let refresh: (value: unknown) => void = () => undefined;
    const answered = state.apiFetch.getMockImplementation()!;
    let minted = false;
    state.apiFetch.mockImplementation(async (path: string, token: string, init?: { method?: string }) => {
      if (init?.method === "POST") minted = true;
      if (minted && path.startsWith("/admin/agent-keys?agent_id=")) {
        return new Promise((resolve) => { refresh = resolve; });
      }
      return answered(path, token, init);
    });
    render(<KeysPanel agentId={AGENT} token="tok" toast={toast} />);

    fireEvent.click(await screen.findByRole("button", { name: "Issue a key" }));
    await screen.findByLabelText("New key");
    expect((screen.getByRole("button", { name: "Issue a key" }) as HTMLButtonElement).disabled).toBe(true);

    refresh([row()]);
    const rotate = (await screen.findByRole("button", { name: "Rotate" })) as HTMLButtonElement;
    await waitFor(() => expect(rotate.disabled).toBe(false));
    expect(screen.queryByRole("button", { name: "Issue a key" })).toBeNull();
  });

  it("never lets an older read that lands late set the rows", async () => {
    const answers: Array<(value: unknown) => void> = [];
    let reads = 0;
    state.apiFetch.mockImplementation(async (path: string) => {
      if (!path.startsWith("/admin/agent-keys?agent_id=")) throw new Error(`unexpected ${path}`);
      reads += 1;
      if (reads === 1) throw new ApiError("API 500: {}", 500);
      return new Promise((resolve) => { answers.push(resolve); });
    });
    render(<KeysPanel agentId={AGENT} token="tok" toast={toast} />);

    const retry = await screen.findByRole("button", { name: "Retry" });
    fireEvent.click(retry);
    fireEvent.click(retry);
    await waitFor(() => expect(answers).toHaveLength(2));
    answers[1]([row({ key_prefix: "newerkey" })]);
    expect((await screen.findByTestId("keys-rows")).textContent).toContain("newerkey");
    answers[0]([row({ key_prefix: "olderkey" })]);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByTestId("keys-rows").textContent).toContain("newerkey");
    expect(screen.getByTestId("keys-rows").textContent).not.toContain("olderkey");
  });

  it("refuses a grace that is not a whole number of hours, and sends nothing", async () => {
    // Codex's P2 on 6e7cdba: parseInt read "1.5" and "1e2" as 1.
    rows = [row()];
    render(<KeysPanel agentId={AGENT} token="tok" toast={toast} />);
    const grace = (await screen.findByLabelText(/Grace, hours/)) as HTMLInputElement;

    for (const typed of ["1.5", "1e2", "-1", "721", ""]) {
      toast.mockReset();
      fireEvent.change(grace, { target: { value: typed } });
      fireEvent.click(screen.getByRole("button", { name: "Rotate" }));
      expect(toast, typed).toHaveBeenCalledWith(expect.stringContaining("whole number of hours"), "error");
    }
    expect(state.apiFetch.mock.calls.filter(([path]) => String(path).includes("/rotate"))).toEqual([]);
  });

  it("names an agent that is not registered", async () => {
    rows = [row({ registered: false })];
    render(<KeysPanel agentId={AGENT} token="tok" toast={toast} />);

    expect((await screen.findByTestId("keys-unregistered")).textContent).toContain(AGENT);
  });

  it("tells a tenant admin the tab is the platform's", async () => {
    state.apiFetch.mockRejectedValue(new ApiError("API 403: {}", 403));
    render(<KeysPanel agentId={AGENT} token="tok" toast={toast} />);

    expect(await screen.findByText("Platform operators only.")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
