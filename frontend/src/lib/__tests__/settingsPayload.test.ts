import { describe, expect, it } from "vitest";
import { settingReset, settingUpdates } from "../settingsPayload";
import type { AgentSettingSpec } from "../../types";

function spec(key: string, type: AgentSettingSpec["type"], defaultValue: unknown): AgentSettingSpec {
  return {
    key,
    label: key,
    type,
    default: defaultValue,
    options: type === "enum" ? ["basic", "advanced"] : null,
    description: "",
  };
}

const SPECS = [
  spec("note", "string", "hello"),
  spec("limit", "int", 3),
  spec("ratio", "float", 0.5),
  spec("strict", "bool", true),
  spec("depth", "enum", "advanced"),
  spec("tags", "string_list", []),
];

describe("settingUpdates (K5a)", () => {
  it("sends every declared key, in the declaration's order, as {key, value}", () => {
    const updates = settingUpdates(SPECS, {
      note: "hi",
      limit: 7,
      ratio: 0.25,
      strict: true,
      depth: "basic",
      tags: ["a"],
    });

    expect(updates).toEqual([
      { key: "note", value: "hi" },
      { key: "limit", value: 7 },
      { key: "ratio", value: 0.25 },
      { key: "strict", value: true },
      { key: "depth", value: "basic" },
      { key: "tags", value: ["a"] },
    ]);
  });

  it("sends a value equal to the default too: the server decides it is not a choice", () => {
    const updates = settingUpdates(SPECS, { note: "hello", limit: 3 });

    expect(updates.find((u) => u.key === "note")).toEqual({ key: "note", value: "hello" });
    expect(updates.find((u) => u.key === "limit")).toEqual({ key: "limit", value: 3 });
  });

  it("sends a blank as null — and sends it, rather than leaving it out", () => {
    // An emptied text box, an emptied number (the field holds null), and a
    // key the form never had: each must be SENT, as the clear.
    const updates = settingUpdates(SPECS, { note: "", limit: null });

    expect(updates.map((u) => u.key)).toEqual(SPECS.map((s) => s.key));
    expect(updates.find((u) => u.key === "note")?.value).toBeNull();
    expect(updates.find((u) => u.key === "limit")?.value).toBeNull();
    expect(updates.find((u) => u.key === "ratio")?.value).toBeNull();
  });

  it("sends false and 0 as values, never as clears", () => {
    const updates = settingUpdates(SPECS, { strict: false, limit: 0, ratio: 0, tags: [] });

    expect(updates.find((u) => u.key === "strict")?.value).toBe(false);
    expect(updates.find((u) => u.key === "limit")?.value).toBe(0);
    expect(updates.find((u) => u.key === "ratio")?.value).toBe(0);
    expect(updates.find((u) => u.key === "tags")?.value).toEqual([]);
  });

  it("sends nothing for a key the agent does not declare", () => {
    const updates = settingUpdates([spec("note", "string", "")], { note: "x", stray: 1 });

    expect(updates).toEqual([{ key: "note", value: "x" }]);
  });
});

describe("settingReset (K5a)", () => {
  it("is that one setting, as null: the agent's default", () => {
    expect(settingReset("limit")).toEqual([{ key: "limit", value: null }]);
  });
});
