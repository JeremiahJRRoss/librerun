#!/usr/bin/env python3
"""A two-step LlamaIndex Workflow as a Run Contract v1 agent (blueprint
S5/S5-R, promise 2).

What this example is for
------------------------
It is the answer to "I already have a LlamaIndex Workflow — what does it
cost me to run it on LibreRun?". The answer is this file: the Workflow
is ordinary LlamaIndex, ``librerun_agent.serve`` is the whole transport,
and the two things that are LibreRun's are both one line each —

    llm = OpenAILike(model=run.llm.step("extract"), **_client_kwargs(run))

``run.llm.step("extract")`` names a step the manifest declares. It is
NOT a model name: which model answers, at what temperature and within
what budget is the tenant admin's configuration, resolved by the gateway
at request time (L25, D13). Retargeting a step is an edit in the admin
page; this container is not redeployed and does not know it happened.

``run.llm.client()`` hands over the gateway's address, this agent's
LibreRun key and **this invocation's run token**, and it is called
inside the handler for that last reason. The token is what makes a model
call attributable to one run and one tenant, so on a multi-tenant
deployment an LLM object built once at import time would carry the first
run's token into every later run — the gateway would then resolve the
wrong tenant's step configuration, or, once that token expired, refuse
the call outright (D10). Per invocation, therefore; and the cost of
getting it right is that the object is built two lines lower down than
a LlamaIndex example would normally build it.

Nothing here holds a provider key, and nothing here mentions the stub:
keyless mode is the gateway's business, not the agent's.

The shape
---------
``StartEvent -> extract -> Extracted -> summarize -> StopEvent``. Two
steps because one would not show the thing worth showing: each names its
own ``llm.steps`` entry, so an admin can run ``extract`` on a cheap
model and ``summarize`` on a better one, and the run's trace carries two
costed LLM spans rather than one.

Both steps degrade to a deterministic rule when no model answers — an
example that dies because a gateway is down teaches the wrong lesson —
and both say which of the two produced the result, because keyless the
gateway answers from a fixture and a caller that looked only at the text
would present a fixture as a judgement.

Run standalone:  python agent.py --port 8090
"""
from __future__ import annotations

import argparse
import json
import re

from librerun_agent import LlmError, RunContext, serve
from llama_index.core.workflow import (
    Context,
    Event,
    StartEvent,
    StopEvent,
    Workflow,
    step,
)
from llama_index.llms.openai_like import OpenAILike

# How the result was produced. Two values, kept apart on purpose: a
# reader of the run page should never have to guess whether a sentence
# came from a model or from the fallback below it.
FROM_MODEL = "model"
FROM_RULE = "rule"

# The workflow's own ceiling. The real budget is the invocation's
# ``deadline_seconds`` — the chassis fails the phase when it passes and
# the SDK bounds each gateway call by what is left of it — so this is
# only here because LlamaIndex defaults to 10s, which is shorter than a
# single configured step is allowed to take.
WORKFLOW_TIMEOUT_SECONDS = 1800


class Extracted(Event):
    """What ``extract`` hands to ``summarize``."""

    points: list[str]
    source: str
    provider: str | None = None


def _client_kwargs(run: RunContext) -> dict:
    """``ctx.llm.client()`` mapped onto the names LlamaIndex uses.

    Three values — where the gateway is, which agent is calling, and
    which invocation the call belongs to — under this framework's
    spelling. Every framework spells them differently (``api_base`` here,
    ``baseURL`` in the Vercel example next door), which is why the SDK
    names them after what they are and leaves the mapping to the agent.
    """
    client = run.llm.client()
    return {
        "api_base": client.base_url,
        "api_key": client.api_key,
        "default_headers": dict(client.default_headers),
        # The gateway's models are LibreRun steps, so no context-window
        # table anywhere knows them; state what this agent assumes rather
        # than let the framework guess.
        "context_window": 128000,
        "is_chat_model": True,
        # NO CLIENT-SIDE RETRIES. The framework defaults to three, and
        # every one of them is a second provider call the gateway may
        # already have paid for: a reply lost on the way back looks
        # exactly like a call that never happened. The step's timeout and
        # the invocation's deadline are the platform's policy (L25), and
        # a retry policy belongs beside them, not in each agent.
        "max_retries": 0,
        # The transport ceiling is the invocation's remaining budget,
        # which the SDK already knows; a number of this agent's own would
        # hang up on a call the admin allowed and blame the gateway.
        "timeout": run.seconds_left,
    }


def _envelope(response) -> dict:
    """The gateway's ``librerun`` envelope, off whatever object the
    framework handed back.

    The reply is OpenAI-shaped and OpenAI-shaped replies name no
    provider, so the gateway adds one (``gateway/main.py:_resolved``).
    Reading it takes three attempts because the object belongs to the
    framework, not to us: a dict from one version, a pydantic model with
    extras allowed from another. An agent that assumed one shape would
    mislabel a fixture as a model answer the first time the framework
    changed its mind — which is the one way this design can mislead.
    """
    for get in (
        lambda: response.get("librerun"),
        lambda: getattr(response, "librerun", None),
        lambda: (getattr(response, "model_extra", None) or {}).get("librerun"),
    ):
        try:
            value = get()
        except Exception:  # noqa: BLE001 — a shape we do not have, not an error
            continue
        if isinstance(value, dict):
            return value
    return {}


