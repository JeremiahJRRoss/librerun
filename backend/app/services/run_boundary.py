"""The agent → chassis boundary (blueprint S4; gaps H7, J3, J6).

Every agent-supplied value the chassis persists or forwards crosses this
module, walked by the one walker in ``pii_service`` — terminal output,
the arguments of ``audit_log`` and ``run_store_set``, progress records,
the free text of Run Contract events — so the ``CLAUDE.md`` invariant
"unredacted content never touches the database" holds by construction
rather than by an agent's good manners:

- **content** (string leaves, event text) is redacted in place with the
  intake pipeline's placeholders;
- **identifiers** (object keys, a run-store key, a step id, an action
  type) are checked as their literal text and never rewritten — a
  placeholder would change the value's contract or collide with another
  — so a flagged one **refuses the write**;
- **numbers** run the walker's number rule (Luhn, libphonenumber, the
  social-security context) and a flagged one refuses the write too.

A refusal is :class:`PiiRefused` carrying the reason code the caller
reports — ``pii_in_output`` ends the run ``error``; ``pii_in_audit``,
``pii_in_store`` and ``pii_in_progress`` fail the capability or MCP call
— with the argument and the JSON path named and never the value. The
one exception is a container's ``progress`` event, which has no reply
channel: the container adapter drops it with a warning naming the
position and the run continues.

**The run's tool secrets are scrubbed first** (K8a, D20). The runner holds
``scrubbing(values)`` — every value the run's declared ``secrets[]``
resolve to, grown by ``add_scrub`` as the façade delivers one — through
its phases and their error path, and the MCP endpoint holds one around the
writes a container makes. Inside it, before any PII rule runs, content has
each value replaced, longest first, with ``[REDACTED_SECRET]``, and an
identifier or an object key holding one is refused with the reason
``secret_in_output`` — never a PII reason, since the finding is not PII.
A text its caller cut short may end in the start of a value; that tail is
replaced too. Each run's set holds every value it was delivered in this
process, not only what the rows hold now: a container reads a value in
one MCP request and may write it in another after an admin replaced the
row, and ``scrubbing(…, run_id=…)`` blocks of one run share one set. An
exception an agent raises is scrubbed before the phase span records it
and the runner logs it (``scrub_exception``). What stays uncovered is what
never crosses this module: an agent's own log lines and OTLP export, a
model prompt, a value the agent transformed before emitting it.
"""
from __future__ import annotations

import contextlib
import json
import time
import traceback
from contextvars import ContextVar
from typing import Any, Iterable, Iterator
from uuid import UUID

import structlog

from app.services import pii_service
from app.services.pii_service import (
    PiiDetectorUnavailable,
    PiiRefused,
    UnwalkableValue,
)

logger = structlog.get_logger(__name__)

REASON_OUTPUT = "pii_in_output"
REASON_AUDIT = "pii_in_audit"
REASON_STORE = "pii_in_store"
REASON_PROGRESS = "pii_in_progress"
# K8a: an identifier or an object key holding one of the run's tool
# secrets. Not a PII reason: what was found is the agent's credential.
REASON_SECRET = "secret_in_output"
SECRET_PLACEHOLDER = "[REDACTED_SECRET]"
SECRET_PII_TYPE = "TOOL_SECRET"

# Blueprint S4c (gap H15). When the detector is not ready the boundary
# refuses in exactly the shape it already refuses flagged content: the
# caller's own reason code, a finding, a :class:`PiiRefused`. That is
# deliberate and it is the cheap half of the batch — ``agent_runner``
# ends the run ``error`` on ``pii_in_output`` and the capabilities fail
# the write on ``pii_in_audit`` / ``pii_in_store`` already, so nothing
# downstream needs a second failure mode for "we could not look".
#
# The finding's ``kind`` is ``detector`` rather than content or
# identifier, because no position was flagged: the WALK did not happen.
# The path is the argument, since there is no position to name.
DETECTOR_KIND = "detector"
DETECTOR_PII_TYPE = "DETECTOR_UNAVAILABLE"

