#!/usr/bin/env python3
"""Run Contract v1 reference agent, on the LibreRun agent SDK (blueprint
S4): one async handler, served by ``librerun_agent.serve``.

Echoes its input back as the phase output. The chassis-side tests run it
in-process (``librerun_agent.testing.serve_in_thread(app)``); the
container battery and the demo run the image built from the Dockerfile
next to this file (build context: the repository root, so the SDK
installs from ``sdk/python/librerun-agent``).

The input may ask for the things the S4 acceptance drives through a
container's own instrumentation and output — none of them change what
an ordinary run does, and every one is declared in ``input_schema.json``
so it is reachable through the chassis, not only off it. The switches
carrying text an agent will *emit* are marked ``x-pii``: through the
chassis the handler is handed the intake pipeline's placeholder, which
is the point of the demo; driven directly on the Run Contract (the
battery, the acceptance) it gets whatever was sent, which is what proves
the export walk.

- ``redact``: a text the agent asks the chassis to redact
  (``ctx.pii.redact``), returned as ``redacted``;
- ``print``: a line the handler prints (stdout capture);
- ``instrument``: a text the handler puts into its own span's name, a
  string and an int64 attribute (``instrument_number``), a span event,
  a span link attribute, a log record and a bytes attribute (export
  walking through the relay);
- ``threads``: prints from a thread the handler spawns and from a pool
  job it submits (context propagation);
- ``sleep_seconds``: a slow handler (the deadline);
- ``store_key`` / ``audit``: a ``run_store_set`` and an ``audit_log``
  call with the given arguments (the boundary walk over MCP);
- ``show_settings``: the agent's settings as this tenant set them
  (``ctx.config.settings()``, the MCP ``config_get`` tool), returned as
  ``settings``, with the ``note`` setting as the output's note (K5a);
- ``fetch_secret``: the agent's declared tool secret, ``echo_token``
  (``ctx.secrets.get``, the MCP ``secret_get`` tool), returned only as
  ``secret_set`` — whether this tenant, or every tenant's default, has
  a value. Never the value: the output is persisted (K8b).

Run standalone:  python echo_agent.py --port 8090
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import logging
import threading

from librerun_agent import RunContext, SecretNotSet, serve

_LOG = logging.getLogger("agents.echo")

# Stands in for a ticket, a document, a vendor API's answer — something
# an agent FETCHES after intake. Nothing the chassis redacted on the way
# in can help with it, which is the whole reason the output walk, the
# export walk and `ctx.pii.redact` exist (blueprint gap J3). Driving the
# acceptance with text the user typed would prove intake redaction and
# nothing else, because an `x-pii` input reaches the handler already
# replaced by the placeholder.
RUNTIME_FIXTURE = "pii.fixture@example.com"


def _echo_output(ctx: RunContext) -> dict:
    return {
        "echo": ctx.input,
        "phase": ctx.phase,
        "prior_output_keys": sorted((ctx.prior_output or {}).keys()),
        "note": "echoed by the Run Contract v1 reference agent",
    }


def _instrument(text: str, number: int | None) -> None:
    """The agent's OWN instrumentation, exported by the SDK — not through
    ``ctx.log()``: what the relay's walk has to catch."""
    try:
        from opentelemetry import trace
        from opentelemetry.trace import Link, SpanContext, TraceFlags
    except ImportError:  # the otel extra is optional
        _LOG.info("instrument requested but the otel extra is not installed")
        return
    tracer = trace.get_tracer("agents.echo")
    link_ctx = SpanContext(trace_id=1, span_id=2, is_remote=True, trace_flags=TraceFlags(1))
    attributes = {"note": text, "blob": text.encode()}
    if number is not None:
        attributes["instrument_number"] = int(number)
    with tracer.start_as_current_span(
        f"custom {text}", attributes=attributes, links=[Link(link_ctx, {"why": text})]
    ) as span:
        span.add_event(f"event {text}", {"who": text})
        _LOG.info("log record carrying %s", text)


async def handler(ctx: RunContext) -> dict:
    ctx.progress("running", step="echo", label="Echoing the input")
    ctx.log("echoing the input back")
    output = _echo_output(ctx)
    inp = dict(ctx.input)

    if inp.get("fetch_and_emit"):
        # As if the handler had just fetched it. Every switch that emits
        # text uses it, and it goes into the output too, so the walk at
        # the boundary is exercised as well as the walk on export.
        for switch in ("print", "instrument", "threads"):
            inp[switch] = f"{switch} {RUNTIME_FIXTURE}"
        output["fetched"] = f"the agent read {RUNTIME_FIXTURE} at runtime"

    if inp.get("sleep_seconds"):
        await asyncio.sleep(float(inp["sleep_seconds"]))
    if inp.get("print"):
        print(inp["print"])
    if inp.get("threads"):
        text = str(inp["threads"])
        thread = threading.Thread(target=lambda: print(f"thread {text}"))
        thread.start()
        thread.join()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: print(f"job {text}"))
            future.add_done_callback(lambda f: print(f"callback {text}"))
            future.result()
    if inp.get("instrument"):
        _instrument(str(inp["instrument"]), inp.get("instrument_number"))
    if inp.get("redact"):
        output["redacted"] = await ctx.pii.redact(str(inp["redact"]))
    if inp.get("store_key") is not None:
        await ctx.capabilities.run_store_set(str(inp["store_key"]), inp.get("store_value"))
        output["stored"] = await ctx.capabilities.run_store_get(str(inp["store_key"]))
    if inp.get("audit"):
        audit = inp["audit"]
        output["audited"] = await ctx.capabilities.audit_log(
            str(audit.get("action_type", "echo_event")), audit.get("detail") or {}
        )
    if inp.get("show_settings"):
        # The settings agent.yaml declares, with this TENANT's values
        # (K5a): read live, so an admin's edit on the Settings tab reaches
        # the next run with nothing restarted. The run's own configuration
        # needs no grant.
        settings = await ctx.config.settings()
        output["settings"] = settings
        output["note"] = settings.get("note", output["note"])
    if inp.get("fetch_secret"):
        # The tool secret agent.yaml declares (K8b): this tenant's value,
        # else every tenant's default, from the platform's rows — never
        # its environment. What a real agent would hand to the tool that
        # needs it goes nowhere here: the output says whether it was set,
        # because everything in it is persisted, and the value is not
        # kept past this line.
        try:
            await ctx.secrets.get("echo_token")
            output["secret_set"] = True
        except SecretNotSet:
            output["secret_set"] = False
    if inp.get("ask_model"):
        # A model call through the gateway (blueprint S4a). The agent
        # names its STEP, never a model: the admin picks the model, the
        # gateway redacts what it sends, pays for it and records the one
        # LLM span. Keyless mode needs no change here — the gateway
        # answers from its stub provider with the same span and cost.
        from librerun_agent import LlmError

        try:
            output["model_said"] = await ctx.llm.text("echo", str(inp["ask_model"]))
            output["step_config"] = await ctx.config.step("echo")
        except LlmError as exc:
            # Named, not swallowed: an example that hid a refusal would
            # teach an author to hide theirs.
            output["model_error"] = exc.code

    ctx.progress("completed", step="echo")
    return output


app = serve(handler, name="echo-v1")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    from librerun_agent import run

    run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
