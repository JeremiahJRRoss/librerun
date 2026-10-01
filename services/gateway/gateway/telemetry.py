"""The one LLM span, and the gateway's own exports (blueprint S4a).

Three things happen here.

**The span.** Exactly one per model call, in the run's tree. Its parent
is the ``traceparent`` the caller sent — but only if that header's trace
id is the one the run token was minted with, because a valid token must
not be usable to hang cost and model telemetry outside the run it
belongs to (``403 trace_mismatch``, the same rule the S4 relay applies).
When the header is absent — a framework that drops it — the parent is
the invocation's phase span, whose ``traceparent`` the runner wrote into
the token record, so the call still lands in the one tree.

**The content.** The gateway writes the GenAI content attributes and
events **itself**, prompt side and completion side, each passed through
the walker's string redaction first — whatever ``llm.redact_outbound``
says. That switch governs what the *model* sees; it never governs what
telemetry keeps. LiteLLM's own recorders are not enabled
(``gateway/egress.py``), so there is no second span or log line carrying
the raw reply beside this one.

The rule used here is the **boundary** rule, ``redact`` with
``pii_service.BOUNDARY_SKIP_ENTITIES`` — the same one the chassis's span
and log walkers use, deliberately, so one span does not carry two
strengths of redaction depending on which process wrote which attribute.
That rule skips ``LOCATION`` and ``DATE_TIME``: a place name or a date
in a model's answer stays readable, which is what makes a trace useful
for debugging and is S4's considered choice, not an oversight here.

**The walkers.** S4's two export walkers are installed at boot, so the
gateway's own spans and log lines are walked exactly like the backend's:
the span exporter sits behind ``WalkingSpanExporter``, and every log
record passes ``walk_log_record`` before any handler sees it. The log
half lives in ``gateway/logs.py`` — it is a logging pipeline, not a
tracing one — and :func:`init` calls it so a process that starts
telemetry cannot start it without the walk.
"""
from __future__ import annotations

import re
from contextlib import contextmanager

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.observability import otlp_walk, walkers
from app.services import pii_service

from gateway import errors, logs
from gateway.config import settings

logger = structlog.get_logger(__name__)

_CONTENT_VAR = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"

TRACER_NAME = "librerun.gateway"
SCOPE_ATTRIBUTE = "librerun.scope"

# OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT lives here now, with
# the provider keys (blueprint S4a): this is the process that makes the
# model call, so it is the process the switch governs.
#
# The resolution moved from the backend intact, including the two parts
# that look like fussiness and are not. Legacy boolean spellings are
# normalised, because that is what deployments have in their .env files.
# And anything outside the enum **fails closed** — a typo such as
# NO_CONTNET keeps no content rather than exporting everything, since the
# only person who writes that variable is one trying to suppress content.
CONTENT_DEFAULT = "SPAN_AND_EVENT"
_LEGACY_CONTENT_VALUES = {"true": "SPAN_AND_EVENT", "false": "NO_CONTENT"}
_VALID_CONTENT_VALUES = frozenset(
    {"NO_CONTENT", "SPAN_ONLY", "EVENT_ONLY", "SPAN_AND_EVENT"}
)

_provider: TracerProvider | None = None


def content_capture_value() -> str:
    """The resolved enum value, normalised and failing closed."""
    import os

    raw = (os.environ.get(_CONTENT_VAR) or "").strip()
    if not raw:
        raw = (
            settings.OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT or ""
        ).strip()
    if not raw:
        return CONTENT_DEFAULT
    normalised = _LEGACY_CONTENT_VALUES.get(raw.lower())
    if normalised is not None:
        return normalised
    upper = raw.upper()
    if upper in _VALID_CONTENT_VALUES:
        return upper
    logger.warning(
        "gateway_content_capture_invalid",
        value=raw,
        resolved="NO_CONTENT",
        hint="the only reason to set this variable is to suppress content, "
        "so an unrecognised value suppresses it",
    )
    return "NO_CONTENT"


def capture_content() -> bool:
    return content_capture_value() != "NO_CONTENT"


# The enum has FOUR values and this module had two behaviours: anything
# but NO_CONTENT wrote the payload to the span attribute AND the event.
# So an operator who chose SPAN_ONLY or EVENT_ONLY — the two settings
# whose entire purpose is to pick a destination — still got content
# through the one they had explicitly turned off, defeating whatever
# routing or retention policy made them choose (Codex P2). Resolving the
# enum carefully and then collapsing it to a bool is a setting that does
# not exist, which is decision 54 one field over.
def capture_on_span() -> bool:
    return content_capture_value() in ("SPAN_ONLY", "SPAN_AND_EVENT")


