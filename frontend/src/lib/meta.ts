"use client";

/**
 * The public deployment facts (blueprint S3): ``GET /meta`` answers before
 * anyone signs in — is this the demo, is the LLM a stub, is a trace viewer
 * configured, which agents are installed. The demo banner and the login
 * hint read it; nothing here needs a token.
 */
import { useEffect, useState } from "react";
import { API_BASE } from "./api";

export interface MetaAgent {
  id: string;
  name: string;
}

export interface Meta {
  name: string;
  version: string;
  /** The licence this LibreRun is under (K blueprint A1, R17): REUSE.toml's default. */
  license: string;
  /**
   * Where the source of the running version can be fetched: the operator's
   * LIBRERUN_SOURCE_URL, or else the public repository at the running
   * version's tag. The Source link opens it (AGPL-3.0 section 13).
   */
  source_url: string;
  demo: boolean;
  /**
   * Whether the platform is in keyless (stub) mode. Sourced from the
   * gateway's own /healthz (blueprint S4a) — ``null`` when the gateway
   * cannot be reached, because "not stubbed" would be a guess.
   */
  stub_llm: boolean | null;
  /** "ok" or "unreachable" — why stub_llm may be null. */
  gateway?: "ok" | "unreachable";
  /** True only in demo mode: the shipped default APP_SECRET_KEY is in use. */
  default_secret: boolean;
  trace_viewer_configured: boolean;
  /** The preset rendering the links ("off" when none). */
  trace_viewer: string;
  /** "env" when the environment configured it, "runtime" when an admin override did. */
  trace_viewer_source: "env" | "runtime";
  agents: MetaAgent[];
}

let cached: Promise<Meta | null> | null = null;

/**
 * Fetch ``/meta`` once per page load. A failed fetch resolves to ``null``
 * and is not cached, so the next caller tries again (the backend may
 * still be starting when the login page first renders).
 */
export function fetchMeta(fetchImpl: typeof fetch = fetch): Promise<Meta | null> {
  if (!cached) {
    const attempt = fetchImpl(`${API_BASE}/meta`)
      .then(async (r) => (r.ok ? ((await r.json()) as Meta) : null))
      .catch(() => null)
      .then((m) => {
        if (m === null && cached === attempt) cached = null;
        return m;
      });
    cached = attempt;
  }
  return cached;
}

/** Tests only: forget the cached answer. */
export function resetMetaCache(): void {
  cached = null;
}

/** ``null`` until the answer arrives (or when the backend is unreachable). */
export function useMeta(): Meta | null {
  const [meta, setMeta] = useState<Meta | null>(null);
  useEffect(() => {
    let alive = true;
    fetchMeta().then((m) => {
      if (alive) setMeta(m);
    });
    return () => {
      alive = false;
    };
  }, []);
  return meta;
}
