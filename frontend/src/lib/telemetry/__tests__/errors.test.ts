/**
 * Error-channel tests — the hardest PII channel. The invariant under test:
 * whatever an Error carries (users paste raw vendor logs and code may
 * interpolate them into messages), nothing beyond a class name, a
 * mechanism, a local hash, and an own-bundle asset path ever leaves.
 */
import { beforeEach, describe, expect, it } from "vitest";
import {
  _classifyForTests as classify,
  _resetErrorBudgetsForTests,
  fnv1a,
  normalizeFrames,
} from "../errors";

const SECRET = "LIBRERUN_SECRET_CANARY_9f31 password=hunter2 Bearer tok-abc";
// The page's own origin (jsdom-provided): same-origin frames must use it
// now that foreign origins are rejected even under the bundle prefix.
const ORIGIN = window.location.origin;

function makeError(message: string, stack?: string): Error {
  const err = new Error(message);
  if (stack !== undefined) err.stack = stack;
  return err;
}

beforeEach(() => {
  _resetErrorBudgetsForTests();
});

describe("classify", () => {
  it("never exports the message, in any field", () => {
    const err = makeError(`Could not parse user log: ${SECRET}`);
    const out = classify(err, "window.error");
    expect(out).not.toBeNull();
    const body = JSON.stringify(out);
    expect(body).not.toContain("LIBRERUN_SECRET_CANARY");
    expect(body).not.toContain("hunter2");
    expect(body).not.toContain("Could not parse");
  });

  it("keeps only the bounded class name and drops injected ones", () => {
    const err = makeError("x");
    err.name = `Weird name with ${SECRET}`;
    const out = classify(err, "window.error");
    expect(out!.errorType).toBe("Error");
  });

  it("fingerprints are stable for the same type+frames and hex-shaped", () => {
    const stack = `Error: whatever\n    at fn (${ORIGIN}/_next/static/chunks/main-abc.js:1:2)`;
    const a = classify(makeError("first message", stack), "window.error");
    _resetErrorBudgetsForTests();
    const b = classify(makeError("совершенно other message", stack), "window.error");
    expect(a!.fingerprint).toBe(b!.fingerprint); // message never joins the hash
    expect(a!.fingerprint).toMatch(/^[a-f0-9]{8}$/);
  });

  it("non-Error rejection reasons become a fixed class, nothing more", () => {
    const out = classify(`raw string rejection: ${SECRET}`, "unhandledrejection");
    expect(out!.errorType).toBe("UnhandledRejection");
    expect(JSON.stringify(out)).not.toContain("CANARY");
  });

  it("caps repeats per fingerprint and per session", () => {
    const stack = "Error\n    at /_next/static/chunks/a.js:1:1";
    let emitted = 0;
    for (let i = 0; i < 10; i++) {
      if (classify(makeError("same", stack), "window.error") !== null) emitted++;
    }
    expect(emitted).toBe(5); // MAX_PER_FINGERPRINT

    // Distinct fingerprints keep flowing until the session cap trips.
    let more = 0;
    for (let i = 0; i < 40; i++) {
      const s = `Error\n    at /_next/static/chunks/f${i}.js:1:1`;
      if (classify(makeError("same", s), "window.error") !== null) more++;
    }
    expect(emitted + more).toBe(30); // MAX_PER_SESSION
  });
});

