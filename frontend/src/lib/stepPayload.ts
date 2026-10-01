import type { AgentStepConfig } from "../types";

/**
 * What the admin page PUTs to `/agents/{id}/config/steps` (blueprint S4a).
 *
 * Two rules, both of which the page got wrong and Codex found.
 *
 * **An emptied input is a clear, and a clear has to be SENT.** A field
 * left out of the payload leaves whatever row is already stored, so
 * omitting empties — which is what this did — made an override
 * impossible to remove from the page: clearing the box and saving
 * changed nothing, and the old number came back on the next load. Null
 * is the clear.
 *
 * **Sending a default is not the same as claiming it.** The page renders
 * the *effective* step — the agent's defaults and this tenant's choices
 * in the same inputs — and has no way to tell which is which, so it
 * posts everything and the SERVER decides what counts as an override. A
 * value equal to the manifest's default is stored as nothing, which is
 * why the page re-reads the config after a save rather than trusting the
 * row it just sent.
 *
 * **Every step is sent.** `llm.steps[]` contains only LLM steps — a
 * non-LLM pipeline stage is not in that list at all — so a step with no
 * provider is not "not an LLM step", it is one whose manifest leaves the
 * choice to the tenant's admin. Filtering those out dropped them from
 * the payload, so the agent the feature exists for could never be
 * configured and every invocation stayed `step_not_configured`, with the
 * platform's own error pointing at this page.
 */
export function stepUpdates(
  steps: AgentStepConfig[],
  editable: string[],
): Record<string, unknown>[] {
  return steps.map((s) => {
    const out: Record<string, unknown> = {
      step_id: s.step_id,
      provider: blankToNull(s.provider),
      model: blankToNull(s.model),
    };
    for (const key of editable) {
      out[key] = blankToNull((s as unknown as Record<string, unknown>)[key]);
    }
    return out;
  });
}

function blankToNull(value: unknown): unknown {
  return value === "" || value === undefined ? null : value;
}
