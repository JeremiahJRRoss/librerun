"""Generic step orchestrator — sequencing, progress, OTEL spans, error handling.

Originally shipped under ``app.pipeline.orchestrator`` for the demo agent.
Phase 6 moved it into the shell because it has nothing agent-specific: any agent
that needs Redis-backed step progress and per-step OTEL spans can use it.

The orchestrator is duck-typed on the ``llm`` object — it only calls
``get_step_config(step_id)`` (used for span attributes) and ignores
``KeyError`` for steps that have no LLM config (e.g. web/KB search).
That lets each agent supply its own LLM service without the shell
importing from any agent package.

Each step span carries the OpenInference attributes trace UIs look for:
``openinference.span.kind`` (``CHAIN`` for LLM/logic steps, ``RETRIEVER``
for the search steps), and ``input.value`` / ``output.value`` so the trace
tree shows what each step received and produced rather than empty boxes.
``using_attributes`` from the agent runner has already populated the OTEL
context with session/user/metadata at this point, so
``get_attributes_from_context`` re-merges them onto the manual span — the viewer
docs note that ``start_as_current_span`` does not pick those up automatically
the way the LLM auto-instrumentors do.
"""
from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable
from uuid import UUID

import structlog
from openinference.instrumentation import get_attributes_from_context
from openinference.semconv.trace import (
    OpenInferenceMimeTypeValues,
    OpenInferenceSpanKindValues,
    SpanAttributes,
)
from opentelemetry import trace as _otel_trace
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_pii import user_content
from app.tracing import safe_json

logger = structlog.get_logger(__name__)
_tracer = _otel_trace.get_tracer("librerun.pipeline")

_DEFAULT_STEP_KIND = OpenInferenceSpanKindValues.CHAIN.value
_RETRIEVER_KIND = OpenInferenceSpanKindValues.RETRIEVER.value
# Cap per-document attributes — RAG-aware trace views truncate well before this
# but keeping the count bounded protects us against a runaway response from
# a misconfigured retriever.
class PipelineOrchestrator:
    def __init__(
        self,
        db: AsyncSession,
        redis: Redis,
        llm: Any,
        step_kinds: dict[str, str] | None = None,
    ):
        self.db = db
        self.redis = redis
        self.llm = llm
        # Agents that wire this in get per-step kind badges (CHAIN vs
        # RETRIEVER) in the trace viewer. Agents that don't pass a map get CHAIN —
        # safe default since most steps run logic over an LLM response.
        self.step_kinds = step_kinds or {}

    async def update_progress(
        self,
        run_id: UUID,
        step_id: str,
        status: str,
        duration_ms: int | None = None,
        detail: str | None = None,
    ) -> None:
        from app.services import run_boundary

        # The one progress write path (blueprint S4): the step id is the
        # hash's field name and is refused when flagged; the detail —
        # an exception message, say — is redacted.
        await run_boundary.progress_write(
            self.redis, run_id, step_id, status, detail, duration_ms
        )

    async def get_progress(self, run_id: UUID) -> dict[str, dict]:
        from app.services import run_boundary

        raw = await self.redis.hgetall(run_boundary.progress_key(run_id))
        return {k: json.loads(v) for k, v in raw.items()}

    async def reset_progress(self, run_id: UUID) -> None:
        from app.services import run_boundary
        from app.services.run_boundary import step_models_key

        # Both keys: a re-run that kept the previous attempt's models
        # would report a model for a step this attempt never called.
        await self.redis.delete(
            run_boundary.progress_key(run_id), step_models_key(run_id)
        )

    async def run_step(
        self,
        run_id: UUID,
        step_id: str,
        coro_factory: Callable[[], Awaitable[Any]],
        input_payload: Any = None,
    ) -> Any:
        """Run a step with progress + timing. coro_factory is called to get the coroutine.

        ``input_payload`` is optional and back-compat: callers that don't
        pass it still get a properly-kinded span, just without an
        ``input.value``. Pass a dict that identifies *what* the step is
        operating on (queries, ids, lengths) — not multi-KB blobs. The
        cap in ``safe_json`` is a backstop, not a license to dump prompts.
        """
        kind = self.step_kinds.get(step_id, _DEFAULT_STEP_KIND)
        # Re-merge session/user/metadata from the OTEL context the agent
        # runner stamped via ``using_attributes``. Without this, the viewer's
        # Sessions and Users views don't show step rows.
        attrs: dict[str, Any] = dict(get_attributes_from_context())
        attrs[SpanAttributes.OPENINFERENCE_SPAN_KIND] = kind
        attrs["step_id"] = step_id
        attrs["run_id"] = str(run_id)
        if input_payload is not None:
            attrs[SpanAttributes.INPUT_VALUE] = safe_json(input_payload)
            attrs[SpanAttributes.INPUT_MIME_TYPE] = (
                OpenInferenceMimeTypeValues.JSON.value
            )

        with _tracer.start_as_current_span(
            f"step_{step_id}", attributes=attrs
        ) as span:
            # Pipeline-config lookup only succeeds for LLM-backed steps; KB
            # and web-search steps have no entry and raise KeyError.
            try:
                cfg = self.llm.get_step_config(step_id)
                span.set_attribute("provider", cfg.get("provider", "") or "")
                span.set_attribute("model", cfg.get("model", "") or "")
            except KeyError:
                pass

            await self.update_progress(run_id, step_id, "running")
            logger.info("step_started", step_id=step_id, run_id=str(run_id))
            start = time.monotonic()
            try:
                result = await coro_factory()
                ms = int((time.monotonic() - start) * 1000)

                # Output is always JSON-serialised on success — that's what
                # makes the Output column populate in the viewer's trace tree.
                span.set_attribute(
                    SpanAttributes.OUTPUT_VALUE, safe_json(result)
                )
                span.set_attribute(
                    SpanAttributes.OUTPUT_MIME_TYPE,
                    OpenInferenceMimeTypeValues.JSON.value,
                )
                # Retrieval documents are stamped by the step itself through
                # ``kb.stamp`` (blueprint S2): the chassis no longer guesses
                # at an agent's result shapes.

                await self.update_progress(run_id, step_id, "complete", duration_ms=ms)
                span.set_attribute("status", "ok")
                span.set_attribute("duration_ms", ms)
                logger.info(
                    "step_completed",
                    step_id=step_id,
                    run_id=str(run_id),
                    duration_ms=ms,
                )
                return result
            except Exception as e:
                ms = int((time.monotonic() - start) * 1000)
                await self.update_progress(
                    run_id, step_id, "error", duration_ms=ms, detail=str(e)[:200]
                )
                span.set_attribute("status", "error")
                span.set_attribute("duration_ms", ms)
                # Output is intentionally not set on the error path — the
                # span status carries the failure signal, and a partial
                # output would be misleading in the UI.
                span.record_exception(e)
                span.set_status(
                    _otel_trace.Status(_otel_trace.StatusCode.ERROR, str(e)[:200])
                )
                # LLM-generated error messages can echo the prompt; wrap in
                # user_content so the PII redactor scrubs the value.
                logger.warning(
                    "step_errored",
                    step_id=step_id,
                    run_id=str(run_id),
                    duration_ms=ms,
                    error=user_content(str(e)),
                )
                raise
