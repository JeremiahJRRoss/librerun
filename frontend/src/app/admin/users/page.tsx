"use client";

import { useEffect, useState } from "react";
import NavBar from "../../../components/NavBar";
import ScopeChip from "../../../components/ScopeChip";
import { apiFetch } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { useToast } from "../../../lib/toast";

interface UserRow {
  id: string;
  email: string;
  display_name?: string | null;
  role: "admin" | "customer";
  auth_provider: string;
  is_active: boolean;
  last_sign_in?: string | null;
}

export default function UsersPage() {
  const { token } = useAuth();
  const { toast } = useToast();
  const [users, setUsers] = useState<UserRow[]>([]);

  async function refresh() {
    const u = await apiFetch<UserRow[]>("/admin/users", token);
    setUsers(u);
  }
  useEffect(() => { refresh().catch(() => {}); }, [token]);

  async function setRole(id: string, role: "admin" | "customer") {
    await apiFetch(`/admin/users/${id}`, token, { method: "PUT", body: JSON.stringify({ role }) });
    await refresh();
    toast("Role updated", "success");
  }
  async function revoke(id: string) {
    await apiFetch(`/admin/users/${id}/revoke`, token, { method: "POST" });
    toast("Sessions revoked", "success");
  }

  return (
    <div>
      <NavBar />
      <main className="mx-auto max-w-5xl p-6">
        {/* K9: this tenant's users, set by an admin of this tenant. */}
        <section data-scope-region="users">
        <div className="mb-4 flex items-center gap-2">
          <h1 className="text-2xl font-bold">Users</h1>
          <ScopeChip scope="tenant" />
        </div>
        <div className="rounded border bg-white">
          <table className="w-full text-sm">
            <thead className="bg-slate-100 text-left">
              <tr>
                <th className="px-2 py-1">Email</th>
                <th className="px-2 py-1">Display</th>
                <th className="px-2 py-1">Role</th>
                <th className="px-2 py-1">Provider</th>
                <th className="px-2 py-1">Last sign in</th>
                <th className="px-2 py-1">Active</th>
                <th className="px-2 py-1"></th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className="border-t">
                  <td className="px-2 py-1">{u.email}</td>
                  <td className="px-2 py-1">{u.display_name ?? ""}</td>
                  <td className="px-2 py-1">
                    <select
                      value={u.role}
                      onChange={(e) => setRole(u.id, e.target.value as "admin" | "customer")}
                      className="rounded border px-1 py-0.5 text-xs"
                    >
                      <option value="customer">customer</option>
                      <option value="admin">admin</option>
                    </select>
                  </td>
                  <td className="px-2 py-1 text-xs">{u.auth_provider}</td>
                  <td className="px-2 py-1 text-xs">{u.last_sign_in ? new Date(u.last_sign_in).toLocaleString() : "—"}</td>
                  <td className="px-2 py-1">{u.is_active ? "✓" : "✗"}</td>
                  <td className="px-2 py-1"><button onClick={() => revoke(u.id)} className="text-xs text-red-600">Revoke</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        </section>
      </main>
    </div>
  );
}
