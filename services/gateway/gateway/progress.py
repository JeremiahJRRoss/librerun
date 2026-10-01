"""Writing the model a step actually used back onto the run (D13).

The run page shows a step list. What it could not show until now is
which model answered that step — which is exactly the thing an admin
just changed in the UI, and therefore exactly the thing they need to see
change without restarting anything.

The gateway writes it into ``run:{id}:step_models``, beside the
progress hash rather than inside it. The progress entry has one writer
and a closed shape — status, duration, detail, written whole by
``run_boundary.progress_write`` — and the orchestrator writes the
terminal status AFTER the call, so a model merged into that entry was
overwritten by the next write every single time. One fact, one writer,
one key; ``GET /runs/{id}/progress`` joins them.
"""
from __future__ import annotations

import redis.asyncio as aioredis
import structlog

from gateway.config import settings

logger = structlog.get_logger(__name__)


def step_models_key(run_id: str) -> str:
    """The chassis's own helper, imported rather than re-spelled: two
    copies of a key name is how one of them quietly stops matching."""
    from app.services.run_boundary import step_models_key as _key

    return _key(run_id)


async def _write(redis, key: str, field: str, value: str) -> None:
    """The chassis's own writer, imported for the same reason as the key
    name above: this hash needs an expiry like every other ``run:`` hash,
    and a second copy of "hset then expire, pipelined" is the thing that
    quietly stops matching."""
    from app.services.run_boundary import run_hash_write

    await run_hash_write(redis, key, field, value)


async def record_model(principal, step, response: dict) -> None:
    """Best effort, always: a run must not fail because a cosmetic field
    could not be written."""
    if not principal.run_id or not step.step_id:
        return
    model = str(response.get("model") or step.model or step.target or "")
    if not model:
        return
    try:
        async with aioredis.from_url(
            settings.REDIS_URL.get_secret_value(), decode_responses=True
        ) as redis:
            await _write(
                redis, step_models_key(str(principal.run_id)), step.step_id, model
            )
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "llm_progress_model_not_recorded",
            error=str(exc),
            error_type=type(exc).__name__,
        )