def capture_in_event() -> bool:
    return content_capture_value() in ("EVENT_ONLY", "SPAN_AND_EVENT")


def init() -> None:
    """Install the tracer provider and the log walk. Idempotent."""
    global _provider
    logs.configure()
    if _provider is not None:
        return
    resource_attributes = {"service.name": settings.OTEL_SERVICE_NAME}
    provider = TracerProvider(resource=Resource.create(resource_attributes))
    # The chassis's record of what IT wrote, kept beside the span rather
    # than in it (S4): the walk must not redact the gateway's own service
    # name or its identity attributes, and a marker inside the span would
    # be one an agent could forge.
    otlp_walk.stamps.set_resource({k: str(v) for k, v in resource_attributes.items()})
    endpoint = (settings.OTEL_EXPORTER_OTLP_ENDPOINT or "").strip()
    if endpoint:
        try:
            exporter = walkers.WalkingSpanExporter.for_endpoint(
                endpoint, settings.OTEL_EXPORTER_OTLP_PROTOCOL
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "gateway_otel_exporter_unavailable",
                endpoint=endpoint,
                error=str(exc),
                error_type=type(exc).__name__,
            )
    if settings.OTEL_DEBUG:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _provider = provider


def shutdown() -> None:
    global _provider
    if _provider is not None:
        try:
            _provider.force_flush(timeout_millis=5000)
            _provider.shutdown()
        except Exception:  # noqa: BLE001
            pass
    _provider = None


def _tracer():
    return trace.get_tracer(TRACER_NAME)


# A syntactically valid W3C trace id: 32 lowercase hex digits. Used only
# to decide whether a header the propagator REJECTED still names a trace.
_TRACE_ID_RE = re.compile(r"[0-9a-f]{32}")


def _refuse_foreign_trace():
    raise errors.forbidden(
        "trace_mismatch",
        "the traceparent header names a different trace than the run "
        "token was minted with; one token belongs to one trace",
    )


def _extract(header: str):
    """The context a ``traceparent`` yields, and the span context in it.

    ``extract`` never raises: a header it cannot parse — a truncated one,
    a bad version, a non-hex or all-zero span id — yields a context with
    nothing in it, and a span started from that context is a NEW ROOT.
    That is the shape this function exists to make visible: an orphaned
    root span is not an error anywhere, it is simply a run whose LLM call
    is missing from its tree and sitting in a trace of its own.
    """
    context = TraceContextTextMapPropagator().extract({"traceparent": header})
    return context, trace.get_current_span(context).get_span_context()


def parent_context(principal, traceparent: str | None):
    """The context this span hangs from, or None for a root span.

    A ``traceparent`` naming a different trace than the token was minted
    with is refused rather than followed: the token is valid, so the call
    would otherwise succeed with its cost and its model recorded in
    somebody else's trace.

    A ``traceparent`` that names nothing usable is a different case, and
    a call is not worth failing over it: the parent falls back to the
    invocation's phase span, exactly as it does for the framework that
    sends no header at all — the difference being that this one says so,
    because a header the caller believes it is sending and the gateway
    cannot use is a bug in the caller and it should be findable.
    """
    header = (traceparent or "").strip()
    if header:
        context, span_context = _extract(header)
        if span_context.is_valid:
            if (
                principal.trace_id
                and format(span_context.trace_id, "032x") != principal.trace_id
            ):
                _refuse_foreign_trace()
            return context
        # Unusable. Before falling back, honour what the header SAYS: a
        # malformed header can still name another trace in its trace-id
        # field, and following the token's context for it would quietly
        # do the very thing the refusal above exists to prevent.
        #
        # "Names a trace" means the field IS a trace id — 32 lowercase
        # hex — not merely that the header has a second dash-separated
        # field. Without that, ``not-a-traceparent`` reads as naming the
        # trace ``a`` and a caller with a broken header gets a 403.
        parts = header.split("-")
        names_a_trace = len(parts) >= 2 and _TRACE_ID_RE.fullmatch(parts[1])
        if names_a_trace and principal.trace_id and parts[1] != principal.trace_id:
            _refuse_foreign_trace()
        logger.warning(
            "gateway_traceparent_unusable",
            fields=len(parts),
            names_a_trace=bool(names_a_trace),
            fallback="run_token" if principal.traceparent else "root",
            hint="the header did not parse to a valid span context; the span "
            "hangs from the invocation's phase span instead",
        )
    if principal.traceparent:
        context, span_context = _extract(principal.traceparent)
        if span_context.is_valid:
            return context
        # The token's own pointer is the chassis's write, not the
        # caller's. If THAT is unusable the tree cannot be joined at all,
        # and a root span is all that is left — said out loud, since it
        # means the runner wrote something the propagator rejects.
        logger.warning(
            "gateway_run_token_traceparent_unusable",
            hint="the run token's phase pointer did not parse; this call's "
            "span is a root of its own",
        )
    return None


