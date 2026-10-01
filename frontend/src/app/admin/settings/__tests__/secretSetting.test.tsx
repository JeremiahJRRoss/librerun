/**
 * K6: a secret setting on the application settings page (L31).
 *
 * The API never sends a secret's value — `value` and `default_value` are
 * null, and `secret` says where the value comes from — so the page has
 * nothing to show and must not pretend otherwise: a password input that is
 * never pre-filled, a chip for the source (with the fingerprint when the
 * store holds a readable row), no "Default:" line, and Set, or Replace and
 * Clear. The input is emptied after every one of them, whatever the
 * server answered, so a typed value does not linger in the page.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const KEY = "auth.azure_client_secret";
const TYPED = "a-value-typed-into-the-page";

type Source = "runtime" | "env" | "unset" | "unreadable";

function secretRow(source: Source, fingerprint: string | null = null) {
  const set = source === "runtime" || source === "unreadable";
  return {
    key: KEY,
    value: null,
    default_value: null,
    value_type: "secret",
    description: "Microsoft Entra ID client secret for Microsoft sign-in.",
    is_default: !set,
    updated_at: set ? "2026-09-28T10:00:00Z" : null,
    updated_by: null,
    secret: {
      set,
      source,
      fingerprint: source === "runtime" ? fingerprint : null,
      updated_at: set ? "2026-09-28T10:00:00Z" : null,
      updated_by: null,
    },
  };
}

const plainRow = {
  key: "max_upload_size_mb",
  value: 50,
  default_value: 50,
  value_type: "int",
  description: "Maximum accepted upload size per file, in megabytes.",
  is_default: true,
  updated_at: null,
  updated_by: null,
};

const state = vi.hoisted(() => ({
  rows: [] as unknown[],
  calls: [] as { path: string; method: string; body: unknown }[],
  answer: null as unknown,
  fail: null as Error | null,
  toast: vi.fn(),
}));

vi.mock("../../../../components/NavBar", () => ({ default: () => null }));
vi.mock("../../../../lib/auth", () => ({
  useAuth: () => ({
    token: "tok",
    user: { id: "u-1", email: "a@example.com", role: "admin", tenant_id: "t-1", is_platform_admin: true },
  }),
}));
vi.mock("../../../../lib/toast", () => ({ useToast: () => ({ toast: state.toast }) }));
vi.mock("../../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../../lib/api")>();
  return {
    ...actual,
    apiFetch: async (path: string, _token: string, init?: { method?: string; body?: string }) => {
      if (path === "/health") return { status: "ok", env: "test" };
      if (path === "/admin/settings") return state.rows;
      // K7's Model providers section reads its own list on the same page.
      if (path === "/admin/providers" && !init?.method) {
        return { reported: false, stub: false, gateway_version: null, updated_at: null, public_key_pem: null, providers: [] };
      }
      // T2's Certificates panel reads its status on the same page too.
      if (path === "/admin/tls" && !init?.method) {
        return { edge: "off", site: [], issuer: null, root: null, leaf: null, source: null, choice: null, environment: null, why: null, needs: ["edge_off"], acknowledged_root: null };
      }
      // K9's Deployment panel reads the deployment view on the same page.
      if (path === "/admin/deployment" && !init?.method) {
        return { version: "0", license: "AGPL-3.0-only", source_url: null, demo: true, stub: true, gateway: { reachable: true, reported: false, version: null, updated_at: null, providers: [] }, settings: [], otlp_headers: [], transport: { scheme: "http", host: "localhost" } };
      }
      state.calls.push({
        path,
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(init.body) : undefined,
      });
      if (state.fail) throw state.fail;
      return state.answer;
    },
  };
});

import AdminSettingsPage from "../page";

function secretInput(): HTMLInputElement {
  return screen.getByLabelText(`New value for ${KEY}`) as HTMLInputElement;
}

beforeEach(() => {
  state.calls = [];
  state.answer = null;
  state.fail = null;
  vi.stubGlobal("confirm", () => true);
});

afterEach(() => {
  cleanup();
  state.toast.mockReset();
  vi.unstubAllGlobals();
});

describe("a secret setting", () => {
  it("is a password input never pre-filled, with no Default line and no value", async () => {
    state.rows = [secretRow("runtime", "0123456789ab"), plainRow];
    render(<AdminSettingsPage />);

    const input = await waitFor(secretInput);
    expect(input.type).toBe("password");
    expect(input.value).toBe("");
    expect(screen.getByTestId(`secret-source-${KEY}`).textContent).toBe("runtime · 0123456789ab");
    // The plain row keeps its Default line; the secret has none, and no
    // Save button of its own — Replace and Clear instead.
    expect(screen.getAllByText(/^Default:/)).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Replace" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Clear" })).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Save" })).toHaveLength(1);
  });

  it.each([
    ["env", "from the environment", "Set"],
    ["unset", "not set", "Set"],
    ["unreadable", "unreadable — replace or clear", "Replace"],
  ] as const)("says %s as “%s” and offers %s", async (source, chip, action) => {
    state.rows = [secretRow(source)];
    render(<AdminSettingsPage />);

    expect((await screen.findByTestId(`secret-source-${KEY}`)).textContent).toBe(chip);
    expect(screen.getByRole("button", { name: action })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Clear" }) !== null).toBe(source === "unreadable");
  });

  it("sets the value it was given, then empties the input and shows the fingerprint", async () => {
    state.rows = [secretRow("unset")];
    state.answer = secretRow("runtime", "fedcba987654");
    render(<AdminSettingsPage />);

    const input = await waitFor(secretInput);
    fireEvent.change(input, { target: { value: TYPED } });
    fireEvent.click(screen.getByRole("button", { name: "Set" }));

    await waitFor(() =>
      expect(screen.getByTestId(`secret-source-${KEY}`).textContent).toBe("runtime · fedcba987654")
    );
    expect(state.calls).toEqual([{ path: `/admin/settings/${KEY}`, method: "PUT", body: { value: TYPED } }]);
    expect(secretInput().value).toBe("");
    expect(document.body.innerHTML).not.toContain(TYPED);
  });

  it("clears the row, then offers Set again", async () => {
    state.rows = [secretRow("runtime", "0123456789ab")];
    state.answer = secretRow("env");
    render(<AdminSettingsPage />);

    fireEvent.click(await screen.findByRole("button", { name: "Clear" }));

    await waitFor(() =>
      expect(screen.getByTestId(`secret-source-${KEY}`).textContent).toBe("from the environment")
    );
    expect(state.calls).toEqual([{ path: `/admin/settings/reset/${KEY}`, method: "POST", body: undefined }]);
    expect(screen.getByRole("button", { name: "Set" })).toBeTruthy();
  });

  it("empties the input after a refused write too, and says why", async () => {
    state.rows = [secretRow("unset")];
    state.fail = new Error("The secrets store has no key, so a secret cannot be stored.");
    render(<AdminSettingsPage />);

    const input = await waitFor(secretInput);
    fireEvent.change(input, { target: { value: TYPED } });
    fireEvent.click(screen.getByRole("button", { name: "Set" }));

    await waitFor(() => expect(state.toast).toHaveBeenCalled());
    expect(state.toast.mock.calls[0]).toEqual([
      "The secrets store has no key, so a secret cannot be stored.",
      "error",
    ]);
    expect(secretInput().value).toBe("");
    expect(screen.getByTestId(`secret-source-${KEY}`).textContent).toBe("not set");
  });

  it("sends nothing for an empty input", async () => {
    state.rows = [secretRow("unset")];
    render(<AdminSettingsPage />);

    const set = await screen.findByRole("button", { name: "Set" });
    expect((set as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(secretInput(), { target: { value: "   " } });
    expect((set as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(set);
    expect(state.calls).toEqual([]);
  });
});
