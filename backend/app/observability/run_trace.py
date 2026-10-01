"""One trace per run — the root ``run`` span and its W3C context
(blueprint S4, promise 3).

The submission request opens the run's root span, ``run``, creates the
run row inside it and ends it there: an ended parent is a valid parent
for later children, and the tree renders whole. The span's context is
persisted on the row as the serialized W3C pair — ``root_traceparent``
(trace id, root span id, trace flags) and ``root_tracestate`` (any
vendor state an upstream caller sent) — and every phase span, in
whatever process and however long after an approval, starts with that
pair parsed back as its **remote parent**, flags and state included. So
the trace id never changes for the run's life, a later phase samples as
the first did, vendor routing state survives the approval gate, and the
gate itself sits inside the one tree.

Both inbound headers are caller-controlled, so ``traceparent`` is
accepted only in its fixed hex grammar and ``tracestate`` only within
the W3C limits (32 members, 512 bytes, the member grammar); the trace
state is then checked as an **identifier** — walked, never rewritten,
because a placeholder is not a vendor value — and a flagged one is
dropped before the row is written, with a platform-plane warning naming
the header and never the value.

The persisted flags always carry the sampled bit: :class:`RunRootSampler`
returns record-and-sample for the root ``run`` span whatever an upstream
``traceparent`` says, because promise 3 is one tree per run and the
SDK's default parent-based sampler would let a caller's ``00`` silence
a run. Everything else keeps the default parent-based behaviour, so
phases restored from the sampled root are sampled too. The pair is the
SDK's own context, not a rewrite of it: a run continuing a caller's
trace persists ``-01``, and a run whose trace id the chassis generated
persists ``-03`` — the W3C Level 2 random-trace-id bit the SDK sets
beside the sampled one, which downstream samplers may rely on.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Iterator, Mapping
from uuid import UUID

import structlog
from opentelemetry import trace as _otel_trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_ON,
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
)
from opentelemetry.trace import (
    INVALID_SPAN,
    Link,
    NonRecordingSpan,
    Span,
    SpanContext,
    SpanKind,
    TraceFlags,
    TraceState,
    get_current_span,
    set_span_in_context,
)
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)

from app.services import pii_service

logger = structlog.get_logger(__name__)
_tracer = _otel_trace.get_tracer("librerun.run")
_propagator = TraceContextTextMapPropagator()

RUN_ROOT_SPAN_NAME = "run"
# Stamped on the root at creation so the sampler decides by more than a
# span name an instrumentor could also pick.
RUN_ROOT_ATTRIBUTE = "librerun.run.root"

TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"

# W3C Trace Context version 00 in its fixed grammar — lowercase hex,
# exact field widths — and nothing else. A future version would parse
# as 00 only by a lenient reader; this one is strict on purpose.
_TRACEPARENT_RE = re.compile(r"\A00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})\Z")
TRACEPARENT_MAX_CHARS = 55

TRACESTATE_MAX_MEMBERS = 32
TRACESTATE_MAX_BYTES = 512
# W3C tracestate member grammar: a simple key (lcalpha, then up to 255
# of lcalpha / digit / _ - * /) or a multi-tenant ``tenant@system`` key;
# a value of up to 256 printable characters excluding ``,`` and ``=``
# that does not end in a space.
_TRACESTATE_KEY_RE = re.compile(
    r"\A(?:[a-z][a-z0-9_\-*/]{0,255}|[a-z0-9][a-z0-9_\-*/]{0,240}@[a-z][a-z0-9_\-*/]{0,13})\Z"
)
_TRACESTATE_VALUE_RE = re.compile(
    r"\A[\x20-\x2b\x2d-\x3c\x3e-\x7e]{0,255}[\x21-\x2b\x2d-\x3c\x3e-\x7e]\Z"
)


# --------------------------------------------------------------------------
# Inbound headers
# --------------------------------------------------------------------------


def parse_traceparent(value: str | None) -> tuple[int, int, int] | None:
    """``(trace_id, span_id, flags)`` for a header in the fixed grammar,
    ``None`` for anything else — including the all-zero ids the
    specification forbids."""
    if not isinstance(value, str) or len(value) != TRACEPARENT_MAX_CHARS:
        return None
    match = _TRACEPARENT_RE.match(value)
    if match is None:
        return None
    trace_id = int(match.group(1), 16)
    span_id = int(match.group(2), 16)
    if trace_id == 0 or span_id == 0:
        return None
    return trace_id, span_id, int(match.group(3), 16)


def parse_tracestate(value: str | None) -> tuple[str | None, str | None]:
    """``(normalized_header, None)`` for a header within the W3C limits and
    grammar, ``(None, reason)`` otherwise. The normalized form is the
    non-empty members joined by ``,`` with the optional whitespace the
    grammar allows around them removed."""
    if not isinstance(value, str):
        return None, "not_a_string"
    if len(value.encode("utf-8", "surrogateescape")) > TRACESTATE_MAX_BYTES:
        return None, "too_long"
    members: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        member = raw.strip(" \t")
        if not member:
            continue  # empty list members are legal and carry nothing
        key, sep, member_value = member.partition("=")
        if not sep or not _TRACESTATE_KEY_RE.match(key) or not _TRACESTATE_VALUE_RE.match(member_value):
            return None, "member_grammar"
        if key in seen:
            return None, "duplicate_key"
        seen.add(key)
        members.append(f"{key}={member_value}")
    if len(members) > TRACESTATE_MAX_MEMBERS:
        return None, "too_many_members"
    return (",".join(members) if members else None), None


def _header_values(headers: Mapping[str, Any], name: str) -> str | None:
    """The header's value, with a repeated header joined as one list the
    way the specification reads it. Works on a plain dict and on
    Starlette's multi-valued ``Headers``."""
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        values = [v for v in getlist(name) if isinstance(v, str)]
        return ",".join(values) if values else None
    value = headers.get(name)
    return value if isinstance(value, str) else None


