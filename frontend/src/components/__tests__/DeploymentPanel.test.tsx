/**
 * The Deployment panel on Application Settings (K9-04; D16, D43).
 *
 * Read-only, chipped `deployment`: each value with where it came from and
 * where to change it, the header variables by presence, and the transport
 * the page was loaded over — through the edge, a link to T2's Certificates
 * panel on the same page; on plain HTTP, a pointer to Install.md's "HTTPS
 * at the edge".
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DeploymentView } from "../../types";

const state = vi.hoisted(() => ({ apiFetch: vi.fn() }));

vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  apiFetch: state.apiFetch,
}));

import DeploymentPanel from "../DeploymentPanel";

function view(scheme: "https" | "http"): DeploymentView {
  return {
    version: "1.0.0",
    license: "AGPL-3.0-only",
    source_url: "https://example.invalid/src",
    demo: true,
    stub: true,
    gateway: { reachable: true, reported: true, version: "1.0.0", updated_at: "2026-09-30T12:00:00Z", providers: [] },
    settings: [
      { name: "LIBRERUN_DEMO", env_class: 2, value: true, source: "env", hint: "Change it in .env, then restart the backend." },
      { name: "LOG_LEVEL", env_class: 2, value: "INFO", source: "default", hint: "Change it in .env, then restart the backend." },
    ],
    otlp_headers: [
      { name: "OTEL_EXPORTER_OTLP_HEADERS", set: true },
      { name: "OTEL_EXPORTER_OTLP_TRACES_HEADERS", set: false },
    ],
    transport: { scheme, host: "librerun.example.lan" },
  };
}

beforeEach(() => {
  state.apiFetch.mockReset();
});
afterEach(cleanup);

describe("DeploymentPanel", () => {
  it("links the transport row to the Certificates panel through the edge", async () => {
    state.apiFetch.mockResolvedValue(view("https"));
    render(<DeploymentPanel token="tok" />);

    const transport = await screen.findByTestId("deployment-transport");
    const link = transport.querySelector('a[href="#certificates"]');
    expect(link).toBeTruthy();
    expect(transport.textContent).toContain("HTTPS through the edge");
    expect(state.apiFetch).toHaveBeenCalledWith("/admin/deployment", "tok");
  });

  it("points at Install.md's HTTPS section on plain HTTP", async () => {
    state.apiFetch.mockResolvedValue(view("http"));
    render(<DeploymentPanel token="tok" />);

    const transport = await screen.findByTestId("deployment-transport");
    expect(transport.querySelector("a")).toBeNull();
    expect(transport.textContent).toContain("HTTPS at the edge");
    expect(transport.textContent).toContain("Plain HTTP");
  });

  it("is read-only and chipped deployment, each value with its source and hint", async () => {
    state.apiFetch.mockResolvedValue(view("http"));
    render(<DeploymentPanel token="tok" />);

    const settings = await screen.findByTestId("deployment-settings");
    const region = document.querySelector("[data-scope-region]");
    expect(region?.getAttribute("data-scope-region")).toBe("deployment");
    expect(region?.querySelector('[data-scope="deployment"]')).toBeTruthy();
    expect(document.querySelector("input, select, textarea")).toBeNull();

    const demo = settings.querySelector('[data-setting="LIBRERUN_DEMO"]');
    expect(demo?.textContent).toContain("true");
    expect(demo?.textContent).toContain("environment");
    expect(settings.querySelector('[data-setting="LOG_LEVEL"]')?.textContent).toContain("default");
    const headers = screen.getByTestId("deployment-headers").textContent ?? "";
    expect(headers).toContain("OTEL_EXPORTER_OTLP_HEADERS: set");
    expect(headers).toContain("OTEL_EXPORTER_OTLP_TRACES_HEADERS: not set");
  });

  it("says when the view could not be read, with a retry", async () => {
    state.apiFetch.mockRejectedValue(new Error("down"));
    render(<DeploymentPanel token="tok" />);

    expect(await screen.findByText(/The deployment could not be read/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });
});
