"use client";

/**
 * The Source link (K blueprint A1, gate R17): where the source of the
 * version this LibreRun runs can be fetched, and the licence it is under,
 * both as ``GET /meta`` reports them. AGPL-3.0 section 13 asks whoever
 * runs a modified LibreRun for people who use it over a network to offer
 * them that source; this link is the place the offer is made, on the login
 * page and in the navigation bar.
 *
 * Built with ``createElement`` rather than JSX because, when it was
 * written, vitest collected only ``.test.ts`` files under ``src/lib``.
 * Since K4b it collects ``.test.ts`` and ``.test.tsx`` files in every
 * ``__tests__`` directory under ``src``, with React's automatic JSX
 * runtime (``vitest.config.mts``), so a JSX component renders in a test
 * too; this one is left as it is.
 *
 * It renders nothing until ``/meta`` answers — a link to a guessed address
 * would be worse than none — and nothing when the answer is not an http(s)
 * URL a browser can open.
 */
import { createElement } from "react";
import { useMeta } from "../lib/meta";

/** An http(s) URL, the only kind this link will open. */
export function isSourceHref(url: unknown): url is string {
  return typeof url === "string" && /^https?:\/\//i.test(url);
}

export default function SourceLink({ className }: { className?: string }) {
  const meta = useMeta();
  if (!meta || !isSourceHref(meta.source_url)) return null;
  // Outside this origin, so a new tab without an opener (CLAUDE.md).
  return createElement(
    "a",
    {
      href: meta.source_url,
      target: "_blank",
      rel: "noopener noreferrer",
      className,
      "data-testid": "source-link",
      title: `The source of this LibreRun (${meta.version}), under ${meta.license}`,
    },
    `Source · ${meta.license}`,
  );
}
