/**
 * The agent page's Secrets tab (K8b; D29, D32, L31).
 *
 * Each declared name shows two rows — this tenant's and every tenant's
 * default — with their states and never a value; a field is never
 * pre-filled and is emptied as its request is sent; Set PUTs and Clear
 * DELETEs the row's own scope, and the list is read again after each;
 * the default is closed to a tenant admin, and a 403 closes it the same
 * way; a 503 names its code and points at Install.md. The agent here is
 * a synthetic id: the panel names no agent (L13), and neither does this
 * file.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentSecretsList, AgentSecretState, UserProfile } from "../../types";

const state = vi.hoisted(() => ({ apiFetch: vi.fn() }));

vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  apiFetch: state.apiFetch,
}));

import { ApiError } from "../../lib/api";
import { SecretsPanel } from "../agentPage/SecretsPanel";

const AGENT = "probe-agent";
// A value a broken server might send back; the page must never show it.
const PLANTED = "planted-value-the-page-must-never-show";
const TYPED = "a-value-typed-by-the-admin";

function user(isPlatformAdmin: boolean): UserProfile {
  return {
    id: "u-1",
    email: "admin@example.com",
    role: "admin",
    tenant_id: "t-1",
    is_platform_admin: isPlatformAdmin,
  };
}

const UNSET = { set: false, fingerprint: null, updated_at: null, updated_by: null, last_used_at: null };

function secret(name: string, overrides: Partial<AgentSecretState> = {}): AgentSecretState {
  return {
    name,
    effective: "unset",
    tenant: { ...UNSET },
    agent: { ...UNSET },
    environment: { set: false },
    ...overrides,
  };
}

function listing(secrets: AgentSecretState[], runtime = "python-package"): AgentSecretsList {
  return { agent_id: AGENT, runtime, secrets };
}

/** The mock API: GET answers `answer()`, a write answers `write()`. */
function serve(answer: () => AgentSecretsList, write: (path: string, init: RequestInit) => unknown = () => undefined) {
  state.apiFetch.mockImplementation(async (path: string, _token: string, init?: RequestInit) => {
    if (!init?.method) return answer();
    return write(path, init);
  });
}

const reads = () => state.apiFetch.mock.calls.filter(([, , init]) => !init?.method).length;
const writes = () => state.apiFetch.mock.calls.filter(([, , init]) => init?.method);
const region = (name: string, chip: "agent_tenant" | "agent") =>
  document.querySelector(`[data-secret="${name}"] [data-scope-region="${chip}"]`) as HTMLElement;
const field = (name: string, label: "this tenant" | "the default") =>
  screen.getByLabelText(`New value for ${name} (${label})`) as HTMLInputElement;

let toast: ReturnType<typeof vi.fn>;

async function mount(isPlatformAdmin = false) {
  render(<SecretsPanel agentId={AGENT} user={user(isPlatformAdmin)} token="tok" toast={toast} />);
  await screen.findByText("Tool secrets");
}