def _walk_text(value) -> str:
    text = value if isinstance(value, str) else str(value)
    return pii_service.redact(
        text, skip_entities=pii_service.BOUNDARY_SKIP_ENTITIES, quiet=True
    )[0]


def walked_messages(messages) -> list[dict]:
    """The prompt side, for the span.

    Only the textual positions the convention captures: message content
    and tool-call arguments. A non-text part is recorded as its type and
    size, never its payload — a base64 image in a span attribute is both
    useless and a disclosure.
    """
    out: list[dict] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        entry: dict = {"role": str(message.get("role") or "")}
        content = message.get("content")
        if isinstance(content, str):
            entry["content"] = _walk_text(content)
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append({"type": "text", "content": _walk_text(part["text"])})
                elif isinstance(part, dict):
                    parts.append(
                        {
                            "type": str(part.get("type") or "unknown"),
                            "bytes": len(str(part)),
                        }
                    )
            entry["content"] = parts
        # A replayed assistant refusal: prose the model reads, like
        # content. Recorded for the same reason the request walker
        # redacts it (Codex P1/P2).
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal:
            entry["refusal"] = _walk_text(refusal)
        audio = _walked_audio(message)
        if audio:
            entry["audio"] = audio
        calls = message.get("tool_calls")
        if isinstance(calls, list) and calls:
            entry["tool_calls"] = [
                {
                    "id": (call or {}).get("id"),
                    "name": ((call or {}).get("function") or {}).get("name"),
                    "arguments": _walk_text(
                        ((call or {}).get("function") or {}).get("arguments") or ""
                    ),
                }
                for call in calls
                if isinstance(call, dict)
            ]
        # The legacy spelling, on the prompt side: an assistant turn
        # replaying an earlier legacy call carries it here, and its
        # arguments are model-visible text like any other.
        legacy = message.get("function_call")
        if isinstance(legacy, dict) and legacy:
            entry["function_call"] = {
                "name": legacy.get("name"),
                "arguments": _walk_text(legacy.get("arguments") or ""),
            }
        out.append(entry)
    return out


def _walked_audio(message: dict) -> dict | None:
    """An audio completion's recordable part.

    ``modalities`` and ``audio`` are on the forwarded list on purpose, so
    an audio-only reply is a shape this gateway invites: the answer sits
    in ``message.audio`` with ``content`` null, and the walker recorded a
    choice with nothing in it — the fifth spelling of decisions 50, 53,
    55 and 57.

    The TRANSCRIPT is text the model produced, so it is walked and kept
    like any other. The ``data`` is base64 audio: keeping it would put
    megabytes of unredactable payload on a span, so what is kept is its
    SIZE. Its ``id`` is a provider string and is not stamped, for the
    same reason ``gen_ai.response.id`` is not.

    ``bytes`` is accepted alongside ``data`` because the stream assembler
    counts the pieces as they arrive and has the number already; asking
    it for a string to measure would mean rebuilding the payload purely
    to take its length.
    """
    audio = message.get("audio")
    if not isinstance(audio, dict) or not audio:
        return None
    out: dict = {}
    transcript = audio.get("transcript")
    if isinstance(transcript, str) and transcript:
        out["transcript"] = _walk_text(transcript)
    data = audio.get("data")
    if isinstance(data, str):
        out["bytes"] = len(data)
    elif isinstance(audio.get("bytes"), int) and not isinstance(
        audio.get("bytes"), bool
    ):
        out["bytes"] = audio["bytes"]
    return out or None


