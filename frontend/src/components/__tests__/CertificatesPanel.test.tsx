/**
 * Certificates on Application Settings (T2; L42, L43, D44).
 *
 * The panel says what the edge serves and why, and each need with its
 * action; each choice carries its environment line. A key is read from the
 * file the admin picks and sent once, in the change's body — never put on
 * the page, and the inputs are emptied after every change, whatever the
 * answer. With the edge off it says so and offers no change. The region is
 * chipped `platform`, so K9's scope-chip test finds it.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TlsNeed, TlsStatus } from "../../types";

const state = vi.hoisted(() => ({ apiFetch: vi.fn() }));

vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  apiFetch: state.apiFetch,
}));

import { ApiError } from "../../lib/api";
import CertificatesPanel, { ENVIRONMENT_LINES } from "../CertificatesPanel";

const KEY_LINE = "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgcanarycanarycanary";
const KEY = `-----BEGIN PRIVATE KEY-----\n${KEY_LINE}\n-----END PRIVATE KEY-----\n`;
const CERT = "-----BEGIN CERTIFICATE-----\nMIIBcertificate\n-----END CERTIFICATE-----\n";
const ROOT_SHA = "ab".repeat(32);

function status(overrides: Partial<TlsStatus> = {}): TlsStatus {
  return {
    edge: "on",
    site: ["librerun.example.lan"],
    issuer: { kind: "internal", ca: "local", email: null },
    root: {
      ca: "local",
      name: "Caddy Local Authority",
      subject: "CN=Caddy Local Authority - 2026 ECC Root",
      issuer: "CN=Caddy Local Authority - 2026 ECC Root",
      names: [],
      not_before: "2026-09-28T00:00:00Z",
      not_after: "2036-09-28T00:00:00Z",
      sha256: ROOT_SHA,
    },
    leaf: {
      subject: null,
      issuer: "CN=Caddy Local Authority - ECC Intermediate",
      names: ["librerun.example.lan"],
      not_before: "2026-09-30T00:00:00Z",
      not_after: "2026-09-30T12:00:00Z",
      sha256: "cd".repeat(32),
    },
    source: "environment",
    choice: null,
    environment: { variable: "LIBRERUN_TLS", ca: null },
    why: "No choice is recorded on Application Settings, so the environment's applies: LIBRERUN_TLS.",
    needs: ["trust_root"],
    acknowledged_root: null,
    ...overrides,
  };
}

let current: TlsStatus;
const toast = vi.fn();

beforeEach(() => {
  current = status();
  state.apiFetch.mockReset();
  state.apiFetch.mockImplementation(async (path: string, _token: string, init?: { method?: string }) => {
    if (path === "/admin/tls" && !init?.method) return current;
    return current;
  });
});

afterEach(() => {
  cleanup();
  toast.mockReset();
  vi.unstubAllGlobals();
});

function calls(): { path: string; method: string; body: unknown }[] {
  return state.apiFetch.mock.calls
    .filter(([, , init]) => init?.method)
    .map(([path, , init]) => ({ path, method: init.method, body: init.body ? JSON.parse(init.body) : undefined }));
}

async function renderPanel() {
  render(<CertificatesPanel token="tok" toast={toast} />);
  await screen.findByRole("heading", { name: "Certificates" });
  await waitFor(() => expect(state.apiFetch).toHaveBeenCalledWith("/admin/tls", "tok"));
}

function pick(label: string, content: string, name: string) {
  const input = screen.getByLabelText(label) as HTMLInputElement;
  fireEvent.change(input, { target: { files: [new File([content], name)] } });
  return input;
}

describe("the Certificates panel", () => {
  it("is a platform region, and says what the edge serves and why", async () => {
    await renderPanel();
    const region = document.querySelector("[data-scope-region]") as HTMLElement;
    expect(region).toBeTruthy();
    expect(region.querySelector("[data-scope]")?.getAttribute("data-scope")).toBe("platform");
    await screen.findByTestId("tls-serving");
    expect(screen.getByTestId("tls-serving").textContent).toContain("librerun.example.lan");
    expect(screen.getByTestId("tls-serving").textContent).toContain(ROOT_SHA);
    expect(screen.getByTestId("tls-source").textContent).toContain("From the environment (LIBRERUN_TLS)");
    // Each choice, with the environment line that would set it instead.
    for (const line of [ENVIRONMENT_LINES.ca, ENVIRONMENT_LINES.files, ENVIRONMENT_LINES.acme]) {
      expect(screen.getByText(line)).toBeTruthy();
    }
    // ACME that cannot issue leaves the edge with nothing to serve: the page says so first.
    expect(screen.getByTestId("tls-acme-warning").textContent).toContain("serves no certificate");
    expect(screen.queryByRole("button", { name: "Use the environment's setting" })).toBeNull();
  });

  it.each([
    ["trust_root", /trusts this root: fingerprint ab/],
    ["root_changed", /Load your CA again below, or trust the new root and acknowledge it/],
    ["edge_restart", /restart the edge/],
    ["files_ending", /upload its replacement below/],
    ["ca_ending", /load its successor below/],
    ["acme_requirements", /resolve publicly to this host, and port 443/],
  ] as [TlsNeed, RegExp][])("offers %s's action", async (need, text) => {
    current = status({ needs: [need] });
    await renderPanel();
    const item = await screen.findByTestId(`tls-need-${need}`);
    expect(item.textContent).toMatch(text);
  });

  it("downloads the root and records it as trusted", async () => {
    const fetched: string[] = [];
    vi.stubGlobal("fetch", async (url: string, init: { headers: Record<string, string> }) => {
      fetched.push(`${url} ${init.headers.Authorization}`);
      return new Response(CERT, { status: 200 });
    });
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:root", revokeObjectURL: () => undefined }));
    await renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Download the root" }));
    await waitFor(() => expect(fetched).toHaveLength(1));
    expect(fetched[0]).toMatch(/\/admin\/tls\/root\.pem Bearer tok$/);

    current = status({ needs: [], acknowledged_root: ROOT_SHA });
    fireEvent.click(screen.getByRole("button", { name: "It is trusted" }));
    await waitFor(() => expect(screen.queryByTestId("tls-need-trust_root")).toBeNull());
    expect(calls()).toEqual([{ path: "/admin/tls/acknowledge", method: "POST", body: undefined }]);
  });

  it("loads a CA, sends its key once, and never shows it", async () => {
    await renderPanel();
    const cert = pick("CA certificate", CERT, "ca.crt");
    const key = pick("CA private key", KEY, "ca.key");
    current = status({ source: "ui", choice: { kind: "ca", by: "u-1", by_email: "a@example.com", at: "2026-09-30T02:00:00Z" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Load the CA" }));
    });
    await waitFor(() => expect(calls()).toHaveLength(1));
    expect(calls()[0]).toEqual({ path: "/admin/tls/ca", method: "PUT", body: { certificate: CERT, key: KEY } });
    await waitFor(() => expect(screen.getByTestId("tls-source").textContent).toContain("by a@example.com"));
    expect(cert.value).toBe("");
    expect(key.value).toBe("");
    expect(document.body.innerHTML).not.toContain(KEY_LINE);
    expect(toast).toHaveBeenCalledWith("Load the CA: done — the edge serves it now.", "success");
  });

  it("says why a change was refused, and empties the inputs anyway", async () => {
    state.apiFetch.mockImplementation(async (path: string, _token: string, init?: { method?: string }) => {
      if (init?.method === "PUT") {
        throw new ApiError(
          'API 422: {"detail": "The certificate is not a CA: its basicConstraints do not say CA:TRUE.", "check": "basicConstraints"}',
          422,
        );
      }
      return current;
    });
    await renderPanel();
    const cert = pick("Certificate chain", CERT, "site.crt");
    const key = pick("Certificate private key", KEY, "site.key");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Use these files" }));
    });
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        "Use these files: The certificate is not a CA: its basicConstraints do not say CA:TRUE.",
        "error",
      ),
    );
    expect(cert.value).toBe("");
    expect(key.value).toBe("");
    expect(document.body.innerHTML).not.toContain(KEY_LINE);
  });

  it("chooses ACME with an e-mail, and goes back to the environment", async () => {
    current = status({ source: "ui", choice: { kind: "acme", by: null, by_email: null, at: null } });
    await renderPanel();
    fireEvent.change(screen.getByLabelText("ACME e-mail"), { target: { value: "  ops@example.com " } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Use ACME" }));
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Use the environment's setting" }));
    });
    await waitFor(() => expect(calls()).toHaveLength(2));
    expect(calls()).toEqual([
      { path: "/admin/tls/acme", method: "PUT", body: { email: "ops@example.com" } },
      { path: "/admin/tls/choice", method: "DELETE", body: undefined },
    ]);
  });

  it("with the edge off, says so and offers no change", async () => {
    current = status({ edge: "off", site: [], issuer: null, root: null, leaf: null, source: null, environment: null, needs: ["edge_off"] });
    await renderPanel();
    expect((await screen.findByTestId("tls-edge-off")).textContent).toContain("The HTTPS edge is off");
    for (const name of ["Load the CA", "Use these files", "Use ACME", "Use the environment's setting", "It is trusted"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    expect(document.querySelectorAll("input")).toHaveLength(0);
  });
});
