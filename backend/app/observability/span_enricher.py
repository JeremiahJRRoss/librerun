"""``AgentSpanEnricher`` SpanProcessor — server-side identity on every span.

Ported from the retired community backend (blueprint §2.4 salvage; its
ADR-013) as the follow-up to batch B3: the GenAI-convention LLM
instrumentors adopted under decision D2 do not read OpenInference's
``using_attributes`` context, so LLM spans from OpenAI / Google GenAI
would otherwise lose the agent / run / session metadata that
per-agent and per-run trace filtering depends on
(``docs/authoring/Agents_Install.md`` §Observability documents that contract).

This processor closes the gap one layer below the instrumentors: its
``on_start`` stamps identity attributes onto **every** span the process
creates — manual phase spans, FastAPI request spans, and LLM spans from
any instrumentation family — reading the structlog contextvars that
``agent_runner`` (and the request middleware) already bind. No
instrumentor cooperation required; OpenInference's ``using_attributes``
stays in place for its own semantic conventions on Anthropic spans.

``on_start`` runs before sampler decisions, so the attributes are
visible to any tail-based sampling downstream. ``on_end`` / ``shutdown``
/ ``force_flush`` are no-ops — this processor only enriches, never
exports. Spans created outside any bound context (startup, health
checks) simply get no enricher attributes.
"""

from __future__ import annotations

from typing import Any

import structlog
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from app.logging_context import current_scope
from app.observability import otlp_walk

# Map structlog contextvar names → OTEL attribute names. Underscore →
# dot follows OpenTelemetry semantic-convention style; ``session.id`` /
# ``user.id`` mirror the OpenInference attribute names so both
# instrumentation families speak one vocabulary in the trace viewer.
# ``user_email`` is deliberately NOT mapped — identity yes, PII no.
# A value may name several attributes: ``run.id`` / ``run.number`` are the
# authoritative spellings (blueprint S1, L18) and ``case.id`` /
# ``case.number`` ride along as duplicates for one release, because saved
# trace queries depend on them; the duplicates go at v1.1.
_CONTEXTVAR_TO_ATTRIBUTE: dict[str, str | tuple[str, ...]] = {
    "agent_id": "agent.id",
    "agent_name": "agent.name",
    "tenant_id": "tenant.id",
    "run_id": ("run.id", "case.id"),
    "run_number": ("run.number", "case.number"),
    "phase": "phase",
    "session_id": "session.id",
    "user_id": "user.id",
}


class AgentSpanEnricher(SpanProcessor):
    """SpanProcessor that stamps agent / tenant / run identity at span start."""

    def __init__(
        self, contextvar_attribute_map: dict[str, str | tuple[str, ...]] | None = None
    ) -> None:
        # Tests / deployments can extend the map without subclassing.
        self._attribute_map: dict[str, str | tuple[str, ...]] = (
            dict(_CONTEXTVAR_TO_ATTRIBUTE)
            if contextvar_attribute_map is None
            else dict(contextvar_attribute_map)
        )

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        # The plane stamp goes on EVERY span, bound context or not: an
        # explicit ``librerun.scope=platform`` beats making routers infer
        # "no agent.id means chassis" (Agents_Design.md "Observability
        # contract"). Same predicate as the ``librerun_scope`` log field.
        scope = current_scope()
        span.set_attribute("librerun.scope", scope)
        # Exactly what the CHASSIS wrote on this span, so the export walk
        # can leave those values alone without trusting a key name. An
        # agent setting `user.id` on this span afterwards changes the
        # value, the record no longer matches, and it is walked like any
        # other content (backend/app/observability/otlp_walk.py).
        stamped: dict[str, str] = {"librerun.scope": scope}
        ctx = structlog.contextvars.get_contextvars()
        if ctx:
            for key, attributes in self._attribute_map.items():
                value = ctx.get(key)
                if value is None or value == "":
                    continue
                names = (attributes,) if isinstance(attributes, str) else attributes
                for attribute in names:
                    coerced = _coerce(value)
                    span.set_attribute(attribute, coerced)
                    stamped[attribute] = str(coerced)
        # Recorded beside the span, not on it: an attribute the agent can
        # overwrite proves nothing about who wrote it.
        context = span.get_span_context()
        otlp_walk.stamps.record_span(context.trace_id, context.span_id, stamped)

    def on_end(self, span: ReadableSpan) -> None:
        """No-op — enrichment happens at start so samplers see the attrs."""
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def _coerce(value: Any) -> Any:
    """Coerce arbitrary context values to an OTEL-acceptable attribute type."""
    if isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


__all__ = ["AgentSpanEnricher"]
