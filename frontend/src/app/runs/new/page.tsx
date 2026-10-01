"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import AgentCards from "../../../components/AgentCards";
import DynamicForm from "../../../components/DynamicForm";
import NavBar from "../../../components/NavBar";
import { apiFetch, apiUpload } from "../../../lib/api";
import { useAuth } from "../../../lib/auth";
import { recordNavigationIntent } from "../../../lib/telemetry/facade";
import { useToast } from "../../../lib/toast";
import type {
  AgentInfo,
  AgentScenario,
  JsonSchema,
  PiiRedactionPreview,
  RunDetail,
} from "../../../types";

// Intake is schema-driven for every agent (blueprint B8): the form —
// stepped wizard or single page — renders from the agent's input schema
// and its manifest's ui.intake.steps. The agent is chosen from cards
// (blueprint S7, D3): name, description, framework badge and a "Try a
// sample" chip per scenario; a chip fills the form and opens it on the
// Review step, so the next click is Submit.
//
// `?agent=<id>&from=<run id>` is how "Run again" lands here from a run
// that ended in error: the card is preselected and the form starts from
// that run's stored (redacted) inputs.

export default function NewRunPage() {
  // useSearchParams opts a route into client rendering, and Next requires
  // the boundary to be explicit or `next build` fails on this page.
  return (
    <Suspense fallback={null}>
      <NewRunForm />
    </Suspense>
  );
}

function NewRunForm() {
  const { token } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const { toast } = useToast();
  const wantedAgent = searchParams.get("agent");
  const fromRun = searchParams.get("from");
  const [agent, setAgent] = useState<AgentInfo | null>(null);
  const [agentSchema, setAgentSchema] = useState<JsonSchema | null>(null);
  const [agentSchemaErr, setAgentSchemaErr] = useState<string | null>(null);
  // Prefill payload — from a sample chip or from the run being re-run.
  // ``loadCount`` keys the form so a load remounts it with the new values.
  const [formInitial, setFormInitial] = useState<Record<string, unknown> | null>(null);
  const [startAtReview, setStartAtReview] = useState(false);
  const [loadCount, setLoadCount] = useState(0);
  const [preselect, setPreselect] = useState<string | null>(wantedAgent);
  const [submitting, setSubmitting] = useState(false);

  const agentId = agent?.agent_id ?? null;

  useEffect(() => {
    if (!token) router.push("/login");
  }, [token, router]);

  // "Run again": the source run's inputs become the form's initial
  // values, and its agent the card to select.
  useEffect(() => {
    if (!token || !fromRun) return;
    let alive = true;
    apiFetch<RunDetail>(`/runs/${fromRun}`, token)
      .then((run) => {
        if (!alive) return;
        if (run.user_inputs) {
          setFormInitial(run.user_inputs);
          setLoadCount((n) => n + 1);
        }
        if (run.agent_id) setPreselect(run.agent_id);
        toast(`Starting again from ${run.run_number}`, "info");
      })
      .catch(() => {
        if (alive) toast("Could not load the earlier run's inputs", "error");
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, fromRun]);

  // Fetch the agent's JSON Schema as soon as one is selected.
  useEffect(() => {
    setAgentSchema(null);
    setAgentSchemaErr(null);
    if (!agentId || !token) return;
    let alive = true;
    apiFetch<JsonSchema>(`/agents/${agentId}/input-schema`, token)
      .then((s) => {
        if (alive) setAgentSchema(s);
      })
      .catch((e) => {
        if (alive) setAgentSchemaErr(e.message ?? "Failed to load schema");
      });
    return () => {
      alive = false;
    };
  }, [agentId, token]);

  function selectAgent(next: AgentInfo) {
    if (next.agent_id === agentId) return;
    setAgent(next);
    // A different agent, a different form: nothing loaded carries over
    // (a sample's inputs are one agent's shape) — except the inputs of
    // the run being re-run, which belong to the agent being preselected.
    if (!(fromRun && preselect === next.agent_id)) {
      setFormInitial(null);
      setStartAtReview(false);
    }
    setLoadCount((n) => n + 1);
  }

  function loadSample(next: AgentInfo, sample: AgentScenario) {
    setAgent(next);
    setFormInitial(sample.user_inputs);
    setStartAtReview(true);
    setLoadCount((n) => n + 1);
    toast(`Sample loaded — review and submit`, "success");
  }

  async function piiPreview(
    file: File,
    kind: string,
  ): Promise<PiiRedactionPreview | null> {
    if (!token) return null;
    const form = new FormData();
    form.append("file", file);
    form.append("file_type", kind);
    try {
      const preview = await apiUpload<PiiRedactionPreview>(
        "/files/redact-preview",
        token,
        form,
      );
      toast(`${preview.redactions_applied.length} PII items redacted`, "success");
      return preview;
    } catch {
      toast("Redaction preview failed", "error");
      return null;
    }
  }

  async function submit(values: Record<string, unknown>) {
    if (!token || !agentId) return;
    setSubmitting(true);
    try {
      const res = await apiFetch<{ run_id: string }>(
        `/runs?agent_id=${encodeURIComponent(agentId)}`,
        token,
        { method: "POST", body: JSON.stringify(values) },
      );
      toast("Run submitted", "success");
      recordNavigationIntent("push");
      router.push(`/runs/${res.run_id}`);
    } catch (e: any) {
      toast(e.message || "Submit failed", "error");
    } finally {
      setSubmitting(false);
    }
  }

  if (!token) return null;

  return (
    <div>
      <NavBar />
      <main className="mx-auto max-w-4xl p-6">
        <h1 className="mb-4 text-2xl font-bold">New Run</h1>

        <section className="mb-6 rounded border bg-white p-4">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-slate-600">
            Choose an agent
          </h2>
          <AgentCards
            token={token}
            selected={agentId}
            preselect={preselect}
            onSelect={selectAgent}
            onSample={loadSample}
          />
        </section>

        {agent && (
          <section data-testid="intake-form">
            {agentSchemaErr && (
              <div className="mb-3 rounded border border-red-300 bg-red-50 p-3 text-sm text-red-800">
                {agentSchemaErr}
              </div>
            )}
            {!agentSchema && !agentSchemaErr && (
              <div className="rounded border bg-white p-6 text-sm text-slate-500">
                Loading form…
              </div>
            )}
            {agentSchema && (
              <DynamicForm
                key={`${agent.agent_id}:${loadCount}`}
                schema={agentSchema}
                initial={formInitial ?? undefined}
                steps={agent.ui.intake.steps}
                startAtReview={startAtReview}
                piiPreview={piiPreview}
                submitLabel="Submit Run"
                submitting={submitting}
                onSubmit={submit}
              />
            )}
          </section>
        )}
      </main>
    </div>
  );
}
