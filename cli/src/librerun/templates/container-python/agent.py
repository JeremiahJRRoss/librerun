#!/usr/bin/env python3
"""__AGENT_NAME__ — a Python container agent on the LibreRun agent SDK.

Scaffolded by ``librerun init __AGENT_ID__ --template container-python``.
One async handler; ``librerun_agent.serve`` is the whole transport (Run
Contract v1: ``/healthz``, ``POST /v1/runs``, the events stream, the
output). Edit the handler freely; keep the seams:

* the model is named by STEP (``answer``, declared in agent.yaml), never
  by model name — the admin page picks the model per tenant (D13), and
  the SDK sends this invocation's run token with every call, so nothing
  is configured per call and no provider key is anywhere in this image;
* anything the handler fetches itself goes through ``ctx.pii.redact``
  before a model or the output sees it — intake redacts what the user
  typed, nothing redacts what you fetched;
* a keyless run hands the gateway this agent's own fixture, and the
  output says which of the two answered;
* a key of this agent's own for a service it calls is a tool secret:
  declare its name under ``secrets:`` in agent.yaml and read it with
  ``_tool_secret`` below — never a provider key, and never logged or
  returned.

Prove it:  librerun battery --agent __AGENT_ID__
Standalone:  python agent.py --port 8090
The whole story:  docs/authoring/Container_Agents.md, docs/authoring/SDK.md
"""
from __future__ import annotations

import argparse
import json

from librerun_agent import CapabilityError, LlmError, RunContext, SecretNotSet, serve

# The step this agent calls — declared under llm.steps in agent.yaml.
STEP = "answer"

# What `answer_source` may say. Keyless, the gateway answers from THIS
# agent's own fixture as an ordinary completion, so "a call succeeded" and
# "a model decided" are different questions; the gateway's `librerun`
# envelope names the provider, and the label is read from it.
SOURCE_RULES = "rules"
SOURCE_STUB = "stub-fixture"
SOURCE_MODEL = "model"
SOURCE_UNKNOWN = "unattributed"


def by_the_rules(question: str, context: str) -> str:
    """The deterministic answer: the fallback when no model answers, and
    the keyless fixture handed to the gateway — one function, so the two
    can never drift apart."""
    if context.strip():
        return f"Start from what changed: {context.strip()[:200]}"
    return f"Restate the question and gather one concrete example: {question.strip()[:200]}"


async def _tool_secret(ctx: RunContext, name: str) -> str | None:
    """One of this agent's tool secrets — a key of its own for a service it
    calls: this tenant's value, else every tenant's default, over the MCP
    ``secret_get`` tool. The name must be declared under ``secrets:`` in
    agent.yaml, and this template declares none, so nothing calls this
    until you do. ``None`` is ``SecretNotSet``, a declared name nobody has
    set: read it as "no key" and carry on without the service. A name
    agent.yaml does not declare raises ``SecretNotDeclared`` — a typo,
    not a state. The value is this invocation's alone: never log it, and
    never put it in progress, a log line or the output, which the chassis
    persists."""
    try:
        return await ctx.secrets.get(name)
    except SecretNotSet:
        return None


async def handler(ctx: RunContext) -> dict:
    ctx.progress("running", step=STEP, label="Answering")
    question = str(ctx.input.get("question", ""))      # already redacted by the chassis
    context = str(ctx.input.get("context", ""))
    rules = by_the_rules(question, context)

    # Something this agent "fetched" after intake — a ticket, a document,
    # an API reply would stand here. It never passed through intake, so
    # it goes through the same pipeline (the `pii` grant) before anything
    # downstream sees it. Both the chassis and the container battery
    # advertise the run-scoped MCP server that serves `redact`, so this
    # normally answers; `except CapabilityError` covers every way it can
    # fail — refused for a grant, refused by the walk, the detector
    # unavailable, or the endpoint unreachable — and when it does, the
    # fetched text is not used at all rather than used unredacted.
    try:
        fetched = await ctx.pii.redact(f"Context note for {question[:80]}")
        ctx.log(f"{len(fetched)} chars of fetched context, redacted")
    except CapabilityError as exc:
        ctx.log(f"redaction unavailable ({exc}); the fetched note is not used", level="warning")
        fetched = ""

    answer, source = rules, SOURCE_RULES
    try:
        response = await ctx.llm.complete(
            STEP,                                  # the step id, not a model name
            [
                {"role": "system", "content": "Answer the operator's question in two sentences, using the context."},
                {"role": "user", "content": f"Question: {question}\nContext: {context}\n{fetched}"},
            ],
            # This agent's own keyless fixture: honoured by the gateway only
            # when the resolved provider is the stub, inert otherwise.
            librerun={"stub_reply": rules},
        )
        text = str(((response.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        provider = (response.get("librerun") or {}).get("provider")
        if text:
            answer = text
            if provider is None:
                source = SOURCE_UNKNOWN
            elif str(provider).lower() == "stub":
                source = SOURCE_STUB
            else:
                source = SOURCE_MODEL
    except LlmError as exc:
        # Named, not swallowed: the gateway's own code (`unknown_step`,
        # `llm_not_granted`, `pii_in_identifier`, …) is what an operator
        # needs, and the run carries on with the rule.
        ctx.log(f"no model answered the {STEP} step ({exc.code}); answering by rule", level="warning")
    except Exception as exc:  # noqa: BLE001 — a model is an improvement on the rule, not a dependency of it
        ctx.log(f"no model answered the {STEP} step ({type(exc).__name__}); answering by rule", level="warning")

    ctx.progress("completed", step=STEP)
    # The phase output: a JSON object. The run page renders it
    # (output.mode: structured); the chassis walks it for PII first.
    return {
        "question": question,
        "answer": answer,
        "answer_source": source,
        "phase": ctx.phase,
    }


app = serve(handler, name="__AGENT_ID__")


def main() -> None:
    parser = argparse.ArgumentParser(description="__AGENT_NAME__ (Run Contract v1)")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    from librerun_agent import run

    run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