def upstream_context(headers: Mapping[str, Any]) -> Context | None:
    """The validated upstream parent for a submission, or ``None``.

    ``traceparent`` outside its grammar is ignored (a platform-plane
    warning names the header); ``tracestate`` is read only beside a
    valid ``traceparent`` (as the specification says), only within its
    limits, and only when the identifier check does not flag it — the
    submission proceeds without vendor state otherwise. The returned
    context carries the upstream span as a remote, non-recording parent
    on top of the current context, so a root opened in it joins the
    caller's trace.
    """
    raw_parent = _header_values(headers, TRACEPARENT_HEADER)
    if raw_parent is None:
        return None
    parsed = parse_traceparent(raw_parent)
    if parsed is None:
        logger.warning(
            "traceparent_ignored", header=TRACEPARENT_HEADER, reason="grammar"
        )
        return None
    trace_id, span_id, flags = parsed

    trace_state = TraceState()
    raw_state = _header_values(headers, TRACESTATE_HEADER)
    if raw_state is not None:
        normalized, reason = parse_tracestate(raw_state)
        if reason is not None:
            logger.warning(
                "tracestate_ignored", header=TRACESTATE_HEADER, reason=reason
            )
        elif normalized is not None:
            finding = pii_service.check_identifier(normalized, path=TRACESTATE_HEADER)
            if finding is not None:
                # Never the value: the header name, the class and the type.
                logger.warning(
                    "tracestate_dropped",
                    header=TRACESTATE_HEADER,
                    reason="pii_in_identifier",
                    pii_type=finding.pii_type,
                )
            else:
                trace_state = TraceState.from_header([normalized])

    parent = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=True,
        trace_flags=TraceFlags(flags),
        trace_state=trace_state,
    )
    return set_span_in_context(NonRecordingSpan(parent))


# --------------------------------------------------------------------------
# The root span and its persisted pair
# --------------------------------------------------------------------------


def _root_attributes(
    *, run_id: UUID | str, run_number: str | None, agent_id: str, tenant_id: UUID | str
) -> dict:
    return {
        RUN_ROOT_ATTRIBUTE: True,
        "run.id": str(run_id),
        "run.number": run_number or "",
        "agent.id": agent_id,
        "tenant.id": str(tenant_id),
    }


@contextmanager
def root_span(
    *,
    upstream: Context | None,
    run_id: UUID | str,
    run_number: str | None,
    agent_id: str,
    tenant_id: UUID | str,
) -> Iterator[Span]:
    """Open the run's root ``run`` span as the current span.

    Its parent is the validated upstream context when the submission
    carried one and nothing otherwise — never the serving request's own
    span, which belongs to the platform plane: the run is a tree of its
    own that a caller's trace may enclose.
    """
    parent_ctx = upstream if upstream is not None else set_span_in_context(INVALID_SPAN)
    with _tracer.start_as_current_span(
        RUN_ROOT_SPAN_NAME,
        context=parent_ctx,
        kind=SpanKind.INTERNAL,
        attributes=_root_attributes(
            run_id=run_id, run_number=run_number, agent_id=agent_id, tenant_id=tenant_id
        ),
    ) as span:
        yield span


