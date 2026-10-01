"use client";

/**
 * Model providers, on Application Settings (K7; L33, D16, D19, D34).
 *
 * A platform admin pastes a provider key here. The page reads the gateway's
 * public key, seals the key to it in this browser (`lib/sealing.ts`), and
 * sends the backend a blob the backend cannot open; the gateway adopts it
 * and serves it to the next model call, nothing restarted. The field is a
 * password field, never pre-filled — the server has no key to give — and it
 * is emptied after every Set, Replace or Clear, whatever the server said.
 *
 * Off a secure context (plain HTTP off localhost) WebCrypto is not there, so
 * the page says why and points at gateway.env and the HTTPS edge, and never
 * posts anything (D19). With no public key — the gateway's store key is
 * blank — it says that instead. It shows the fingerprint of the key it seals
 * to, for the operator to compare once with the gateway's boot line (D34).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/api";
import { SealError, canSeal, seal, sealingFingerprint } from "../lib/sealing";
import ScopeChip from "./ScopeChip";

type ProviderName = "openai" | "anthropic" | "google";

export interface ProviderEntry {
  name: ProviderName;
  aliases: string[];
  source: "runtime" | "env" | "unset" | null;
  fingerprint: string | null;
  set_by: string | null;
  set_at: string | null;
  row: "runtime" | "pending" | "rejected" | null;
  reason: "unsealable" | "unopenable" | null;
}

export interface ProvidersStatus {
  reported: boolean;
  stub: boolean | null;
  gateway_version: string | null;
  updated_at: string | null;
  public_key_pem: string | null;
  providers: ProviderEntry[];
}

const POLL_MS = 2000;
const POLL_FOR_MS = 60_000;

const TITLES: Record<ProviderName, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  google: "Google",
};

export function providerChip(entry: ProviderEntry): { text: string; tone: string } {
  if (entry.row === "pending") return { text: "pending", tone: "bg-sky-100 text-sky-900" };
  if (entry.row === "rejected") {
    return { text: `rejected · ${entry.reason ?? "unknown"}`, tone: "bg-amber-100 text-amber-900" };
  }
  if (entry.source === "runtime") {
    return { text: `runtime · ${entry.fingerprint ?? ""}`, tone: "bg-emerald-100 text-emerald-800" };
  }
  if (entry.source === "env") return { text: "from gateway.env", tone: "bg-slate-200 text-slate-700" };
  return { text: "not set", tone: "bg-slate-200 text-slate-700" };
}

export default function ModelProviders({
  token,
  toast,
}: {
  token: string | null;
  toast: (message: string, kind?: "success" | "error" | "info") => void;
}) {
  const [status, setStatus] = useState<ProvidersStatus | null>(null);
  const [failed, setFailed] = useState(false);
  const [fingerprint, setFingerprint] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const secure = canSeal();
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async (): Promise<ProvidersStatus | null> => {
    try {
      const next = await apiFetch<ProvidersStatus>("/admin/providers", token);
      // A backend older than K7, or anything else answering, is a failure
      // to read — never a list to render.
      if (!next || !Array.isArray(next.providers)) throw new Error("not a providers list");
      if (!mounted.current) return next;
      setStatus(next);
      setFailed(false);
      if (secure && next.public_key_pem) {
        setFingerprint(await sealingFingerprint(next.public_key_pem));
      } else {
        setFingerprint(null);
      }
      return next;
    } catch {
      if (mounted.current) setFailed(true);
      return null;
    }
  }, [token, secure]);

  useEffect(() => {
    load();
  }, [load]);

  const clearDraft = (name: string) => setDrafts((prev) => ({ ...prev, [name]: "" }));

  async function waitForGateway(name: ProviderName): Promise<ProviderEntry | undefined> {
    const deadline = Date.now() + POLL_FOR_MS;
    while (Date.now() < deadline && mounted.current) {
      await new Promise((resolve) => setTimeout(resolve, POLL_MS));
      const next = await load();
      const entry = next?.providers.find((e) => e.name === name);
      if (entry && entry.row !== "pending") return entry;
    }
    return undefined;
  }

  async function setKey(entry: ProviderEntry) {
    const value = drafts[entry.name] ?? "";
    setBusy(entry.name);
    try {
      // Re-read the key to seal to: the gateway may have rotated it since the
      // page loaded, and a blob sealed to the old one would be rejected.
      const current = await load();
      const pem = current?.public_key_pem;
      if (!secure || !pem) throw new SealError("There is no key to seal to; see above.");
      const sealed = await seal(pem, entry.name, value);
      clearDraft(entry.name);
      await apiFetch(`/admin/providers/${entry.name}/key`, token, {
        method: "POST",
        body: JSON.stringify({ sealed }),
      });
      toast(`Sent ${TITLES[entry.name]}'s key, sealed; waiting for the gateway…`, "info");
      const settled = await waitForGateway(entry.name);
      if (!settled) toast(`The gateway has not picked up ${TITLES[entry.name]}'s key yet.`, "error");
      else if (settled.row === "rejected") toast(`The gateway rejected ${TITLES[entry.name]}'s key.`, "error");
      else toast(`${TITLES[entry.name]}'s key is in effect.`, "success");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Setting the key failed", "error");
    } finally {
      clearDraft(entry.name);
      setBusy(null);
    }
  }

  async function clearKey(entry: ProviderEntry) {
    if (!confirm(`Clear ${TITLES[entry.name]}'s stored key? gateway.env's applies again, if it has one.`)) return;
    setBusy(entry.name);
    try {
      await apiFetch(`/admin/providers/${entry.name}/key`, token, { method: "DELETE" });
      toast(`Cleared ${TITLES[entry.name]}'s stored key.`, "success");
      await waitForGateway(entry.name);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Clear failed", "error");
    } finally {
      clearDraft(entry.name);
      setBusy(null);
    }
  }

  const canPaste = secure && !!status?.public_key_pem;

  return (
    <section
      aria-labelledby="model-providers"
      data-scope-region="model-providers"
      className="mb-6 rounded border bg-white p-4"
    >
      <div className="flex items-center gap-2">
        <h2 id="model-providers" className="text-lg font-semibold">
          Model providers
        </h2>
        <ScopeChip scope="platform" />
      </div>
      <p className="mt-1 text-xs text-slate-600">
        A key pasted here is sealed in this browser to the gateway&apos;s public key and held by the
        gateway alone; the backend stores what it cannot open. A key set here wins over gateway.env&apos;s
        for its provider, and Clear returns that provider to gateway.env.
      </p>

      {failed && (
        <p className="mt-3 text-sm text-red-800">
          The providers could not be read.{" "}
          <button className="underline" onClick={() => load()}>
            Retry
          </button>
        </p>
      )}

      {status && !status.reported && (
        <p data-testid="providers-unreported" className="mt-3 text-sm text-slate-700">
          The gateway has not reported what it holds yet. It writes that when it starts; if it is
          running, give it a moment and reload.
        </p>
      )}

      {status && !secure && (
        <div
          data-testid="providers-refusal"
          className="mt-3 rounded border border-amber-400 bg-amber-50 p-3 text-sm text-amber-900"
        >
          <strong>This page cannot seal a key here.</strong> It is served over plain HTTP from{" "}
          <span className="font-mono">{typeof window !== "undefined" ? window.location.origin : ""}</span>, which
          is not a secure context, so the browser offers no WebCrypto to seal with — and a key is never sent
          unsealed. Put provider keys in gateway.env, which works everywhere, or open this page over HTTPS
          (docs/platform/Install.md, &ldquo;HTTPS at the edge&rdquo;) or on localhost.
        </div>
      )}

      {status && secure && status.reported && !status.public_key_pem && (
        <div
          data-testid="providers-no-key"
          className="mt-3 rounded border border-amber-400 bg-amber-50 p-3 text-sm text-amber-900"
        >
          <strong>The gateway has no key to seal to.</strong> Its store key, LIBRERUN_GATEWAY_SECRETS_KEY in
          gateway.env, is blank, so a provider key cannot be kept here. gateway.env&apos;s provider keys serve
          meanwhile; docs/platform/Install.md, &ldquo;The gateway&apos;s store key&rdquo;, says how to add one.
        </div>
      )}

      {status?.stub && (
        <p data-testid="providers-keyless" className="mt-3 text-xs text-slate-600">
          Keyless mode is on (LIBRERUN_STUB_LLM): every step answers from fixtures, so a key set here waits,
          unused, until keyless mode is turned off.
        </p>
      )}

      {canPaste && fingerprint && (
        <p className="mt-3 text-xs text-slate-600">
          Seals to{" "}
          <span data-testid="providers-fingerprint" className="break-all font-mono">
            {fingerprint}
          </span>{" "}
          — the same fingerprint the gateway logged at boot (gateway_sealing_key). Compare them once.
        </p>
      )}

      {status && (
        <ul className="mt-3 divide-y">
          {status.providers.map((entry) => {
            const chip = providerChip(entry);
            const draft = drafts[entry.name] ?? "";
            const stored = entry.row !== null;
            return (
              <li key={entry.name} className="py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="font-semibold">{TITLES[entry.name]}</h3>
                  <span
                    data-testid={`provider-chip-${entry.name}`}
                    className={`rounded px-1.5 py-0.5 text-xs ${chip.tone}`}
                  >
                    {chip.text}
                  </span>
                  {entry.aliases.length > 0 && (
                    <span className="text-xs text-slate-500">serves: {entry.aliases.join(", ")}</span>
                  )}
                </div>
                {entry.row === "rejected" && (
                  <p className="mt-1 text-xs text-amber-900">
                    {entry.reason === "unopenable"
                      ? "The stored key does not open under the gateway's store key (a lost or rotated key)."
                      : "The stored key does not open under the gateway's sealing key (sealed to an older one)."}{" "}
                    gateway.env serves meanwhile; Replace or Clear it.
                  </p>
                )}
                {entry.name === "google" && (
                  <p className="mt-1 text-xs text-slate-500">
                    A Vertex service-account JSON is too long to paste: put it in gateway.env as
                    GOOGLE_AI_API_KEY_FILE.
                  </p>
                )}
                {canPaste && (
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <input
                      type="password"
                      autoComplete="new-password"
                      aria-label={`New ${TITLES[entry.name]} key`}
                      data-testid={`provider-input-${entry.name}`}
                      value={draft}
                      onChange={(e) => setDrafts((prev) => ({ ...prev, [entry.name]: e.target.value }))}
                      placeholder={stored ? "A new key, to replace it" : "The key, to set it"}
                      className="w-72 rounded border px-2 py-1 text-sm"
                    />
                    <button
                      data-testid={`provider-set-${entry.name}`}
                      onClick={() => setKey(entry)}
                      disabled={busy === entry.name || !draft.trim()}
                      className="rounded bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-50"
                    >
                      {busy === entry.name ? "Working…" : stored ? "Replace" : "Set"}
                    </button>
                    {stored && (
                      <button
                        data-testid={`provider-clear-${entry.name}`}
                        onClick={() => clearKey(entry)}
                        disabled={busy === entry.name}
                        className="rounded border px-3 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                      >
                        Clear
                      </button>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
