"use client";

/**
 * The deployment, on Application Settings (K9-04; D16, L29, D43).
 *
 * What the backend runs with, read-only: the version, licence and source;
 * the gateway's version, report and keyless mode; each posture and
 * bootstrap name with its value, whether it came from the environment or
 * is the default, and where to change it; the OTLP header variables, set
 * or not; and the transport this page was loaded over. Nothing here is
 * editable — a posture changes by a reviewed deployment, not a toggle
 * (L29) — and nothing here is a secret: the server answers an allowlist of
 * names, never the environment, a header's value or a URL's userinfo.
 *
 * "Environment" means the process environment, and compose passes a
 * default for most of these names, so a value compose supplied reads as
 * "environment" too; the panel says so.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/api";
import type { DeploymentSetting, DeploymentView } from "../types";
import ScopeChip from "./ScopeChip";

function shown(value: DeploymentSetting["value"]): string {
  if (value === null || value === undefined) return "—";
  if (value === "") return "(blank)";
  return String(value);
}

export default function DeploymentPanel({ token }: { token: string | null }) {
  const [view, setView] = useState<DeploymentView | null>(null);
  const [failed, setFailed] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    try {
      const next = await apiFetch<DeploymentView>("/admin/deployment", token);
      // A backend older than K9, or anything else answering, is a failure
      // to read — never a view to render.
      if (!next || !Array.isArray(next.settings)) throw new Error("not a deployment view");
      if (!mounted.current) return;
      setView(next);
      setFailed(false);
    } catch {
      if (mounted.current) setFailed(true);
    }
  }, [token]);

  useEffect(() => {
    load();
  }, [load]);

  const https = view?.transport.scheme === "https";

  return (
    <section
      aria-labelledby="deployment"
      data-scope-region="deployment"
      className="mb-6 rounded border bg-white p-4"
    >
      <div className="flex items-center gap-2">
        <h2 id="deployment" className="text-lg font-semibold">
          Deployment
        </h2>
        <ScopeChip scope="deployment" />
      </div>
      <p className="mt-1 text-xs text-slate-600">
        What this deployment runs with, read-only: each value is changed in the environment, and a
        change needs a restart. &ldquo;Environment&rdquo; means the process environment, compose&apos;s
        defaults included; &ldquo;default&rdquo; is the backend&apos;s own. No secret is shown here.
      </p>

      {failed && (
        <p className="mt-3 text-sm text-red-800">
          The deployment could not be read.{" "}
          <button className="underline" onClick={() => load()}>
            Retry
          </button>
        </p>
      )}
      {!view && !failed && <p className="mt-3 text-sm text-slate-500">Reading the deployment…</p>}

      {view && (
        <>
          <dl
            data-testid="deployment-facts"
            className="mt-3 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-sm"
          >
            <dt className="text-slate-500">Version</dt>
            <dd className="font-mono">{view.version}</dd>
            <dt className="text-slate-500">Licence</dt>
            <dd>{view.license}</dd>
            <dt className="text-slate-500">Source</dt>
            <dd className="break-all font-mono text-xs">{view.source_url ?? "—"}</dd>
            <dt className="text-slate-500">Gateway</dt>
            <dd>
              {view.gateway.reported
                ? `${view.gateway.version ?? "—"}, reported ${view.gateway.updated_at ?? "—"}`
                : "not reported yet"}
              {view.gateway.reachable ? "" : " — not answering now"}
            </dd>
            <dt className="text-slate-500">Keyless (stub)</dt>
            <dd>{view.stub === null ? "unknown: the gateway is not answering" : String(view.stub)}</dd>
            <dt className="text-slate-500">Transport</dt>
            <dd data-testid="deployment-transport">
              {https ? (
                <>
                  HTTPS through the edge, at {view.transport.host ?? "—"}.{" "}
                  <a href="#certificates" className="underline">
                    What the edge serves: Certificates
                  </a>
                </>
              ) : (
                <>
                  Plain HTTP, at {view.transport.host ?? "—"}: no edge in front. docs/platform/Install.md,
                  &ldquo;HTTPS at the edge&rdquo;, says how to turn it on.
                </>
              )}
            </dd>
          </dl>

          <table data-testid="deployment-settings" className="mt-4 w-full text-left text-sm">
            <thead>
              <tr className="text-xs text-slate-500">
                <th className="py-1 pr-3 font-normal">Name</th>
                <th className="py-1 pr-3 font-normal">Value</th>
                <th className="py-1 pr-3 font-normal">From</th>
                <th className="py-1 font-normal">To change it</th>
              </tr>
            </thead>
            <tbody>
              {view.settings.map((row) => (
                <tr key={row.name} data-setting={row.name} className="border-t align-top">
                  <td className="py-1 pr-3 font-mono text-xs">{row.name}</td>
                  <td className="break-all py-1 pr-3 font-mono text-xs">{shown(row.value)}</td>
                  <td className="py-1 pr-3 text-xs">{row.source === "env" ? "environment" : "default"}</td>
                  <td className="py-1 text-xs text-slate-600">{row.hint}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <ul data-testid="deployment-headers" className="mt-3 space-y-0.5 text-xs text-slate-700">
            {view.otlp_headers.map((header) => (
              <li key={header.name}>
                <span className="font-mono">{header.name}</span>: {header.set ? "set" : "not set"}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