def serialize(span_context: SpanContext) -> tuple[str, str | None]:
    """The W3C pair for a span context: ``traceparent`` (the SDK's flags
    as they are — see the module docstring) and the ``tracestate``
    header, ``None`` when the state is empty."""
    traceparent = (
        f"00-{span_context.trace_id:032x}-{span_context.span_id:016x}"
        f"-{int(span_context.trace_flags):02x}"
    )
    state = span_context.trace_state.to_header() if span_context.trace_state else ""
    return traceparent, (state or None)


def persist_root(run, span: Span) -> bool:
    """Write the root's pair and trace id onto the run row. ``False`` (and
    nothing written) when tracing is off and the span has no context."""
    sc = span.get_span_context()
    if not sc.is_valid:
        return False
    traceparent, tracestate = serialize(sc)
    run.root_traceparent = traceparent
    run.root_tracestate = tracestate
    run.trace_id = format(sc.trace_id, "032x")
    return True


def restore_root(run) -> Context | None:
    """The persisted root parsed back as a remote parent context on top
    of the current context, or ``None`` when the row carries no valid
    pair (a run from before this batch, or tracing off at submission)."""
    raw = getattr(run, "root_traceparent", None)
    if not raw:
        return None
    parsed = parse_traceparent(raw)
    if parsed is None:
        logger.warning(
            "run_root_unparseable", run_id=str(getattr(run, "id", "")), header="root_traceparent"
        )
        return None
    trace_id, span_id, flags = parsed
    state_header = getattr(run, "root_tracestate", None) or ""
    trace_state = TraceState.from_header([state_header]) if state_header else TraceState()
    parent = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=True,
        trace_flags=TraceFlags(flags),
        trace_state=trace_state,
    )
    return set_span_in_context(NonRecordingSpan(parent))


def ensure_root(run, *, agent_id: str) -> Context | None:
    """Restore the run's root, minting one first for a row that has none.

    A run submitted before this batch (or while tracing was off) has no
    pair; its next phase opens the root now — so from here on the run is
    one tree — and persists it. Returns ``None`` only when tracing is
    off, in which case the phase span is a no-op anyway.
    """
    ctx = restore_root(run)
    if ctx is not None:
        return ctx
    with root_span(
        upstream=None,
        run_id=run.id,
        run_number=getattr(run, "run_number", None),
        agent_id=agent_id,
        tenant_id=run.tenant_id,
    ) as span:
        span.set_attribute("librerun.run.root_minted_late", True)
        if not persist_root(run, span):
            return None
    logger.info("run_root_minted_late", run_id=str(run.id))
    return restore_root(run)


# --------------------------------------------------------------------------
# Outbound headers (the Run Contract)
# --------------------------------------------------------------------------


def propagation_headers(context: Context | None = None) -> dict[str, str]:
    """``traceparent`` / ``tracestate`` for the current span (the phase
    span while a phase runs) — empty when no span is active."""
    carrier: dict[str, str] = {}
    _propagator.inject(carrier, context=context)
    return carrier


def current_trace_id() -> str | None:
    sc = get_current_span().get_span_context()
    return format(sc.trace_id, "032x") if sc.is_valid else None


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------


def _parent_trace_state(parent_context: Context | None) -> TraceState | None:
    sc = get_current_span(parent_context).get_span_context()
    return sc.trace_state if sc.is_valid else None


class RunRootSampler(Sampler):
    """The run plane's sampler: the root ``run`` span is always recorded
    and sampled, whatever its (remote) parent's flags; every other span
    follows the delegate, by default the SDK's own parent-based
    always-on — so a phase restored from the sampled root is sampled,
    and platform spans behave as before."""

    def __init__(self, delegate: Sampler | None = None) -> None:
        self._delegate = delegate if delegate is not None else ParentBased(ALWAYS_ON)

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Mapping[str, Any] | None = None,
        links: Any = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        if name == RUN_ROOT_SPAN_NAME and (attributes or {}).get(RUN_ROOT_ATTRIBUTE) is True:
            return SamplingResult(
                Decision.RECORD_AND_SAMPLE, attributes, _parent_trace_state(parent_context)
            )
        return self._delegate.should_sample(
            parent_context=parent_context,
            trace_id=trace_id,
            name=name,
            kind=kind,
            attributes=attributes,
            links=links,
            trace_state=trace_state,
        )

    def get_description(self) -> str:
        return f"RunRootSampler{{{self._delegate.get_description()}}}"


__all__ = [
    "RUN_ROOT_ATTRIBUTE",
    "RUN_ROOT_SPAN_NAME",
    "RunRootSampler",
    "TRACEPARENT_HEADER",
    "TRACESTATE_HEADER",
    "current_trace_id",
    "ensure_root",
    "parse_traceparent",
    "parse_tracestate",
    "persist_root",
    "propagation_headers",
    "restore_root",
    "root_span",
    "serialize",
    "upstream_context",
]
