"""Helpers for stamping OpenInference attributes on manual OTEL spans.

The auto-instrumentors (OpenInference's OpenAI/Anthropic/Google packages)
shape the LLM ``ChatCompletion`` spans for us, but the phase- and step-level
spans an agent creates by hand are plain OTEL spans until we annotate them with
the conventions OpenInference-aware trace UIs are built around —
``openinference.span.kind``, ``input.value``, ``output.value``. Without
those, such viewers render the rows
as kind-less, input-less, output-less boxes in the trace tree.

``_safe_json`` is the one helper both ``agent_runner`` and the
``PipelineOrchestrator`` need: turn arbitrary Python values into a JSON
string suitable for an OpenInference attribute, falling back to ``repr``
when the value isn't serialisable, and capping the string so a runaway
payload can't bloat a single span.
"""
from __future__ import annotations

import json
from typing import Any

# 50 KB per attribute is well under typical backend hard limits but generous enough
# that a real prompt or response rarely truncates. The trace explorer also
# truncates aggressively past ~100 KB, so leaving headroom keeps the JSON
# tree viewer usable.
_DEFAULT_MAX_BYTES = 50_000


def safe_json(value: Any, max_bytes: int = _DEFAULT_MAX_BYTES) -> str:
    """JSON-serialize ``value`` for an OpenInference attribute.

    ``default=str`` lets us swallow UUIDs, datetimes, and Pydantic models
    without a custom encoder. If the value isn't JSON-encodable at all
    (e.g. a circular reference), fall back to ``repr`` rather than letting
    the tracing path raise — instrumentation must never break the caller.
    """
    try:
        s = json.dumps(value, default=str)
    except (TypeError, ValueError):
        s = repr(value)
    if len(s) > max_bytes:
        s = s[:max_bytes] + "...[truncated]"
    return s
