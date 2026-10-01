/**
 * The admin hub (K9-01, K9-02).
 *
 * Each card says what its page edits, or shows, under its scope chip; the
 * platform's two say "platform operators only" to a tenant admin. A
 * platform admin sees the ids a key is installed for with no agent
 * registered under them, each linked to its page, and an agent-id field
 * that opens one for a first key. The ids are synthetic: the hub names no
 * agent (L13).
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { UserProfile } from "../../../types";

const state = vi.hoisted(() => ({
  user: null as UserProfile | null,
  push: vi.fn(),
  keysRead: 0,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: state.push, replace: () => undefined }),
}));
vi.mock("../../../components/NavBar", () => ({ default: () => null }));
vi.mock("../../../lib/auth", () => ({ useAuth: () => ({ token: "tok", user: state.user }) }));
vi.mock("../../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/api")>()),
  apiFetch: async (path: string) => {
    if (path === "/agents") return [];
    if (path === "/admin/drift-summary") return { last_24h: 0, recent: [] };
    if (path === "/admin/agent-keys") {
      state.keysRead += 1;
      const key = (agentId: string, registered: boolean, role = "current") => ({
        agent_id: agentId, key_prefix: "probepro", source: "admin", role, issued_at: "2026-09-30T12:00:00Z",
        issued_by: null, previous_since: null, previous_until: null, last_used_at: null, rotatable: true, registered,
      });
      return [key("probe-here", true), key("probe-gone", false), key("probe-gone", false, "previous")];
    }
    throw new Error(`unexpected ${path}`);
  },
}));

import AdminHomePage from "../page";

function user(isPlatformAdmin: boolean): UserProfile {
  return { id: "u-1", email: "a@example.com", role: "admin", tenant_id: "t-1", is_platform_admin: isPlatformAdmin };
}

beforeEach(() => {
  state.user = user(true);
  state.push.mockReset();
  state.keysRead = 0;
});
afterEach(cleanup);

describe("the admin hub", () => {
  it("says what each page edits under its chip, and no longer promises upload limits", async () => {
    render(<AdminHomePage />);
    const settings = await screen.findByRole("link", { name: /Application Settings/ });
    expect(settings.textContent).toContain("model providers and certificates");
    expect(document.body.textContent).not.toContain("upload limits");
    expect(document.body.textContent).not.toContain("pipeline flags");

    for (const [label, scope] of [
      ["Users & Access", "tenant"],
      ["Auth Configuration", "tenant"],
      ["Feedback Dashboard", "tenant"],
      ["Activity Audit Log", "tenant"],
      ["Application Settings", "platform"],
      ["Observability", "deployment"],
    ] as const) {
      const card = screen.getByRole("link", { name: new RegExp(label) });
      expect(card.querySelectorAll("[data-scope]").length, label).toBe(1);
      expect(card.querySelector(`[data-scope="${scope}"]`), label).toBeTruthy();
    }
    // A platform admin is not told the platform's pages are closed to them.
    expect(document.body.textContent).not.toContain("platform operators only");
  });

  it("tells a tenant admin which pages are the platform operator's, and reads no keys", async () => {
    state.user = user(false);
    render(<AdminHomePage />);
    const settings = await screen.findByRole("link", { name: /Application Settings/ });
    expect(settings.textContent).toContain("platform operators only");
    expect(screen.getByRole("link", { name: /Observability/ }).textContent).toContain("platform operators only");
    expect(screen.getByRole("link", { name: /Users & Access/ }).textContent).not.toContain("platform operators only");
    expect(screen.queryByTestId("agent-keys-hub")).toBeNull();
    expect(state.keysRead).toBe(0);
  });

  it("links a platform admin to the ids with keys and no registered agent", async () => {
    render(<AdminHomePage />);
    const listed = await screen.findByTestId("unregistered-keys");
    const links = Array.from(listed.querySelectorAll("a")).map((a) => [a.textContent, a.getAttribute("href")]);
    expect(links).toEqual([["probe-gone", "/admin/agents/probe-gone/config"]]);
  });

  it("opens an agent's page from its id, and only an id a manifest could declare", async () => {
    render(<AdminHomePage />);
    const field = await screen.findByLabelText(/Agent id, for a first key/);
    const open = screen.getByRole("button", { name: "Open" }) as HTMLButtonElement;

    fireEvent.change(field, { target: { value: "Not_An_Id" } });
    expect(open.disabled).toBe(true);
    fireEvent.change(field, { target: { value: "probe-new" } });
    expect(open.disabled).toBe(false);
    fireEvent.click(open);
    await waitFor(() => expect(state.push).toHaveBeenCalledWith("/admin/agents/probe-new/config"));
  });
});