def walked_choices(response: dict) -> list[dict]:
    """The completion side, walked the same way.

    The reply itself is NOT rewritten on its way back to the agent —
    rewriting a model's answer under the agent's feet would change
    structured outputs and tool-call arguments, and every door a
    completion can take into the platform's stores is the chassis
    walker's already. Telemetry is the exception: what the span keeps
    goes through the walk.
    """
    out = []
    for choice in response.get("choices") or []:
        message = (choice or {}).get("message") or {}
        entry: dict = {
            "index": choice.get("index"),
            "finish_reason": choice.get("finish_reason"),
        }
        if message.get("content") is not None:
            entry["content"] = _walk_text(message.get("content"))
        # A safety refusal puts the answer in ``refusal`` and commonly
        # leaves ``content`` null, so a walker reading only ``content``
        # and ``tool_calls`` recorded a choice with nothing in it but an
        # index — the SAME wrong-evidence failure as the streamed tool
        # call (decision 50), the legacy ``function_call`` (decision 53)
        # and the embeddings reply (decision 55). Fourth spelling, and
        # the one that matters most to read back: a refusal is exactly
        # the reply an operator goes to the trace to understand.
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal:
            entry["refusal"] = _walk_text(refusal)
        audio = _walked_audio(message)
        if audio:
            entry["audio"] = audio
        calls = message.get("tool_calls")
        if isinstance(calls, list) and calls:
            entry["tool_calls"] = [
                {
                    "id": (call or {}).get("id"),
                    "name": ((call or {}).get("function") or {}).get("name"),
                    "arguments": _walk_text(
                        ((call or {}).get("function") or {}).get("arguments") or ""
                    ),
                }
                for call in calls
                if isinstance(call, dict)
            ]
        # The legacy spelling, which the gateway deliberately keeps
        # forwarding (``egress.CHAT_PARAMS`` carries ``functions`` and
        # ``function_call``). A legacy response puts the WHOLE answer
        # here with ``content`` null, so reading only ``tool_calls``
        # recorded an empty choice for it — the same wrong-evidence
        # failure as the streamed tool call, one spelling over (Codex
        # P2). Forwarding an API means recording what it answers.
        legacy = message.get("function_call")
        if isinstance(legacy, dict) and legacy:
            entry["function_call"] = {
                "name": legacy.get("name"),
                "arguments": _walk_text(legacy.get("arguments") or ""),
            }
        out.append(entry)
    return out


@contextmanager
def llm_span(
    principal, step, *, operation: str, traceparent: str | None, start_time: int | None = None
):
    """Open the one span for this call, stamped with who it belongs to.

    ``start_time`` (nanoseconds) backdates the span to when the call
    really began. The streaming route starts the provider call before it
    returns a response — so a refusal on the way in can still be a
    status — and opens the span inside the response's own task; without
    this the span would start after the connection was already made and
    under-report every streamed call.
    """
    context = parent_context(principal, traceparent)
    name = f"{operation} {step.model or step.target or step.step_id}"
    with _tracer().start_as_current_span(
        name, context=context, kind=SpanKind.CLIENT, start_time=start_time
    ) as span:
        stamped = {
            SCOPE_ATTRIBUTE: "run",
            "librerun.agent_id": principal.agent_id,
            "librerun.step_id": step.step_id,
        }
        if principal.run_id:
            stamped["librerun.run_id"] = str(principal.run_id)
        if principal.tenant_id:
            stamped["librerun.tenant_id"] = str(principal.tenant_id)
        # The resolved call, stamped for the same reason the identity is
        # (S4): it is what the CHASSIS chose — the provider and model the
        # manifest and this tenant's rows resolved to, and the limits
        # that came with them — and it is not prose. Walked, a model name
        # is destroyed: the recognizers read `claude-sonnet-4-20250514`
        # as a person and Jaeger shows [REDACTED_PERSON_1] for the one
        # fact D13 is about. Nothing from the request body is here; the
        # caller's `model` field is `librerun/<step>` and never survives
        # resolution.
        stamped["gen_ai.operation.name"] = operation
        stamped["gen_ai.system"] = step.provider or "unknown"
        if step.model:
            stamped["gen_ai.request.model"] = step.model
        for key, value in stamped.items():
            span.set_attribute(key, value)
        if step.temperature is not None:
            span.set_attribute("gen_ai.request.temperature", float(step.temperature))
        if step.max_tokens is not None:
            span.set_attribute("gen_ai.request.max_tokens", int(step.max_tokens))
        # Recorded BESIDE the span, keyed by ids an agent cannot choose,
        # so the walk knows which pairs the platform wrote (S4). The span
        # NAME goes in too, under the walker's one reserved key: the
        # GenAI convention makes it `{operation} {model}`, and a model
        # name is exactly what the recognizers destroy — the name is the
        # first thing an operator reads in the viewer.
        context_ids = span.get_span_context()
        record = dict(stamped)
        record[otlp_walk.STAMPED_NAME_KEY] = name
        otlp_walk.stamps.record_span(
            context_ids.trace_id, context_ids.span_id, record
        )
        try:
            yield span
        except errors.GatewayError as exc:
            span.set_attribute("librerun.refusal_code", exc.code)
            span.set_status(Status(StatusCode.ERROR, exc.code))
            raise
        except Exception as exc:  # noqa: BLE001
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


