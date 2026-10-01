"""__AGENT_NAME__ — a LangGraph graph on LibreRun.

Scaffolded by ``librerun init __AGENT_ID__ --template langgraph``. Two
nodes: ``answer`` asks a model through the platform gateway and falls
back to a rule, ``summarise`` shapes the phase output. Edit the nodes
freely; keep the seams, because they are what the platform's promises
stand on:

* the model is named by STEP (``answer``, declared in agent.yaml), never
  by model name — the admin page picks the model per tenant (D13);
* the capability façade arrives in the run CONFIG (``capabilities_of``),
  never in state, because state is what a checkpointer persists;
* a keyless run hands the gateway this agent's own fixture, and the
  output says which of the two answered — a fixture is never shown as a
  judgement.

Prove it:  librerun battery --agent __AGENT_ID__
The whole story:  docs/authoring/LangGraph.md
"""
from __future__ import annotations

import json
import os
from typing import TypedDict

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from langgraph.graph import END, StateGraph

try:  # installed as a package…
    from librerun_langgraph import LangGraphAgent, capabilities_of
except ImportError:  # …or run from a checkout without the adapter installed
    from adapters.librerun_langgraph import LangGraphAgent, capabilities_of

_HERE = os.path.dirname(__file__)

# The step this graph calls — declared under llm.steps in agent.yaml.
STEP = "answer"

# What `answer_source` may say. Keyless, the gateway answers from THIS
# agent's own fixture as an ordinary completion, so "a call succeeded" and
# "a model decided" are different questions; the gateway's `librerun`
# envelope names the provider, and the label is read from it.
SOURCE_RULES = "rules"
SOURCE_STUB = "stub-fixture"
SOURCE_MODEL = "model"
SOURCE_UNKNOWN = "unattributed"


class State(TypedDict, total=False):
    # Injected by the adapter. Declare every key you want to READ:
    # LangGraph filters state through this schema and silently drops
    # anything undeclared.
    user_inputs: dict          # the intake values, already PII-redacted
    prior_analysis: dict | None
    user_edits: str | None
    run_id: str
    # Produced by the nodes.
    answer: str
    answer_source: str
    structured: dict


ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string", "maxLength": 2000}},
}


def by_the_rules(question: str, context: str) -> dict:
    """The deterministic answer. It serves twice — as the fallback when
    no model answers, and as the keyless fixture handed to the gateway —
    so the two can never drift apart."""
    if context.strip():
        return {"answer": f"Start from what changed: {context.strip()[:200]}"}
    return {"answer": f"Restate the question and gather one concrete example: {question.strip()[:200]}"}


async def answer(state: State, config) -> dict:
    ui = state.get("user_inputs") or {}
    question = str(ui.get("question", ""))
    context = str(ui.get("context", ""))
    rules = by_the_rules(question, context)

    caps = capabilities_of(config)
    # `caps.granted(...)`, never `getattr(caps, "llm", None)`: the façade
    # raises CapabilityNotGranted (a RuntimeError) for an ungranted member,
    # which getattr's default does not catch.
    if caps is None or not caps.granted("llm"):
        return {"answer": rules["answer"], "answer_source": SOURCE_RULES}

    # Anything you fetch yourself — a ticket, a document, an API reply —
    # never passed through intake; run it through the same pipeline
    # before a model or your output sees it:
    #     fetched = await caps.pii.redact(fetched)      # the `pii` grant
    try:
        response = await caps.llm.complete(
            STEP,                                  # the step id, not a model name
            [
                {"role": "system", "content": "Answer the operator's question in two sentences, using the context."},
                {"role": "user", "content": f"Question: {question}\nContext: {context}"},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": STEP, "strict": True, "schema": ANSWER_SCHEMA},
            },
            # This agent's own keyless fixture: honoured by the gateway only
            # when the resolved provider is the stub, inert otherwise.
            librerun={"stub_reply": json.dumps(rules)},
        )
        parsed = json.loads((response["choices"][0]["message"]["content"] or "").strip())
        provider = (response.get("librerun") or {}).get("provider")
    except Exception:  # noqa: BLE001 — a model is an improvement on the rule, not a dependency of it
        # Never widen this to BaseException: the phase deadline cancels the
        # task with CancelledError, and catching that would turn every
        # deadline into a silent fallback.
        return {"answer": rules["answer"], "answer_source": SOURCE_RULES}

    # The chassis's own validator, on the WHOLE reply: `response_format`
    # is a request the gateway forwards, not a guarantee.
    try:
        Draft202012Validator(ANSWER_SCHEMA).validate(parsed)
    except ValidationError:
        return {"answer": rules["answer"], "answer_source": SOURCE_RULES}

    if provider is None:
        source = SOURCE_UNKNOWN
    elif str(provider).lower() == "stub":
        source = SOURCE_STUB
    else:
        source = SOURCE_MODEL
    return {"answer": parsed["answer"], "answer_source": source}


def summarise(state: State) -> dict:
    """The phase output: the adapter reads `structured` from the final
    state and the run page renders it (output.mode: structured)."""
    ui = state.get("user_inputs") or {}
    return {
        "structured": {
            "question": str(ui.get("question", "")),
            "answer": state.get("answer", ""),
            "answer_source": state.get("answer_source", SOURCE_RULES),
        }
    }


def build_graph():
    graph = StateGraph(State)
    graph.add_node("answer", answer)
    graph.add_node("summarise", summarise)
    graph.set_entry_point("answer")
    graph.add_edge("answer", "summarise")
    graph.add_edge("summarise", END)
    return graph.compile()


def input_schema() -> dict:
    with open(os.path.join(_HERE, "input_schema.json"), encoding="utf-8") as handle:
        return json.load(handle)


class __CLASS_NAME__(LangGraphAgent):
    """The whole integration: the graph bound to the manifest's phase.
    Discovery instantiates this with no arguments and checks `agent_id`
    against agent.yaml."""

    def __init__(self) -> None:
        super().__init__(
            agent_id="__AGENT_ID__",
            display_name="__AGENT_NAME__",
            description="A LangGraph graph that answers a question through the platform gateway.",
            input_schema=input_schema(),
            graphs={"answer": build_graph()},
        )
