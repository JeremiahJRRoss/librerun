"""Example agent: a LangGraph graph running on LibreRun (blueprint B16).

Deliberately boring on purpose. The point is to show the *seams* — a
compiled graph becoming a manifest agent, and a graph node reaching a
model through the platform rather than around it — not to show off a
clever graph.

Two nodes call a model (`classify` and `draft`, the ids the manifest
declares under `llm.steps`); the rest are deterministic. Each of those
two is written the way an example should be:

* it computes a **rule-based answer first**, and that one answer serves
  twice — as the fallback if the call fails, and as the keyless fixture
  it hands the gateway as `librerun.stub_reply`. The gateway honours a
  supplied fixture only when the resolved provider is the stub, so it is
  inert the moment a real key is configured (L13: an agent's fixtures
  live in the agent, not in the platform);
* it says in the output **which source answered**, so a fixture is never
  displayed as a judgement.

So this example still runs in CI and in the demo with no provider key
anywhere, while producing the same costed LLM spans, the same outbound
redaction and the same per-step admin configuration (D13) a credentialled
run produces.

Two phases, mapped to the two graphs below, so the example exercises the
multi-phase path and the human approval gate rather than only the
single-graph case:

    analyze      -> triage_graph      (classify, then summarise)
    investigate  -> plan_graph        (gather context, then draft a plan)

The two graphs' names are the phases; the two model calls' names are the
`llm.steps` ids. They are deliberately not the same vocabulary: a phase
is a unit of human review, a step is a unit of model configuration.

Run it next to VITA::

    LIBRERUN_AGENTS_PATH=agents/_examples uvicorn app.main:app --reload
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, TypedDict

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from langgraph.graph import END, StateGraph

try:  # installed as a package…
    from librerun_langgraph import LangGraphAgent, capabilities_of
except ImportError:  # …or run from this checkout
    from adapters.librerun_langgraph import LangGraphAgent, capabilities_of

_HERE = os.path.dirname(__file__)

SEVERITY_MARKERS = {
    "critical": ("outage", "data loss", "breach", "cannot log in", "down"),
    "high": ("failing", "error", "timeout", "rejected", "500"),
    "medium": ("slow", "intermittent", "warning", "retry"),
}


class TriageState(TypedDict, total=False):
    # Injected by the adapter. All JSON data, deliberately: state is what
    # a checkpointer persists, so the capability façade is NOT here — it
    # arrives in the run config (see gather_context).
    # Declare every one you want to READ: LangGraph filters state
    # through this schema and silently drops anything undeclared, so an
    # omitted key is not a smaller graph, it is a node that cannot see
    # what the adapter sent.
    user_inputs: dict
    prior_analysis: dict | None
    user_edits: str | None
    run_id: str
    probed: list[str]
    # Produced by the nodes
    severity: str
    signals: list[str]
    rationale: str
    severity_source: str
    summary: str
    context: list[str]
    structured: dict


_LOG = logging.getLogger("agents.langgraph_triage")


def probe(state: TriageState) -> dict:
    """Emit, on request, the things the platform's guarantees are about.

    An in-process agent shares the backend's process: its spans go
    through the backend's span processors, its ``logging`` calls through
    the queue, and its ``print()`` and ``sys.stderr`` writes through the
    descriptor capture. Whether *that* holds is not something a test of
    the chassis alone can answer, because the chassis is not the thing
    being doubted — the agent is. So the example can be asked to behave
    like the agent you worry about, and the acceptance drives it.

    Nothing here changes the triage. Every switch is off by default, and
    the node is a no-op without a ``probe`` object in the input.

    ``attach_handler`` is the negative one: attaching a log handler
    inside an invocation must raise ``RuntimeError``, because a handler
    the agent owns is a way around the redacting queue. The refusal is
    recorded rather than swallowed, so a chassis that stopped refusing
    shows up as a missing entry instead of as silence.
    """
    switches = (state.get("user_inputs") or {}).get("probe") or {}
    if not switches:
        return {}
    text = str(switches.get("text") or "")
    done: list[str] = []

    if switches.get("span_attribute"):
        try:
            from opentelemetry import trace

            span = trace.get_current_span()
            span.set_attribute("agent.probe.text", text)
            span.add_event("probe", {"text": text})
            done.append("span_attribute")
        except ImportError:
            done.append("span_attribute:unavailable")
    if switches.get("log"):
        _LOG.info("probe log line: %s", text)
        done.append("log")
    if switches.get("print"):
        print(f"probe print: {text}")
        done.append("print")
    if switches.get("stderr"):
        sys.stderr.write(f"probe stderr: {text}\n")
        done.append("stderr")
    if switches.get("exception"):
        try:
            raise ValueError(f"probe exception: {text}")
        except ValueError:
            _LOG.exception("probe exception line: %s", text)
        done.append("exception")
    if switches.get("attach_handler"):
        stray = logging.StreamHandler(sys.stderr)
        try:
            _LOG.addHandler(stray)
        except RuntimeError:
            done.append("attach_handler:refused")
        else:
            _LOG.removeHandler(stray)
            done.append("attach_handler:allowed")

    return {"probed": done}


def _text(state: TriageState) -> str:
    ui = state.get("user_inputs") or {}
    return " ".join(
        str(ui.get(k, "")) for k in ("title", "description", "service", "logs")
    ).lower()


def _by_the_rules(state: TriageState) -> dict:
    """Severity from the words actually present in the report.

    Kept as a first-class answer rather than a stand-in: it is what the
    node falls back to when the model call fails, and it is the fixture
    the node hands the gateway for a keyless run. One function, so the
    two can never drift apart.
    """
    blob = _text(state)
    for severity, markers in SEVERITY_MARKERS.items():
        hits = [m for m in markers if m in blob]
        if hits:
            return {
                "severity": severity,
                "signals": hits,
                "rationale": f"matched {', '.join(hits)}",
            }
    return {
        "severity": "low",
        "signals": [],
        "rationale": "no severity markers found in the report",
    }


CLASSIFY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["severity", "signals", "rationale"],
    "properties": {
        "severity": {"type": "string", "enum": list(SEVERITY_MARKERS) + ["low"]},
        "signals": {
            "type": "array",
            "maxItems": 6,
            "items": {"type": "string", "maxLength": 80},
        },
        "rationale": {"type": "string", "maxLength": 300},
    },
}

DRAFT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["next_steps"],
    "properties": {
        "next_steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {"type": "string", "maxLength": 200},
        }
    },
}


async def _ask_the_model(config, step: str, prompt: str, schema: dict, fixture: dict):
    """One model call, or ``None`` when the platform could not make it.

    This is the whole integration an agent author has to copy, and every
    line of it is load-bearing:

    * ``caps.granted("llm")`` rather than ``getattr(caps, "llm", None)``.
      The façade raises ``CapabilityNotGranted`` — a ``RuntimeError`` —
      for a member the manifest does not grant, and ``getattr``'s default
      only swallows ``AttributeError``, so the familiar spelling does not
      degrade, it detonates. ``granted`` is the predicate the façade
      exposes for exactly this question.
    * the **step id**, not a model name. Which model answers, at what
      temperature and within what budget is the tenant's configuration,
      resolved by the gateway per request (L25, D13). An agent that names
      a model has taken that decision away from the operator.
    * ``response_format``, so the reply is JSON this node can read rather
      than prose it would have to parse.
    * ``librerun.stub_reply``, the agent's own keyless fixture. The
      gateway honours it **only** when the resolved provider is the stub,
      so it cannot make a credentialled deployment answer for a provider
      that was never called. Without it the stub would synthesise an
      instance of the schema above — valid, deterministic, and always the
      first enum member, which would report every keyless incident as
      ``critical``.

    Returns ``(answer, provider)``. The provider is the second half of
    the reply and it is not optional bookkeeping: keyless, the gateway
    resolves every step to the ``stub`` provider and hands back THIS
    AGENT'S OWN FIXTURE as a perfectly ordinary successful completion.
    A caller that looks only at the content cannot tell that from a
    model's answer, and will label a fixture as a judgement — which is
    the one way this whole design can mislead (Codex P2). The gateway
    says who answered in its ``librerun`` envelope, so the honest label
    is a fact of the reply rather than a guess about the deployment.

    A failure returns ``(None, None)`` instead of raising: a model is an
    improvement on the rule here, not a dependency of it, and an example
    that dies because a gateway is down teaches the wrong lesson.

    The obvious objection to so broad an ``except`` is that it would also
    swallow the phase deadline, and it does not. The runner enforces
    ``phases[].deadline_seconds`` with ``asyncio.timeout``, which works
    by CANCELLING the task; ``CancelledError`` is a ``BaseException``, so
    this clause never sees it. Measured, rather than reasoned about, on a
    real socket with a real client: httpx propagates the cancellation
    untouched instead of translating it into an ``HTTPError`` the way it
    does a timeout, so the clause catches nothing and the phase fails
    with its deadline named. What must not happen is this clause WIDENING
    to ``BaseException`` — that would turn every deadline into a silent
    fall back to the keyword rule, and
    ``test_a_cancelled_phase_is_not_swallowed_as_a_failed_call`` fails if
    either of this module's two broad clauses does.
    """
    caps = capabilities_of(config)
    if caps is None or not caps.granted("llm"):
        return None, None
    try:
        response = await caps.llm.complete(
            step,
            [
                {
                    "role": "system",
                    "content": (
                        "You are triaging a production incident for an "
                        "on-call responder. Answer only with the JSON the "
                        "schema describes."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": step, "strict": True, "schema": schema},
            },
            librerun={"stub_reply": json.dumps(fixture)},
        )
        content = (response["choices"][0]["message"]["content"] or "").strip()
        parsed = json.loads(content)
        # The PROVIDER THAT ANSWERED, as the gateway resolved it — the
        # admin's own word, `stub` when the platform is keyless. Absent
        # only from a gateway that did not send the envelope.
        provider = ((response.get("librerun") or {}).get("provider")) or None
    except Exception as exc:  # noqa: BLE001 — see the docstring
        # The type only. The message can carry whatever the provider put
        # in it, and deciding how to store that is the platform's job,
        # not this example's.
        _LOG.info("llm_step_unavailable", extra={"step": step, "why": type(exc).__name__})
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    return parsed, provider


# What a `*_source` field may say. Four values because four different
# things happen, and collapsing any two of them hides something a reader
# of the run page would want to know.
def _valid(answer, schema: dict) -> bool:
    """Does the reply match the schema the node asked for?

    `response_format` is a REQUEST the gateway forwards, not a guarantee
    it can enforce — and D13 exists precisely so an admin can retarget a
    step to another provider, which may honour structured output loosely
    or not at all. So the shape is checked here, before the node relies
    on it, and a reply that does not conform falls back to the rule
    rather than failing the phase.

    THE CHASSIS'S OWN VALIDATOR, not a hand-rolled one. `intake.py` has
    used `Draft202012Validator` to check agent input against its declared
    schema since B8, and `jsonschema` is a pinned direct dependency — so
    an in-process agent, which shares the backend's process and therefore
    its dependencies, has it for free.

    This was forty lines of hand-written checking, and three review
    rounds each found another keyword it silently ignored: first
    `maxItems` and `maxLength`, and before that the difference between
    checking one field and checking the answer. The argument for keeping
    it — "an example should stay dependency-light" — was never true:
    the library was already installed and already the chassis's way of
    doing exactly this. An example is copied, so the practice it shows
    has to be the one an author should follow.

    A CONTAINER agent does not share this process: it declares
    `jsonschema` in its own image, or validates another way. That is the
    only thing that changes across the runtime boundary here.
    """
    try:
        Draft202012Validator(schema).validate(answer)
    except ValidationError:
        return False
    return True


SOURCE_RULES = "rules"            # no usable answer: ungranted, unreachable, unparseable
SOURCE_STUB = "stub-fixture"      # keyless: the gateway replayed this agent's own fixture
SOURCE_MODEL = "model"            # a provider answered
SOURCE_UNKNOWN = "unattributed"   # something answered and the gateway did not say who

STUB_PROVIDER = "stub"


def _source(provider: str | None, *, used: bool) -> str:
    """Who decided, named honestly.

    `unattributed` rather than `model` is the deliberate choice for a
    reply with no provider in it: defaulting to `model` is how a future
    gateway that stopped sending the envelope would silently reintroduce
    the fixture-as-judgement bug, and a label that cannot be wrong by
    omission is worth one extra word.
    """
    if not used:
        return SOURCE_RULES
    if provider is None:
        return SOURCE_UNKNOWN
    return SOURCE_STUB if provider.lower() == STUB_PROVIDER else SOURCE_MODEL


async def classify(state: TriageState, config) -> dict:
    """Assign a severity, asking a model and falling back to the rule.

    A fallback, deliberately NOT a floor. `_valid` checks the reply's
    SHAPE, never its quality, so a model that answers replaces the rule
    outright — in either direction. That is the design and not an
    oversight: the `degraded-search` scenario exists because the keyword
    rule reads that report as `high` (it matches "errors" inside "nothing
    errors"), and the whole point is that a model should correct it DOWN
    to `medium`. Clamping the model to the rule's severity would make the
    example's clearest argument for calling a model impossible to
    demonstrate.

    Also where ``probe`` runs, when the input asks for it. Deliberately
    not its own node: a node is a progress step the run page shows, and
    a diagnostic affordance has no business appearing in a user's
    timeline or changing the graph an author is reading as the example.
    """
    rules = _by_the_rules(state)
    ui = state.get("user_inputs") or {}
    prompt = "\n".join(
        f"{field}: {ui.get(field, '')}"
        for field in ("title", "service", "description", "logs")
    )
    answer, provider = await _ask_the_model(
        config, "classify", prompt, CLASSIFY_SCHEMA, rules
    )

    out = dict(rules)
    # The WHOLE answer, not just the field the branch reads.
    used = _valid(answer, CLASSIFY_SCHEMA)
    if used:
        out = {
            "severity": answer["severity"],
            "signals": list(answer["signals"]),
            "rationale": answer["rationale"],
        }
    return {
        "severity": out["severity"],
        "signals": out["signals"],
        "rationale": out["rationale"],
        # WHO answered, carried into the output. Keyless the reply IS
        # this agent's own fixture, returned as an ordinary successful
        # completion — so "a call succeeded" is NOT the same question as
        # "a model decided", and only the provider distinguishes them.
        "severity_source": _source(provider, used=used),
        **probe(state),
    }


def summarise(state: TriageState) -> dict:
    ui = state.get("user_inputs") or {}
    severity = state.get("severity", "low")
    signals = state.get("signals") or []
    summary = (
        f"{ui.get('service', 'unknown service')}: {ui.get('title', 'untitled report')} "
        f"— triaged {severity}"
        + (f" on {', '.join(signals)}" if signals else " with no severity markers found")
    )
    structured = {
        "severity": severity,
        "signals": signals,
        "summary": summary,
        "service": ui.get("service", ""),
        "rationale": state.get("rationale", ""),
        # One of the FOUR values `_source()` can return — `rules`,
        # `stub-fixture`, `model`, `unattributed` — never a yes/no about
        # whether a model was reached. Keyless the reply IS this agent's
        # own fixture, returned as an ordinary successful completion, so
        # collapsing `stub-fixture` into `model` is exactly the defect
        # this field exists to prevent, and collapsing `unattributed`
        # into it hides a gateway that stopped naming the provider.
        # `_source` is the single definition; this comment names the
        # values rather than redefining them.
        "severity_source": state.get("severity_source", SOURCE_RULES),
    }
    # What the probe node actually managed to do — names only, never the
    # text it was given, which the platform is the one allowed to decide
    # how to store.
    if state.get("probed"):
        structured["probed"] = list(state["probed"])
    return {"summary": summary, "structured": structured}


async def gather_context(state: TriageState, config) -> dict:
    """Use the granted knowledge-base capability, if the manifest grants one.

    Shows how a graph node reaches platform capabilities: through the
    per-run façade in the run CONFIG, never by importing chassis
    internals. Note the second parameter — LangGraph passes the config to
    any node that declares it, and the façade lives there rather than in
    state because a checkpointer would try to persist state and this
    object is both unserializable and scoped to one run. Degrades to a
    stated absence rather than failing — an example that only works with
    a configured vector store would teach the wrong lesson.
    """
    caps = capabilities_of(config)
    # `caps.granted("kb")`, NOT `getattr(caps, "kb", None)`: the façade
    # raises `CapabilityNotGranted`, a RuntimeError, for an ungranted
    # member, and `getattr`'s default only catches `AttributeError`. The
    # familiar spelling was here and could never have degraded — it would
    # have killed the phase while this docstring promised otherwise.
    if caps is None or not caps.granted("kb"):
        return {"context": ["no knowledge-base capability granted — skipped"]}
    kb = caps.kb
    try:
        if not kb.available():
            return {"context": ["knowledge base granted but not configured"]}
        # The summary comes from the PREVIOUS phase, so read it from
        # prior_analysis the way `draft` does. `summary` is a channel
        # of the analyze graph; this node runs in the investigate graph,
        # which never has one — reading state here searched the empty
        # string and made the capability lookup meaningless. Own state
        # second, so a single-graph agent reusing this node still works.
        query = (state.get("prior_analysis") or {}).get("summary") or state.get(
            "summary", ""
        )
        hits = await kb.search(queries=[query], top_k=3)
        return {"context": [str(h)[:200] for h in (hits or [])] or ["no matches"]}
    except Exception as exc:  # a capability failure must not kill the run
        return {"context": [f"knowledge-base lookup failed: {type(exc).__name__}"]}


def _checklist(severity: str, service: str) -> list[str]:
    """The responder's steps, by rule. Fallback and keyless fixture both,
    for the reason ``_by_the_rules`` gives."""
    return [
        f"Acknowledge the report and page the {service or 'owning'} team"
        if severity in ("critical", "high")
        else "Queue for the next working day",
        "Confirm scope: how many users, since when, which region",
        "Check recent deploys and config changes in the affected window",
        "Capture one failing request end to end, with its trace id",
    ]


async def draft(state: TriageState, config) -> dict:
    """The manifest's ``draft`` step, and the graph's ``draft`` node.

    The two names match because a node that exists to run one step
    should be named for it — not because the platform connects them
    today. **It does not.** The adapter emits a progress row per node,
    id `phase:node`; the gateway records the model under the LLM step
    id; `GET /runs/{id}/progress` joins those by exact id. So this
    node's row is `investigate:draft` while the model sits under
    `draft`, and no model is displayed for it.

    That is gap **E5**, not something this example can fix. A join
    matching the row id's last segment was written and reverted: a
    progress id is free text an agent chooses, so a row named
    `phase:draft` need not be the work that called `draft`, and
    inferring otherwise attributes models to rows that never made the
    call. D13 is visible on the span and the cost; the page waits on an
    association the agent states explicitly."""
    prior = state.get("prior_analysis") or {}
    severity = prior.get("severity") or state.get("severity", "low")
    service = prior.get("service") or ""
    summary = prior.get("summary") or state.get("summary", "")
    context = state.get("context") or []
    rules = {"next_steps": _checklist(severity, service)}

    prompt = "\n".join(
        [
            f"severity: {severity}",
            f"service: {service or 'unknown'}",
            f"summary: {summary}",
            "knowledge base: " + ("; ".join(context) if context else "nothing found"),
            "",
            "Give the first concrete steps for the responder, most urgent first.",
        ]
    )
    answer, provider = await _ask_the_model(config, "draft", prompt, DRAFT_SCHEMA, rules)

    steps = rules["next_steps"]
    used = _valid(answer, DRAFT_SCHEMA)
    if used:
        steps = list(answer["next_steps"])

    return {
        "structured": {
            "severity": severity,
            "summary": summary,
            "context": context,
            "next_steps": steps,
            # Both provenances, because they are answered separately: the
            # triage can come from a model and the plan from the rule, or
            # the other way round, if one call fails and the other does
            # not. One combined "a model was used" field would hide that.
            "severity_source": prior.get("severity_source", SOURCE_RULES),
            "next_steps_source": _source(provider, used=used),
            "note": (
                "Produced by the LangGraph example adapter. Model calls go "
                "through the platform gateway as the `classify` and `draft` "
                "steps; each falls back to a deterministic rule, and the "
                "`*_source` fields say which answered."
            ),
        }
    }


def _triage_graph():
    g = StateGraph(TriageState)
    g.add_node("classify", classify)
    g.add_node("summarise", summarise)
    g.set_entry_point("classify")
    g.add_edge("classify", "summarise")
    g.add_edge("summarise", END)
    return g.compile()


def _plan_graph():
    g = StateGraph(TriageState)
    g.add_node("gather_context", gather_context)
    g.add_node("draft", draft)
    g.set_entry_point("gather_context")
    g.add_edge("gather_context", "draft")
    g.add_edge("draft", END)
    return g.compile()


def _input_schema() -> dict:
    with open(os.path.join(_HERE, "input_schema.json")) as f:
        return json.load(f)


class LangGraphTriageAgent(LangGraphAgent):
    """The whole integration: bind the graphs to the manifest's phases.

    Discovery instantiates this with no arguments and checks that
    ``agent_id`` matches ``agent.yaml``, so the adapter's configuration
    lives here rather than in a factory the registry cannot call. The
    graphs are compiled once per process, at construction.
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="langgraph-triage",
            display_name="LangGraph Triage (example)",
            description=(
                "Example adapter agent: triages an incident report with a "
                "compiled LangGraph graph, then drafts next steps."
            ),
            input_schema=_input_schema(),
            graphs={"analyze": _triage_graph(), "investigate": _plan_graph()},
        )