beforeEach(() => {
  state.apiFetch.mockReset();
  toast = vi.fn();
  vi.spyOn(window, "confirm").mockReturnValue(true);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("SecretsPanel", () => {
  it("names each declared secret with its two rows, their states and where a run reads it", async () => {
    serve(() =>
      listing([
        secret("search_key", {
          effective: "tenant",
          tenant: {
            set: true,
            fingerprint: "a1b2c3d4e5f6",
            updated_at: "2026-09-30T04:00:00Z",
            updated_by: "u-1",
            last_used_at: "2026-09-30T04:05:00Z",
          },
          agent: { set: true, fingerprint: "0f9e8d7c6b5a", updated_at: "2026-09-29T10:00:00Z" },
        }),
        secret("index_key", { environment: { set: true }, effective: "environment" }),
      ]),
    );
    await mount();

    expect(state.apiFetch).toHaveBeenCalledWith(`/agents/${AGENT}/secrets`, "tok");
    expect([...document.querySelectorAll("[data-secret]")].map((li) => li.getAttribute("data-secret"))).toEqual([
      "search_key",
      "index_key",
    ]);

    // Each row is an editable region with its one chip (K9's scopeChips).
    const mine = region("search_key", "agent_tenant");
    const fallback = region("search_key", "agent");
    for (const [row, scope] of [
      [mine, "agent_tenant"],
      [fallback, "agent"],
    ] as const) {
      const chips = row.querySelectorAll("[data-scope]");
      expect(chips).toHaveLength(1);
      expect(chips[0].getAttribute("data-scope")).toBe(scope);
      expect(row.querySelectorAll("input")).toHaveLength(1);
    }
    expect(within(mine).getByText("this agent · this tenant")).toBeTruthy();
    expect(within(fallback).getByText("this agent")).toBeTruthy();

    expect(within(mine).getByText("a1b2c3d4e5f6")).toBeTruthy();
    expect(within(mine).getByText(/set by you/)).toBeTruthy();
    expect(within(mine).queryByText(/last used by a run: never/)).toBeNull();
    expect(within(fallback).getByText("0f9e8d7c6b5a")).toBeTruthy();
    expect(within(fallback).getByText(/last used by a run: never/)).toBeTruthy();
    expect(within(mine).getByRole("button", { name: "Replace" })).toBeTruthy();

    const other = region("index_key", "agent_tenant");
    expect(within(other).getByText("not set")).toBeTruthy();
    expect(within(other).getByRole("button", { name: "Set" })).toBeTruthy();
    expect(within(other).queryByRole("button", { name: "Clear" })).toBeNull();

    expect(document.querySelector('[data-secret="search_key"] [data-effective]')?.textContent).toContain(
      "this tenant's value",
    );
    // An in-process agent's fallback: the environment, by its variable's name.
    const env = document.querySelector('[data-secret="index_key"] [data-effective]');
    expect(env?.getAttribute("data-effective")).toBe("environment");
    expect(env?.textContent).toContain("from the environment (INDEX_KEY)");
    expect(document.querySelector('[data-secret="index_key"] [data-environment]')?.getAttribute("data-environment")).toBe(
      "set",
    );
  });

  it("never pre-fills a field, and empties it as Set is sent, then reads the list again", async () => {
    // A set row beside an unset one: nothing the server says of a row —
    // its fingerprint, who, when — may reach a field.
    const setRow = secret("index_key", {
      effective: "tenant",
      tenant: { set: true, fingerprint: "a1b2c3d4e5f6", updated_by: "u-1" },
      agent: { set: true, fingerprint: "0f9e8d7c6b5a" },
    });
    let answer = listing([secret("search_key"), setRow]);
    let sent: RequestInit | undefined;
    let sentPath = "";
    let fieldWhenSent = "unread";
    serve(
      () => answer,
      (path, init) => {
        sent = init;
        sentPath = path;
        fieldWhenSent = field("search_key", "this tenant").value;
        answer = listing([
          secret("search_key", {
            effective: "tenant",
            tenant: { set: true, fingerprint: "c0ffee123456", updated_by: "u-1" },
          }),
          setRow,
        ]);
        return answer.secrets[0];
      },
    );
    await mount();

    expect(document.querySelectorAll("input")).toHaveLength(4);
    for (const input of document.querySelectorAll("input")) {
      expect(input.type).toBe("password");
      expect(input.getAttribute("autocomplete")).toBe("new-password");
      expect(input.value).toBe("");
    }
    fireEvent.change(field("search_key", "this tenant"), { target: { value: TYPED } });
    expect(field("search_key", "this tenant").value).toBe(TYPED);
    await act(async () => {
      fireEvent.click(within(region("search_key", "agent_tenant")).getByRole("button", { name: "Set" }));
    });

    expect(sentPath).toBe(`/agents/${AGENT}/secrets/tenant/search_key`);
    expect(sent?.method).toBe("PUT");
    expect(JSON.parse(String(sent?.body))).toEqual({ value: TYPED });
    // Emptied as the request went, not when it answered.
    expect(fieldWhenSent).toBe("");
    expect(field("search_key", "this tenant").value).toBe("");
    expect(reads()).toBe(2);
    expect(within(region("search_key", "agent_tenant")).getByText("c0ffee123456")).toBeTruthy();
    expect(within(region("search_key", "agent_tenant")).getByRole("button", { name: "Replace" })).toBeTruthy();
    expect(document.body.innerHTML).not.toContain(TYPED);
    for (const input of document.querySelectorAll("input")) {
      expect(input.value).toBe("");
    }
  });

  it("clears the row's own scope with DELETE, and a platform admin's Set writes the default", async () => {
    serve(() =>
      listing([
        secret("search_key", {
          effective: "tenant",
          tenant: { set: true, fingerprint: "a1b2c3d4e5f6" },
          agent: { set: true, fingerprint: "0f9e8d7c6b5a" },
        }),
      ]),
    );
    await mount(true);

    await act(async () => {
      fireEvent.click(within(region("search_key", "agent_tenant")).getByRole("button", { name: "Clear" }));
    });
    expect(window.confirm).toHaveBeenCalledTimes(1);
    expect(writes()).toHaveLength(1);
    expect(writes()[0][0]).toBe(`/agents/${AGENT}/secrets/tenant/search_key`);
    expect(writes()[0][2]).toEqual({ method: "DELETE" });

    fireEvent.change(field("search_key", "the default"), { target: { value: TYPED } });
    await act(async () => {
      fireEvent.click(within(region("search_key", "agent")).getByRole("button", { name: "Replace" }));
    });
    expect(writes()).toHaveLength(2);
    expect(writes()[1][0]).toBe(`/agents/${AGENT}/secrets/agent/search_key`);
    expect(writes()[1][2].method).toBe("PUT");
    expect(field("search_key", "the default").value).toBe("");
    expect(reads()).toBe(3);
  });

  it("sends nothing when a Clear is not confirmed", async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    serve(() => listing([secret("search_key", { tenant: { set: true, fingerprint: "a1b2c3d4e5f6" } })]));
    await mount();

    await act(async () => {
      fireEvent.click(within(region("search_key", "agent_tenant")).getByRole("button", { name: "Clear" }));
    });
    expect(writes()).toHaveLength(0);
  });

  it("closes the default row to a tenant admin, and leaves this tenant's open", async () => {
    serve(() => listing([secret("search_key", { agent: { set: true, fingerprint: "0f9e8d7c6b5a" } })]));
    await mount(false);

    const fallback = region("search_key", "agent");
    expect(within(fallback).getByText("Platform operators only.")).toBeTruthy();
    expect(field("search_key", "the default").disabled).toBe(true);
    for (const button of within(fallback).getAllByRole("button")) {
      expect((button as HTMLButtonElement).disabled).toBe(true);
    }
    // Who set a default is the operator's to see; the API sends no one.
    expect(within(fallback).queryByText(/set by/)).toBeNull();

    const mine = region("search_key", "agent_tenant");
    expect(within(mine).queryByText("Platform operators only.")).toBeNull();
    expect(field("search_key", "this tenant").disabled).toBe(false);
  });

  it("renders a 403 on the default as the same closed row", async () => {
    serve(
      () => listing([secret("search_key")]),
      () => {
        throw new ApiError(
          'API 403: {"detail":"Platform operator only — a tool secret\'s default is every tenant\'s","code":"platform_admin_only"}',
          403,
        );
      },
    );
    await mount(true);

    const fallback = region("search_key", "agent");
    expect(within(fallback).queryByText("Platform operators only.")).toBeNull();
    fireEvent.change(field("search_key", "the default"), { target: { value: TYPED } });
    await act(async () => {
      fireEvent.click(within(fallback).getByRole("button", { name: "Set" }));
    });
    expect(within(fallback).getByText("Platform operators only.")).toBeTruthy();
    expect(field("search_key", "the default").disabled).toBe(true);
    expect(field("search_key", "the default").value).toBe("");
    expect(field("search_key", "this tenant").disabled).toBe(false);
  });

  it.each([
    ["secrets_store_unconfigured", "The secrets store key"],
    ["secrets_store_key_shared", "The gateway's store key"],
  ])("names a 503's code, %s, and points at Install.md", async (code, section) => {
    serve(
      () => listing([secret("search_key")]),
      () => {
        throw new ApiError(`API 503: {"detail":"the store refused","code":"${code}"}`, 503);
      },
    );
    await mount();

    fireEvent.change(field("search_key", "this tenant"), { target: { value: TYPED } });
    await act(async () => {
      fireEvent.click(within(region("search_key", "agent_tenant")).getByRole("button", { name: "Set" }));
    });
    const note = screen.getByRole("alert");
    expect(note.getAttribute("data-store-refusal")).toBe(code);
    expect(note.textContent).toContain(code);
    expect(note.textContent).toContain("docs/platform/Install.md");
    expect(note.textContent).toContain(section);
    expect(field("search_key", "this tenant").value).toBe("");
  });

  it("says a container's own environment is invisible to the platform", async () => {
    serve(() => listing([secret("search_key", { environment: null })], "container"));
    await mount();

    expect(screen.getByRole("note").textContent).toContain("its own environment is invisible to the platform");
    expect(document.querySelector("[data-environment]")).toBeNull();
    expect(document.querySelector("[data-effective]")?.textContent).toContain("secret_not_set");
  });

  it("never renders a value, even one a broken server sends", async () => {
    const leaky = secret("search_key", {
      effective: "tenant",
      tenant: { set: true, fingerprint: "a1b2c3d4e5f6" },
    }) as AgentSecretState & Record<string, unknown>;
    leaky.value = PLANTED;
    (leaky.tenant as Record<string, unknown>).value = PLANTED;
    (leaky.agent as Record<string, unknown>).value = PLANTED;
    serve(() => listing([leaky]));
    await mount(true);

    expect(document.body.innerHTML).not.toContain(PLANTED);
    for (const input of document.querySelectorAll("input")) {
      expect(input.value).toBe("");
    }
  });

  it("says so when the list cannot be read", async () => {
    state.apiFetch.mockRejectedValue(new ApiError('API 500: {"detail":"boom"}', 500));
    render(<SecretsPanel agentId={AGENT} user={user(false)} token="tok" toast={toast} />);
    expect(await screen.findByText(/The secrets could not be read: boom/)).toBeTruthy();
  });
});
