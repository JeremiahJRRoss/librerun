/**
 * K6-11: a tenant admin on the application settings page.
 *
 * The settings are application-global, so their API answers 403 to
 * anyone but a platform admin. The page treats that as a state of its
 * own and explains it — never an endless "Loading settings…", never the
 * generic failure a real error gets. The control case is that error: a
 * 500 must not read as the operators-only explanation.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ settingsStatus: 403, toast: vi.fn() }));

vi.mock("../../../../components/NavBar", () => ({ default: () => null }));
vi.mock("../../../../lib/auth", () => ({
  useAuth: () => ({
    token: "tok",
    user: { id: "u-1", email: "a@example.com", role: "admin", tenant_id: "t-1", is_platform_admin: false },
  }),
}));
vi.mock("../../../../lib/toast", () => ({ useToast: () => ({ toast: state.toast }) }));
vi.mock("../../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../../lib/api")>();
  return {
    ...actual,
    apiFetch: async (path: string) => {
      if (path === "/admin/settings") throw new actual.ApiError("refused", state.settingsStatus);
      if (path === "/health") return { status: "ok", env: "test" };
      throw new Error(`unexpected ${path}`);
    },
  };
});

import AdminSettingsPage from "../page";

afterEach(() => {
  cleanup();
  state.toast.mockReset();
});

describe("the application settings page", () => {
  it("renders 'Platform operators only.' on a 403", async () => {
    state.settingsStatus = 403;
    render(<AdminSettingsPage />);

    expect(await screen.findByText("Platform operators only.")).toBeTruthy();
    expect(document.body.textContent).not.toContain("Failed to load settings");
    expect(state.toast).not.toHaveBeenCalled();
  });

  it("does not explain a real failure as the operators-only state", async () => {
    state.settingsStatus = 500;
    render(<AdminSettingsPage />);

    expect(await screen.findByText(/Failed to load settings/)).toBeTruthy();
    expect(screen.queryByText("Platform operators only.")).toBeNull();
  });
});
