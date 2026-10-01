"use client";

/**
 * Mounts browser telemetry (once, client-side) and observes App Router
 * route commits.
 *
 * The commit boundary is deliberately narrow and honestly named: a route
 * change "commits" when `usePathname`/`useSearchParams` observe the new
 * URL state — Next 14's documented way to react to navigation. That is
 * NOT "the route finished rendering": Server Components and Suspense
 * stream content after commit, and no framework event marks "settled".
 * We measure intent→commit (when a link click / traversal intent was
 * observed) and never hold spans open on network- or DOM-quiet
 * heuristics.
 *
 * `useSearchParams` requires a Suspense boundary in Next 14 — hence the
 * split component.
 */

import { Suspense, useEffect, useRef } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { initTelemetry, routeCommitted } from "./facade";

function NavigationCommitObserver() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const sawInitial = useRef(false);

  useEffect(() => {
    routeCommitted(pathname ?? "/", !sawInitial.current);
    sawInitial.current = true;
    // searchParams participates so same-path query navigations commit too;
    // the resolved route template never contains the query itself.
  }, [pathname, searchParams]);

  return null;
}

export function TelemetryProvider() {
  useEffect(() => {
    initTelemetry();
  }, []);

  return (
    <Suspense fallback={null}>
      <NavigationCommitObserver />
    </Suspense>
  );
}