async def _ask(
    run: RunContext, step_id: str, prompt: str, system: str, schema: dict, fixture: dict
) -> tuple[dict | None, str | None]:
    """One model call for ``step_id``, through LlamaIndex, through the
    gateway. ``(answer, provider)``, or ``(None, None)``.

    ``response_format`` asks for JSON this step can read rather than
    prose it would have to parse, and ``librerun.stub_reply`` is this
    agent's own keyless fixture: the gateway honours it **only** when the
    resolved provider is the stub, so it is inert in a credentialled
    deployment and cannot make the gateway answer for a provider it did
    call. Both travel in ``extra_body`` / the typed parameter because
    that is how the OpenAI client forwards fields it does not own.

    A failure returns ``(None, None)`` rather than raising: a model is an
    improvement on the rule below, not a dependency of it. The broad
    ``except`` cannot swallow the phase deadline — the SDK enforces it by
    CANCELLING the task and ``CancelledError`` is a ``BaseException`` —
    and it must never widen to ``BaseException``, which would turn every
    deadline into a silent fallback.
    """
    try:
        llm = OpenAILike(
            model=run.llm.step(step_id),
            temperature=0.0,
            additional_kwargs={
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": step_id, "strict": True, "schema": schema},
                },
                "extra_body": {"librerun": {"stub_reply": json.dumps(fixture)}},
            },
            **_client_kwargs(run),
        )
        response = await llm.acomplete(f"{system}\n\n{prompt}")
    except LlmError as exc:
        run.log(f"step {step_id} unavailable: {exc.code}", level="warning")
        return None, None
    except Exception as exc:  # noqa: BLE001 — see the docstring
        # The type only. The message may carry whatever a provider put in
        # it, and deciding how to store that is the platform's job.
        run.log(f"step {step_id} unavailable: {type(exc).__name__}", level="warning")
        return None, None
    try:
        parsed = json.loads((response.text or "").strip())
    except ValueError:
        run.log(f"step {step_id} did not answer with the JSON it was asked for", level="warning")
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    return parsed, _envelope(response.raw).get("provider")


def _sentences(text: str) -> list[str]:
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]
    return [s for s in parts if len(s) > 20]


class SummarizeWorkflow(Workflow):
    """Ordinary LlamaIndex, apart from where the LLM comes from.

    The ``RunContext`` is held on the instance because a Workflow is
    constructed per invocation here — which it must be, for the same
    reason the LLM is (D10). Passing it through ``StartEvent`` would work
    equally well; what would not work is a module-level Workflow shared
    across runs.
    """

    def __init__(self, run: RunContext, **kwargs):
        super().__init__(timeout=WORKFLOW_TIMEOUT_SECONDS, **kwargs)
        self._run = run

    @step
    async def extract(self, _ctx: Context, ev: StartEvent) -> Extracted:
        run = self._run
        run.progress("running", step="extract", label="Reading the document")
        document = str(ev.document or "")
        wanted = int(ev.max_points or 5)
        focus = str(ev.focus or "").strip()

        fallback = _sentences(document)[:wanted] or [
            "The document is too short to extract points from."
        ]
        answer, provider = await _ask(
            run,
            "extract",
            (
                f"Document:\n{document}\n\n"
                + (f"Keep this in view: {focus}\n" if focus else "")
                + f"Return at most {wanted} points."
            ),
            "You are extracting the points a reader must not miss from a document.",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["points"],
                "properties": {
                    "points": {
                        "type": "array",
                        "maxItems": 10,
                        "items": {"type": "string", "maxLength": 300},
                    }
                },
            },
            {"points": fallback},
        )
        points = [str(p) for p in (answer or {}).get("points", []) if str(p).strip()]
        if not points:
            run.progress("completed", step="extract", detail="from the document itself")
            return Extracted(points=fallback, source=FROM_RULE)
        run.progress("completed", step="extract")
        return Extracted(points=points[:wanted], source=FROM_MODEL, provider=provider)

    @step
    async def summarize(self, _ctx: Context, ev: Extracted) -> StopEvent:
        run = self._run
        run.progress("running", step="summarize", label="Writing the brief")
        audience = str(run.input.get("audience") or "a support lead")
        joined = " ".join(ev.points)

        answer, provider = await _ask(
            run,
            "summarize",
            "Points:\n- " + "\n- ".join(ev.points) + f"\n\nWrite it for {audience}.",
            "You are writing a short brief. Three sentences at most.",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["summary"],
                "properties": {"summary": {"type": "string", "maxLength": 1200}},
            },
            {"summary": joined[:1200]},
        )
        summary = str((answer or {}).get("summary") or "").strip()
        source = FROM_MODEL if summary else FROM_RULE
        run.progress("completed", step="summarize")
        return StopEvent(
            result={
                "summary": summary or joined[:1200],
                "points": ev.points,
                "audience": audience,
                # Which half of each step produced what is on screen. Not
                # bookkeeping: keyless, the gateway answers from this
                # agent's own fixture, and a run page that did not say so
                # would present a fixture as a judgement.
                "extract_source": ev.source,
                "summarize_source": source,
                # …and who answered, in the admin's own vocabulary
                # (`stub` on a keyless deployment).
                "provider": provider or ev.provider,
                "framework": "llamaindex",
            }
        )


async def handler(ctx: RunContext) -> dict:
    """The Run Contract phase: build the Workflow for THIS invocation,
    run it, hand back its result as the phase output."""
    ctx.log("summarizing with a two-step LlamaIndex Workflow")
    workflow = SummarizeWorkflow(ctx)
    result = await workflow.run(
        document=ctx.input.get("document", ""),
        focus=ctx.input.get("focus", ""),
        max_points=ctx.input.get("max_points", 5),
    )
    output = getattr(result, "result", result)
    return dict(output) if isinstance(output, dict) else {"summary": str(output)}


app = serve(handler, name="llamaindex-summarize")


def main() -> None:
    parser = argparse.ArgumentParser(description="LlamaIndex summarize example agent")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    from librerun_agent import run as serve_forever

    serve_forever(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
