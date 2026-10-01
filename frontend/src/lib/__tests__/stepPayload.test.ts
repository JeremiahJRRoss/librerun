import { describe, expect, it } from "vitest";
import { stepUpdates } from "../stepPayload";
import type { AgentStepConfig } from "../../types";

const EDITABLE = ["temperature", "max_tokens", "timeout_seconds"];

function step(overrides: Partial<AgentStepConfig> = {}): AgentStepConfig {
  return {
    step_id: "analyze",
    label: "Analyze",
    description: "",
    provider: "openai",
    model: "gpt-4o",
    temperature: 0,
    max_tokens: 2000,
    timeout_seconds: 120,
    overridden: [],
    ...overrides,
  };
}

describe("stepUpdates (blueprint S4a)", () => {
  it("sends an emptied number as null, not as nothing", () => {
    // The defect: an omitted field leaves the stored row alone, so
    // clearing the box and saving used to change nothing at all.
    const [update] = stepUpdates(
      [step({ max_tokens: null as unknown as number })],
      EDITABLE,
    );

    expect("max_tokens" in update).toBe(true);
    expect(update.max_tokens).toBeNull();
  });

  it("sends an emptied model as null too", () => {
    const [update] = stepUpdates([step({ model: "" })], EDITABLE);

    expect(update.model).toBeNull();
  });

  it("sends every editable field, so a clear of any one of them lands", () => {
    const [update] = stepUpdates([step()], EDITABLE);

    expect(Object.keys(update).sort()).toEqual(
      ["max_tokens", "model", "provider", "step_id", "temperature", "timeout_seconds"],
    );
  });

  it("keeps a zero, which is a value and not an empty box", () => {
    const [update] = stepUpdates([step({ temperature: 0 })], EDITABLE);

    expect(update.temperature).toBe(0);
  });

  it("sends a step whose manifest leaves the provider to the admin", () => {
    // `llm.steps[]` holds only LLM steps, so an absent provider means
    // "the admin chooses", not "no model here". Dropping such a step
    // made that agent impossible to configure at all.
    const updates = stepUpdates(
      [step(), step({ step_id: "summarize", provider: null, model: null })],
      EDITABLE,
    );

    expect(updates.map((u) => u.step_id)).toEqual(["analyze", "summarize"]);
    expect(updates[1].provider).toBeNull();
    expect(updates[1].model).toBeNull();
  });

  it("sends a provider the admin has just chosen for such a step", () => {
    const [update] = stepUpdates(
      [step({ provider: "anthropic", model: "claude-sonnet-4-6" })],
      EDITABLE,
    );

    expect(update.provider).toBe("anthropic");
    expect(update.model).toBe("claude-sonnet-4-6");
  });

  it("sends an emptied provider as null, not as an empty string", () => {
    const [update] = stepUpdates([step({ provider: "" })], EDITABLE);

    expect(update.provider).toBeNull();
  });

  it("sends only the fields the server declared editable", () => {
    const [update] = stepUpdates([step()], ["max_tokens"]);

    expect("temperature" in update).toBe(false);
    expect(update.max_tokens).toBe(2000);
  });
});
