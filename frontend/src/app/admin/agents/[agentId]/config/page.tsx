"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect } from "react";
import AgentPageTabs from "../../../../../components/agentPage/AgentPageTabs";
import NavBar from "../../../../../components/NavBar";
import { useAuth } from "../../../../../lib/auth";

export default function AgentConfigPage() {
  const { token, user } = useAuth();
  const router = useRouter();
  const params = useParams<{ agentId: string }>();
  const agentId = params.agentId;

  useEffect(() => {
    if (!token) router.push("/login");
    else if (user && user.role !== "admin") router.push("/dashboard");
  }, [token, user, router]);

  if (!token || !user) return null;

  return (
    <div>
      <NavBar />
      <main className="mx-auto max-w-5xl p-6">
        <h1 className="mb-1 text-2xl font-bold">Agent configuration</h1>
        <p className="mb-6 font-mono text-sm text-slate-500">{agentId}</p>
        {/* K4b: a tab shell for every registered agent (D30). */}
        <AgentPageTabs agentId={agentId} />
      </main>
    </div>
  );
}