__all__ = [
    "DETECTOR_KIND",
    "DETECTOR_PII_TYPE",
    "PiiDetectorUnavailable",
    "PiiRefused",
    "REASON_AUDIT",
    "REASON_OUTPUT",
    "REASON_PROGRESS",
    "REASON_SECRET",
    "REASON_STORE",
    "RUN_KEY_TTL_SECONDS",
    "SECRET_PLACEHOLDER",
    "ScrubbedError",
    "UnwalkableValue",
    "add_scrub",
    "check_name",
    "forget_run",
    "progress_key",
    "progress_write",
    "run_hash_write",
    "run_kv_key",
    "scrub_exception",
    "scrub_secrets",
    "scrubbing",
    "step_models_key",
    "redact_text",
    "walk_value",
]


# ----- the run's tool secrets (K8a, D20) --------------------------------------


class _ScrubSet:
    """The values one run was delivered, longest first, so a value that
    contains another is replaced whole. Held by reference in the context
    variable, so a value the façade adds in a task the agent spawned is in
    the set the runner's walk reads: a task copies the context, and the
    copy points at this same object."""

    __slots__ = ("values",)

    def __init__(self, values: Iterable[str]) -> None:
        self.values: tuple[str, ...] = ()
        self.extend(values)

    def extend(self, values: Iterable[str]) -> None:
        merged = set(self.values)
        merged.update(v for v in values if isinstance(v, str) and v)
        self.values = tuple(sorted(merged, key=lambda v: (-len(v), v)))


_SCRUB: ContextVar[_ScrubSet | None] = ContextVar("librerun_run_secrets", default=None)

# The shortest tail of a cut text that is taken for the start of a value.
_MIN_CUT_TAIL = 4


@contextlib.contextmanager
def scrubbing(values: Iterable[str], *, run_id: object = None) -> Iterator[None]:
    """Scrub ``values`` from everything this module walks inside the block.

    With ``run_id``, the block's set is that run's in this process: it
    already holds every value an earlier block of the run was delivered,
    and what this block is delivered stays for the later ones (Codex on
    #173) — an MCP request after an admin rotated a row still scrubs the
    value the container was handed before."""
    scrub = _run_set(run_id) if run_id is not None else _ScrubSet(())
    scrub.extend(values)
    token = _SCRUB.set(scrub)
    try:
        yield
    finally:
        _SCRUB.reset(token)


# Every value each run was delivered, by run id, in this process: held here
# and nowhere else — never in Redis (L31). ``forget_run`` drops a run's set
# when the run ends; one untouched for ``RUN_KEY_TTL_SECONDS`` goes as the
# run's own Redis keys do, so a run this process never finishes (another
# process runs its phases) cannot keep one forever.
_DELIVERED: dict[str, tuple[_ScrubSet, float]] = {}


def _run_set(run_id: object) -> _ScrubSet:
    now = time.monotonic()
    for stale in [
        key for key, (_, touched) in _DELIVERED.items() if now - touched > RUN_KEY_TTL_SECONDS
    ]:
        _DELIVERED.pop(stale, None)
    key = str(run_id)
    scrub = _DELIVERED[key][0] if key in _DELIVERED else _ScrubSet(())
    _DELIVERED[key] = (scrub, now)
    return scrub


def forget_run(run_id: object) -> None:
    """Drop every value ``run_id`` was delivered in this process: the run
    has ended, and no request of it can write again."""
    _DELIVERED.pop(str(run_id), None)


def add_scrub(value: str) -> None:
    """Add a value just delivered to the current block's set (a no-op
    outside one)."""
    current = _SCRUB.get()
    if current is not None and isinstance(value, str) and value:
        current.extend((value,))


def _values() -> tuple[str, ...]:
    current = _SCRUB.get()
    return current.values if current is not None else ()


def scrub_secrets(text: str, *, cut: bool = False) -> str:
    """``text`` with every value in the current set replaced by
    ``[REDACTED_SECRET]``, longest first. With ``cut``, a tail of at least
    four characters that is the start of a value — what a caller's length
    cap leaves of one — is replaced as well."""
    values = _values()
    if not values or not text:
        return text
    for value in values:
        if value in text:
            text = text.replace(value, SECRET_PLACEHOLDER)
    if cut:
        for value in values:
            for length in range(min(len(value) - 1, len(text)), _MIN_CUT_TAIL - 1, -1):
                if text.endswith(value[:length]):
                    return text[: len(text) - length] + SECRET_PLACEHOLDER
    return text


