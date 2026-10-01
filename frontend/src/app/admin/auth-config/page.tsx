"use client";

import { useEffect, useState } from "react";
import NavBar from "../../../components/NavBar";
import ScopeChip from "../../../components/ScopeChip";
import { apiFetch } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { useToast } from "../../../lib/toast";

interface AuthCfg {
  google_enabled: boolean;
  google_allowed_domains: string[];
  google_allowed_emails: string[];
  microsoft_enabled: boolean;
  microsoft_allowed_tenants: string[];
  microsoft_allowed_emails: string[];
  credentials_enabled: boolean;
}

export default function AuthConfigPage() {
  const { token } = useAuth();
  const { toast } = useToast();
  const [cfg, setCfg] = useState<AuthCfg | null>(null);

  useEffect(() => {
    apiFetch<AuthCfg>("/admin/auth-config", token).then(setCfg).catch(() => {});
  }, [token]);

  async function save() {
    if (!cfg) return;
    await apiFetch("/admin/auth-config", token, { method: "PUT", body: JSON.stringify(cfg) });
    toast("Saved", "success");
  }

  if (!cfg) return null;
  return (
    <div>
      <NavBar />
      <main className="mx-auto max-w-3xl p-6">
        {/* K9: this tenant's sign-in methods, set by an admin of this tenant. */}
        <section data-scope-region="auth-config">
        <div className="mb-4 flex items-center gap-2">
          <h1 className="text-2xl font-bold">Authentication Configuration</h1>
          <ScopeChip scope="tenant" />
        </div>
        <div className="space-y-3 rounded border bg-white p-4 text-sm">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={cfg.google_enabled} onChange={(e) => setCfg({...cfg, google_enabled: e.target.checked})} />
            Google SSO enabled
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={cfg.microsoft_enabled} onChange={(e) => setCfg({...cfg, microsoft_enabled: e.target.checked})} />
            Microsoft SSO enabled
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={cfg.credentials_enabled} onChange={(e) => setCfg({...cfg, credentials_enabled: e.target.checked})} />
            Email/password enabled
          </label>
          <label className="block">
            Google allowed domains (comma-sep)
            <input className="mt-1 w-full rounded border px-2 py-1" value={cfg.google_allowed_domains.join(",")} onChange={(e) => setCfg({...cfg, google_allowed_domains: e.target.value.split(",").map(s => s.trim()).filter(Boolean)})} />
          </label>
        </div>
        <button onClick={save} className="mt-4 rounded bg-blue-600 px-4 py-2 text-white">Save</button>
        </section>
      </main>
    </div>
  );
}
