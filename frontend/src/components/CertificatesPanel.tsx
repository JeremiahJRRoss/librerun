"use client";

/**
 * Certificates, on Application Settings (K blueprint T2; L42, L43, D44).
 *
 * What the HTTPS edge serves, the source in effect and why, and what it
 * needs next, each need with its action (L43). A platform admin loads a CA
 * — at install, or after a restore to bring back the root the browsers
 * already trust — or brings a certificate and key, or chooses ACME, or goes
 * back to the environment's setting, without a shell; beside each choice is
 * the environment line that would set it instead.
 *
 * A key is read from the file the admin picks and sent once, in the body
 * of the change, which the edge hands to `edge-control` alone — never to
 * the backend. The page holds it for that request only: it is never put in
 * state, never rendered, and the file inputs are emptied after every
 * change, whatever the answer (L42). With the edge off (`edge_off`) the
 * panel says so and offers no change: plain HTTP has no edge to change.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE, ApiError, apiFetch, unauthorizedError } from "../lib/api";
import type { TlsNeed, TlsStatus } from "../types";
import ScopeChip from "./ScopeChip";

// Each choice's environment equivalent, as .env.example spells it.
export const ENVIRONMENT_LINES = {
  ca: "LIBRERUN_TLS_CA=/certs/<ca.crt> /certs/<ca.key>",
  files: "LIBRERUN_TLS=/certs/<cert> /certs/<key>",
  acme: "LIBRERUN_TLS=<e-mail>",
  internal: "LIBRERUN_TLS=internal",
} as const;

type Toast = (message: string, kind?: "success" | "error" | "info") => void;

// FileReader rather than File.text(): the same in every browser the UI
// supports, and in the test environment.
function readText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(new Error(`${file.name} could not be read.`));
    reader.readAsText(file);
  });
}

function day(iso: string | null | undefined): string {
  return iso ? new Date(iso).toISOString().slice(0, 10) : "—";
}

// A refusal's own words: edge-control's 422 names the check and never
// carries what was sent, and the backend's answers carry no key either.
function refusal(e: unknown): string {
  if (e instanceof ApiError) {
    const text = e.message.replace(/^API \d+: /, "");
    try {
      const body = JSON.parse(text) as { detail?: unknown };
      if (typeof body.detail === "string") return body.detail;
    } catch {
      // not JSON: the message as it is
    }
    return text || e.message;
  }
  return e instanceof Error ? e.message : "The change failed";
}

export default function CertificatesPanel({ token, toast }: { token: string | null; toast: Toast }) {
  const [status, setStatus] = useState<TlsStatus | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const caCert = useRef<HTMLInputElement>(null);
  const caKey = useRef<HTMLInputElement>(null);
  const siteCert = useRef<HTMLInputElement>(null);
  const siteKey = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await apiFetch<TlsStatus>("/admin/tls", token));
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, [token]);

  useEffect(() => {
    load();
  }, [load]);

  function emptyInputs() {
    for (const input of [caCert, caKey, siteCert, siteKey]) {
      if (input.current) input.current.value = "";
    }
  }

  async function change(label: string, method: "PUT" | "DELETE", path: string, body?: () => Promise<unknown>) {
    setBusy(label);
    try {
      const payload = body ? await body() : undefined;
      await apiFetch<unknown>(path, token, {
        method,
        body: payload === undefined ? undefined : JSON.stringify(payload),
      });
      toast(`${label}: done — the edge serves it now.`, "success");
    } catch (e) {
      toast(`${label}: ${refusal(e)}`, "error");
    } finally {
      emptyInputs();
      setBusy(null);
      await load();
    }
  }

  async function pair(cert: HTMLInputElement | null, key: HTMLInputElement | null) {
    const certFile = cert?.files?.[0];
    const keyFile = key?.files?.[0];
    if (!certFile || !keyFile) throw new Error("Pick both files first.");
    return { certificate: await readText(certFile), key: await readText(keyFile) };
  }

  async function acknowledge() {
    setBusy("acknowledge");
    try {
      setStatus(await apiFetch<TlsStatus>("/admin/tls/acknowledge", token, { method: "POST" }));
      toast("The root is recorded as trusted.", "success");
    } catch (e) {
      toast(refusal(e), "error");
    } finally {
      setBusy(null);
    }
  }

  async function downloadRoot() {
    try {
      const headers: Record<string, string> = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const res = await fetch(`${API_BASE}/admin/tls/root.pem`, { headers });
      if (res.status === 401) throw unauthorizedError(token);
      if (!res.ok) throw new ApiError(`API ${res.status}: ${await res.text().catch(() => "")}`, res.status);
      const url = URL.createObjectURL(await res.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "librerun-edge-root.crt";
      link.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast(refusal(e), "error");
    }
  }

  const needText: Record<TlsNeed, string> = {
    edge_off: "The HTTPS edge is off: this deployment serves plain HTTP.",
    edge_restart:
      "The edge's admin socket answers nothing: restart the edge (docker restart librerun-edge, or podman restart). The previous files are in place.",
    trust_root: `Every machine that browses here trusts this root: fingerprint ${status?.root?.sha256 ?? "—"}.`,
    root_changed:
      "The root changed since it was last acknowledged — a restore starts a new one (L42). Load your CA again below, or trust the new root and acknowledge it.",
    files_ending: `Your certificate ends on ${day(status?.leaf?.not_after)}: upload its replacement below.`,
    ca_ending: `The CA the edge issues from ends on ${day(status?.root?.not_after)}: load its successor below.`,
    acme_requirements: `ACME needs ${status?.site?.[0] ?? "the site's name"} to resolve publicly to this host, and port 443 reaching the edge from outside.`,
  };

  const on = status?.edge === "on";
  const issuer = status?.issuer;
  const serving = !issuer
    ? "unknown"
    : issuer.kind === "internal"
      ? `the edge's own CA (${issuer.ca ?? "local"})`
      : issuer.kind === "acme"
        ? `ACME, as ${issuer.email ?? "—"}`
        : "your certificate and key";

  return (
    <section
      aria-labelledby="certificates"
      data-scope-region="certificates"
      className="mb-6 rounded border bg-white p-4"
    >
      <div className="flex items-center gap-2">
        <h2 id="certificates" className="text-lg font-semibold">
          Certificates
        </h2>
        <ScopeChip scope="platform" />
      </div>
      <p className="mt-1 text-xs text-slate-600">
        What the HTTPS edge serves and what it needs next. Every choice here has an environment line
        that would set it instead; a choice made here wins until &ldquo;Use the environment&apos;s
        setting&rdquo;. A key you load goes to the edge&apos;s control alone, is never shown again,
        and is not in any backup: keep your own copy.
      </p>

      {failed && (
        <p className="mt-3 text-sm text-red-800">
          The certificates could not be read.{" "}
          <button className="underline" onClick={() => load()}>
            Retry
          </button>
        </p>
      )}
      {!status && !failed && <p className="mt-3 text-sm text-slate-500">Reading the edge…</p>}

      {status && !on && (
        <p data-testid="tls-edge-off" className="mt-3 text-sm text-slate-700">
          {needText.edge_off} docs/platform/Install.md, &ldquo;HTTPS at the edge&rdquo;, says how to
          turn it on; there is nothing to change here until it runs.
        </p>
      )}

      {status && on && (
        <>
          <dl data-testid="tls-serving" className="mt-3 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-sm">
            <dt className="text-slate-500">Site</dt>
            <dd>{status.site.join(", ") || "—"}</dd>
            <dt className="text-slate-500">Issued by</dt>
            <dd>{serving}</dd>
            {status.leaf && (
              <>
                <dt className="text-slate-500">Serving</dt>
                <dd className="break-all">
                  {status.leaf.names.join(", ") || "—"}, from {status.leaf.issuer ?? "—"}, until{" "}
                  {day(status.leaf.not_after)} · SHA-256 {status.leaf.sha256}
                </dd>
              </>
            )}
            {status.root && (
              <>
                <dt className="text-slate-500">Root</dt>
                <dd className="break-all">
                  {status.root.subject ?? status.root.name ?? status.root.ca}, until {day(status.root.not_after)} ·
                  SHA-256 {status.root.sha256}
                </dd>
              </>
            )}
          </dl>

          <p data-testid="tls-source" className="mt-3 text-sm text-slate-700">
            <strong>
              {status.source === "ui"
                ? `Chosen on this page${status.choice?.by_email ? ` by ${status.choice.by_email}` : ""}${
                    status.choice?.at ? ` on ${day(status.choice.at)}` : ""
                  }.`
                : `From the environment (${status.environment?.variable ?? "LIBRERUN_TLS"}).`}
            </strong>{" "}
            {status.why}
          </p>

          {status.needs.length > 0 && (
            <ul data-testid="tls-needs" className="mt-3 space-y-2">
              {status.needs.map((need) => (
                <li
                  key={need}
                  data-testid={`tls-need-${need}`}
                  className="rounded border border-amber-300 bg-amber-50 p-2 text-sm text-amber-950"
                >
                  {needText[need]}
                  {need === "trust_root" && (
                    <span className="ml-2 inline-flex gap-2">
                      <button className="underline" onClick={() => downloadRoot()}>
                        Download the root
                      </button>
                      <button
                        className="underline disabled:opacity-50"
                        disabled={busy !== null}
                        onClick={() => acknowledge()}
                      >
                        It is trusted
                      </button>
                      <span className="text-xs">(docs/platform/Install.md, &ldquo;Trust the local CA&rdquo;)</span>
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}

          <div className="mt-4 space-y-4 text-sm">
            <fieldset className="rounded border p-3" disabled={busy !== null}>
              <legend className="px-1 font-medium">Load a CA — at install, or after a restore</legend>
              <label className="block text-xs text-slate-600">
                CA certificate (PEM)
                <input ref={caCert} aria-label="CA certificate" type="file" accept=".crt,.pem" className="block" />
              </label>
              <label className="mt-1 block text-xs text-slate-600">
                Its private key (PEM)
                <input ref={caKey} aria-label="CA private key" type="file" accept=".key,.pem" className="block" />
              </label>
              <button
                className="mt-2 rounded bg-blue-600 px-3 py-1 text-white"
                onClick={() => change("Load the CA", "PUT", "/admin/tls/ca", () => pair(caCert.current, caKey.current))}
              >
                Load the CA
              </button>
              <p className="mt-1 text-xs text-slate-500">
                Or in the environment: <code>{ENVIRONMENT_LINES.ca}</code>
              </p>
            </fieldset>

            <fieldset className="rounded border p-3" disabled={busy !== null}>
              <legend className="px-1 font-medium">A certificate and key of your own</legend>
              <label className="block text-xs text-slate-600">
                Certificate chain (PEM), naming {status.site[0] ?? "the site"}
                <input ref={siteCert} aria-label="Certificate chain" type="file" accept=".crt,.pem" className="block" />
              </label>
              <label className="mt-1 block text-xs text-slate-600">
                Its private key (PEM)
                <input ref={siteKey} aria-label="Certificate private key" type="file" accept=".key,.pem" className="block" />
              </label>
              <button
                className="mt-2 rounded bg-blue-600 px-3 py-1 text-white"
                onClick={() =>
                  change("Use these files", "PUT", "/admin/tls/files", () => pair(siteCert.current, siteKey.current))
                }
              >
                Use these files
              </button>
              <p className="mt-1 text-xs text-slate-500">
                Or in the environment: <code>{ENVIRONMENT_LINES.files}</code>
              </p>
            </fieldset>

            <fieldset className="rounded border p-3" disabled={busy !== null}>
              <legend className="px-1 font-medium">ACME (Let&apos;s Encrypt, then ZeroSSL)</legend>
              <label className="block text-xs text-slate-600">
                Account e-mail
                <input
                  aria-label="ACME e-mail"
                  type="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  className="block rounded border px-2 py-1"
                />
              </label>
              <button
                className="mt-2 rounded bg-blue-600 px-3 py-1 text-white disabled:opacity-50"
                disabled={!email.trim()}
                onClick={() => change("Use ACME", "PUT", "/admin/tls/acme", async () => ({ email: email.trim() }))}
              >
                Use ACME
              </button>
              <p className="mt-1 text-xs text-slate-500">
                Or in the environment: <code>{ENVIRONMENT_LINES.acme}</code>
              </p>
              <p data-testid="tls-acme-warning" className="mt-1 text-xs text-amber-800">
                Until ACME can issue — the name resolving publicly to this host, and port 443 reaching
                the edge — the edge serves no certificate, and this page with it. docs/platform/Install.md,
                &ldquo;Certificates in the admin UI&rdquo;, says how to go back from a shell.
              </p>
            </fieldset>

            {status.source === "ui" && (
              <div className="rounded border p-3">
                <button
                  className="rounded border px-3 py-1 disabled:opacity-50"
                  disabled={busy !== null}
                  onClick={() => change("Use the environment's setting", "DELETE", "/admin/tls/choice")}
                >
                  Use the environment&apos;s setting
                </button>
                <p className="mt-1 text-xs text-slate-500">
                  The environment says <code>{status.environment?.variable ?? "LIBRERUN_TLS"}</code>; with
                  neither set, <code>{ENVIRONMENT_LINES.internal}</code>.
                </p>
              </div>
            )}
          </div>
        </>
      )}
    </section>
  );
}