class ScrubbedError(Exception):
    """What a run's exception becomes when scrubbing its arguments leaves a
    delivered value in its text — a ``__str__`` of its own that reads other
    attributes. Its message names the type it stands for."""


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and every exception it was raised from or during."""
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or any(current is known for known in seen):
            continue
        seen.append(current)
        pending.extend((current.__cause__, current.__context__))
    return seen


def _rendered(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


def scrub_exception(exc: BaseException) -> BaseException:
    """``exc`` with the run's values out of its text, for what records it
    next — the phase span's exception event and ``run_failed``'s log line,
    neither of which walks it (Codex on #173).

    Every exception in its chain has its string arguments and notes
    scrubbed in place, so its type, which the runner classifies the run by,
    is kept. One whose rendering still holds a value after that comes back
    as a :class:`ScrubbedError` naming its type, to be raised ``from None``.
    Outside a scrubbing block, or when nothing in it holds a value, ``exc``
    itself, untouched."""
    values = _values()
    if not values or not any(value in _rendered(exc) for value in values):
        return exc
    for current in _chain(exc):
        current.args = tuple(
            scrub_secrets(arg) if isinstance(arg, str) else arg for arg in current.args
        )
        notes = getattr(current, "__notes__", None)
        if isinstance(notes, list):
            current.__notes__ = [
                scrub_secrets(note) if isinstance(note, str) else note for note in notes
            ]
    if not _holds_secret(_rendered(exc)):
        return exc
    try:
        text = str(exc)
    except Exception:  # noqa: BLE001 — a __str__ that raises says nothing
        text = ""
    return ScrubbedError(f"{type(exc).__name__}: {scrub_secrets(text)}")


def _secret_refusal(reason: str, argument: str, path: str) -> PiiRefused:
    finding = pii_service.Finding(
        path=path, kind="identifier", pii_type=SECRET_PII_TYPE, confidence=1.0, action="refused"
    )
    # The path and the class, never the value — nor which value it was.
    logger.warning(
        "boundary_refused_secret", reason=REASON_SECRET, caller_reason=reason,
        argument=argument, path=path,
    )
    return PiiRefused(REASON_SECRET, argument, finding)


def _holds_secret(text: str) -> bool:
    return any(value in text for value in _values())


def _scrub_value(value: Any, *, argument: str, reason: str) -> Any:
    """A JSON-like value with the run's values replaced in its strings and
    an object key holding one refused, under the walker's own guards."""
    budget = [pii_service.WALK_MAX_NODES]

    def _visit(node: Any, path: str, depth: int) -> Any:
        budget[0] -= 1
        if budget[0] < 0:
            raise UnwalkableValue(f"more than {pii_service.WALK_MAX_NODES} nodes")
        if depth > pii_service.WALK_MAX_DEPTH:
            raise UnwalkableValue(f"deeper than {pii_service.WALK_MAX_DEPTH} levels")
        if isinstance(node, str):
            return scrub_secrets(node)
        if isinstance(node, dict):
            out = {}
            for index, (key, child) in enumerate(node.items()):
                if _holds_secret(str(key)):
                    # Addressed by its ordinal, as the walker addresses a
                    # flagged key: the key is the value it must not name.
                    raise _secret_refusal(reason, argument, f"{path}.<key {index}>")
                out[key] = _visit(child, f"{path}.{key}", depth + 1)
            return out
        if isinstance(node, (list, tuple)):
            return [_visit(item, f"{path}[{i}]", depth + 1) for i, item in enumerate(node)]
        return node

    return _visit(value, "$", 0)


def _detector_refusal(exc: PiiDetectorUnavailable, reason: str, argument: str) -> PiiRefused:
    """Turn "the detector is not ready" into the boundary's own refusal."""
    finding = pii_service.Finding(
        path=argument,
        kind=DETECTOR_KIND,
        pii_type=DETECTOR_PII_TYPE,
        confidence=1.0,
        action="refused",
    )
    logger.warning(
        "boundary_refused_detector_unavailable",
        reason=reason,
        argument=argument,
        state=exc.state,
        error=exc.error,
    )
    return PiiRefused(reason, argument, finding)


def redact_text(
    text: Any, *, argument: str = "text", reason: str = REASON_OUTPUT
) -> str | None:
    """Content: free text an agent emitted, redacted the way intake
    redacts it. ``None`` stays ``None``; anything else is a string.

    Raises :class:`PiiRefused` when the detector is not ready — text the
    chassis could not fully walk is not text it forwards (S4c).
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    text = scrub_secrets(text, cut=True)
    try:
        return pii_service.redact(
            text, skip_entities=pii_service.BOUNDARY_SKIP_ENTITIES, stage="run_boundary"
        )[0]
    except PiiDetectorUnavailable as exc:
        raise _detector_refusal(exc, reason, argument) from exc


def _refuse(reason: str, argument: str, finding: pii_service.Finding) -> PiiRefused:
    # The path and the class, never the value.
    logger.warning(
        "boundary_refused",
        reason=reason,
        argument=argument,
        path=finding.path,
        kind=finding.kind,
        pii_type=finding.pii_type,
    )
    return PiiRefused(reason, argument, finding)


def check_name(text: Any, *, argument: str, reason: str, path: str | None = None) -> str:
    """Identifier: a name the chassis will use as a key or a label,
    checked as its literal text, never rewritten. Returns it unchanged or
    raises :class:`PiiRefused`."""
    value = text if isinstance(text, str) else str(text)
    if _holds_secret(value):
        raise _secret_refusal(reason, argument, path or argument)
    finding = pii_service.check_identifier(value, path=path or argument)
    if finding is not None:
        raise _refuse(reason, argument, finding)
    return value


def walk_value(value: Any, *, argument: str, reason: str) -> Any:
    """A JSON-like argument: string leaves redacted in place, keys and
    numbers checked. Returns the walked copy or raises
    :class:`PiiRefused` (a :class:`UnwalkableValue` — deeper or wider
    than the walker's guards — propagates as it is)."""
    if _values():
        value = _scrub_value(value, argument=argument, reason=reason)
    try:
        result = pii_service.walk(value)
    except PiiDetectorUnavailable as exc:
        raise _detector_refusal(exc, reason, argument) from exc
    if result.refused:
        raise _refuse(reason, argument, result.refusals[0])
    return result.value


# HOW LONG A RUN'S REDIS HASHES LIVE. Nothing deletes them when a run
# finishes — the only delete is `orchestrator.reset_progress`, which runs
# on a RE-RUN — so without an expiry `run:{id}:progress`,
# `run:{id}:step_models` and `run:{id}:kv` are immortal: measured `ttl=-1`
# on all three.
#
# SEVEN DAYS IS A CLAIM ABOUT REVIEWERS, NOT ABOUT RUNS, and adding a TTL
# where there was none CREATES a failure mode rather than inheriting one,
# so it is argued rather than borrowed from the `kv` hash that already
# used this number:
#
#   * `expire` is reset on every write, so the window runs from the LAST
#     progress write, not from submission;
#   * nothing reaps a parked run — a run waits on a human indefinitely;
#   * `GET /runs/{id}/progress` reads this hash and has NO fallback
#     (`runs.py` does one `hgetall`), so an expired key empties the step
#     list while the run itself stays valid and resumable.
#
# So the number to beat is not `LIBRERUN_MAX_PHASE_SECONDS` — a phase
# deadline never bounds a parked run — it is how long a reviewer may
# plausibly leave one parked. Seven days covers a holiday week. If that
# is ever judged too tight the answer is a LONGER TTL, never dropping the
# expiry, because `ttl=-1` is the leak this exists to close. For a run
# that completed, losing the hash costs nothing visible: the detail page
# renders from the agent's report or from `structured_output`, neither of
# which reads it.
RUN_KEY_TTL_SECONDS = 7 * 24 * 3600


async def run_hash_write(redis, key: str, field: str, value: str) -> None:
    """The one way a ``run:``-shaped hash is written: the field and the
    key's expiry, as ONE round trip and one transaction.

    Every writer used to spend two — or, in two of the three, set no
    expiry at all. Two awaits are two chances to be interrupted between
    them, and the window is not theoretical: `routers/mcp.py` and the
    gateway's `auth.py` both pipeline for exactly this reason, recorded
    there as "two awaits let the key expire in between".

    `execute()` is awaited BARE. `hset` then `expire` replies
    ``[fields_added, True]``, and unpacking it would couple this helper
    to redis-py's reply shape for nothing — the caller wants neither
    number.
    """
    pipe = redis.pipeline(transaction=True)
    pipe.hset(key, field, value)
    pipe.expire(key, RUN_KEY_TTL_SECONDS)
    await pipe.execute()


def progress_key(run_id: UUID | str) -> str:
    return f"run:{run_id}:progress"


def run_kv_key(run_id: UUID | str) -> str:
    """The run's scratch hash, ``run:{id}:kv``.

    Here rather than in ``capabilities`` for the reason
    ``step_models_key`` already gives: two copies of a key name is how
    one of them quietly stops matching. This is the third ``run:`` hash
    and was the last one still spelled where it was used.
    """
    return f"run:{run_id}:kv"


def step_models_key(run_id: UUID | str) -> str:
    """The model that answered each step, in a hash of its own.

    NOT a field of the progress entry. Two processes write a step's row —
    the orchestrator its status and timing, the gateway the model that
    answered (D13) — and :func:`progress_write` writes a CLOSED shape:
    one JSON object with exactly the three keys the platform stores, so
    an agent cannot add a fourth. Merging into it would either reopen
    that shape or lose whichever write landed second, and the second one
    is the terminal status, which always comes after the model. Separate
    keys give each fact one writer and no race; the read path
    (``GET /runs/{id}/progress``) joins them.

    The value is the model name, a bare string: it comes from the
    platform's own configuration or the provider's reply, never from
    agent text.
    """
    return f"run:{run_id}:step_models"


# What a progress record may say, and what an agent's word for it maps
# to. `StepProgress.status` (backend/app/schemas/run.py) is a Literal of
# the five on the left, so anything else stored makes `GET
# /runs/{id}/progress` fail response validation with a 500 — and the
# status was agent-controlled, written verbatim, and never walked, so an
# address in it was stored in Redis and every later read of that run's
# progress was a server error.
STORED_STATUSES = ("pending", "running", "complete", "skipped", "error")
WIRE_STATUS_MAP = {
    "pending": "pending",
    "running": "running",
    "completed": "complete",
    "complete": "complete",
    "skipped": "skipped",
    "failed": "error",
    "error": "error",
}


def normalize_status(status: object) -> str:
    """An agent's word for a step's state, as the platform stores it.

    Unknown values degrade to ``running`` rather than refusing the write:
    progress is cosmetic, the terminal event decides the run's fate, and
    a run should not fail because an agent invented a word. Nothing of
    the original survives, so the status needs no walk — it is one of
    five constants or it is not stored.
    """
    return WIRE_STATUS_MAP.get(str(status).strip().lower(), "running")


async def progress_write(
    redis,
    run_id: UUID | str,
    step_id: str,
    status: str,
    detail: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """The one write path of a run's progress hash — the runner's
    callback, the in-process ``progress`` capability and the orchestrator
    all land here. The step id is the hash's field name, so it is an
    identifier (refused when flagged, ``pii_in_progress``); the detail is
    content, redacted; the status is normalized to the five the platform
    stores, so an agent cannot put text of its own in it."""
    field = check_name(
        step_id, argument="step_id", reason=REASON_PROGRESS, path="progress.step_id"
    )
    await run_hash_write(
        redis,
        progress_key(run_id),
        field,
        json.dumps(
            {
                "status": normalize_status(status),
                "duration_ms": duration_ms,
                "detail": redact_text(
                    detail, argument="detail", reason=REASON_PROGRESS
                ),
            }
        ),
    )