def record_prompt(span, messages) -> None:
    if not capture_content():
        return
    import json as _json

    payload = _json.dumps(walked_messages(messages), default=str)
    if capture_on_span():
        span.set_attribute("gen_ai.input.messages", payload)
    if capture_in_event():
        span.add_event(
            "gen_ai.client.inference.operation.details",
            {"gen_ai.input.messages": payload},
        )


# Every operation ``llm_span`` is opened with. ``record_response``
# handles each one by name and records NOTHING as content for a name it
# does not know: silence about an unfamiliar response shape is honest,
# and reading it with the wrong walker is how an embeddings span came to
# claim the model returned no messages.
RECORDED_OPERATIONS = ("chat", "embeddings")


def record_response(
    span, response: dict, step, cost: float | None, *, operation: str
) -> None:
    usage = response.get("usage") or {}
    if usage.get("prompt_tokens") is not None:
        span.set_attribute("gen_ai.usage.input_tokens", int(usage["prompt_tokens"]))
    if usage.get("completion_tokens") is not None:
        span.set_attribute("gen_ai.usage.output_tokens", int(usage["completion_tokens"]))
    if response.get("model"):
        span.set_attribute("gen_ai.response.model", str(response["model"]))
        if step.model and str(response["model"]) == str(step.model):
            # Stamped only when the provider echoed back the value the
            # chassis chose — then it is the chassis's own string, and
            # vouching for it is vouching for something already known.
            # A different one (a dated variant, say) came from outside
            # this deployment and is walked like any other text: a
            # mangled model name in the trace is a smaller price than a
            # position where a provider's reply is exempt by
            # construction. `gen_ai.response.id` is never stamped, for
            # the same reason.
            context_ids = span.get_span_context()
            otlp_walk.stamps.amend_span(
                context_ids.trace_id,
                context_ids.span_id,
                {"gen_ai.response.model": str(response["model"])},
            )
    if response.get("id"):
        span.set_attribute("gen_ai.response.id", str(response["id"]))
    if cost is not None:
        span.set_attribute("librerun.cost_usd", float(cost))
    if operation == "embeddings":
        # An embeddings reply has ``data``, not ``choices``. Handing it
        # to ``walked_choices`` produced ``gen_ai.output.messages: []`` —
        # an affirmative statement that the model returned no messages,
        # on every embeddings span in the system. That is the round-6 and
        # round-7 failure a third time: evidence that is WRONG rather
        # than missing. What came back is vectors, so what the span keeps
        # is their shape — counts, not content, and so not behind the
        # content switch.
        data = response.get("data") or []
        span.set_attribute("librerun.embeddings.count", len(data))
        first = (data[0] or {}).get("embedding") if data else None
        if isinstance(first, list):
            span.set_attribute("librerun.embeddings.dimensions", len(first))
        elif isinstance(first, str):
            # `encoding_format: "base64"` — the vectors came back packed,
            # so their width is not `len()` of anything here. Recording
            # the encoding says WHY the dimensions are absent, which is
            # the difference between evidence that is missing and
            # evidence that is missing for no stated reason. Deriving the
            # count would mean asserting a dtype this span cannot see.
            span.set_attribute("librerun.embeddings.encoding", "base64")
        return
    if operation != "chat":
        return
    if not capture_content():
        return
    import json as _json

    payload = _json.dumps(walked_choices(response), default=str)
    if capture_on_span():
        span.set_attribute("gen_ai.output.messages", payload)
    if capture_in_event():
        span.add_event(
            "gen_ai.client.inference.operation.details",
            {"gen_ai.output.messages": payload},
        )
