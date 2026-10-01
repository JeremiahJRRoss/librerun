import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchMeta, resetMetaCache } from "../meta";

const body = {
  name: "LibreRun",
  version: "1.0.0",
  demo: true,
  stub_llm: true,
  default_secret: false,
  trace_viewer_configured: true,
  trace_viewer: "jaeger",
  trace_viewer_source: "env",
  agents: [{ id: "demo-agent", name: "Demo Agent" }],
};

function fakeFetch(status: number) {
  return vi.fn(async () => ({ ok: status < 400, status, json: async () => body })) as unknown as typeof fetch;
}

describe("fetchMeta (blueprint S3)", () => {
  afterEach(() => resetMetaCache());

  it("asks /meta once per page load and hands every caller the same answer", async () => {
    const f = fakeFetch(200);
    const a = await fetchMeta(f);
    const b = await fetchMeta(f);
    expect(a).toEqual(body);
    expect(b).toBe(a);
    expect(f).toHaveBeenCalledTimes(1);
    expect((f as unknown as { mock: { calls: string[][] } }).mock.calls[0][0]).toMatch(/\/meta$/);
  });

  it("does not cache a failure: the backend may still be starting", async () => {
    const failing = vi.fn(async () => {
      throw new Error("connection refused");
    }) as unknown as typeof fetch;
    expect(await fetchMeta(failing)).toBeNull();
    const ok = fakeFetch(200);
    expect(await fetchMeta(ok)).toEqual(body);
    expect(ok).toHaveBeenCalledTimes(1);
  });

  it("treats a non-2xx answer as no facts, and retries later", async () => {
    expect(await fetchMeta(fakeFetch(503))).toBeNull();
    expect(await fetchMeta(fakeFetch(200))).toEqual(body);
  });
});
