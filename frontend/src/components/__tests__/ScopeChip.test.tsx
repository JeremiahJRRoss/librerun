/**
 * The scope chip (K4b): each tier of the configuration blueprint's §1.2
 * reads in the words K9 puts beside every field.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import ScopeChip, { SCOPE_LABELS, type Scope } from "../ScopeChip";

afterEach(cleanup);

describe("ScopeChip", () => {
  it("labels each scope in K9's words", () => {
    expect(SCOPE_LABELS).toEqual({
      deployment: "deployment",
      platform: "platform",
      tenant: "this tenant",
      agent: "this agent",
      agent_tenant: "this agent · this tenant",
    });
  });

  it("tells one agent's default from its value in this tenant (K8b)", () => {
    // The Secrets tab puts both on one page: the chip's words, and the
    // scope a page finds it by, must never read the same.
    render(<ScopeChip scope="agent" />);
    const chip = screen.getByText("this agent");
    expect(chip.getAttribute("data-scope")).toBe("agent");
    expect(chip.getAttribute("title")).toMatch(/platform admin/);
    cleanup();
    render(<ScopeChip scope="agent_tenant" />);
    expect(screen.queryByText("this agent")).toBeNull();
    expect(screen.getByText("this agent · this tenant").getAttribute("data-scope")).toBe("agent_tenant");
  });

  it("renders the label, and the scope as data a page can find it by", () => {
    for (const scope of Object.keys(SCOPE_LABELS) as Scope[]) {
      render(<ScopeChip scope={scope} />);
      const chip = screen.getByText(SCOPE_LABELS[scope]);
      expect(chip.getAttribute("data-scope")).toBe(scope);
      expect(chip.getAttribute("title")).toBeTruthy();
      cleanup();
    }
  });
});
