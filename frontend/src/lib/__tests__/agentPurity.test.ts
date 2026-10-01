/**
 * No component keys on an agent id (blueprint S7, L13).
 *
 * The chassis renders every agent from its manifest; a component that
 * compares `agent_id` to a literal, or switches on one, is the platform
 * special-casing one agent — the thing `chassis-purity` catches by the
 * demo agent's NAME, and this catches by the SHAPE, whatever the name.
 * The scan is negative-tested inline: every pattern must flag a snippet
 * written to violate it, so a scan that stopped matching cannot pass on
 * a clean tree.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = join(__dirname, "..", "..");

// Each pattern names one way of keying on an agent id.
const PATTERNS: Array<{ why: string; re: RegExp }> = [
  { why: "comparing agent_id to a literal", re: /\bagent_id\s*[!=]==?\s*["'`]/ },
  { why: "comparing agentId to a literal", re: /\bagentId\s*[!=]==?\s*["'`]/ },
  { why: "a literal agent id (<name>-v<n>)", re: /["'`][a-z0-9]+(?:-[a-z0-9]+)*-v\d+["'`]/ },
  { why: "switching on an agent id", re: /switch\s*\(\s*(?:[\w.]*\.)?agent_?[iI]d\s*\)/ },
  { why: "a map keyed by agent id", re: /\bbyAgentId\b|\bAGENT_(?:VIEWS|COMPONENTS|LABELS)\b/ },
];

function* walk(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === "__tests__" || entry === "node_modules") continue;
      yield* walk(full);
    } else if (/\.(tsx?|jsx?)$/.test(entry)) {
      yield full;
    }
  }
}

function violations(source: string): string[] {
  return PATTERNS.filter((p) => p.re.test(source)).map((p) => p.why);
}

describe("no component keys on an agent id", () => {
  it("the scan bites: every pattern flags the snippet written to violate it", () => {
    const probes = [
      ['if (detail.agent_id === "some-agent-v1") return <Special />;', "comparing agent_id to a literal"],
      ["const rich = agentId == 'demo-v2';", "comparing agentId to a literal"],
      ["const label = LABELS['triage-v1'];", "a literal agent id (<name>-v<n>)"],
      ["switch (run.agent_id) { default: break; }", "switching on an agent id"],
      ["const AGENT_VIEWS = { a: A };", "a map keyed by agent id"],
    ] as const;
    for (const [snippet, why] of probes) {
      expect(violations(snippet), snippet).toContain(why);
    }
  });

  it("the scan admits the clean shapes the chassis really uses", () => {
    for (const clean of [
      "const name = agents[c.agent_id] ?? c.agent_id;",
      "tagSurface({ owner: 'agent', agentId: detail.agent_id, runId: id });",
      'apiFetch(`/agents/${agentId}/scenarios`, token)',
      'if (a.agent_id === selected) { }',
      "const v = 'web-vitals';",
    ]) {
      expect(violations(clean), clean).toEqual([]);
    }
  });

  it("frontend/src carries none", () => {
    const found: string[] = [];
    let scanned = 0;
    for (const file of walk(SRC)) {
      scanned += 1;
      const hits = violations(readFileSync(file, "utf8"));
      if (hits.length) found.push(`${relative(SRC, file)}: ${hits.join(", ")}`);
    }
    // A scan of nothing proves nothing.
    expect(scanned).toBeGreaterThan(20);
    expect(found).toEqual([]);
  });
});