describe("normalizeFrames", () => {
  it("a path-like token in the MESSAGE line never becomes a bundle module", () => {
    // The Codex P1 case: application code interpolates user text that
    // happens to look like a /_next/ path into the Error message. The
    // message line is not frame-shaped and must be skipped entirely.
    const err = makeError(
      "auth failed for /_next/private-token-SECRET99",
      "Error: auth failed for /_next/private-token-SECRET99\n" +
        "    at fn (https://evil.example.net/x.js:1:1)"
    );
    const out = classify(err, "window.error");
    expect(out!.bundleModule).toBeUndefined();
    expect(JSON.stringify(out)).not.toContain("private-token");
  });

  it("multi-line messages are skipped until the first frame-shaped line", () => {
    const stack =
      "Error: line one\n" +
      "/_next/private-more-SECRET\n" +
      `    at fn (${ORIGIN}/_next/static/chunks/ok.js:1:1)`;
    expect(normalizeFrames(stack)).toEqual(["/_next/static/chunks/ok.js"]);
  });

  it("gecko/webkit-style frames (fn@url, @url) are recognized", () => {
    const stack =
      `fn@${ORIGIN}/_next/static/chunks/page.js:1:2\n` +
      `@${ORIGIN}/_next/static/chunks/anon.js:3:4`;
    expect(normalizeFrames(stack)).toEqual([
      "/_next/static/chunks/page.js",
      "/_next/static/chunks/anon.js",
    ]);
  });

  it("non-static /_next/ paths are not treated as bundles", () => {
    const stack = `Error\n    at fn (${ORIGIN}/_next/data/build/leak.json:1:1)`;
    expect(normalizeFrames(stack)).toEqual([]);
  });

  it("non-http URL schemes are URL-shaped too — extension frames are rejected", () => {
    // Round-4 Codex case: chrome-extension:// is not http(s), so an
    // http-only absolute matcher would fall through to the relative
    // branch and substring-match the imitation path inside the URL.
    const stack =
      "Error\n" +
      "    at fn (chrome-extension://abcdefgh/_next/static/customer-secret.js:1:1)\n" +
      "    at fn2 (moz-extension://xyz/_next/static/also-foreign.js:1:1)";
    expect(normalizeFrames(stack)).toEqual([]);
  });

  it("schemes WITHOUT double slashes (data:) are rejected too", () => {
    // Round-5 Codex case: no "//" anywhere, so a ://-only URL detector
    // would fall through to the relative substring match. The forged
    // variant needs no script injection — a pasted string interpolated
    // into an Error message can fabricate this exact frame shape.
    const stack =
      "Error\n" +
      "    at fn (data:text/javascript,/_next/static/customer-secret.js:1:1)\n" +
      "fn@data:text/javascript,/_next/static/also-bad.js:1:1";
    expect(normalizeFrames(stack)).toEqual([]);
    // Legitimate relative frames — colons only as :line:col — still work.
    expect(
      normalizeFrames("Error\n    at /_next/static/chunks/ok.js:3:4")
    ).toEqual(["/_next/static/chunks/ok.js"]);
  });

  it("protocol-relative frame URLs resolve to a concrete origin and are rejected when foreign", () => {
    const stack =
      "Error\n    at fn (//evil.example/_next/static/chunks/leak.js:1:1)";
    expect(normalizeFrames(stack)).toEqual([]);
  });

  it("a FOREIGN origin's /_next/static/ path is rejected — origin is checked, not just the prefix", () => {
    // The round-2 Codex case: a third-party script deliberately served
    // under a bundle-imitating path must not enter telemetry.
    const stack =
      "Error\n" +
      "    at fn (https://evil.example/_next/static/chunks/customer-secret.js:1:1)\n" +
      `    at ok (${ORIGIN}/_next/static/chunks/real.js:2:2)`;
    const frames = normalizeFrames(stack);
    expect(frames).toEqual(["/_next/static/chunks/real.js"]);
    expect(JSON.stringify(frames)).not.toContain("customer-secret");
  });

  it("keeps own-bundle paths and strips query strings and foreign frames", () => {
    const stack = [
      `Error: leak ${SECRET}`,
      `    at fn (${ORIGIN}/_next/static/chunks/page-abc.js?ts=${SECRET}:10:20)`,
      "    at other (https://evil.example.net/payload.js:1:1)",
      `    at fn2 (${ORIGIN}/_next/static/css/style.css#frag:2:2)`,
    ].join("\n");
    const frames = normalizeFrames(stack);
    expect(frames).toEqual([
      "/_next/static/chunks/page-abc.js",
      "/_next/static/css/style.css",
    ]);
    expect(JSON.stringify(frames)).not.toContain("CANARY");
    expect(JSON.stringify(frames)).not.toContain("evil.example.net");
  });

  it("handles absent stacks", () => {
    expect(normalizeFrames(undefined)).toEqual([]);
  });
});

describe("fnv1a", () => {
  it("is deterministic and 8 hex chars", () => {
    expect(fnv1a("abc")).toBe(fnv1a("abc"));
    expect(fnv1a("abc")).toMatch(/^[a-f0-9]{8}$/);
    expect(fnv1a("abc")).not.toBe(fnv1a("abd"));
  });
});
