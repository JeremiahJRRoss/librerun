"use client";

import { useEffect, useState } from "react";
import NavBar from "../../../components/NavBar";
import PlatformOnly from "../../../components/PlatformOnly";
import { ApiError, apiFetch } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";

/**
 * Where telemetry goes once it has left the backend (blueprint S7a).
 *
 * The one question an operator asks after wiring a vendor up is "is it
 * actually going there?", and the honest answer has parts: which
 * overlay Vector and the otel-bridge were started with, whether that
 * name is one LibreRun ships an overlay for at all, what the overlay
 * declares it ships, and whether the backend can still reach the
 * router. Each is labelled for what it is — the sink list is the
 * overlay's own declaration, not a read-back from Vector — so nothing
 * here reads as a delivery receipt.
 */
interface OverlaySink {
  id: string;
  leg: string;
  type: string;
  destination: string;
}
interface Overlay {
  vendor: string | null;
  label?: string;
  supported: boolean;
  active: boolean;
  detail: string;
  configs: string[];
  sinks: OverlaySink[];
  sinks_are: string;
}
interface VectorHealth {
  endpoint: string | null;
  reachable: boolean | null;
  checked: string;
  detail: string;
}
interface SpanProcessor {
  processor_class: string;
  exporter_class?: string;
  endpoint?: string;
}
interface OtelStatus {
  provider_class: string;
  otel_endpoint: string | null;
  otel_protocol: string;
  otel_service_name: string;
  otel_debug: boolean;
  trace_viewer: string;
  overlay: Overlay;
  vector: VectorHealth;
  span_processors: SpanProcessor[];
  force_flush_5s: boolean | null;
  force_flush_error?: string;
}

