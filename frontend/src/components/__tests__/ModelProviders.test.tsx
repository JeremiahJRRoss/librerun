/**
 * Model providers on Application Settings (K7; L33, D19, D34).
 *
 * The section shows each provider's state as a chip, shows the fingerprint
 * of the key it seals to, seals a pasted key and posts the blob — never the
 * key — empties the field whatever happens, and waits for the gateway. Off a
 * secure context, or with no key to seal to, it explains and never posts
 * (D19's refusal branch). Sealing is stood in here: jsdom has no WebCrypto,
 * and `sealing.test.ts` holds the real thing to openssl.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import type { ProviderEntry, ProvidersStatus } from "../ModelProviders";

const state = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  canSeal: vi.fn(),
  seal: vi.fn(),
  sealingFingerprint: vi.fn(),
}));

vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  apiFetch: state.apiFetch,
}));

vi.mock("../../lib/sealing", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/sealing")>()),
  canSeal: state.canSeal,
  seal: state.seal,
  sealingFingerprint: state.sealingFingerprint,
}));

import ModelProviders from "../ModelProviders";

const PEM = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n";

function entry(name: ProviderEntry["name"], overrides: Partial<ProviderEntry> = {}): ProviderEntry {
  return {
    name,
    aliases: [name],
    source: "unset",
    fingerprint: null,
    set_by: null,
    set_at: null,
    row: null,
    reason: null,
    ...overrides,
  };
}

function status(overrides: Partial<ProvidersStatus> = {}, providers?: ProviderEntry[]): ProvidersStatus {
  return {
    reported: true,
    stub: false,
    gateway_version: "1.0.0",
    updated_at: null,
    public_key_pem: PEM,
    providers: providers ?? [entry("openai"), entry("anthropic"), entry("google")],
    ...overrides,
  };
}

type Toast = (message: string, kind?: "success" | "error" | "info") => void;
let toast: Mock<Toast>;

async function mount() {
  await act(async () => {
    render(<ModelProviders token="tok" toast={toast} />);
  });
}

const calls = () => state.apiFetch.mock.calls.map(([path, , init]) => `${init?.method ?? "GET"} ${path}`);

beforeEach(() => {
  state.apiFetch.mockReset();
  state.canSeal.mockReset().mockReturnValue(true);
  state.seal.mockReset().mockResolvedValue("U0VBTEVELUJMT0I=");
  state.sealingFingerprint.mockReset().mockResolvedValue("SHA256:" + "ab".repeat(32));
  toast = vi.fn<Toast>();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("ModelProviders", () => {
  it("shows each provider's state as a chip, and the key it seals to", async () => {
    state.apiFetch.mockResolvedValue(
      status({}, [
        entry("openai", { source: "env" }),
        entry("anthropic", { source: "runtime", row: "runtime", fingerprint: "f00dfacecafe" }),
        entry("google", { row: "rejected", reason: "unsealable", source: "env" }),
      ])
    );
    await mount();
    expect(screen.getByTestId("provider-chip-openai").textContent).toBe("from gateway.env");
    expect(screen.getByTestId("provider-chip-anthropic").textContent).toBe("runtime · f00dfacecafe");
    expect(screen.getByTestId("provider-chip-google").textContent).toBe("rejected · unsealable");
    expect(screen.getByTestId("providers-fingerprint").textContent).toBe("SHA256:" + "ab".repeat(32));
    expect(state.sealingFingerprint).toHaveBeenCalledWith(PEM);
    // A stored key can be replaced or cleared; the field is never pre-filled.
    expect(screen.getByTestId("provider-set-anthropic").textContent).toBe("Replace");
    expect(screen.getByTestId("provider-clear-anthropic")).toBeTruthy();
    expect(screen.queryByTestId("provider-clear-openai")).toBeNull();
    expect((screen.getByTestId("provider-input-anthropic") as HTMLInputElement).value).toBe("");
  });

  it("off a secure context explains, points at gateway.env and never posts (D19)", async () => {
    state.canSeal.mockReturnValue(false);
    state.apiFetch.mockResolvedValue(status());
    await mount();
    const refusal = screen.getByTestId("providers-refusal");
    expect(refusal.textContent).toMatch(/not a secure context/);
    expect(refusal.textContent).toMatch(/gateway\.env/);
    expect(refusal.textContent).toMatch(/HTTPS at the edge/);
    expect(screen.queryByTestId("provider-input-openai")).toBeNull();
    expect(screen.queryByTestId("providers-fingerprint")).toBeNull();
    expect(calls()).toEqual(["GET /admin/providers"]);
    expect(state.seal).not.toHaveBeenCalled();
  });

  it("with no key to seal to says so and never posts (K7-07)", async () => {
    state.apiFetch.mockResolvedValue(status({ public_key_pem: null }));
    await mount();
    expect(screen.getByTestId("providers-no-key").textContent).toMatch(/LIBRERUN_GATEWAY_SECRETS_KEY/);
    expect(screen.queryByTestId("provider-input-openai")).toBeNull();
    expect(calls()).toEqual(["GET /admin/providers"]);
  });

  it("says the gateway has not reported, and in keyless mode that keys wait", async () => {
    state.apiFetch.mockResolvedValue(status({ reported: false, public_key_pem: null }));
    await mount();
    expect(screen.getByTestId("providers-unreported")).toBeTruthy();
    cleanup();
    state.apiFetch.mockResolvedValue(status({ stub: true }));
    await mount();
    expect(screen.getByTestId("providers-keyless").textContent).toMatch(/waits, unused/);
  });

  it("re-reads the key, seals, posts the blob, empties the field and waits for the gateway", async () => {
    vi.useFakeTimers();
    const pending = status({}, [entry("openai", { row: "pending" }), entry("anthropic"), entry("google")]);
    const adopted = status({}, [
      entry("openai", { source: "runtime", row: "runtime", fingerprint: "0123456789ab" }),
      entry("anthropic"),
      entry("google"),
    ]);
    state.apiFetch.mockImplementation(async (path: string, _token: string, init?: RequestInit) => {
      if (init?.method === "POST") return { name: "openai", row: "pending" };
      const posted = state.apiFetch.mock.calls.some(([, , i]) => i?.method === "POST");
      const reads = state.apiFetch.mock.calls.filter(([, , i]) => !i?.method).length;
      if (!posted) return status();
      return reads < 5 ? pending : adopted;
    });
    await mount();

    const input = screen.getByTestId("provider-input-openai") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "sk-proj-pasted-here" } });
    await act(async () => {
      fireEvent.click(screen.getByTestId("provider-set-openai"));
    });

    expect(state.seal).toHaveBeenCalledWith(PEM, "openai", "sk-proj-pasted-here");
    const post = state.apiFetch.mock.calls.find(([, , init]) => init?.method === "POST");
    expect(post?.[0]).toBe("/admin/providers/openai/key");
    expect(JSON.parse(post?.[2]?.body as string)).toEqual({ sealed: "U0VBTEVELUJMT0I=" });
    expect(post?.[2]?.body).not.toContain("sk-proj-pasted-here");
    expect(input.value).toBe("");
    // The key was re-read before sealing: a GET after mount, before the POST.
    expect(calls().slice(0, 3)).toEqual(["GET /admin/providers", "GET /admin/providers", "POST /admin/providers/openai/key"]);

    for (let i = 0; i < 4; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
    }
    expect(screen.getByTestId("provider-chip-openai").textContent).toBe("runtime · 0123456789ab");
    expect(toast).toHaveBeenLastCalledWith("OpenAI's key is in effect.", "success");
  });

  it("refuses a key the gateway would not take, and still empties the field", async () => {
    state.apiFetch.mockResolvedValue(status());
    const { SealError } = await import("../../lib/sealing");
    state.seal.mockRejectedValue(new SealError("A provider key is one word of visible ASCII."));
    await mount();
    const input = screen.getByTestId("provider-input-anthropic") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "two words" } });
    await act(async () => {
      fireEvent.click(screen.getByTestId("provider-set-anthropic"));
    });
    expect(toast).toHaveBeenCalledWith("A provider key is one word of visible ASCII.", "error");
    expect(calls().some((call) => call.startsWith("POST"))).toBe(false);
    expect(input.value).toBe("");
  });

  it("clears a stored key once asked, and never without asking", async () => {
    vi.useFakeTimers();
    const stored = status({}, [entry("openai", { source: "runtime", row: "runtime", fingerprint: "0123456789ab" })]);
    state.apiFetch.mockImplementation(async (_path: string, _token: string, init?: RequestInit) =>
      init?.method === "DELETE" ? undefined : state.apiFetch.mock.calls.some(([, , i]) => i?.method === "DELETE")
        ? status({}, [entry("openai", { source: "env" })])
        : stored
    );
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mount();
    await act(async () => {
      fireEvent.click(screen.getByTestId("provider-clear-openai"));
    });
    expect(calls().some((call) => call.startsWith("DELETE"))).toBe(false);

    confirm.mockReturnValue(true);
    await act(async () => {
      fireEvent.click(screen.getByTestId("provider-clear-openai"));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(calls()).toContain("DELETE /admin/providers/openai/key");
    expect(screen.getByTestId("provider-chip-openai").textContent).toBe("from gateway.env");
    confirm.mockRestore();
  });
});
