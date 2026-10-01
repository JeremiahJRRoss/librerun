/**
 * A tenant admin on the platform operator's pages and tab (K9-07; L31, D29).
 *
 * Application Settings, Observability and the agent page's Keys tab answer
 * a tenant admin 403. Each page says why — "Platform operators only." —
 * rather than a red error or an endless wait; the control is Observability's
 * real failure, which must still read as one.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ status: 403 }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: () => undefined, replace: () => undefined }),
}));
vi.mock("../../../components/NavBar", () => ({ default: () => null }));
vi.mock("../../../lib/auth", () => ({
  useAuth: () => ({
    token: "tok",
    user: { id: "u-1", email: "a@example.com", role: "admin", tenant_id: "t-1", is_platform_admin: false },
  }),
}));
vi.mock("../../../lib/toast", () => ({ useToast: () => ({ toast: () => undefined }) }));
vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    apiFetch: async (path: string) => {
      if (path === "/health") return { status: "ok", env: "test" };
      if (path === "/admin/settings" || path === "/admin/otel-status" || path.startsWith("/admin/agent-keys")) {
        throw new actual.ApiError(`API ${state.status}: {"detail": "refused"}`, state.status);
      }
      throw new Error(`unexpected ${path}`);
    },
  };
});

import { KeysPanel } from "../../../components/agentPage/KeysPanel";
import ObservabilityStatusPage from "../observability/page";
import AdminSettingsPage from "../settings/page";

beforeEach(() => {
  state.status = 403;
});
afterEach(cleanup);

describe("the platform operator's pages, for a tenant admin", () => {
  it("Application Settings explains the 403", async () => {
    render(<AdminSettingsPage />);
    expect(await screen.findByText("Platform operators only.")).toBeTruthy();
    expect(screen.getByTestId("platform-only").textContent).toContain("application-global");
  });

  it("Observability explains the 403, and not as a failure to read it", async () => {
    render(<ObservabilityStatusPage />);
    expect(await screen.findByText("Platform operators only.")).toBeTruthy();
    expect(document.body.textContent).not.toContain("Could not read /admin/otel-status");
  });

  it("Observability still reports a real failure as one", async () => {
    state.status = 500;
    render(<ObservabilityStatusPage />);
    expect(await screen.findByText(/Could not read \/admin\/otel-status/)).toBeTruthy();
    expect(screen.queryByText("Platform operators only.")).toBeNull();
  });

  it("the Keys tab explains the 403", async () => {
    render(<KeysPanel agentId="probe-agent" token="tok" toast={() => undefined} />);
    expect(await screen.findByText("Platform operators only.")).toBeTruthy();
    expect(screen.getByTestId("platform-only").textContent).toContain("every tenant's runs");
  });
});
