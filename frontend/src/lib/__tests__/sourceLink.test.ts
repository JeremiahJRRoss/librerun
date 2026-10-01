/**
 * The Source link (K blueprint A1, gate R17), rendered for real: React 18's
 * `createRoot` into jsdom, driven with `act`, against `/meta` answered by a
 * stubbed `fetch`. The backend half — what `/meta` says — is
 * `backend/tests/test_source_access.py`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import SourceLink from "../../components/SourceLink";
import { resetMetaCache } from "../meta";

// React 18 warns about updates outside act() unless the environment says
// that the test drives act() itself, which these do.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SOURCE = "https://git.example.com/your-org/librerun/tree/v1.2.3";

const body = {
  name: "LibreRun",
  version: "1.2.3",
  license: "AGPL-3.0-only",
  source_url: SOURCE,
  demo: false,
  stub_llm: false,
  gateway: "ok",
  default_secret: false,
  trace_viewer_configured: false,
  trace_viewer: "off",
  trace_viewer_source: "env",
  agents: [],
};

function answering(json: unknown) {
  return vi.fn(async () => ({ ok: true, status: 200, json: async () => json }));
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  resetMetaCache();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  resetMetaCache();
  vi.unstubAllGlobals();
});

async function render() {
  await act(async () => {
    root.render(createElement(SourceLink));
  });
}

describe("the Source link (AGPL-3.0 section 13)", () => {
  it("renders the Source link from /meta in a new tab", async () => {
    vi.stubGlobal("fetch", answering(body));

    await render();

    const link = container.querySelector("a");
    expect(link).not.toBeNull();
    expect(link!.getAttribute("href")).toBe(SOURCE);
    // Outside this origin: a new tab, and no window.opener handed to the
    // page it opens (CLAUDE.md, "External links open in new tabs").
    expect(link!.getAttribute("target")).toBe("_blank");
    expect(link!.getAttribute("rel")).toBe("noopener noreferrer");
    // It names the licence as well as the place.
    expect(link!.textContent).toContain("AGPL-3.0-only");
    expect(link!.textContent).toContain("Source");
  });

  it("renders nothing until /meta answers", async () => {
    let answer: (value: unknown) => void = () => {};
    const pending = new Promise((resolve) => {
      answer = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => pending.then(() => ({ ok: true, status: 200, json: async () => body }))),
    );

    await render();
    // No guessed link while the question is open.
    expect(container.innerHTML).toBe("");

    await act(async () => {
      answer(undefined);
      await pending;
    });
    expect(container.querySelector("a")?.getAttribute("href")).toBe(SOURCE);
  });

  it("opens nothing that is not an http(s) URL", async () => {
    vi.stubGlobal("fetch", answering({ ...body, source_url: "javascript:alert(1)" }));

    await render();

    expect(container.innerHTML).toBe("");
  });
});