function Pill({ tone, children }: { tone: "ok" | "warn" | "off"; children: React.ReactNode }) {
  const tones = {
    ok: "bg-green-100 text-green-800",
    warn: "bg-amber-100 text-amber-800",
    off: "bg-slate-100 text-slate-600",
  };
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${tones[tone]}`}>
      {children}
    </span>
  );
}

export default function ObservabilityStatusPage() {
  const { token } = useAuth();
  const [status, setStatus] = useState<OtelStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  useEffect(() => {
    if (!token) return;
    apiFetch<OtelStatus>("/admin/otel-status", token)
      .then((s) => {
        setStatus(s);
        setError(null);
      })
      .catch((e) => {
        // K9-05: the pipeline is the deployment's, so a tenant admin is
        // refused (403). That is a state to explain, not an error.
        if (e instanceof ApiError && e.status === 403) setForbidden(true);
        else setError(String(e?.message || e));
      });
  }, [token]);

  if (forbidden) {
    return (
      <div>
        <NavBar />
        <main className="mx-auto max-w-5xl p-6">
          <h1 className="mb-4 text-2xl font-bold">Observability</h1>
          <PlatformOnly reason="Where the deployment's telemetry goes is the deployment's, one pipeline for every tenant, so it is shown to an admin of the platform tenant." />
        </main>
      </div>
    );
  }
  if (error) {
    return (
      <div>
        <NavBar />
        <main className="mx-auto max-w-5xl p-6">
          <h1 className="mb-4 text-2xl font-bold">Observability</h1>
          <p className="rounded border border-red-300 bg-red-50 p-4 text-sm text-red-800">
            Could not read /admin/otel-status: {error}
          </p>
        </main>
      </div>
    );
  }
  if (!status) return null;

  const o = status.overlay;
  // Three states, not two: no overlay is the shipped default and is
  // fine; an unrecognised name is the one that needs shouting about,
  // because Vector and the bridge refuse to start on it.
  const overlayTone = !o.supported ? "warn" : o.active ? "ok" : "off";
  const overlayName = !o.vendor
    ? "None — nothing forwarded to a vendor"
    : `${o.label || o.vendor}${o.supported ? "" : " (unsupported)"}`;

  return (
    <div>
      <NavBar />
      <main className="mx-auto max-w-5xl p-6">
        <h1 className="mb-1 text-2xl font-bold">Observability</h1>
        <p className="mb-6 text-sm text-slate-600">
          Where this deployment&apos;s telemetry goes, and what the backend can
          see of the path from here.
        </p>

        <section className="mb-8">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-600">
            Vendor overlay
          </h2>
          <div className="rounded border bg-white p-4">
            <div className="flex items-center gap-3">
              <span className="text-lg font-semibold">{overlayName}</span>
              <Pill tone={overlayTone}>
                {!o.supported ? "unsupported" : o.active ? "active" : "not selected"}
              </Pill>
            </div>
            <p className="mt-2 text-sm text-slate-700">{o.detail}</p>
            {o.configs.length > 0 && (
              <ul className="mt-3 space-y-0.5 font-mono text-xs text-slate-500">
                {o.configs.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            )}
          </div>
        </section>

        {o.sinks.length > 0 && (
          <section className="mb-8">
            <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-slate-600">
              Sinks
            </h2>
            <p className="mb-3 text-xs text-slate-500">{o.sinks_are}.</p>
            <div className="overflow-hidden rounded border bg-white">
              <table className="w-full text-sm">
                <thead className="bg-slate-50 text-left text-xs uppercase text-slate-600">
                  <tr>
                    <th className="p-2">Sink</th>
                    <th className="p-2">Leg</th>
                    <th className="p-2">Type</th>
                    <th className="p-2">Destination</th>
                  </tr>
                </thead>
                <tbody>
                  {o.sinks.map((s) => (
                    <tr key={s.id} className="border-t">
                      <td className="p-2 font-mono text-xs">{s.id}</td>
                      <td className="p-2">{s.leg}</td>
                      <td className="p-2 font-mono text-xs">{s.type}</td>
                      <td className="p-2 text-slate-700">{s.destination}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        <section className="mb-8">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-600">
            Telemetry router
          </h2>
          <div className="rounded border bg-white p-4">
            <div className="flex items-center gap-3">
              <span className="font-mono text-sm">
                {status.vector.endpoint || "(no OTLP endpoint configured)"}
              </span>
              <Pill
                tone={
                  status.vector.reachable === null
                    ? "off"
                    : status.vector.reachable
                      ? "ok"
                      : "warn"
                }
              >
                {status.vector.reachable === null
                  ? "not exporting"
                  : status.vector.reachable
                    ? "reachable"
                    : "unreachable"}
              </Pill>
            </div>
            <p className="mt-2 text-sm text-slate-700">{status.vector.detail}</p>
            <p className="mt-1 text-xs text-slate-500">Checked: {status.vector.checked}</p>
          </div>
        </section>

        <section>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-600">
            Backend exporter
          </h2>
          <div className="space-y-1 rounded border bg-white p-4 text-sm">
            <div>
              <span className="text-slate-500">service.name: </span>
              <span className="font-mono">{status.otel_service_name}</span>
            </div>
            <div>
              <span className="text-slate-500">protocol: </span>
              <span className="font-mono">{status.otel_protocol}</span>
            </div>
            <div>
              <span className="text-slate-500">provider: </span>
              <span className="font-mono">{status.provider_class}</span>
            </div>
            <div>
              <span className="text-slate-500">trace viewer: </span>
              <span className="font-mono">{status.trace_viewer}</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-slate-500">force_flush (5s):</span>
              <Pill
                tone={
                  status.force_flush_5s === null
                    ? "off"
                    : status.force_flush_5s
                      ? "ok"
                      : "warn"
                }
              >
                {status.force_flush_5s === null
                  ? "n/a"
                  : status.force_flush_5s
                    ? "drained"
                    : "stuck"}
              </Pill>
              {status.force_flush_error && (
                <span className="text-xs text-amber-700">{status.force_flush_error}</span>
              )}
            </div>
            {status.span_processors.length > 0 && (
              <ul className="mt-2 space-y-0.5 font-mono text-xs text-slate-500">
                {status.span_processors.map((p, i) => (
                  <li key={i}>
                    {p.processor_class}
                    {p.exporter_class ? ` → ${p.exporter_class}` : ""}
                    {p.endpoint ? ` (${p.endpoint})` : ""}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}
